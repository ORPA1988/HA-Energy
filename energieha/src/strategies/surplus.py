"""Simple surplus strategy: charge from PV excess, discharge to cover load.

Battery: only mode control (charge/discharge/idle), power set by inverter.
PHEV: charge power tracks PV surplus, clamped to min/max charge limits.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..models import Config, ForecastPoint, Plan, PricePoint, Snapshot, TimeSlot
from .helpers import (calc_grid_balance, calc_phev_power, get_forecast_for_time,
                      get_price_for_time, is_grid_charging, update_soc)

logger = logging.getLogger(__name__)


def plan_surplus(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
) -> Plan:
    """Create a 24h plan based on simple PV surplus logic."""
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    slot_minutes = config.slot_duration_min
    num_slots = (24 * 60) // slot_minutes
    slots = []
    soc = snapshot.battery_soc

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = get_forecast_for_time(pv_forecast, slot_start)
        price = get_price_for_time(prices, slot_start)
        load_w = snapshot.load_power_w if i == 0 else config.load_per_slot_w

        surplus_w = pv_w - load_w
        phev_w = calc_phev_power(surplus_w, config, snapshot)
        remaining_surplus = surplus_w - phev_w

        battery_mode = "idle"
        battery_w_est = 0.0

        if remaining_surplus > 0 and soc < config.max_soc_percent:
            battery_mode = "charge"
            headroom_wh = (config.max_soc_percent - soc) / 100.0 * config.battery_capacity_wh
            battery_w_est = min(remaining_surplus, headroom_wh / (slot_minutes / 60.0))

        elif surplus_w < 0 and soc > config.min_soc_percent:
            battery_mode = "discharge"
            deficit_w = abs(surplus_w)
            available_wh = (soc - config.min_soc_percent) / 100.0 * config.battery_capacity_wh
            battery_w_est = -min(deficit_w, available_wh / (slot_minutes / 60.0))

        # Grid-charge limit: don't grid-charge above max_grid_charge_soc
        if is_grid_charging(pv_w, load_w, battery_w_est) and soc >= config.max_grid_charge_soc:
            battery_mode = "idle"
            battery_w_est = 0.0

        soc = update_soc(soc, battery_w_est, slot_minutes, config)
        grid_w = calc_grid_balance(pv_w, load_w, phev_w, battery_w_est)

        slots.append(TimeSlot(
            start=slot_start, duration_min=slot_minutes,
            pv_forecast_w=pv_w, price_eur_kwh=price, load_estimate_w=load_w,
            planned_battery_mode=battery_mode, planned_battery_w=battery_w_est,
            planned_phev_w=phev_w, planned_grid_w=grid_w, projected_soc=soc,
        ))

    logger.info("Surplus plan: %d slots, SOC %.1f%%→%.1f%%, PHEV=%s",
                len(slots), snapshot.battery_soc,
                slots[-1].projected_soc if slots else soc,
                "connected" if snapshot.phev_connected else "off")

    return Plan(created_at=now, strategy="surplus", slots=slots, tz=config.timezone)
