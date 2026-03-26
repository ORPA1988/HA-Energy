"""PV-forecast-based strategy: smart scheduling using solar prediction.

Battery: only mode control (charge/discharge/idle), power set by inverter.
PHEV: charge power tracks PV surplus, clamped to min/max charge limits.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..models import Config, ForecastPoint, Plan, PricePoint, Snapshot, TimeSlot

logger = logging.getLogger(__name__)

# Daytime boundaries in local time (approximate, good enough for Central Europe)
SUNRISE_HOUR = 6
SUNSET_HOUR = 20


def plan_forecast(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
) -> Plan:
    """Create a 24h plan based on PV forecast intelligence.

    Priority for PV surplus:
    1. House load
    2. PHEV charging (surplus-tracking)
    3. House battery (mode only)
    4. Grid export
    """
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    slot_minutes = config.slot_duration_min
    num_slots = (24 * 60) // slot_minutes

    # Analyze PV forecast
    total_pv_wh = sum(fp.power_w * ((fp.end - fp.start).total_seconds() / 3600.0)
                      for fp in pv_forecast)
    total_load_wh = config.estimated_daily_load_kwh * 1000.0
    pv_coverage = total_pv_wh / total_load_wh if total_load_wh > 0 else 0

    # SOC target at sunrise
    if pv_coverage > 0.8:
        target_soc_sunrise = config.min_soc_percent + 10
    elif pv_coverage > 0.4:
        target_soc_sunrise = config.min_soc_percent + 30
    else:
        target_soc_sunrise = int(config.max_soc_percent * 0.8)

    logger.info("Forecast: PV coverage %.0f%%, target SOC sunrise: %d%%",
                pv_coverage * 100, target_soc_sunrise)

    # Cheapest night price for grid-charge decision
    cheapest_night_price = float("inf")
    if prices:
        for pp in prices:
            h = pp.start.astimezone(tz).hour
            if h < SUNRISE_HOUR or h >= SUNSET_HOUR:
                cheapest_night_price = min(cheapest_night_price, pp.price_eur_kwh)

    # Build plan
    slots = []
    soc = snapshot.battery_soc

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = _get_forecast_for_time(pv_forecast, slot_start)
        price = _get_price_for_time(prices, slot_start)
        load_w = snapshot.load_power_w if i == 0 else config.load_per_slot_w
        # Use local hour for day/night classification
        hour = slot_start.astimezone(tz).hour

        surplus_w = pv_w - load_w
        battery_mode = "idle"
        battery_w_est = 0.0
        phev_w = 0.0

        if SUNRISE_HOUR <= hour < SUNSET_HOUR:
            # --- DAYTIME ---
            # PHEV gets surplus first
            if config.phev_enabled and snapshot.phev_connected and surplus_w > 0:
                if surplus_w >= config.phev_min_charge_w:
                    phev_w = min(surplus_w, config.phev_max_charge_w)

            remaining = surplus_w - phev_w

            if remaining > 0 and soc < config.max_soc_percent:
                battery_mode = "charge"
                headroom_wh = (config.max_soc_percent - soc) / 100.0 * config.battery_capacity_wh
                battery_w_est = min(remaining, headroom_wh / (slot_minutes / 60.0))
            elif surplus_w < 0 and soc > config.min_soc_percent:
                battery_mode = "discharge"
                available_wh = (soc - config.min_soc_percent) / 100.0 * config.battery_capacity_wh
                battery_w_est = -min(abs(surplus_w), available_wh / (slot_minutes / 60.0))

        elif hour >= SUNSET_HOUR or hour < 1:
            # --- EVENING: discharge ---
            if soc > config.min_soc_percent:
                deficit_w = max(0, load_w - pv_w)
                available_wh = (soc - config.min_soc_percent) / 100.0 * config.battery_capacity_wh
                battery_mode = "discharge"
                battery_w_est = -min(deficit_w, available_wh / (slot_minutes / 60.0))

        else:
            # --- NIGHT: potential grid charging ---
            if soc < target_soc_sunrise:
                should_charge = (
                    pv_coverage < 0.5
                    or price <= cheapest_night_price * 1.1
                )
                if should_charge:
                    battery_mode = "charge"
                    need_wh = (target_soc_sunrise - soc) / 100.0 * config.battery_capacity_wh
                    battery_w_est = need_wh / (slot_minutes / 60.0)

        # Update SOC
        energy_wh = battery_w_est * (slot_minutes / 60.0)
        soc += (energy_wh / config.battery_capacity_wh) * 100.0
        soc = max(config.min_soc_percent, min(config.max_soc_percent, soc))

        # Grid balance
        net = pv_w - load_w - phev_w - battery_w_est
        grid_w = -net

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

    logger.info("Forecast plan: PV %.0f%%, SOC %.1f%%→%.1f%%",
                pv_coverage * 100, snapshot.battery_soc,
                slots[-1].projected_soc if slots else soc)

    return Plan(created_at=now, strategy="forecast", slots=slots, tz=config.timezone)


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
