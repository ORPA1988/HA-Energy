"""Collect current energy system state from Home Assistant sensors."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from config import get_config
from ha_client import get_ha_client
from models import CarState, EnergyState, EVChargeMode

logger = logging.getLogger(__name__)


class DataCollector:
    """Reads live sensor data from HA and assembles an EnergyState snapshot."""

    def __init__(self):
        self._cfg = get_config()
        self._ha = get_ha_client()
        self._decomposer = None
        self._ev_soc_warned: bool = False  # Rate-limit EV SOC unavailable warning

    def _get_decomposer(self):
        if self._decomposer is None:
            from data.load_decomposition import get_load_decomposer
            self._decomposer = get_load_decomposer()
        return self._decomposer

    async def get_current_state(
        self,
        current_price_ct: float = 0.0,
        current_price_net_ct: float = 0.0,
        current_price_total_ct: float = 0.0,
    ) -> EnergyState:
        cfg = self._cfg
        ha = self._ha

        # Fetch all sensor values in parallel
        coros = [
            ha.get_state_value(cfg.pv_power_sensor, 0.0),
            ha.get_state_value(cfg.battery_soc_sensor, 50.0),
            ha.get_state_value(cfg.battery_power_sensor, 0.0),
            ha.get_state_value(cfg.grid_power_sensor, 0.0),
        ]
        has_powerloss = bool(cfg.inverter_powerloss_sensor)
        if has_powerloss:
            coros.append(ha.get_state_value(cfg.inverter_powerloss_sensor, 0.0))
        results = await asyncio.gather(*coros)
        pv_w, battery_soc, battery_power, grid_power = results[:4]
        powerloss = results[4] if has_powerloss else 0.0

        # Use load decomposition if any loads are marked for subtraction
        has_subtractable = any(dl.subtract_from_total for dl in cfg.deferrable_loads)
        if has_subtractable:
            decomposer = self._get_decomposer()
            house_load = await decomposer.get_base_load_w()
        else:
            # Energy balance: PV + grid_import = house + battery_charge + grid_export + powerloss
            # grid_power: positive = import, negative = export
            # battery_power: positive = charging, negative = discharging
            # powerloss: inverter conversion losses (always positive)
            # Therefore: house = PV + grid_power - battery_power - powerloss
            house_load = max(0.0, pv_w + grid_power - battery_power - powerloss)

        # Solar surplus: what's available beyond house load and battery charging
        surplus = max(0.0, pv_w - house_load - max(0.0, battery_power))

        # EV from HA sensor (go-e data merged separately by devices/goe.py)
        ev_soc = None
        ev_soc_raw = await ha.get_state(cfg.ev_soc_sensor)
        if ev_soc_raw:
            try:
                ev_soc = float(ev_soc_raw["state"])
            except (ValueError, TypeError):
                ev_soc = None
        
        # Warn if EV charging configured but sensor unavailable (once only)
        if ev_soc is None and (cfg.goe_enabled or cfg.ev_charging_windows):
            if not self._ev_soc_warned:
                logger.warning("EV SOC sensor '%s' unavailable but EV charging is enabled. "
                              "Optimization will use fallback SOC values.",
                              cfg.ev_soc_sensor)
                self._ev_soc_warned = True
        elif ev_soc is not None:
            self._ev_soc_warned = False  # Reset when sensor becomes available

        return EnergyState(
            timestamp=datetime.now(),
            pv_power_w=pv_w,
            battery_soc_percent=battery_soc,
            battery_power_w=battery_power,
            battery_capacity_kwh=cfg.battery_capacity_kwh,
            grid_power_w=grid_power,
            inverter_powerloss_w=powerloss,
            house_load_w=house_load,
            surplus_w=surplus,
            ev_soc_percent=ev_soc,
            ev_charge_mode=EVChargeMode(cfg.ev_charge_mode) if cfg.ev_charge_mode in EVChargeMode._value2member_map_ else EVChargeMode.SMART,
            price_raw_ct_kwh=current_price_ct,
            price_net_ct_kwh=current_price_net_ct,
            price_total_ct_kwh=current_price_total_ct,
            feed_in_ct_kwh=cfg.price_feed_in_ct_kwh,
        )
