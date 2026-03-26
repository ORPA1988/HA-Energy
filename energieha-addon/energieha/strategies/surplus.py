"""Simple surplus strategy: charge from PV excess, discharge to cover load.

Battery: only mode control (charge/discharge/idle), power set by inverter.
PHEV: charge power tracks PV surplus, clamped to min/max charge limits.
"""

import logging
from datetime import datetime, timedelta, timezone

from ..models import Config, ForecastPoint, Plan, PricePoint, Snapshot, TimeSlot

logger = logging.getLogger(__name__)


def plan_surplus(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
) -> Plan:
    """Create a 24h plan based on simple PV surplus logic.

    Priority order for PV surplus:
    1. Cover house load
    2. Charge PHEV (if connected, adjusted to surplus)
    3. Charge house battery (mode only, inverter sets power)
    4. Export to grid
    """
    now = datetime.now(timezone.utc)
    slot_minutes = config.slot_duration_min
    num_slots = (24 * 60) // slot_minutes
    slots = []
    soc = snapshot.battery_soc

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = _get_forecast_for_time(pv_forecast, slot_start)
        price = _get_price_for_time(prices, slot_start)
        load_w = snapshot.load_power_w if i == 0 else config.load_per_slot_w

        # Net surplus after house load
        surplus_w = pv_w - load_w

        # --- PHEV: gets surplus first (between min and max charge power) ---
        phev_w = 0.0
        if config.phev_enabled and snapshot.phev_connected and surplus_w > 0:
            if surplus_w >= config.phev_min_charge_w:
                phev_w = min(surplus_w, config.phev_max_charge_w)
            # Below min charge power → PHEV off (can't charge below minimum)

        remaining_surplus = surplus_w - phev_w

        # --- House battery: mode only ---
        battery_mode = "idle"
        battery_w_est = 0.0  # estimated power (informational)

        if remaining_surplus > 0 and soc < config.max_soc_percent:
            battery_mode = "charge"
            # Estimate: inverter takes what it can, capped by SOC headroom
            headroom_wh = (config.max_soc_percent - soc) / 100.0 * config.battery_capacity_wh
            max_this_slot = headroom_wh / (slot_minutes / 60.0)
            battery_w_est = min(remaining_surplus, max_this_slot)

        elif surplus_w < 0 and soc > config.min_soc_percent:
            # Deficit → discharge to cover load
            battery_mode = "discharge"
            deficit_w = abs(surplus_w)
            available_wh = (soc - config.min_soc_percent) / 100.0 * config.battery_capacity_wh
            max_this_slot = available_wh / (slot_minutes / 60.0)
            battery_w_est = -min(deficit_w, max_this_slot)

        # Update projected SOC
        energy_wh = battery_w_est * (slot_minutes / 60.0)
        soc += (energy_wh / config.battery_capacity_wh) * 100.0
        soc = max(config.min_soc_percent, min(config.max_soc_percent, soc))

        # Grid balance
        net = pv_w - load_w - phev_w - battery_w_est
        grid_w = -net  # positive = import

        slots.append(TimeSlot(
            start=slot_start,
            duration_min=slot_minutes,
            pv_forecast_w=pv_w,
            price_eur_kwh=price,
            load_estimate_w=load_w,
            planned_battery_mode=battery_mode,
            planned_battery_w=battery_w_est,
            planned_phev_w=phev_w,
            planned_grid_w=grid_w,
            projected_soc=soc,
        ))

    logger.info("Surplus plan: %d slots, SOC %.1f%%→%.1f%%, PHEV=%s",
                len(slots), snapshot.battery_soc,
                slots[-1].projected_soc if slots else soc,
                "connected" if snapshot.phev_connected else "off")

    return Plan(created_at=now, strategy="surplus", slots=slots)


def _get_forecast_for_time(forecast: list[ForecastPoint], t: datetime) -> float:
    for fp in forecast:
        if fp.start <= t < fp.end:
            return fp.power_w
    return 0.0


def _get_price_for_time(prices: list[PricePoint], t: datetime) -> float:
    for pp in prices:
        if pp.start <= t < pp.end:
            return pp.price_eur_kwh
    return 0.0
