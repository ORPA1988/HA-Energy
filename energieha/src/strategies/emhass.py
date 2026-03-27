"""EMHASS-based strategy: uses linear programming for optimal scheduling.

Calls the EMHASS REST API for day-ahead optimization, then reads the
result sensors (p_batt_forecast, soc_batt_forecast) and converts them
to TimeSlot objects. PHEV is handled separately (EMHASS doesn't know about it).
"""

import logging
from datetime import datetime, timedelta, timezone
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

    # Call EMHASS optimization — use actual SOC (may be below min_soc)
    soc_init = snapshot.battery_soc
    soc_final = config.max_grid_charge_soc  # Target: grid-charge limit

    client.dayahead_optim(
        pv_forecast_w=pv_w_list,
        load_forecast_w=load_w_list,
        prices_eur=price_list,
        soc_init=soc_init,
        soc_final=soc_final,
        export_price_eur=config.export_price_eur,
    )

    # Read EMHASS result sensors from HA (reuse same client credentials)
    from ..ha_client import HaClient
    ha = HaClient()

    batt_forecast = _read_forecast_sensor(ha, "sensor.p_batt_forecast", num_slots)
    soc_forecast = _read_forecast_sensor(ha, "sensor.soc_batt_forecast", num_slots)

    # Check data freshness and availability
    EMHASS_MAX_AGE_SECONDS = 6 * 3600  # 6h = 1.5× trigger interval

    batt_state = ha.get_state("sensor.p_batt_forecast")
    if batt_state:
        last_updated = batt_state.get("last_updated", "")
        logger.info("EMHASS results: %d batt points, %d soc points (updated: %s)",
                    len(batt_forecast), len(soc_forecast), last_updated[:19])
        # Freshness check
        if last_updated:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_updated)).total_seconds()
                if age > EMHASS_MAX_AGE_SECONDS:
                    raise ValueError(f"EMHASS data stale ({age/3600:.1f}h old, max {EMHASS_MAX_AGE_SECONDS/3600:.0f}h)")
            except (ValueError, TypeError):
                pass  # Can't parse timestamp — skip freshness check
    else:
        logger.info("EMHASS results: %d batt points, %d soc points",
                    len(batt_forecast), len(soc_forecast))

    if not batt_forecast:
        raise ValueError("EMHASS returned no battery forecast data")

    # Rebase EMHASS SOC forecast onto actual SOC
    # EMHASS calculates SOC from the optimization start, which may differ
    # from current reality (e.g. battery discharged further since last optim)
    if soc_forecast and soc_forecast[0] > 0:
        soc_offset = snapshot.battery_soc - soc_forecast[0]
        if abs(soc_offset) > 1.0:  # Only rebase if >1% difference
            logger.info("EMHASS SOC rebase: actual=%.1f%% forecast=%.1f%% offset=%.1f%%",
                        snapshot.battery_soc, soc_forecast[0], soc_offset)
            soc_forecast = [max(config.min_soc_percent,
                               min(config.max_soc_percent, s + soc_offset))
                           for s in soc_forecast]

    # EMHASS uses hourly resolution — map to our slot resolution
    # Each EMHASS hour covers (60/slot_minutes) slots
    slots_per_emhass = max(1, 60 // slot_minutes)

    # Build plan from EMHASS output
    slots = []
    soc = snapshot.battery_soc

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = pv_w_list[i]
        load_w = load_w_list[i]
        price = price_list[i]

        # Map slot index to EMHASS hourly index
        # EMHASS convention: positive=discharge, negative=charge
        # EnergieHA convention: positive=charge, negative=discharge
        # → invert sign
        emhass_idx = i // slots_per_emhass
        raw_batt = batt_forecast[emhass_idx] if emhass_idx < len(batt_forecast) else 0.0
        battery_w = -raw_batt  # Invert: EMHASS positive=discharge → our negative

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
        soc_idx = i // slots_per_emhass
        if soc_idx < len(soc_forecast) and soc_forecast[soc_idx] > 0:
            soc = soc_forecast[soc_idx]
        else:
            soc = update_soc(soc, battery_w, slot_minutes, config)

        grid_w = calc_grid_balance(pv_w, load_w, phev_w, battery_w)

        # Clip night export: no point exporting when PV=0
        # WR handles this via "Zero Export to CT" anyway, but plan looks cleaner
        if pv_w < 10 and grid_w < -10 and battery_mode == "discharge":
            battery_w = -(load_w + phev_w)  # Match discharge to load exactly
            grid_w = 0.0

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
    """Read a forecast sensor published by EMHASS. Returns list of float values.

    EMHASS publishes data in various attribute formats:
    - battery_scheduled_power: [{"date": ..., "p_batt_forecast": "-360.23"}, ...]
    - battery_scheduled_soc:   [{"date": ..., "soc_batt_forecast": "59.92"}, ...]
    - forecast_data / forecasts / data: generic formats
    """
    data = ha.get_state(entity_id)
    if not data:
        logger.warning("EMHASS sensor %s not found", entity_id)
        return []

    attrs = data.get("attributes", {})

    # Try all known EMHASS attribute names
    forecast_data = None
    for attr_name in ("battery_scheduled_power", "battery_scheduled_soc",
                      "forecast_data", "forecasts", "data"):
        if attr_name in attrs and isinstance(attrs[attr_name], list):
            forecast_data = attrs[attr_name]
            logger.debug("EMHASS %s: using attribute '%s' with %d entries",
                         entity_id, attr_name, len(forecast_data))
            break

    if forecast_data:
        values = []
        for item in forecast_data:
            if isinstance(item, (int, float)):
                values.append(float(item))
            elif isinstance(item, dict):
                # EMHASS uses the sensor name as value key (e.g. "p_batt_forecast")
                # Try all numeric-looking values in the dict
                val = None
                for k, v in item.items():
                    if k == "date":
                        continue
                    try:
                        val = float(v)
                        break
                    except (ValueError, TypeError):
                        continue
                if val is not None:
                    values.append(val)
        if values:
            return values

    # Fallback: single state value repeated
    try:
        val = float(data.get("state", 0))
        logger.debug("EMHASS %s: using state value %.1f as fallback", entity_id, val)
        return [val] * expected_len
    except (ValueError, TypeError):
        return []
