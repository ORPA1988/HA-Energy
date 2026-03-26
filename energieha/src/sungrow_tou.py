"""Sungrow TOU (Time-of-Use) adapter: maps 24h plan to inverter programs.

The Sungrow inverter supports 6 TOU programs, each with:
  - Start time / End time
  - Charging mode: Disabled / Grid / Generator / Both
  - SOC target (%)
  - Power limit (W)

Combined with global settings:
  - energy_pattern: "Load First" → battery discharges to serve load
  - work_mode: "Zero Export To CT" → no grid export
  - time_of_use: "Enabled" → TOU programs active

Mapping logic:
  - charge  → charging="Grid", soc=target_soc%
  - discharge/idle → charging="Disabled", soc=min_soc%
    (Load First automatically discharges to serve load)
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from .ha_client import HaClient
from .models import Config, Plan, Snapshot

logger = logging.getLogger(__name__)

MAX_PROGRAMS = 6


class TouBlock:
    """A consolidated time block with uniform battery mode."""

    def __init__(self, start_hhmm: str, end_hhmm: str, mode: str,
                 target_soc: int, power_w: int = 12000):
        self.start_hhmm = start_hhmm
        self.end_hhmm = end_hhmm
        self.mode = mode
        self.target_soc = target_soc
        self.power_w = power_w

    @property
    def charging(self) -> str:
        return "Grid" if self.mode == "charge" else "Disabled"

    def __repr__(self):
        return (f"TouBlock({self.start_hhmm}-{self.end_hhmm} "
                f"{self.mode} SOC={self.target_soc}%)")


class SungrowTouAdapter:
    """Maps a 24h Plan to Sungrow TOU programs via HA service calls."""

    def __init__(self, client: HaClient, config: Config):
        self._client = client
        self._config = config
        self._last_blocks = None  # For change detection

    def apply(self, plan: Plan, snapshot: Snapshot) -> None:
        """Convert plan to TOU programs and write to inverter."""
        if not plan.slots:
            logger.warning("TOU: Empty plan, skipping")
            return

        tz = ZoneInfo(self._config.timezone)

        # 1. Consolidate slots into time blocks
        blocks = self._consolidate(plan, tz)

        # 2. Reduce to max 6 programs
        blocks = self._reduce_to_max(blocks, MAX_PROGRAMS)

        # 3. Check for changes
        block_key = [(b.start_hhmm, b.end_hhmm, b.charging, b.target_soc)
                     for b in blocks]
        if block_key == self._last_blocks:
            logger.debug("TOU: Programs unchanged, skipping write")
            return

        # 4. Write to inverter
        if self._config.dry_run:
            logger.info("TOU DRY RUN: Would write %d programs:", len(blocks))
            for i, b in enumerate(blocks):
                logger.info("  Program %d: %s–%s charging=%s SOC=%d%%",
                            i + 1, b.start_hhmm, b.end_hhmm,
                            b.charging, b.target_soc)
        else:
            self._write_programs(blocks)

        self._last_blocks = block_key

    def _consolidate(self, plan: Plan, tz) -> list[TouBlock]:
        """Merge consecutive slots with same mode into time blocks."""
        blocks = []
        current_mode = None
        current_start = None
        current_end = None
        max_soc_in_block = 0

        for slot in plan.slots:
            local_start = slot.start.astimezone(tz)
            local_end = local_start + timedelta(minutes=slot.duration_min)
            mode = slot.planned_battery_mode
            soc = round(slot.projected_soc)

            if mode == current_mode and current_end is not None:
                # Extend current block
                current_end = local_end
                max_soc_in_block = max(max_soc_in_block, soc)
            else:
                # Save previous block
                if current_mode is not None:
                    target_soc = self._target_soc_for_mode(
                        current_mode, max_soc_in_block)
                    blocks.append(TouBlock(
                        start_hhmm=current_start.strftime("%H:%M"),
                        end_hhmm=current_end.strftime("%H:%M"),
                        mode=current_mode,
                        target_soc=target_soc,
                    ))
                # Start new block
                current_mode = mode
                current_start = local_start
                current_end = local_end
                max_soc_in_block = soc

        # Save last block
        if current_mode is not None:
            target_soc = self._target_soc_for_mode(
                current_mode, max_soc_in_block)
            blocks.append(TouBlock(
                start_hhmm=current_start.strftime("%H:%M"),
                end_hhmm=current_end.strftime("%H:%M"),
                mode=current_mode,
                target_soc=target_soc,
            ))

        logger.debug("TOU: Consolidated %d slots into %d blocks",
                     len(plan.slots), len(blocks))
        return blocks

    def _target_soc_for_mode(self, mode: str, projected_soc: int) -> int:
        """Determine SOC target for a TOU program based on mode."""
        if mode == "charge":
            # Charge up to the projected SOC (capped by max)
            return min(projected_soc, self._config.max_soc_percent)
        else:
            # Discharge/idle: set min SOC as floor
            return self._config.min_soc_percent

    def _reduce_to_max(self, blocks: list[TouBlock], max_count: int) -> list[TouBlock]:
        """Reduce block count to fit within TOU program limit."""
        if len(blocks) <= max_count:
            return blocks

        # Strategy: merge adjacent blocks with same charging mode
        merged = [blocks[0]]
        for b in blocks[1:]:
            prev = merged[-1]
            if prev.charging == b.charging:
                # Merge: extend end time, keep higher SOC target
                merged[-1] = TouBlock(
                    start_hhmm=prev.start_hhmm,
                    end_hhmm=b.end_hhmm,
                    mode=prev.mode if prev.mode == "charge" else b.mode,
                    target_soc=max(prev.target_soc, b.target_soc),
                )
            else:
                merged.append(b)

        if len(merged) <= max_count:
            return merged

        # Still too many: keep the max_count most important blocks
        # Prioritize charge blocks, then longest blocks
        merged.sort(key=lambda b: (
            0 if b.mode == "charge" else 1,
            -self._block_duration_minutes(b),
        ))
        result = merged[:max_count]
        # Re-sort by start time
        result.sort(key=lambda b: b.start_hhmm)

        logger.info("TOU: Reduced %d blocks to %d programs", len(blocks), len(result))
        return result

    @staticmethod
    def _block_duration_minutes(block: TouBlock) -> int:
        """Estimate block duration in minutes from HH:MM strings."""
        sh, sm = map(int, block.start_hhmm.split(":"))
        eh, em = map(int, block.end_hhmm.split(":"))
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        if end_min <= start_min:
            end_min += 24 * 60  # Crosses midnight
        return end_min - start_min

    def _write_programs(self, blocks: list[TouBlock]) -> None:
        """Write TOU programs to Sungrow inverter via HA service calls."""
        for i in range(MAX_PROGRAMS):
            prog_num = i + 1

            if i < len(blocks):
                block = blocks[i]
                charging = block.charging
                soc = block.target_soc
                start_time = block.start_hhmm
                end_time = block.end_hhmm
            else:
                # Unused program: disable
                charging = "Disabled"
                soc = self._config.min_soc_percent
                start_time = "23:50"
                end_time = "23:55"

            # Set charging mode
            self._client.call_service("select", "select_option", {
                "entity_id": f"select.inverter_program_{prog_num}_charging",
                "option": charging,
            })

            # Set SOC target
            self._client.call_service("number", "set_value", {
                "entity_id": f"number.inverter_program_{prog_num}_soc",
                "value": soc,
            })

            # Set start time
            self._client.call_service("time", "set_value", {
                "entity_id": f"time.inverter_program_{prog_num}_time",
                "time": start_time,
            })

            # Set end time
            self._client.call_service("input_datetime", "set_datetime", {
                "entity_id": f"input_datetime.inverter_program_{prog_num}_end",
                "time": end_time,
            })

        logger.info("TOU: Wrote %d programs to inverter (%d active, %d disabled)",
                     MAX_PROGRAMS, len(blocks), MAX_PROGRAMS - len(blocks))
        for i, b in enumerate(blocks):
            logger.info("  Program %d: %s–%s charging=%s SOC=%d%%",
                        i + 1, b.start_hhmm, b.end_hhmm, b.charging, b.target_soc)
