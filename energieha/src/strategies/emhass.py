"""EMHASS-based strategy: LP optimization directly as Python library.

Calls EMHASS Optimization engine in-process. No REST API, no publish-data.
Results come back as pandas DataFrame with P_batt, SOC_opt, P_grid etc.

EMHASS sign: P_batt positive = discharge, negative = charge
EnergieHA:   positive = charge, negative = discharge → sign inverted
"""

import logging
import math
import pathlib
import time as _time
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
    logger.info("EMHASS optimization engine loaded (direct Python)")
except ImportError as e:
    logger.warning("EMHASS not available: %s", e)


def plan_emhass(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
) -> Plan:
    """Create a today+tomorrow plan using EMHASS LP optimization."""
    if not _emhass_available:
        raise ImportError("EMHASS not installed")

    t0 = _time.monotonic()
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    slot_minutes = config.slot_duration_min
    emhass_step = config.emhass_optimization_time_step

    # --- 48h horizon (today + tomorrow) ---
    tomorrow_end = (now + timedelta(days=1)).replace(hour=23, minute=59)
    hours_until_end = (tomorrow_end - now).total_seconds() / 3600.0
    num_emhass_points = min(int(hours_until_end * 60 / emhass_step) + 1,
                            48 * 60 // emhass_step)
    num_slots = min(int(hours_until_end * 60 / slot_minutes) + 1,
                    48 * 60 // slot_minutes)

    # --- Build input arrays with REAL data ---
    pv_w_list = []
    load_w_list = []
    price_list = []
    for i in range(num_emhass_points):
        slot_start = now + timedelta(minutes=i * emhass_step)
        pv_w_list.append(get_forecast_for_time(pv_forecast, slot_start))

        # Load: current for slot 0, day/night profile for rest
        if i == 0:
            load_w_list.append(snapshot.load_power_w)
        else:
            h = slot_start.hour
            if 6 <= h < 22:
                load_w_list.append(config.load_per_slot_w * 1.15)  # Tag +15%
            else:
                load_w_list.append(config.load_per_slot_w * 0.7)   # Nacht -30%

        price_list.append(get_price_for_time(prices, slot_start))

    # --- Build EMHASS config dicts ---
    retrieve_hass_conf, optim_conf, plant_conf, emhass_conf = \
        _build_configs(config, snapshot, tz)

    # --- Log inputs ---
    charge_max = plant_conf["battery_charge_power_max"]
    pv_max = max(pv_w_list) if pv_w_list else 0
    price_min = min(price_list) if price_list else 0
    price_max = max(price_list) if price_list else 0
    logger.info("EMHASS: %d intervals (%.0fh), SOC=%.1f%%, costfun=%s, step=%dmin",
                num_emhass_points, hours_until_end, snapshot.battery_soc,
                config.emhass_costfun, emhass_step)
    logger.info("EMHASS inputs: PV max=%.0fW, Load=%.0fW, Prices %.1f-%.1fct, "
                "Batt charge=%.0fW, Grid max=%.0fW",
                pv_max, config.load_per_slot_w, price_min * 100, price_max * 100,
                charge_max, config.maximum_power_from_grid)

    # --- Build DataFrames ---
    freq = pd.Timedelta(minutes=emhass_step)
    idx = pd.date_range(start=now, periods=num_emhass_points, freq=freq, tz=tz)

    p_pv = np.array(pv_w_list)
    p_load = np.array(load_w_list)
    unit_load_cost = np.array(price_list)
    unit_prod_price = np.full(num_emhass_points, config.export_price_eur)

    df_input = pd.DataFrame({
        "unit_load_cost": unit_load_cost,
        "unit_prod_price": unit_prod_price,
    }, index=idx)

    # --- Run optimization ---
    try:
        opt = Optimization(
            retrieve_hass_conf=retrieve_hass_conf,
            optim_conf=optim_conf,
            plant_conf=plant_conf,
            var_load_cost="unit_load_cost",
            var_prod_price="unit_prod_price",
            costfun=config.emhass_costfun,
            emhass_conf=emhass_conf,
            logger=logger,
            opt_time_delta=int(hours_until_end),
        )

        opt_res = opt.perform_optimization(
            df_input, p_pv, p_load, unit_load_cost, unit_prod_price,
            soc_init=snapshot.battery_soc / 100.0,
            soc_final=config.max_grid_charge_soc / 100.0,
        )

        if opt_res is None or (hasattr(opt_res, 'empty') and opt_res.empty):
            raise ValueError("Empty optimization result")

        elapsed = _time.monotonic() - t0
        cost_val = opt_res.get("cost_fun_profit", opt_res.get("cost_profit", pd.Series([0]))).sum()
        logger.info("EMHASS OK: %d rows in %.1fs, cost=%.2f, columns=%s",
                    len(opt_res), elapsed, cost_val, list(opt_res.columns))

        # Log first rows
        for j in range(min(3, len(opt_res))):
            r = opt_res.iloc[j]
            logger.info("  [%s] PV=%.0f Load=%.0f Batt=%.0f Grid=%.0f SOC=%.1f%% Price=%.1fct",
                       idx[j].strftime("%H:%M"), r.get("P_PV", 0), r.get("P_Load", 0),
                       r.get("P_batt", 0), r.get("P_grid", 0),
                       r.get("SOC_opt", 0) * 100, r.get("unit_load_cost", 0) * 100)

    except Exception as e:
        logger.error("EMHASS optimization failed: %s", e, exc_info=True)
        raise

    # --- Extract results ---
    batt_forecast = opt_res["P_batt"].tolist() if "P_batt" in opt_res.columns else []
    soc_forecast = (opt_res["SOC_opt"] * 100).tolist() if "SOC_opt" in opt_res.columns else []

    if not batt_forecast:
        raise ValueError("Missing P_batt in result")

    # --- Write EMHASS sensors to HA ---
    _publish_emhass_sensors(batt_forecast, soc_forecast, pv_w_list, load_w_list,
                            idx, config, cost_val, elapsed)

    # --- Rebase SOC ---
    if soc_forecast and abs(soc_forecast[0] - snapshot.battery_soc) > 1.0:
        offset = snapshot.battery_soc - soc_forecast[0]
        soc_forecast = [max(config.min_soc_percent, min(config.max_soc_percent, s + offset))
                       for s in soc_forecast]

    # --- Map to EnergieHA slots ---
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

    charge_count = sum(1 for s in slots if s.planned_battery_mode == "charge")
    discharge_count = sum(1 for s in slots if s.planned_battery_mode == "discharge")
    logger.info("EMHASS plan: %d slots (%.0fh), SOC %.1f%%→%.1f%%, %d charge/%d discharge",
                len(slots), len(slots) * slot_minutes / 60,
                snapshot.battery_soc, slots[-1].projected_soc if slots else soc,
                charge_count, discharge_count)

    return Plan(created_at=now, strategy="emhass", slots=slots, tz=config.timezone)


def _build_configs(config: Config, snapshot: Snapshot, tz):
    """Build EMHASS config dicts from defaults + our overrides."""
    import json

    freq = pd.Timedelta(minutes=config.emhass_optimization_time_step)
    eff_single = math.sqrt(config.round_trip_efficiency)

    # Load EMHASS defaults
    defaults = {}
    try:
        import emhass
        p = pathlib.Path(emhass.__file__).parent / "data" / "config_defaults.json"
        with open(p) as f:
            defaults = json.load(f)
    except Exception:
        pass

    retrieve_hass_conf = {
        "optimization_time_step": freq,
        "time_zone": tz,
        "sensor_power_photovoltaics": config.entity_pv_power,
        "sensor_power_load_no_var_loads": config.entity_load_power,
        "method_ts_round": "nearest",
        "continual_publish": False,
        "delta_forecast_daily": defaults.get("delta_forecast_daily", 1),
    }

    optim_conf = {
        "set_use_battery": True,
        "number_of_deferrable_loads": 0,
        "nominal_power_of_deferrable_loads": [],
        "minimum_power_of_deferrable_loads": [],
        "operating_hours_of_each_deferrable_load": [],
        "operating_timesteps_of_each_deferrable_load": [],
        "start_timesteps_of_each_deferrable_load": [],
        "end_timesteps_of_each_deferrable_load": [],
        "treat_deferrable_load_as_semi_cont": [],
        "set_deferrable_load_single_constant": [],
        "set_deferrable_startup_penalty": [],
        "set_deferrable_max_startups": [],
        "def_current_state": [],
        "def_load_config": {},
        "set_nocharge_from_grid": config.emhass_nocharge_from_grid,
        "set_nodischarge_to_grid": config.emhass_nodischarge_to_grid,
        "set_total_pv_sell": False,
        "set_battery_dynamic": defaults.get("set_battery_dynamic", False),
        "weight_battery_discharge": config.emhass_weight_battery_discharge,
        "weight_battery_charge": config.emhass_weight_battery_charge,
        "lp_solver_timeout": defaults.get("lp_solver_timeout", 45),
        "lp_solver_mip_rel_gap": defaults.get("lp_solver_mip_rel_gap", 0),
        "num_threads": defaults.get("num_threads", 0),
        "delta_forecast_daily": defaults.get("delta_forecast_daily", 1),
    }

    charge_power = snapshot.grid_charge_power_w if snapshot.grid_charge_power_w > 0 \
        else config.emhass_battery_charge_power_max
    discharge_power = snapshot.grid_charge_power_w if snapshot.grid_charge_power_w > 0 \
        else config.emhass_battery_discharge_power_max

    plant_conf = {
        "battery_nominal_energy_capacity": config.battery_capacity_kwh * 1000,
        "battery_minimum_state_of_charge": config.min_soc_percent / 100.0,
        "battery_maximum_state_of_charge": config.max_soc_percent / 100.0,
        "battery_target_state_of_charge": config.max_grid_charge_soc / 100.0,
        "battery_charge_power_max": charge_power,
        "battery_discharge_power_max": discharge_power,
        "battery_charge_efficiency": eff_single,
        "battery_discharge_efficiency": eff_single,
        "inverter_is_hybrid": False,
        "compute_curtailment": False,
        "pv_inverter_model": "",
        "battery_dynamic_max": defaults.get("battery_dynamic_max", 0.9),
        "battery_dynamic_min": defaults.get("battery_dynamic_min", -0.9),
        "set_soc_recovery": False,
        "soc_recovery_target": 0.5,
        "soc_recovery_penalty": 0.0,
        "maximum_power_from_grid": config.maximum_power_from_grid,
        "maximum_power_to_grid": config.maximum_power_to_grid,
    }

    emhass_conf = {
        "data_path": pathlib.Path("/tmp/emhass_data"),
        "root_path": pathlib.Path("/app"),
    }
    emhass_conf["data_path"].mkdir(parents=True, exist_ok=True)

    return retrieve_hass_conf, optim_conf, plant_conf, emhass_conf


def _publish_emhass_sensors(batt_forecast, soc_forecast, pv_list, load_list,
                             time_index, config, cost_val, elapsed):
    """Write EMHASS results to HA sensors (compatible with old EMHASS addon)."""
    try:
        from ..ha_client import HaClient
        ha = HaClient()

        # Battery power forecast
        entries_batt = [{"date": time_index[i].isoformat(),
                         "p_batt_forecast": str(round(batt_forecast[i], 2))}
                        for i in range(len(batt_forecast))]
        ha.set_state("sensor.p_batt_forecast", str(round(batt_forecast[0], 2)), {
            "friendly_name": "Battery Power Forecast",
            "device_class": "power", "unit_of_measurement": "W",
            "battery_scheduled_power": entries_batt,
        })

        # SOC forecast
        if soc_forecast:
            entries_soc = [{"date": time_index[i].isoformat(),
                           "soc_batt_forecast": str(round(soc_forecast[i], 2))}
                          for i in range(len(soc_forecast))]
            ha.set_state("sensor.soc_batt_forecast", str(round(soc_forecast[0], 2)), {
                "friendly_name": "Battery SOC Forecast",
                "device_class": "battery", "unit_of_measurement": "%",
                "battery_scheduled_soc": entries_soc,
            })

        # Optimization status
        ha.set_state("sensor.optim_status", "Optimal", {
            "friendly_name": "EMHASS optimization status",
        })

        # Diagnostics
        ha.set_state("sensor.energieha_emhass_diag", "ok", {
            "friendly_name": "EnergieHA EMHASS Diagnostics",
            "icon": "mdi:bug",
            "mode": "direct_python",
            "costfun": config.emhass_costfun,
            "cost_value": round(cost_val, 2),
            "optimization_time_s": round(elapsed, 1),
            "batt_points": len(batt_forecast),
            "soc_points": len(soc_forecast),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info("EMHASS sensors published to HA")
    except Exception as e:
        logger.warning("Failed to publish EMHASS sensors: %s", e)
