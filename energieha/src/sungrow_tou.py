"""Sungrow TOU (Time-of-Use) adapter: maps 24h plan to 6 inverter programs.

IMPORTANT: The Sungrow inverter requires exactly 6 programs that run
sequentially and cover the full day without gaps. Each program's end time
must equal the next program's start time.

Mapping logic:
  - charge  → charging="Grid", soc=target_soc%
  - discharge/idle → charging="Disabled", soc=min_soc%
    (Load First pattern automatically discharges to serve load)
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from .ha_client import HaClient
from .models import Config, Plan, Snapshot

logger = logging.getLogger(__name__)

MAX_PROGRAMS = 6

# Fixed program boundaries: split the day into 6 x 4-hour blocks
# Programs must be sequential and cover 24h
DEFAULT_BOUNDARIES = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"]


class TouProgram:
    """One of 6 sequential TOU programs."""

    def __init__(self, start_hhmm: str, end_hhmm: str, charging: str,
                 target_soc: int, power_w: int = 12000):
        self.start_hhmm = start_hhmm
        self.end_hhmm = end_hhmm
        self.charging = charging  # "Grid" or "Disabled"
        self.target_soc = target_soc
        self.power_w = power_w

    def __repr__(self):
        return (f"TOU({self.start_hhmm}-{self.end_hhmm} "
                f"{self.charging} SOC={self.target_soc}%)")


class SungrowTouAdapter:
    """Maps a 24h Plan to 6 sequential Sungrow TOU programs."""

    def __init__(self, client: HaClient, config: Config):
        self._client = client
        self._config = config
        self._last_programs = None
        self._validated = False

    def _validate_entities(self) -> bool:
        """Check that all required Sungrow TOU entities exist."""
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
        logger.info("Sungrow TOU: All %d program entities validated", MAX_PROGRAMS)
        return True

    def apply(self, plan: Plan, snapshot: Snapshot) -> None:
        """Convert plan to 6 sequential TOU programs and write to inverter."""
        if not plan.slots:
            logger.warning("TOU: Empty plan, skipping")
            return

        if not self._validate_entities():
            return

        tz = ZoneInfo(self._config.timezone)
        programs = self._plan_to_programs(plan, tz)

        # Change detection
        prog_key = [(p.start_hhmm, p.end_hhmm, p.charging, p.target_soc)
                     for p in programs]
        if prog_key == self._last_programs:
            logger.debug("TOU: Programs unchanged, skipping write")
            return

        if self._config.dry_run:
            logger.info("TOU DRY RUN: Would write %d programs:", len(programs))
            for i, p in enumerate(programs):
                logger.info("  Program %d: %s-%s charging=%s SOC=%d%%",
                            i + 1, p.start_hhmm, p.end_hhmm,
                            p.charging, p.target_soc)
        else:
            self._write_programs(programs)

        self._last_programs = prog_key

    def _plan_to_programs(self, plan: Plan, tz) -> list[TouProgram]:
        """Convert plan slots into exactly 6 sequential programs covering 24h.

        Strategy:
        1. Consolidate 96 slots into mode-blocks
        2. Map blocks onto 6 fixed 4-hour time windows
        3. For each window: determine dominant mode and SOC target
        """
        # Step 1: Build a per-hour summary from plan slots
        # hour_data[0..23] = {"charge_slots": n, "discharge_slots": n, "idle_slots": n, "max_soc": x, "has_grid_charge": bool}
        hour_data = {}
        from .strategies.helpers import is_grid_charging

        for slot in plan.slots:
            local_start = slot.start.astimezone(tz)
            h = local_start.hour
            if h not in hour_data:
                hour_data[h] = {"charge": 0, "discharge": 0, "idle": 0,
                                "max_soc": 0, "has_grid_charge": False}
            hour_data[h][slot.planned_battery_mode] += 1
            hour_data[h]["max_soc"] = max(hour_data[h]["max_soc"],
                                          round(slot.projected_soc))
            if is_grid_charging(slot.pv_forecast_w, slot.load_estimate_w,
                                slot.planned_battery_w):
                hour_data[h]["has_grid_charge"] = True

        # Step 2: Build 6 programs for 4-hour blocks
        programs = []
        boundaries = DEFAULT_BOUNDARIES + ["00:00"]  # wrap around

        for i in range(MAX_PROGRAMS):
            start = boundaries[i]
            end = boundaries[i + 1]
            sh = int(start.split(":")[0])

            # Count modes across the 4 hours in this block
            total_charge = 0
            total_discharge = 0
            total_idle = 0
            block_max_soc = 0
            block_grid_charge = False

            for h_offset in range(4):
                h = (sh + h_offset) % 24
                if h in hour_data:
                    total_charge += hour_data[h]["charge"]
                    total_discharge += hour_data[h]["discharge"]
                    total_idle += hour_data[h]["idle"]
                    block_max_soc = max(block_max_soc, hour_data[h]["max_soc"])
                    block_grid_charge = block_grid_charge or hour_data[h]["has_grid_charge"]

            # Dominant mode
            if total_charge > total_discharge and total_charge > total_idle:
                charging = "Grid"
                soc_limit = (self._config.max_grid_charge_soc if block_grid_charge
                             else self._config.max_soc_percent)
                target_soc = min(block_max_soc, soc_limit)
            else:
                charging = "Disabled"
                target_soc = self._config.min_soc_percent

            programs.append(TouProgram(
                start_hhmm=start, end_hhmm=end,
                charging=charging, target_soc=target_soc,
            ))

        return programs

    def _write_programs(self, programs: list[TouProgram]) -> None:
        """Write exactly 6 sequential TOU programs to Sungrow inverter."""
        for i, prog in enumerate(programs):
            prog_num = i + 1

            self._client.call_service("select", "select_option", {
                "entity_id": f"select.inverter_program_{prog_num}_charging",
                "option": prog.charging,
            })
            self._client.call_service("number", "set_value", {
                "entity_id": f"number.inverter_program_{prog_num}_soc",
                "value": prog.target_soc,
            })
            self._client.call_service("time", "set_value", {
                "entity_id": f"time.inverter_program_{prog_num}_time",
                "time": prog.start_hhmm,
            })
            self._client.call_service("input_datetime", "set_datetime", {
                "entity_id": f"input_datetime.inverter_program_{prog_num}_end",
                "time": prog.end_hhmm,
            })

        logger.info("TOU: Wrote 6 sequential programs:")
        for i, p in enumerate(programs):
            logger.info("  Program %d: %s-%s charging=%s SOC=%d%%",
                        i + 1, p.start_hhmm, p.end_hhmm,
                        p.charging, p.target_soc)
