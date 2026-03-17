"""EVCC-style real-time EV charging controller (runs every 30s)."""
from __future__ import annotations

import logging
from typing import Optional

from config import get_config
from data.collector import DataCollector
from devices.goe import get_goe_charger
from devices.wallbox import WallboxInterface, HAEntityWallbox, OCPPWallbox, WallboxCarState
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

    Supports multiple wallbox backends via WallboxInterface:
    - go-e Charger (native API)
    - Generic HA entity-based wallboxes
    - OCPP-compatible wallboxes
    """

    def __init__(self):
        self._cfg = get_config()
        self._ha = get_ha_client()
        self._goe = get_goe_charger()
        self._wallbox: Optional[WallboxInterface] = None
        self._wallboxes: dict[str, WallboxInterface] = {}  # Multi-EV: name → wallbox
        self._collector = DataCollector()
        self._last_current_a: int = 0
        self._last_currents: dict[str, int] = {}  # Multi-EV: name → last current
        self._charging_active: bool = False
        self._charging_states: dict[str, bool] = {}  # Multi-EV: name → active
        # Smart mode: injected by coordinator
        self.smart_current_a: int = 0
        self.smart_enabled: bool = False

    def _get_wallbox(self) -> Optional[WallboxInterface]:
        """Get the active wallbox interface (go-e or generic HA)."""
        if self._goe.available:
            return None  # Use go-e directly (legacy path)
        if self._wallbox is None and self._cfg.goe_enabled:
            # No go-e configured but charging enabled — try generic HA wallbox
            # This uses switch entity from config as a basic wallbox
            self._wallbox = HAEntityWallbox(
                name="wallbox",
                switch_entity=self._cfg.battery_charge_switch,  # Reuse for now
                max_current_a=self._cfg.goe_max_current_a,
                phases=self._cfg.goe_phases,
            )
        return self._wallbox

    def _get_wallboxes(self) -> dict[str, WallboxInterface]:
        """Build wallbox instances from ev_configs (lazy init)."""
        if self._wallboxes:
            return self._wallboxes
        for ev in self._cfg.ev_configs:
            if ev.wallbox_type == "goe" and ev.use_global_goe:
                continue  # Uses legacy go-e path
            if ev.wallbox_type == "ha_entity" and ev.switch_entity:
                self._wallboxes[ev.name] = HAEntityWallbox(
                    name=ev.name,
                    switch_entity=ev.switch_entity,
                    power_sensor=ev.power_sensor,
                    current_number=ev.current_number,
                    car_state_sensor=ev.car_state_sensor,
                    max_current_a=ev.max_charge_current_a,
                    phases=ev.phases,
                )
            elif ev.wallbox_type == "ocpp" and ev.ocpp_entity_prefix:
                self._wallboxes[ev.name] = OCPPWallbox(
                    name=ev.name,
                    entity_prefix=ev.ocpp_entity_prefix,
                    max_current_a=ev.max_charge_current_a,
                    phases=ev.phases,
                )
        return self._wallboxes

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

    async def _set_charging_enabled(self, enabled: bool) -> None:
        """Enable/disable charging via the best available backend."""
        if self._goe.available:
            await self._goe.set_enabled(enabled)
            return
        wb = self._get_wallbox()
        if wb:
            await wb.set_enabled(enabled)
            return
        # Fallback: direct HA switch
        if enabled:
            await self._ha.turn_on(self._cfg.battery_charge_switch)
        else:
            await self._ha.turn_off(self._cfg.battery_charge_switch)

    async def _set_charging_current(self, current_a: int) -> None:
        """Set charge current via the best available backend."""
        if self._goe.available:
            await self._goe.set_current(current_a)
            return
        wb = self._get_wallbox()
        if wb:
            await wb.set_current(current_a)

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

        # Read-only mode: skip all control actions
        if cfg.read_only:
            logger.debug("[READ-ONLY] Skipping EV control cycle")
            return

        # Determine phases from go-e status, wallbox status, or config
        wb = self._get_wallbox()
        if goe_status:
            phases = goe_status.phases_active
        elif wb:
            wb_status = await wb.get_status()
            phases = wb_status.phases_active if wb_status else cfg.goe_phases
        else:
            phases = cfg.goe_phases

        # Update smart mode setpoints from LP/genetic result
        self.smart_current_a = lp_current_a
        self.smart_enabled = lp_enabled

        # Determine mode
        mode = EVChargeMode(cfg.ev_charge_mode)

        # Override mode if car not connected
        car_connected = True
        if goe_status and goe_status.car_state == CarState.NONE:
            car_connected = False
        elif wb and not goe_status:
            wb_st = await wb.get_status()
            if wb_st and wb_st.car_state == WallboxCarState.NONE:
                car_connected = False

        if not car_connected:
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
            await self._set_charging_enabled(False)
            self._charging_active = False
            logger.debug("EV charging stopped (mode=%s, surplus=%.0fW)", mode.value, state.surplus_w)

        elif target_a > 0:
            if not self._charging_active:
                await self._set_charging_enabled(True)
                self._charging_active = True

            if target_a != self._last_current_a:
                await self._set_charging_current(target_a)
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

        # Multi-EV: control additional wallboxes from ev_configs
        wallboxes = self._get_wallboxes()
        if wallboxes:
            await self._run_multi_ev(state, wallboxes)

    async def _run_multi_ev(self, state: EnergyState, wallboxes: dict[str, WallboxInterface]) -> None:
        """Control additional wallboxes from ev_configs using LP schedule data."""
        for name, wb in wallboxes.items():
            ev_cfg = next((e for e in self._cfg.ev_configs if e.name == name), None)
            if not ev_cfg:
                continue

            mode = EVChargeMode(ev_cfg.charge_mode)
            if mode == EVChargeMode.OFF:
                if self._charging_states.get(name, False):
                    await wb.set_enabled(False)
                    self._charging_states[name] = False
                continue

            # Read EV SOC from sensor if available
            ev_soc = None
            if ev_cfg.soc_sensor:
                soc_raw = await self._ha.get_state(ev_cfg.soc_sensor)
                if soc_raw:
                    try:
                        ev_soc = float(soc_raw["state"])
                    except (ValueError, TypeError):
                        pass

            target_a = self.get_ev_setpoint(
                surplus_w=state.surplus_w,
                mode=mode,
                ev_soc=ev_soc,
                target_soc=ev_cfg.target_soc,
                phases=ev_cfg.phases,
            )

            was_active = self._charging_states.get(name, False)
            if target_a == 0 and was_active:
                await wb.set_enabled(False)
                self._charging_states[name] = False
                logger.debug("EV '%s' charging stopped", name)
            elif target_a > 0:
                if not was_active:
                    await wb.set_enabled(True)
                    self._charging_states[name] = True
                last = self._last_currents.get(name, 0)
                if target_a != last:
                    await wb.set_current(target_a)
                    self._last_currents[name] = target_a
                    logger.debug("EV '%s' current: %dA → %dA", name, last, target_a)

            # Publish per-EV sensor
            prefix = name.lower().replace(" ", "_").replace("-", "_")
            await self._ha.publish_sensor(
                f"{prefix}_charge_current_a", target_a, "A",
                {"mode": mode.value},
            )


# Global singleton
_realtime: Optional[RealtimeController] = None


def get_realtime_controller() -> RealtimeController:
    global _realtime
    if _realtime is None:
        _realtime = RealtimeController()
    return _realtime
