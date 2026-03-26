"""PV-forecast-based strategy: smart scheduling using solar prediction.

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


def plan_forecast(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
    sunrise_hour: int = 6,
    sunset_hour: int = 20,
) -> Plan:
    """Create a 24h plan based on PV forecast intelligence."""
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

    # Cap by grid-charge limit
    target_soc_sunrise = min(target_soc_sunrise, config.max_grid_charge_soc)

    logger.info("Forecast: PV coverage %.0f%%, sunrise=%d:00, sunset=%d:00, target SOC: %d%%",
                pv_coverage * 100, sunrise_hour, sunset_hour, target_soc_sunrise)

    # Cheapest night price
    cheapest_night_price = float("inf")
    if prices:
        for pp in prices:
            h = pp.start.astimezone(tz).hour
            if h < sunrise_hour or h >= sunset_hour:
                cheapest_night_price = min(cheapest_night_price, pp.price_eur_kwh)

    slots = []
    soc = snapshot.battery_soc

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = get_forecast_for_time(pv_forecast, slot_start)
        price = get_price_for_time(prices, slot_start)
        load_w = snapshot.load_power_w if i == 0 else config.load_per_slot_w
        hour = slot_start.astimezone(tz).hour

        surplus_w = pv_w - load_w
        battery_mode = "idle"
        battery_w_est = 0.0
        phev_w = 0.0

        if sunrise_hour <= hour < sunset_hour:
            # --- DAYTIME ---
            phev_w = calc_phev_power(surplus_w, config, snapshot)
            remaining = surplus_w - phev_w

            if remaining > 0 and soc < config.max_soc_percent:
                battery_mode = "charge"
                headroom_wh = (config.max_soc_percent - soc) / 100.0 * config.battery_capacity_wh
                battery_w_est = min(remaining, headroom_wh / (slot_minutes / 60.0))
            elif surplus_w < 0 and soc > config.min_soc_percent:
                battery_mode = "discharge"
                available_wh = (soc - config.min_soc_percent) / 100.0 * config.battery_capacity_wh
                battery_w_est = -min(abs(surplus_w), available_wh / (slot_minutes / 60.0))

        elif hour >= sunset_hour or hour < 1:
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

        # Grid-charge limit
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

    logger.info("Forecast plan: PV %.0f%%, SOC %.1f%%→%.1f%%",
                pv_coverage * 100, snapshot.battery_soc,
                slots[-1].projected_soc if slots else soc)

    return Plan(created_at=now, strategy="forecast", slots=slots, tz=config.timezone)
