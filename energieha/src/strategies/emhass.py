"""EMHASS-based strategy: uses linear programming for optimal scheduling.

Calls the EMHASS REST API for day-ahead optimization, then reads the
result sensors (p_batt_forecast, soc_batt_forecast) and converts them
to TimeSlot objects. PHEV is handled separately (EMHASS doesn't know about it).
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..emhass_client import EmhassClient
from ..models import Config, ForecastPoint, Plan, PricePoint, Snapshot, TimeSlot
from .helpers import (calc_grid_balance, calc_phev_power, get_forecast_for_time,
                      get_price_for_time, is_grid_charging, update_soc)

logger = logging.getLogger(__name__)


def plan_emhass(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
) -> Plan:
    """Create a 24h plan using EMHASS linear programming optimization."""
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    slot_minutes = config.slot_duration_min
    num_slots = (24 * 60) // slot_minutes

    # Build time-aligned arrays for EMHASS
    pv_w_list = []
    load_w_list = []
    price_list = []

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w_list.append(get_forecast_for_time(pv_forecast, slot_start))
        load_w_list.append(snapshot.load_power_w if i == 0 else config.load_per_slot_w)
        price_list.append(get_price_for_time(prices, slot_start))

    # Validate inputs
    client = EmhassClient(config.emhass_url)

    errors = client.validate_inputs(
        pv_w_list, load_w_list, price_list,
        snapshot.battery_soc, config.min_soc_percent, config.max_soc_percent)
    if errors:
        for e in errors:
            logger.error("EMHASS validation: %s", e)
        raise ValueError(f"EMHASS input validation failed: {errors[0]}")

    # Check EMHASS availability
    if not client.is_available():
        raise ConnectionError(f"EMHASS not reachable at {config.emhass_url}")

    # Call EMHASS optimization
    soc_init = max(snapshot.battery_soc, config.min_soc_percent)
    soc_final = config.max_grid_charge_soc  # Target: grid-charge limit

    client.dayahead_optim(
        pv_forecast_w=pv_w_list,
        load_forecast_w=load_w_list,
        prices_eur=price_list,
        soc_init=soc_init,
        soc_final=soc_final,
    )

    # Read EMHASS result sensors from HA
    from ..ha_client import HaClient
    import os
    ha = HaClient()

    batt_forecast = _read_forecast_sensor(ha, "sensor.p_batt_forecast", num_slots)
    soc_forecast = _read_forecast_sensor(ha, "sensor.soc_batt_forecast", num_slots)

    # Build plan from EMHASS output
    slots = []
    soc = snapshot.battery_soc

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = pv_w_list[i]
        load_w = load_w_list[i]
        price = price_list[i]

        # Battery power from EMHASS (positive=charge, negative=discharge)
        battery_w = batt_forecast[i] if i < len(batt_forecast) else 0.0

        # Determine mode from power
        if battery_w > 50:
            battery_mode = "charge"
        elif battery_w < -50:
            battery_mode = "discharge"
        else:
            battery_mode = "idle"
            battery_w = 0.0

        # Grid-charge limit enforcement
        if is_grid_charging(pv_w, load_w, battery_w) and soc >= config.max_grid_charge_soc:
            battery_mode = "idle"
            battery_w = 0.0

        # PHEV from surplus (EMHASS doesn't handle this)
        surplus_w = pv_w - load_w
        phev_w = calc_phev_power(surplus_w - max(0, battery_w), config, snapshot)

        # SOC from EMHASS if available, else simulate
        if i < len(soc_forecast) and soc_forecast[i] > 0:
            soc = soc_forecast[i]
        else:
            soc = update_soc(soc, battery_w, slot_minutes, config)

        grid_w = calc_grid_balance(pv_w, load_w, phev_w, battery_w)

        slots.append(TimeSlot(
            start=slot_start, duration_min=slot_minutes,
            pv_forecast_w=pv_w, price_eur_kwh=price, load_estimate_w=load_w,
            planned_battery_mode=battery_mode, planned_battery_w=battery_w,
            planned_phev_w=phev_w, planned_grid_w=grid_w, projected_soc=soc,
        ))

    logger.info("EMHASS plan: %d slots, SOC %.1f%%→%.1f%%, %d charge/%d discharge",
                len(slots), snapshot.battery_soc,
                slots[-1].projected_soc if slots else soc,
                sum(1 for s in slots if s.planned_battery_mode == "charge"),
                sum(1 for s in slots if s.planned_battery_mode == "discharge"))

    return Plan(created_at=now, strategy="emhass", slots=slots, tz=config.timezone)


def _read_forecast_sensor(ha, entity_id: str, expected_len: int) -> list[float]:
    """Read a forecast sensor published by EMHASS. Returns list of float values."""
    data = ha.get_state(entity_id)
    if not data:
        logger.warning("EMHASS sensor %s not found", entity_id)
        return []

    # Try to read forecast array from attributes
    attrs = data.get("attributes", {})
    forecast_data = (attrs.get("forecast_data")
                     or attrs.get("forecasts")
                     or attrs.get("data")
                     or [])

    if isinstance(forecast_data, list) and forecast_data:
        values = []
        for item in forecast_data:
            if isinstance(item, (int, float)):
                values.append(float(item))
            elif isinstance(item, dict):
                val = item.get("value", item.get("state", 0))
                values.append(float(val))
        return values

    # Fallback: single state value repeated
    try:
        val = float(data.get("state", 0))
        return [val] * expected_len
    except (ValueError, TypeError):
        return []
