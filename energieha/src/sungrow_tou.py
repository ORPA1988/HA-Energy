"""Sungrow TOU (Time-of-Use) adapter: maps 24h plan to 6 inverter programs.

Program layout (sequential, no gaps):
  P1: 00:00 → charge_start   Disabled (idle/discharge, SOC=min)
  P2: charge_start → charge_end   Grid (charge from grid, SOC=target)
  P3: charge_end → 23:50     Disabled (idle/discharge, SOC=min)
  P4: 23:50 → 23:52          Disabled (dummy)
  P5: 23:52 → 23:54          Disabled (dummy)
  P6: 23:54 → 23:56          Disabled (dummy)

If no grid-charging is needed, P2 gets a 1-minute window and Disabled.
End of P(n) always equals Start of P(n+1).
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
            for entity_id in [
                f"select.inverter_program_{n}_charging",
                f"number.inverter_program_{n}_soc",
                f"time.inverter_program_{n}_time",
                f"input_datetime.inverter_program_{n}_end",
            ]:
                state = self._client.get_state(entity_id)
                if state is None:
                    missing.append(entity_id)

        if missing:
            logger.error("Sungrow TOU: %d entities missing: %s",
                         len(missing), ", ".join(missing[:6]))
            return False

        self._validated = True
        logger.info("Sungrow TOU: All program entities validated")
        return True

    def apply(self, plan: Plan, snapshot: Snapshot) -> None:
        """Convert plan to 6 sequential TOU programs and write to inverter."""
        if not plan.slots:
            logger.warning("TOU: Empty plan, skipping")
            return

        if not self._validate_entities():
            return

        tz = ZoneInfo(self._config.timezone)
        programs = self._build_programs(plan, tz)

        # Change detection
        prog_key = [(p["start"], p["end"], p["charging"], p["soc"])
                     for p in programs]
        if prog_key == self._last_programs:
            logger.debug("TOU: Programs unchanged, skipping write")
            return

        if self._config.dry_run:
            logger.info("TOU DRY RUN: Would write %d programs:", len(programs))
            for i, p in enumerate(programs):
                logger.info("  P%d: %s-%s charging=%s SOC=%d%%",
                            i + 1, p["start"], p["end"],
                            p["charging"], p["soc"])
        else:
            self._write_programs(programs)

        self._last_programs = prog_key

    def _build_programs(self, plan: Plan, tz) -> list[dict]:
        """Build 6 sequential programs from the plan.

        Finds the grid-charge window (if any) and frames it with
        idle/discharge programs before and after.
        """
        from .strategies.helpers import is_grid_charging

        # Find charge window: first and last slot where grid-charging happens
        charge_start_hhmm = None
        charge_end_hhmm = None
        charge_max_soc = 0

        for slot in plan.slots:
            local = slot.start.astimezone(tz)
            local_end = local + timedelta(minutes=slot.duration_min)
            hhmm = local.strftime("%H:%M")
            end_hhmm = local_end.strftime("%H:%M")

            is_charge = (slot.planned_battery_mode == "charge"
                         and slot.planned_battery_w > 50)

            if is_charge:
                if charge_start_hhmm is None:
                    charge_start_hhmm = hhmm
                charge_end_hhmm = end_hhmm
                charge_max_soc = max(charge_max_soc, round(slot.projected_soc))

        # Apply grid-charge SOC limit
        has_grid_charge = False
        if charge_start_hhmm:
            for slot in plan.slots:
                if (slot.planned_battery_mode == "charge"
                    and is_grid_charging(slot.pv_forecast_w, slot.load_estimate_w,
                                         slot.planned_battery_w)):
                    has_grid_charge = True
                    break

        if has_grid_charge:
            soc_limit = self._config.max_grid_charge_soc
        else:
            soc_limit = self._config.max_soc_percent
        charge_target_soc = min(charge_max_soc, soc_limit) if charge_max_soc > 0 else 0

        min_soc = self._config.min_soc_percent

        # Build programs
        if charge_start_hhmm and charge_target_soc > min_soc:
            # There is a charge window
            p1_end = charge_start_hhmm
            p2_start = charge_start_hhmm
            p2_end = charge_end_hhmm
            p3_start = charge_end_hhmm
            p2_charging = "Grid"
            p2_soc = charge_target_soc
        else:
            # No charge needed — P2 gets a minimal dummy window
            p1_end = "23:44"
            p2_start = "23:44"
            p2_end = "23:46"
            p3_start = "23:46"
            p2_charging = "Disabled"
            p2_soc = min_soc

        programs = [
            {"start": "00:00", "end": p1_end,   "charging": "Disabled", "soc": min_soc},
            {"start": p2_start, "end": p2_end,   "charging": p2_charging, "soc": p2_soc},
            {"start": p3_start, "end": "23:50",  "charging": "Disabled", "soc": min_soc},
            {"start": "23:50",  "end": "23:52",  "charging": "Disabled", "soc": min_soc},
            {"start": "23:52",  "end": "23:54",  "charging": "Disabled", "soc": min_soc},
            {"start": "23:54",  "end": "23:56",  "charging": "Disabled", "soc": min_soc},
        ]

        logger.info("TOU: charge window %s-%s → Grid SOC=%d%% (grid_charge=%s)",
                     charge_start_hhmm or "none", charge_end_hhmm or "none",
                     charge_target_soc, has_grid_charge)

        return programs

    def _write_programs(self, programs: list[dict]) -> None:
        """Write exactly 6 sequential TOU programs to Sungrow inverter."""
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
                        i + 1, p["start"], p["end"], p["charging"], p["soc"])
