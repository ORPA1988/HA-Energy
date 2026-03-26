"""Plan executor: publishes control parameters as add-on-owned HA entities.

Published control entities:
  sensor.energieha_battery_mode   — "charge" / "discharge" / "idle"
  sensor.energieha_phev_charge_w  — target PHEV charge power in W (0 = off)
  sensor.energieha_grid_setpoint  — planned grid import(+)/export(-) in W

Battery power is NOT set by the add-on — the inverter determines it.
PHEV charge power is adjusted to PV surplus within configured min/max limits.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .ha_client import HaClient
from .models import Config, Plan

logger = logging.getLogger(__name__)

PREFIX = "sensor.energieha"

MODE_IDLE = "idle"
MODE_CHARGE = "charge"
MODE_DISCHARGE = "discharge"

MIN_POWER_THRESHOLD_W = 50


class Executor:
    """Publishes control parameters as add-on-owned HA sensor entities."""

    MIN_MODE_HOLD_SECONDS = 120  # Don't flip modes faster than 2 minutes

    def __init__(self, client: HaClient, config: Config):
        self._client = client
        self._config = config
        self._last_mode = None
        self._last_phev_w = None
        self._last_mode_change = None  # datetime of last mode change

    def execute(self, plan: Plan) -> None:
        """Publish control parameters for the current time slot."""
        slot = plan.current_slot
        if slot is None:
            logger.warning("No current slot in plan – publishing idle")
            self._publish_idle()
            return

        mode = slot.planned_battery_mode
        phev_w = round(slot.planned_phev_w)
        grid_w = round(slot.planned_grid_w)

        # Hysteresis: don't flip modes too fast
        if (mode != self._last_mode and self._last_mode_change is not None):
            now_dt = datetime.now(ZoneInfo(self._config.timezone))
            elapsed = (now_dt - self._last_mode_change).total_seconds()
            if elapsed < self.MIN_MODE_HOLD_SECONDS:
                logger.debug("Hysteresis: holding %s for %ds more",
                             self._last_mode, self.MIN_MODE_HOLD_SECONDS - elapsed)
                mode = self._last_mode

        # Only write if values changed
        if mode == self._last_mode and phev_w == self._last_phev_w:
            logger.debug("Control unchanged (%s, PHEV %dW) – skipping", mode, phev_w)
            return

        now = datetime.now(ZoneInfo(self._config.timezone)).isoformat()

        try:
            # Battery mode (inverter handles power)
            self._client.set_state(f"{PREFIX}_battery_mode", mode, {
                "friendly_name": "EnergieHA Battery Mode",
                "icon": _mode_icon(mode),
                "options": [MODE_IDLE, MODE_CHARGE, MODE_DISCHARGE],
                "estimated_power_w": round(slot.planned_battery_w),
                "projected_soc": round(slot.projected_soc, 1),
                "dry_run": self._config.dry_run,
                "timestamp": now,
            })

            # PHEV charge power + Wallbox Ampere control
            if self._config.phev_enabled:
                phev_active = phev_w >= self._config.phev_min_charge_w
                # Convert W → A for go-eCharger (single phase)
                phev_ampere = int(phev_w / self._config.phev_voltage) if phev_active else 0
                phev_ampere = max(0, min(phev_ampere, 16))  # clamp 0-16A

                # Publish informational sensor
                self._client.set_state(f"{PREFIX}_phev_charge_w", str(phev_w), {
                    "friendly_name": "EnergieHA PHEV Charge Power",
                    "unit_of_measurement": "W",
                    "device_class": "power",
                    "state_class": "measurement",
                    "icon": "mdi:car-electric" if phev_active else "mdi:car-electric-outline",
                    "active": phev_active,
                    "ampere": phev_ampere,
                    "min_charge_w": self._config.phev_min_charge_w,
                    "max_charge_w": self._config.phev_max_charge_w,
                    "timestamp": now,
                })

                # Publish target ampere for go-eCharger automation
                self._client.set_state(f"{PREFIX}_phev_target_ampere", str(phev_ampere), {
                    "friendly_name": "EnergieHA PHEV Target Ampere",
                    "unit_of_measurement": "A",
                    "device_class": "current",
                    "icon": "mdi:current-ac",
                    "wallbox_entity": self._config.entity_phev_ampere_limit,
                    "timestamp": now,
                })

            # Grid setpoint
            self._client.set_state(f"{PREFIX}_grid_setpoint", str(grid_w), {
                "friendly_name": "EnergieHA Grid Setpoint",
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
                "icon": "mdi:transmission-tower",
                "description": "Positive=import, negative=export",
                "timestamp": now,
            })

            logger.info("Control: battery=%s | PHEV=%dW | grid=%dW | "
                        "price=%.4f€/kWh | SOC→%.1f%%",
                        mode, phev_w, grid_w,
                        slot.price_eur_kwh, slot.projected_soc)

        except Exception as e:
            logger.error("Failed to publish control entities: %s", e)

        if mode != self._last_mode:
            self._last_mode_change = datetime.now(ZoneInfo(self._config.timezone))
        self._last_mode = mode
        self._last_phev_w = phev_w

        if self._config.dry_run:
            logger.info("DRY RUN – entities published but marked as dry_run")

    def _publish_idle(self) -> None:
        """Publish idle state for all control entities."""
        now = datetime.now(ZoneInfo(self._config.timezone)).isoformat()
        try:
            self._client.set_state(f"{PREFIX}_battery_mode", MODE_IDLE, {
                "friendly_name": "EnergieHA Battery Mode",
                "icon": "mdi:battery-outline",
                "options": [MODE_IDLE, MODE_CHARGE, MODE_DISCHARGE],
                "estimated_power_w": 0,
                "timestamp": now,
            })
            if self._config.phev_enabled:
                self._client.set_state(f"{PREFIX}_phev_charge_w", "0", {
                    "friendly_name": "EnergieHA PHEV Charge Power",
                    "unit_of_measurement": "W",
                    "device_class": "power",
                    "icon": "mdi:car-electric-outline",
                    "active": False,
                    "ampere": 0,
                    "timestamp": now,
                })
                self._client.set_state(f"{PREFIX}_phev_target_ampere", "0", {
                    "friendly_name": "EnergieHA PHEV Target Ampere",
                    "unit_of_measurement": "A",
                    "device_class": "current",
                    "icon": "mdi:current-ac",
                    "timestamp": now,
                })
            self._client.set_state(f"{PREFIX}_grid_setpoint", "0", {
                "friendly_name": "EnergieHA Grid Setpoint",
                "unit_of_measurement": "W",
                "device_class": "power",
                "icon": "mdi:transmission-tower",
                "timestamp": now,
            })
        except Exception as e:
            logger.error("Failed to publish idle state: %s", e)
        self._last_mode = MODE_IDLE
        self._last_phev_w = 0


def _mode_icon(mode: str) -> str:
    return {
        MODE_CHARGE: "mdi:battery-charging-high",
        MODE_DISCHARGE: "mdi:battery-arrow-down",
        MODE_IDLE: "mdi:battery-outline",
    }.get(mode, "mdi:battery-outline")
