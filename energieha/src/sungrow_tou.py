"""Sungrow TOU (Time-of-Use) adapter: maps 24h plan to 6 inverter programs.

Sungrow TOU charging modes with "Load First" energy pattern:
  - "Disabled" + SOC target: PV charges battery exclusively up to SOC target.
    House load is served from grid. Battery does NOT discharge to serve load.
  - "Grid" + SOC target: Grid + PV charge battery up to SOC target.
    Use ONLY when cheap grid-charging is intended.
  - "Disabled" + SOC=min_soc: Normal operation. Battery discharges to serve
    house load (Load First). PV covers load first, surplus charges battery.

Program layout:
  P1: 00:00 → charge_start   Disabled SOC=min (discharge/idle)
  P2: charge_start → end     Disabled SOC=target (PV-only) OR Grid SOC=target (grid-charge)
  P3: charge_end → 23:50     Disabled SOC=min (discharge/idle)
  P4-P6: 23:50-23:56         Disabled SOC=min (dummy, last minutes)
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from .ha_client import HaClient
from .models import Config, Plan, Snapshot

logger = logging.getLogger(__name__)

MAX_PROGRAMS = 6


class SungrowTouAdapter:
    """Maps a 24h Plan to 6 sequential Sungrow TOU programs."""

    def __init__(self, client: HaClient, config: Config):
        self._client = client
        self._config = config
        self._last_programs = None
        self._validated = False

    def _validate_entities(self) -> bool:
        if self._validated:
            return True
        missing = []
        for n in range(1, MAX_PROGRAMS + 1):
            for eid in [f"select.inverter_program_{n}_charging",
                        f"number.inverter_program_{n}_soc",
                        f"time.inverter_program_{n}_time",
                        f"input_datetime.inverter_program_{n}_end"]:
                if self._client.get_state(eid) is None:
                    missing.append(eid)
        if missing:
            logger.error("TOU: %d entities missing: %s", len(missing), ", ".join(missing[:6]))
            return False
        self._validated = True
        logger.info("TOU: All program entities validated")
        return True

    def apply(self, plan: Plan, snapshot: Snapshot) -> None:
        if not plan.slots:
            return
        if not self._validate_entities():
            return

        tz = ZoneInfo(self._config.timezone)
        programs = self._build_programs(plan, tz)

        prog_key = [(p["start"], p["end"], p["charging"], p["soc"]) for p in programs]
        if prog_key == self._last_programs:
            logger.debug("TOU: unchanged, skipping")
            return

        if self._config.dry_run:
            logger.info("TOU DRY RUN:")
            for i, p in enumerate(programs):
                logger.info("  P%d: %s-%s %s SOC=%d%%", i+1, p["start"], p["end"], p["charging"], p["soc"])
        else:
            self._write_programs(programs)

        self._last_programs = prog_key

    def _build_programs(self, plan: Plan, tz) -> list[dict]:
        """Build 6 sequential programs from the plan.

        Distinguishes between PV-only charge and grid charge:
        - PV-only → "Disabled" + SOC target (PV charges battery, house from grid)
        - Grid charge → "Grid" + SOC target (grid actively charges battery)

        Program layout (up to 3 active + 3 dummy):
        - P1: Before first charge block → Disabled, min_soc (normal discharge)
        - P2: PV-only charge block → Disabled + SOC target
        - P3: Grid charge block → Grid + SOC target
        - If only grid or only PV, use P2 for the charge block, P3 for after
        """
        from .strategies.helpers import is_grid_charging

        min_soc = self._config.min_soc_percent

        # Scan plan for grid-charge and PV-charge blocks separately
        grid_start = None
        grid_end = None
        grid_max_soc = 0
        pv_start = None
        pv_end = None
        pv_max_soc = 0

        for slot in plan.slots:
            is_charge = (slot.planned_battery_mode == "charge"
                         and slot.planned_battery_w > 50)
            if not is_charge:
                continue

            local = slot.start.astimezone(tz)
            local_end = local + timedelta(minutes=slot.duration_min)
            t_start = local.strftime("%H:%M")
            t_end = local_end.strftime("%H:%M")

            if is_grid_charging(slot.pv_forecast_w, slot.load_estimate_w,
                                slot.planned_battery_w):
                # Grid charging slot
                if grid_start is None:
                    grid_start = t_start
                grid_end = t_end
                grid_max_soc = max(grid_max_soc, round(slot.projected_soc))
            else:
                # PV-only charging slot
                if pv_start is None:
                    pv_start = t_start
                pv_end = t_end
                pv_max_soc = max(pv_max_soc, round(slot.projected_soc))

        # Build programs based on what we found
        if grid_start and pv_start:
            # Both PV and grid charge - use 3 active programs
            grid_soc = min(grid_max_soc, self._config.max_grid_charge_soc)
            pv_soc = min(pv_max_soc, self._config.max_soc_percent)

            # Determine order: PV first or Grid first?
            if pv_start <= grid_start:
                # PV block → Grid block → rest
                programs = [
                    {"start": "00:00",    "end": pv_start,   "charging": "Disabled", "soc": min_soc},
                    {"start": pv_start,   "end": grid_start, "charging": "Disabled", "soc": pv_soc},
                    {"start": grid_start, "end": grid_end,   "charging": "Grid",     "soc": grid_soc},
                    {"start": grid_end,   "end": "23:50",    "charging": "Disabled", "soc": min_soc},
                    {"start": "23:50",    "end": "23:52",    "charging": "Disabled", "soc": min_soc},
                    {"start": "23:52",    "end": "23:54",    "charging": "Disabled", "soc": min_soc},
                ]
                reason = (f"PV-Ladung {pv_start}-{grid_start} (SOC {pv_soc}%) "
                          f"+ Netzladung {grid_start}-{grid_end} (Grid bis {grid_soc}%)")
            else:
                # Grid block → PV block → rest
                programs = [
                    {"start": "00:00",    "end": grid_start, "charging": "Disabled", "soc": min_soc},
                    {"start": grid_start, "end": grid_end,   "charging": "Grid",     "soc": grid_soc},
                    {"start": grid_end,   "end": pv_end,     "charging": "Disabled", "soc": pv_soc},
                    {"start": pv_end,     "end": "23:50",    "charging": "Disabled", "soc": min_soc},
                    {"start": "23:50",    "end": "23:52",    "charging": "Disabled", "soc": min_soc},
                    {"start": "23:52",    "end": "23:54",    "charging": "Disabled", "soc": min_soc},
                ]
                reason = (f"Netzladung {grid_start}-{grid_end} (Grid bis {grid_soc}%) "
                          f"+ PV-Ladung {grid_end}-{pv_end} (SOC {pv_soc}%)")

        elif grid_start:
            # Only grid charge
            grid_soc = min(grid_max_soc, self._config.max_grid_charge_soc)

            # Wrap-around protection
            if grid_end <= grid_start:
                grid_end = "23:50"

            programs = [
                {"start": "00:00",    "end": grid_start, "charging": "Disabled", "soc": min_soc},
                {"start": grid_start, "end": grid_end,   "charging": "Grid",     "soc": grid_soc},
                {"start": grid_end,   "end": "23:50",    "charging": "Disabled", "soc": min_soc},
                {"start": "23:50",    "end": "23:52",    "charging": "Disabled", "soc": min_soc},
                {"start": "23:52",    "end": "23:54",    "charging": "Disabled", "soc": min_soc},
                {"start": "23:54",    "end": "23:56",    "charging": "Disabled", "soc": min_soc},
            ]
            reason = f"Netzladung {grid_start}-{grid_end}: Grid-Charging bis {grid_soc}% SOC"

        elif pv_start:
            # Only PV charge
            pv_soc = min(pv_max_soc, self._config.max_soc_percent)

            if pv_end <= pv_start:
                pv_end = "23:50"

            programs = [
                {"start": "00:00",  "end": pv_start, "charging": "Disabled", "soc": min_soc},
                {"start": pv_start, "end": pv_end,   "charging": "Disabled", "soc": pv_soc},
                {"start": pv_end,   "end": "23:50",  "charging": "Disabled", "soc": min_soc},
                {"start": "23:50",  "end": "23:52",  "charging": "Disabled", "soc": min_soc},
                {"start": "23:52",  "end": "23:54",  "charging": "Disabled", "soc": min_soc},
                {"start": "23:54",  "end": "23:56",  "charging": "Disabled", "soc": min_soc},
            ]
            reason = (f"PV-Ladung {pv_start}-{pv_end}: Nur PV-Überschuss → "
                      f"Disabled + SOC-Ziel {pv_soc}%")
        else:
            # No charging
            programs = [
                {"start": "00:00",  "end": "23:44", "charging": "Disabled", "soc": min_soc},
                {"start": "23:44",  "end": "23:46", "charging": "Disabled", "soc": min_soc},
                {"start": "23:46",  "end": "23:50", "charging": "Disabled", "soc": min_soc},
                {"start": "23:50",  "end": "23:52", "charging": "Disabled", "soc": min_soc},
                {"start": "23:52",  "end": "23:54", "charging": "Disabled", "soc": min_soc},
                {"start": "23:54",  "end": "23:56", "charging": "Disabled", "soc": min_soc},
            ]
            reason = "Keine Ladung geplant → alle Programme Disabled"

        self.last_tou_reason = reason

        logger.info("TOU: grid=%s-%s pv=%s-%s",
                     grid_start or "-", grid_end or "-",
                     pv_start or "-", pv_end or "-")
        logger.info("TOU Begründung: %s", reason)

        return programs

    def _write_programs(self, programs: list[dict]) -> None:
        for i, prog in enumerate(programs):
            n = i + 1
            self._client.call_service("select", "select_option", {
                "entity_id": f"select.inverter_program_{n}_charging",
                "option": prog["charging"],
            })
            self._client.call_service("number", "set_value", {
                "entity_id": f"number.inverter_program_{n}_soc",
                "value": prog["soc"],
            })
            self._client.call_service("time", "set_value", {
                "entity_id": f"time.inverter_program_{n}_time",
                "time": prog["start"],
            })
            self._client.call_service("input_datetime", "set_datetime", {
                "entity_id": f"input_datetime.inverter_program_{n}_end",
                "time": prog["end"],
            })

        logger.info("TOU: Wrote 6 programs:")
        for i, p in enumerate(programs):
            logger.info("  P%d: %s-%s %s SOC=%d%%",
                        i+1, p["start"], p["end"], p["charging"], p["soc"])
