"""EMHASS-based strategy: uses linear programming for optimal scheduling.

Calls the EMHASS optimization engine DIRECTLY as a Python library
(not via REST API). This eliminates the publish-data bug and gives
immediate access to optimization results.

EMHASS sign convention: p_batt positive = discharge, negative = charge
EnergieHA convention: positive = charge, negative = discharge
→ Sign is inverted when reading EMHASS results.
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..models import Config, ForecastPoint, Plan, PricePoint, Snapshot, TimeSlot
from .helpers import (calc_grid_balance, calc_phev_power, get_forecast_for_time,
                      get_price_for_time, is_grid_charging, update_soc)

logger = logging.getLogger(__name__)

# Try to import EMHASS optimization engine
_emhass_available = False
try:
    import numpy as np
    import pandas as pd
    from emhass.optimization import Optimization
    from emhass.forecast import Forecast
    _emhass_available = True
    logger.info("EMHASS optimization engine loaded (direct Python integration)")
except ImportError as e:
    logger.warning("EMHASS not available as library: %s", e)


def plan_emhass(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
) -> Plan:
    """Create a plan using EMHASS linear programming optimization."""
    if not _emhass_available:
        raise ImportError("EMHASS optimization engine not installed")

    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    slot_minutes = config.slot_duration_min
    emhass_step = config.emhass_optimization_time_step
    num_emhass_points = (24 * 60) // emhass_step
    num_slots = (24 * 60) // slot_minutes

    # Build time-aligned arrays at EMHASS resolution
    pv_w_list = []
    load_w_list = []
    price_list = []

    for i in range(num_emhass_points):
        slot_start = now + timedelta(minutes=i * emhass_step)
        pv_w_list.append(get_forecast_for_time(pv_forecast, slot_start))
        load_w_list.append(snapshot.load_power_w if i == 0 else config.load_per_slot_w)
        price_list.append(get_price_for_time(prices, slot_start))

    # Build pandas DataFrames for EMHASS
    freq = pd.Timedelta(minutes=emhass_step)
    time_index = pd.date_range(start=now, periods=num_emhass_points, freq=freq, tz=tz)

    P_PV = pd.Series(pv_w_list, index=time_index, name="P_PV")
    P_Load = pd.Series(load_w_list, index=time_index, name="P_Load")
    cost_forecast = pd.Series(price_list, index=time_index, name="cost")
    prod_price = pd.Series([config.export_price_eur] * num_emhass_points,
                           index=time_index, name="prod_price")

    # Battery parameters
    eff_single = math.sqrt(config.round_trip_efficiency)

    optim_conf = {
        "set_use_battery": True,
        "battery_nominal_energy_capacity": config.battery_capacity_kwh * 1000,
        "battery_minimum_state_of_charge": config.min_soc_percent / 100.0,
        "battery_maximum_state_of_charge": config.max_soc_percent / 100.0,
        "battery_target_state_of_charge": config.max_grid_charge_soc / 100.0,
        "battery_charge_power_max": config.emhass_battery_charge_power_max,
        "battery_discharge_power_max": config.emhass_battery_discharge_power_max,
        "battery_charge_efficiency": eff_single,
        "battery_discharge_efficiency": eff_single,
        "num_def_loads": 0,
        "set_nocharge_from_grid": False,
        "set_nodischarge_to_grid": True,
        "set_total_pv_sell": False,
    }

    logger.info("EMHASS direct: %d intervals, SOC=%.1f%%, target=%.1f%%, step=%dmin",
                num_emhass_points, snapshot.battery_soc,
                config.max_grid_charge_soc, emhass_step)

    # Run EMHASS optimization directly
    try:
        opt = Optimization(
            soc_init=snapshot.battery_soc / 100.0,
            soc_final=config.max_grid_charge_soc / 100.0,
            num_def_loads=0,
            P_PV_forecast=P_PV.values,
            P_load_forecast=P_Load.values,
            unit_load_cost=cost_forecast.values,
            unit_prod_price=prod_price.values,
            optim_conf=optim_conf,
            plant_conf={},
            logger=logger,
        )

        opt_res = opt.perform_dayahead_forecast_optim(P_PV, P_Load)

        if opt_res is None or opt_res.empty:
            raise ValueError("EMHASS optimization returned empty result")

        logger.info("EMHASS optimization complete: %d rows, columns: %s",
                    len(opt_res), list(opt_res.columns))

    except Exception as e:
        # If direct optimization fails, try the REST API fallback
        logger.warning("EMHASS direct optimization failed: %s. Trying REST API...", e)
        return _plan_emhass_rest_fallback(snapshot, prices, pv_forecast, config,
                                          pv_w_list, load_w_list, price_list)

    # Extract battery power and SOC from results
    batt_forecast = []
    soc_forecast = []

    if "P_batt" in opt_res.columns:
        batt_forecast = opt_res["P_batt"].tolist()
    if "SOC_opt" in opt_res.columns:
        soc_forecast = (opt_res["SOC_opt"] * 100).tolist()  # decimal → %

    if not batt_forecast:
        raise ValueError("EMHASS result missing P_batt column")

    logger.info("EMHASS results: %d batt points, %d soc points",
                len(batt_forecast), len(soc_forecast))

    # Rebase SOC onto actual
    if soc_forecast and abs(soc_forecast[0] - snapshot.battery_soc) > 1.0:
        offset = snapshot.battery_soc - soc_forecast[0]
        soc_forecast = [max(config.min_soc_percent,
                           min(config.max_soc_percent, s + offset))
                       for s in soc_forecast]

    # Map to EnergieHA slot resolution
    slots_per_emhass = max(1, emhass_step // slot_minutes)
    slots = []
    soc = snapshot.battery_soc

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = get_forecast_for_time(pv_forecast, slot_start)
        load_w = snapshot.load_power_w if i == 0 else config.load_per_slot_w
        price = get_price_for_time(prices, slot_start)

        # EMHASS sign inversion: positive=discharge → our negative
        emhass_idx = i // slots_per_emhass
        raw_batt = batt_forecast[emhass_idx] if emhass_idx < len(batt_forecast) else 0.0
        battery_w = -raw_batt

        if battery_w > 50:
            battery_mode = "charge"
        elif battery_w < -50:
            battery_mode = "discharge"
        else:
            battery_mode = "idle"
            battery_w = 0.0

        # Grid-charge limit
        if is_grid_charging(pv_w, load_w, battery_w) and soc >= config.max_grid_charge_soc:
            battery_mode = "idle"
            battery_w = 0.0

        surplus_w = pv_w - load_w
        phev_w = calc_phev_power(surplus_w - max(0, battery_w), config, snapshot)

        soc_idx = i // slots_per_emhass
        if soc_idx < len(soc_forecast) and soc_forecast[soc_idx] > 0:
            soc = soc_forecast[soc_idx]
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

    # Publish diagnostics
    try:
        from ..ha_client import HaClient
        ha = HaClient()
        ha.set_state("sensor.energieha_emhass_diag", "ok", {
            "friendly_name": "EnergieHA EMHASS Diagnostics",
            "icon": "mdi:bug",
            "mode": "direct_python",
            "result_columns": list(opt_res.columns) if opt_res is not None else [],
            "batt_points": len(batt_forecast),
            "soc_points": len(soc_forecast),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    return Plan(created_at=now, strategy="emhass", slots=slots, tz=config.timezone)


def _plan_emhass_rest_fallback(snapshot, prices, pv_forecast, config,
                                pv_w_list, load_w_list, price_list):
    """Fallback: Call EMHASS via REST API if direct Python fails."""
    from ..emhass_client import EmhassClient

    client = EmhassClient(config.emhass_url)
    if not client.is_available():
        raise ConnectionError(f"EMHASS not reachable at {config.emhass_url}")

    eff_single = math.sqrt(config.round_trip_efficiency)
    battery_params = {
        "set_use_battery": True,
        "battery_nominal_energy_capacity": config.battery_capacity_kwh * 1000,
        "battery_minimum_state_of_charge": config.min_soc_percent / 100.0,
        "battery_maximum_state_of_charge": config.max_soc_percent / 100.0,
        "battery_target_state_of_charge": config.max_grid_charge_soc / 100.0,
        "battery_charge_power_max": config.emhass_battery_charge_power_max,
        "battery_discharge_power_max": config.emhass_battery_discharge_power_max,
        "battery_charge_efficiency": eff_single,
        "battery_discharge_efficiency": eff_single,
    }

    client.dayahead_optim(
        pv_forecast_w=pv_w_list,
        load_forecast_w=load_w_list,
        prices_eur=price_list,
        export_price_eur=config.export_price_eur,
        battery_params=battery_params,
        optimization_time_step=config.emhass_optimization_time_step,
    )

    raise ValueError("EMHASS REST API returned text only - use Price strategy instead")
