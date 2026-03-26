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

        Scans plan for charge window, determines if grid-charging is needed,
        and sets P2 accordingly:
        - Grid-charge needed → P2 = "Grid" + SOC target (grid actively charges battery)
        - PV-only charging  → P2 = "Disabled" + SOC target (PV→battery, house from grid)
        - No charging at all → P2 = minimal dummy window
        """
        from .strategies.helpers import is_grid_charging

        # Scan plan for the FIRST contiguous charge block.
        # Important: plan may span midnight (e.g. 23:15 today → 23:15 tomorrow).
        # We only use the first charge block to avoid wrap-around issues.
        charge_start = None
        charge_end = None
        charge_max_soc = 0
        has_grid_charge = False
        in_charge_block = False

        for slot in plan.slots:
            is_charge = (slot.planned_battery_mode == "charge"
                         and slot.planned_battery_w > 50)

            if is_charge:
                local = slot.start.astimezone(tz)
                local_end = local + timedelta(minutes=slot.duration_min)

                if charge_start is None:
                    charge_start = local.strftime("%H:%M")
                    in_charge_block = True

                if in_charge_block:
                    charge_end = local_end.strftime("%H:%M")
                    charge_max_soc = max(charge_max_soc, round(slot.projected_soc))
                    if is_grid_charging(slot.pv_forecast_w, slot.load_estimate_w,
                                        slot.planned_battery_w):
                        has_grid_charge = True

            elif in_charge_block:
                # First non-charge slot after charge block → block ended
                break

        min_soc = self._config.min_soc_percent

        # Determine P2 charging mode and SOC target
        if charge_start and charge_max_soc > min_soc:
            if has_grid_charge:
                p2_charging = "Grid"
                p2_soc = min(charge_max_soc, self._config.max_grid_charge_soc)
            else:
                p2_charging = "Disabled"
                p2_soc = min(charge_max_soc, self._config.max_soc_percent)

            # Wrap-around protection: if end < start (crosses midnight),
            # extend to end of day. SOC target remains → WR charges until reached.
            if charge_end <= charge_start:
                logger.warning("TOU: Charge window crosses midnight (%s→%s), "
                               "extending to 23:50. SOC target %d%% stays active.",
                               charge_start, charge_end, p2_soc)
                charge_end = "23:50"

            p1_end = charge_start
            p2_start = charge_start
            p2_end = charge_end
            p3_start = charge_end
        else:
            # No charging needed — minimal dummy P2
            p2_charging = "Disabled"
            p2_soc = min_soc
            p1_end = "23:44"
            p2_start = "23:44"
            p2_end = "23:46"
            p3_start = "23:46"

        programs = [
            {"start": "00:00",  "end": p1_end,  "charging": "Disabled", "soc": min_soc},
            {"start": p2_start, "end": p2_end,  "charging": p2_charging, "soc": p2_soc},
            {"start": p3_start, "end": "23:50", "charging": "Disabled", "soc": min_soc},
            {"start": "23:50",  "end": "23:52", "charging": "Disabled", "soc": min_soc},
            {"start": "23:52",  "end": "23:54", "charging": "Disabled", "soc": min_soc},
            {"start": "23:54",  "end": "23:56", "charging": "Disabled", "soc": min_soc},
        ]

        logger.info("TOU: P2=%s SOC=%d%% window=%s-%s (grid_charge=%s)",
                     p2_charging, p2_soc if charge_start else min_soc,
                     charge_start or "none", charge_end or "none", has_grid_charge)

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
