"""EMHASS-based strategy: uses linear programming for optimal scheduling.

Calls the EMHASS optimization engine DIRECTLY as a Python library.
This eliminates the publish-data bug and gives immediate access to results.

EMHASS sign convention: p_batt positive = discharge, negative = charge
EnergieHA convention: positive = charge, negative = discharge
"""

import logging
import math
import pathlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..models import Config, ForecastPoint, Plan, PricePoint, Snapshot, TimeSlot
from .helpers import (calc_grid_balance, calc_phev_power, get_forecast_for_time,
                      get_price_for_time, is_grid_charging, update_soc)

logger = logging.getLogger(__name__)

_emhass_available = False
try:
    import numpy as np
    import pandas as pd
    from emhass.optimization import Optimization
    _emhass_available = True
    logger.info("EMHASS optimization engine loaded (direct Python integration)")
except ImportError as e:
    logger.warning("EMHASS not available as library: %s", e)


def _build_emhass_configs(config: Config, snapshot: Snapshot, num_points: int, tz):
    """Build the configuration dicts that EMHASS Optimization expects."""
    freq = pd.Timedelta(minutes=config.emhass_optimization_time_step)
    eff_single = math.sqrt(config.round_trip_efficiency)

    retrieve_hass_conf = {
        "optimization_time_step": freq,
        "time_zone": tz,
        "sensor_power_photovoltaics": "sensor.pv_power",
        "sensor_power_load_no_var_loads": "sensor.load_power",
        "method_ts_round": "nearest",
        "continual_publish": False,
    }

    optim_conf = {
        "set_use_battery": True,
        "num_def_loads": 0,
        "number_of_deferrable_loads": 0,
        "set_def_constant": [],
        "set_def_timewindow": [],
        "def_load_config": [],
        "treat_def_as_semi_cont": [],
        "set_nocharge_from_grid": False,
        "set_nodischarge_to_grid": True,
        "set_total_pv_sell": False,
        "set_soc_recovery": False,
        "operating_hours_of_each_deferrable_load": [],
        "operating_timesteps_of_each_deferrable_load": [],
        "start_timesteps_of_each_deferrable_load": [],
        "end_timesteps_of_each_deferrable_load": [],
        "weight_battery_discharge": 1.0,
        "weight_battery_charge": 1.0,
    }

    plant_conf = {
        "battery_nominal_energy_capacity": config.battery_capacity_kwh * 1000,
        "battery_minimum_state_of_charge": config.min_soc_percent / 100.0,
        "battery_maximum_state_of_charge": config.max_soc_percent / 100.0,
        "battery_target_state_of_charge": config.max_grid_charge_soc / 100.0,
        "battery_charge_power_max": config.emhass_battery_charge_power_max,
        "battery_discharge_power_max": config.emhass_battery_discharge_power_max,
        "battery_charge_efficiency": eff_single,
        "battery_discharge_efficiency": eff_single,
    }

    emhass_conf = {
        "data_path": pathlib.Path("/tmp/emhass_data"),
        "root_path": pathlib.Path("/app"),
    }

    # Ensure data path exists
    emhass_conf["data_path"].mkdir(parents=True, exist_ok=True)

    return retrieve_hass_conf, optim_conf, plant_conf, emhass_conf


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

    # Build time-aligned arrays
    pv_w_list = []
    load_w_list = []
    price_list = []
    for i in range(num_emhass_points):
        slot_start = now + timedelta(minutes=i * emhass_step)
        pv_w_list.append(get_forecast_for_time(pv_forecast, slot_start))
        load_w_list.append(snapshot.load_power_w if i == 0 else config.load_per_slot_w)
        price_list.append(get_price_for_time(prices, slot_start))

    # Build EMHASS config dicts
    retrieve_hass_conf, optim_conf, plant_conf, emhass_conf = \
        _build_emhass_configs(config, snapshot, num_emhass_points, tz)

    logger.info("EMHASS direct: %d intervals, SOC=%.1f%%, target=%.1f%%, step=%dmin",
                num_emhass_points, snapshot.battery_soc,
                config.max_grid_charge_soc, emhass_step)

    # Build pandas DataFrames
    freq = pd.Timedelta(minutes=emhass_step)
    time_index = pd.date_range(start=now, periods=num_emhass_points, freq=freq, tz=tz)

    p_pv = np.array(pv_w_list)
    p_load = np.array(load_w_list)
    unit_load_cost = np.array(price_list)
    unit_prod_price = np.full(num_emhass_points, config.export_price_eur)

    df_input = pd.DataFrame({
        "unit_load_cost": unit_load_cost,
        "unit_prod_price": unit_prod_price,
    }, index=time_index)

    try:
        opt = Optimization(
            retrieve_hass_conf=retrieve_hass_conf,
            optim_conf=optim_conf,
            plant_conf=plant_conf,
            var_load_cost="unit_load_cost",
            var_prod_price="unit_prod_price",
            costfun="profit",
            emhass_conf=emhass_conf,
            logger=logger,
            opt_time_delta=24,
        )

        opt_res = opt.perform_optimization(
            df_input, p_pv, p_load, unit_load_cost, unit_prod_price,
            soc_init=snapshot.battery_soc / 100.0,
            soc_final=config.max_grid_charge_soc / 100.0,
        )

        if opt_res is None or (hasattr(opt_res, 'empty') and opt_res.empty):
            raise ValueError("EMHASS optimization returned empty result")

        logger.info("EMHASS optimization complete: %d rows, columns: %s",
                    len(opt_res), list(opt_res.columns))

    except Exception as e:
        logger.warning("EMHASS direct failed: %s", e, exc_info=True)
        raise

    # Extract results
    batt_forecast = opt_res["P_batt"].tolist() if "P_batt" in opt_res.columns else []
    soc_forecast = (opt_res["SOC_opt"] * 100).tolist() if "SOC_opt" in opt_res.columns else []

    if not batt_forecast:
        raise ValueError("EMHASS result missing P_batt column")

    # Rebase SOC
    if soc_forecast and abs(soc_forecast[0] - snapshot.battery_soc) > 1.0:
        offset = snapshot.battery_soc - soc_forecast[0]
        soc_forecast = [max(config.min_soc_percent,
                           min(config.max_soc_percent, s + offset))
                       for s in soc_forecast]

    # Map to EnergieHA slots
    slots_per_emhass = max(1, emhass_step // slot_minutes)
    slots = []
    soc = snapshot.battery_soc

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = get_forecast_for_time(pv_forecast, slot_start)
        load_w = snapshot.load_power_w if i == 0 else config.load_per_slot_w
        price = get_price_for_time(prices, slot_start)

        emhass_idx = i // slots_per_emhass
        raw_batt = batt_forecast[emhass_idx] if emhass_idx < len(batt_forecast) else 0.0
        battery_w = -raw_batt  # Sign inversion

        if battery_w > 50:
            battery_mode = "charge"
        elif battery_w < -50:
            battery_mode = "discharge"
        else:
            battery_mode = "idle"
            battery_w = 0.0

        if is_grid_charging(pv_w, load_w, battery_w) and soc >= config.max_grid_charge_soc:
            battery_mode = "idle"
            battery_w = 0.0

        phev_w = calc_phev_power(pv_w - load_w - max(0, battery_w), config, snapshot)

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
        HaClient().set_state("sensor.energieha_emhass_diag", "ok", {
            "friendly_name": "EnergieHA EMHASS Diagnostics",
            "icon": "mdi:bug",
            "mode": "direct_python",
            "result_columns": list(opt_res.columns),
            "batt_points": len(batt_forecast),
            "soc_points": len(soc_forecast),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    return Plan(created_at=now, strategy="emhass", slots=slots, tz=config.timezone)
