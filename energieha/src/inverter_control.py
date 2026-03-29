"""Direct inverter control via Home Assistant services.

Controls Sungrow TOU programs, battery modes, grid charging,
and PHEV charging through HA service calls instead of just publishing sensors.
"""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .ha_client import HaClient
from .models import Config

logger = logging.getLogger(__name__)

# Sungrow TOU entity patterns
TOU_TIME_PATTERN = "time.inverter_program_{}_time"
TOU_END_PATTERN = "input_datetime.inverter_program_{}_end"
TOU_CHARGING_PATTERN = "select.inverter_program_{}_charging"
TOU_SOC_PATTERN = "number.inverter_program_{}_soc"
TOU_POWER_PATTERN = "number.inverter_program_{}_power"

# Sungrow mode entities
WORK_MODE_ENTITY = "select.inverter_work_mode"
ENERGY_PATTERN_ENTITY = "select.inverter_energy_pattern"
TOU_ENABLE_ENTITY = "select.inverter_time_of_use"

# Battery entities
GRID_CHARGE_CURRENT_ENTITY = "number.inverter_battery_grid_charging_current"


class InverterController:
    """Direct control of Sungrow inverter and go-eCharger via HA services."""

    def __init__(self, client: HaClient, config: Config):
        self._client = client
        self._config = config
        self._consecutive_failures = 0
        self._max_failures = 3

    def set_tou_program(self, program_num: int, start_time: str, end_time: str,
                        mode: str, soc_target: int, power_limit: int = 5000) -> bool:
        """Set a single TOU program (1-6).

        Args:
            program_num: Program number 1-6
            start_time: Start time "HH:MM:SS"
            end_time: End time "HH:MM:SS"
            mode: "Grid" or "Disabled"
            soc_target: Target SOC 0-100
            power_limit: Max power in W
        """
        if self._config.dry_run:
            logger.info("DRY RUN: Would set TOU program %d: %s-%s %s SOC=%d%% P=%dW",
                        program_num, start_time, end_time, mode, soc_target, power_limit)
            return True

        if not self._config.direct_control:
            logger.debug("direct_control disabled, skipping TOU program %d", program_num)
            return False

        if self._consecutive_failures >= self._max_failures:
            logger.warning("Circuit breaker open: %d consecutive failures", self._consecutive_failures)
            return False

        try:
            # Set start time
            self._client.call_service("time", "set_value", {
                "entity_id": TOU_TIME_PATTERN.format(program_num),
                "time": start_time,
            })

            # Set charging mode
            self._client.call_service("select", "select_option", {
                "entity_id": TOU_CHARGING_PATTERN.format(program_num),
                "option": mode,
            })

            # Set SOC target
            self._client.call_service("number", "set_value", {
                "entity_id": TOU_SOC_PATTERN.format(program_num),
                "value": soc_target,
            })

            self._consecutive_failures = 0
            logger.info("TOU program %d set: %s-%s %s SOC=%d%%",
                        program_num, start_time, end_time, mode, soc_target)
            return True

        except Exception as e:
            self._consecutive_failures += 1
            logger.error("Failed to set TOU program %d: %s", program_num, e)
            return False

    def set_battery_grid_charge_current(self, amps: float) -> bool:
        """Set the grid charging current limit."""
        if self._config.dry_run:
            logger.info("DRY RUN: Would set grid charge current to %.1fA", amps)
            return True

        if not self._config.direct_control:
            return False

        try:
            entity = self._config.entity_grid_charge_current
            self._client.call_service("number", "set_value", {
                "entity_id": entity,
                "value": amps,
            })
            logger.info("Grid charge current set to %.1fA", amps)
            return True
        except Exception as e:
            logger.error("Failed to set grid charge current: %s", e)
            return False

    def set_phev_charge_current(self, amps: int) -> bool:
        """Set PHEV charge current via go-eCharger."""
        if self._config.dry_run:
            logger.info("DRY RUN: Would set PHEV charge to %dA", amps)
            return True

        if not self._config.direct_control or not self._config.phev_enabled:
            return False

        try:
            self._client.call_service("number", "set_value", {
                "entity_id": self._config.entity_phev_ampere_limit,
                "value": max(0, min(amps, 16)),
            })
            logger.info("PHEV charge current set to %dA", amps)
            return True
        except Exception as e:
            logger.error("Failed to set PHEV charge current: %s", e)
            return False

    def read_tou_programs(self) -> list:
        """Read current TOU program states from HA."""
        programs = []
        for i in range(1, 7):
            try:
                time_state = self._client.get_state(TOU_TIME_PATTERN.format(i))
                charging_state = self._client.get_state(TOU_CHARGING_PATTERN.format(i))
                soc_state = self._client.get_state(TOU_SOC_PATTERN.format(i))

                programs.append({
                    "number": i,
                    "start_time": time_state.get("state", "00:00:00") if time_state else "00:00:00",
                    "mode": charging_state.get("state", "Disabled") if charging_state else "Disabled",
                    "soc_target": int(float(soc_state.get("state", "0"))) if soc_state else 0,
                })
            except Exception as e:
                logger.warning("Failed to read TOU program %d: %s", i, e)
                programs.append({
                    "number": i, "start_time": "?", "mode": "?", "soc_target": 0,
                })
        return programs

    def read_inverter_state(self) -> dict:
        """Read comprehensive inverter state."""
        state = {}
        entities = {
            "work_mode": WORK_MODE_ENTITY,
            "energy_pattern": ENERGY_PATTERN_ENTITY,
            "tou_enabled": TOU_ENABLE_ENTITY,
            "grid_charge_current": GRID_CHARGE_CURRENT_ENTITY,
        }
        for key, entity_id in entities.items():
            data = self._client.get_state(entity_id)
            state[key] = data.get("state", "unknown") if data else "unavailable"

        state["tou_programs"] = self.read_tou_programs()

        # Live sensor values
        live_entities = {
            "battery_soc": self._config.entity_battery_soc,
            "battery_power": self._config.entity_battery_power,
            "pv_power": self._config.entity_pv_power,
            "grid_power": self._config.entity_grid_power,
            "load_power": self._config.entity_load_power,
        }
        for key, eid in live_entities.items():
            try:
                data = self._client.get_state(eid)
                state[key] = float(data.get("state", 0)) if data else 0
            except (ValueError, TypeError):
                state[key] = 0

        # Battery voltage from attributes
        try:
            batt_data = self._client.get_state(self._config.entity_battery_soc)
            if batt_data:
                state["battery_voltage"] = float(batt_data.get("attributes", {}).get("BMS Voltage", 0))
                state["charge_power_w"] = state.get("battery_voltage", 0) * float(state.get("grid_charge_current", 0))
        except (ValueError, TypeError):
            pass

        return state

    def reset_failures(self):
        """Reset the circuit breaker."""
        self._consecutive_failures = 0
