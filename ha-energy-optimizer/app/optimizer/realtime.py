"""EVCC-style real-time EV charging controller (runs every 30s)."""
from __future__ import annotations

import logging
from typing import Optional

from config import get_config
from data.collector import DataCollector
from devices.goe import get_goe_charger
from ha_client import get_ha_client
from models import CarState, EnergyState, EVChargeMode, GoeStatus

logger = logging.getLogger(__name__)

# Minimum surplus to start solar charging (hysteresis prevents flapping)
_SURPLUS_START_THRESHOLD_W = 1400  # ~6A at 230V
_SURPLUS_STOP_THRESHOLD_W = 1000


class RealtimeController:
    """
    EVCC-style real-time controller:
    - solar mode: charges EV with solar surplus only
    - min_solar: charges at minimum current always, adds solar surplus on top
    - fast: charges at maximum current regardless
    - smart: LP optimizer decides (uses pre-computed schedule)
    - off: no charging
    """

    def __init__(self):
        self._cfg = get_config()
        self._ha = get_ha_client()
        self._goe = get_goe_charger()
        self._collector = DataCollector()
        self._last_current_a: int = 0
        self._charging_active: bool = False
        # Smart mode: injected by coordinator
        self.smart_current_a: int = 0
        self.smart_enabled: bool = False

    def compute_surplus(self, pv_w: float, house_load_w: float, battery_power_w: float) -> float:
        """
        Calculate solar power available for EV charging.
        battery_power_w > 0 means battery is charging (consuming power).
        """
        # Surplus = PV output - house consumption - power going into battery
        return max(0.0, pv_w - house_load_w - max(0.0, battery_power_w))

    def get_ev_setpoint(
        self,
        surplus_w: float,
        mode: EVChargeMode,
        ev_soc: Optional[float],
        target_soc: int,
        phases: int,
    ) -> int:
        """
        Calculate target charge current in Amperes.

        Returns 0 if charging should be stopped, otherwise min_current..max_current.
        """
        cfg = self._cfg
        min_a = cfg.ev_min_charge_current_a
        max_a = cfg.ev_max_charge_current_a

        # Never exceed target SOC
        if ev_soc is not None and ev_soc >= target_soc:
            return 0

        if mode == EVChargeMode.OFF:
            return 0

        if mode == EVChargeMode.FAST:
            return max_a

        if mode == EVChargeMode.MIN_SOLAR:
            # Always charge at minimum; boost with surplus
            surplus_current = int(surplus_w / (phases * 230))
            return min(max_a, min_a + max(0, surplus_current - min_a))

        if mode == EVChargeMode.SOLAR:
            surplus_current = int(surplus_w / (phases * 230))
            if not self._charging_active:
                # Start only if surplus > start threshold
                if surplus_w < _SURPLUS_START_THRESHOLD_W:
                    return 0
            else:
                # Keep charging if still above stop threshold
                if surplus_w < _SURPLUS_STOP_THRESHOLD_W:
                    return 0
            return min(max_a, max(min_a, surplus_current))

        if mode == EVChargeMode.SMART:
            return self.smart_current_a if self.smart_enabled else 0

        return 0

    async def run(
        self,
        state: EnergyState,
        goe_status: Optional[GoeStatus],
        lp_current_a: int = 0,
        lp_enabled: bool = False,
        target_soc: int = 80,
    ) -> None:
        """Execute one control cycle. Called every 30s by scheduler."""
        cfg = self._cfg
        phases = goe_status.phases_active if goe_status else cfg.goe_phases

        # Update smart mode setpoints from LP/genetic result
        self.smart_current_a = lp_current_a
        self.smart_enabled = lp_enabled

        # Determine mode
        mode = EVChargeMode(cfg.ev_charge_mode)

        # Override mode if car not connected
        if goe_status and goe_status.car_state == CarState.NONE:
            # No car — nothing to do
            self._charging_active = False
            return

        # Calculate target current
        target_a = self.get_ev_setpoint(
            surplus_w=state.surplus_w,
            mode=mode,
            ev_soc=state.ev_soc_percent,
            target_soc=target_soc,
            phases=phases,
        )

        # Apply changes only if they differ (avoid chattering)
        if target_a == 0 and self._charging_active:
            if self._goe.available:
                await self._goe.set_enabled(False)
            else:
                await self._ha.turn_off(cfg.battery_charge_switch)
            self._charging_active = False
            logger.debug("EV charging stopped (mode=%s, surplus=%.0fW)", mode.value, state.surplus_w)

        elif target_a > 0:
            if not self._charging_active:
                if self._goe.available:
                    await self._goe.set_enabled(True)
                self._charging_active = True

            if target_a != self._last_current_a:
                if self._goe.available:
                    await self._goe.set_current(target_a)
                logger.debug(
                    "EV current: %dA → %dA (mode=%s, surplus=%.0fW)",
                    self._last_current_a, target_a, mode.value, state.surplus_w,
                )
                self._last_current_a = target_a

        # Publish EV state to HA
        await self._ha.publish_sensor(
            "ev_charge_current_a", target_a, "A",
            {"mode": mode.value, "surplus_w": round(state.surplus_w, 0)},
        )


# Global singleton
_realtime: Optional[RealtimeController] = None


def get_realtime_controller() -> RealtimeController:
    global _realtime
    if _realtime is None:
        _realtime = RealtimeController()
    return _realtime
