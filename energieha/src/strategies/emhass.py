"""EMHASS-based strategy: uses linear programming for optimal scheduling.

Calls the EMHASS REST API for day-ahead optimization, then reads the
result sensors (p_batt_forecast, soc_batt_forecast) and converts them
to TimeSlot objects. PHEV is handled separately (EMHASS doesn't know about it).

EMHASS sign convention: p_batt positive = discharge, negative = charge
EnergieHA convention: positive = charge, negative = discharge
→ Sign is inverted when reading EMHASS results.

EMHASS SOC convention: decimal 0.0-1.0 (not percentage 0-100)
→ Converted at API boundary; internal plan uses percentage 0-100.
"""

import logging
import math
import time
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

    # EMHASS time step (may differ from EnergieHA slot duration)
    emhass_step = config.emhass_optimization_time_step
    num_emhass_points = (24 * 60) // emhass_step

    # Build time-aligned arrays at EMHASS resolution
    pv_w_list = []
    load_w_list = []
    price_list = []

    for i in range(num_emhass_points):
        slot_start = now + timedelta(minutes=i * emhass_step)
        pv_w_list.append(get_forecast_for_time(pv_forecast, slot_start))
        load_w_list.append(snapshot.load_power_w if i == 0 else config.load_per_slot_w)
        price_list.append(get_price_for_time(prices, slot_start))

    # Validate inputs
    client = EmhassClient(config.emhass_url)

    # Convert SOC to decimal (EMHASS uses 0.0-1.0, not 0-100%)
    target_soc = config.max_grid_charge_soc / 100.0

    errors = client.validate_inputs(pv_w_list, load_w_list, price_list, target_soc)
    if errors:
        for e in errors:
            logger.error("EMHASS validation: %s", e)
        raise ValueError(f"EMHASS input validation failed: {errors[0]}")

    # Check EMHASS availability
    if not client.is_available():
        raise ConnectionError(f"EMHASS not reachable at {config.emhass_url}")

    # Build battery parameters for EMHASS (all SOC values as decimal 0-1)
    # Efficiency: split round_trip_efficiency symmetrically into charge/discharge
    eff_single = math.sqrt(config.round_trip_efficiency)
    battery_params = {
        "set_use_battery": True,
        "battery_nominal_energy_capacity": config.battery_capacity_kwh * 1000,  # Wh
        "battery_minimum_state_of_charge": config.min_soc_percent / 100.0,
        "battery_maximum_state_of_charge": config.max_soc_percent / 100.0,
        "battery_target_state_of_charge": target_soc,
        "battery_charge_power_max": config.emhass_battery_charge_power_max,
        "battery_discharge_power_max": config.emhass_battery_discharge_power_max,
        "battery_charge_efficiency": eff_single,
        "battery_discharge_efficiency": eff_single,
    }

    result = client.dayahead_optim(
        pv_forecast_w=pv_w_list,
        load_forecast_w=load_w_list,
        prices_eur=price_list,
        export_price_eur=config.export_price_eur,
        battery_params=battery_params,
        optimization_time_step=emhass_step,
    )

    # Publish EMHASS debug info to a diagnostic sensor
    from ..ha_client import HaClient
    ha = HaClient()

    # EMHASS v0.17.1 returns plain text from dayahead-optim, not JSON results.
    # The actual results must be retrieved separately.

    # Step 1: Try to get cached results from EMHASS API endpoints
    cached = client.get_optimization_results()
    if cached:
        logger.info("EMHASS: got cached results with keys: %s", list(cached.keys())[:10])
        result = cached  # Use cached results instead of the text response

    # Publish diagnostics (now with the best result we have)
    _publish_emhass_diag(ha, result, client)

    # Step 2: Try to extract P_batt/SOC from the result
    batt_forecast = []
    soc_forecast = []

    # Try many possible key names
    batt_keys = ("P_batt", "p_batt_forecast", "P_batt_forecast", "opt_p_batt",
                 "battery_power", "P_battery", "p_batt")
    soc_keys = ("SOC_opt", "soc_batt_forecast", "SOC_batt", "opt_soc",
                "battery_soc", "SOC_battery", "soc_batt")

    for key in batt_keys:
        batt_forecast = _extract_from_result(result, key, num_emhass_points)
        if batt_forecast:
            logger.info("EMHASS: found batt data under key '%s' (%d points)", key, len(batt_forecast))
            break

    for key in soc_keys:
        soc_forecast = _extract_from_result(result, key, num_emhass_points)
        if soc_forecast:
            logger.info("EMHASS: found SOC data under key '%s' (%d points)", key, len(soc_forecast))
            break

    if batt_forecast:
        logger.info("EMHASS: using API data (%d batt, %d soc points)",
                    len(batt_forecast), len(soc_forecast))
    else:
        # EMHASS v0.17.1 returns plain text from API - results stored internally.
        # Read from HA sensors (populated by publish-data via HA automation).
        logger.info("EMHASS: API returned text only, reading HA sensors...")

        # Trigger HA automation to run publish-data (fire-and-forget)
        try:
            ha.call_service("automation", "trigger",
                           {"entity_id": "automation.energieha_emhass_optimierung_triggern"})
            logger.info("EMHASS: triggered HA automation for optimization")
        except Exception as e:
            logger.warning("EMHASS: failed to trigger HA automation: %s", e)

        time.sleep(8)  # Wait for optimization + publish-data

        batt_forecast = _read_forecast_sensor(ha, "sensor.p_batt_forecast", num_emhass_points)
        soc_forecast = _read_forecast_sensor(ha, "sensor.soc_batt_forecast", num_emhass_points)

        # Check freshness - use generous limit since EMHASS publishes every 4h
        # and publish-data may be broken (known EMHASS v0.17.1 issue)
        EMHASS_MAX_AGE_SECONDS = 48 * 3600  # 48h - use old data rather than no data
        EMHASS_WARN_AGE_SECONDS = 6 * 3600  # Warn after 6h

        batt_state = ha.get_state("sensor.p_batt_forecast")
        if batt_state:
            last_updated = batt_state.get("last_updated", "")
            logger.info("EMHASS sensors: %d batt, %d soc points (updated: %s)",
                        len(batt_forecast), len(soc_forecast), last_updated[:19])
            if last_updated:
                try:
                    updated_dt = datetime.fromisoformat(last_updated)
                    age = (datetime.now(timezone.utc) - updated_dt).total_seconds()
                except (ValueError, TypeError):
                    age = None
                if age is not None:
                    if age > EMHASS_MAX_AGE_SECONDS:
                        raise ValueError(f"EMHASS data too old ({age/3600:.1f}h, max {EMHASS_MAX_AGE_SECONDS/3600:.0f}h)")
                    elif age > EMHASS_WARN_AGE_SECONDS:
                        logger.warning("EMHASS data stale (%.1fh old) - using anyway, "
                                      "check EMHASS publish-data", age / 3600)

    if not batt_forecast:
        raise ValueError("EMHASS returned no battery forecast data")

    # Validate result length
    if len(batt_forecast) != num_emhass_points:
        logger.warning("EMHASS returned %d batt points, expected %d. "
                       "Check EMHASS optimization_time_step setting (expected %d min).",
                       len(batt_forecast), num_emhass_points, emhass_step)

    # Warn if all battery values are zero (EMHASS may not have battery enabled)
    if all(abs(v) < 1.0 for v in batt_forecast):
        logger.warning("EMHASS battery forecast is all zeros — "
                       "check that set_use_battery is True in EMHASS config")

    # Normalize EMHASS SOC forecast: EMHASS returns decimal (0-1), we use percentage (0-100)
    if soc_forecast:
        if max(soc_forecast) <= 1.5:  # Clearly decimal format
            soc_forecast = [s * 100.0 for s in soc_forecast]
            logger.debug("EMHASS SOC forecast: converted from decimal to percentage")

    # Rebase EMHASS SOC forecast onto actual SOC
    if soc_forecast and soc_forecast[0] > 0:
        soc_offset = snapshot.battery_soc - soc_forecast[0]
        if abs(soc_offset) > 1.0:  # Only rebase if >1% difference
            logger.info("EMHASS SOC rebase: actual=%.1f%% forecast=%.1f%% offset=%.1f%%",
                        snapshot.battery_soc, soc_forecast[0], soc_offset)
            soc_forecast = [max(config.min_soc_percent,
                               min(config.max_soc_percent, s + soc_offset))
                           for s in soc_forecast]

    # Map EMHASS results to EnergieHA slot resolution
    # Each EMHASS interval covers (emhass_step / slot_minutes) EnergieHA slots
    slots_per_emhass = max(1, emhass_step // slot_minutes)

    # Build plan from EMHASS output
    slots = []
    soc = snapshot.battery_soc

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        # Get PV/load/price at slot resolution (not EMHASS resolution)
        pv_w = get_forecast_for_time(pv_forecast, slot_start)
        load_w = snapshot.load_power_w if i == 0 else config.load_per_slot_w
        price = get_price_for_time(prices, slot_start)

        # Map slot index to EMHASS index
        # EMHASS convention: positive = discharge, negative = charge
        # EnergieHA convention: positive = charge, negative = discharge
        # → Invert sign
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


def _publish_emhass_diag(ha, result, client) -> None:
    """Publish EMHASS API response diagnostics as a HA sensor for debugging."""
    import json
    try:
        if isinstance(result, dict):
            keys = list(result.keys())
            # Sample first values for each key
            sample = {}
            for k, v in result.items():
                if isinstance(v, list) and len(v) > 0:
                    sample[k] = f"list[{len(v)}]: {v[:3]}..."
                elif isinstance(v, dict) and len(v) > 0:
                    sample[k] = f"dict[{len(v)}]: {list(v.keys())[:3]}..."
                else:
                    sample[k] = str(v)[:100]
            # Also check cached results endpoint
            cached_info = "N/A"
            try:
                cached = client.get_optimization_results()
                if cached:
                    cached_info = f"dict[{len(cached)}]: {list(cached.keys())[:10]}"
                else:
                    cached_info = "None (no cached results)"
            except Exception:
                cached_info = "Error"

            diag_attrs = {
                "friendly_name": "EnergieHA EMHASS Diagnostics",
                "icon": "mdi:bug",
                "result_type": type(result).__name__,
                "result_keys": keys,
                "result_sample": sample,
                "cached_results": cached_info,
                "emhass_url": client.url,
                "emhass_available": client.is_available(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        else:
            diag_attrs = {
                "friendly_name": "EnergieHA EMHASS Diagnostics",
                "icon": "mdi:bug",
                "result_type": type(result).__name__,
                "result_raw": str(result)[:500],
                "emhass_url": client.url,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ha.set_state("sensor.energieha_emhass_diag",
                      "ok" if isinstance(result, dict) else "error",
                      diag_attrs)
    except Exception as e:
        logger.warning("Failed to publish EMHASS diag: %s", e)


def _extract_from_result(result: dict, key: str, expected_len: int) -> list[float]:
    """Extract forecast values directly from EMHASS API response.

    EMHASS may return results in various formats:
    - {"P_batt": [val1, val2, ...]} - direct list
    - {"P_batt": {"2026-03-28 00:00": val1, ...}} - timestamped dict
    - {"result": {"P_batt": [...]}} - nested under result key
    """
    if not isinstance(result, dict):
        return []

    # Try direct key
    data = result.get(key)

    # Try nested under 'result'
    if data is None and "result" in result:
        nested = result["result"]
        if isinstance(nested, dict):
            data = nested.get(key)

    # Try case-insensitive
    if data is None:
        for k, v in result.items():
            if k.lower() == key.lower():
                data = v
                break

    if data is None:
        return []

    # Parse the data
    if isinstance(data, list):
        try:
            return [float(v) for v in data]
        except (ValueError, TypeError):
            return []
    elif isinstance(data, dict):
        # Timestamped dict: {"2026-03-28 00:00": 123.4, ...}
        try:
            return [float(v) for v in data.values()]
        except (ValueError, TypeError):
            return []

    return []


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
