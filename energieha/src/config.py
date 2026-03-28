"""Configuration loader for EnergieHA."""

import json
import logging
import os

from .models import Config

logger = logging.getLogger(__name__)

OPTIONS_PATH = "/data/options.json"


def load_config() -> Config:
    """Load configuration from HA add-on options file."""
    path = os.environ.get("ENERGIEHA_OPTIONS_PATH", OPTIONS_PATH)

    if not os.path.exists(path):
        logger.warning("Options file %s not found, using defaults", path)
        return Config()

    with open(path, "r") as f:
        data = json.load(f)

    logger.info("Loaded config from %s: strategy=%s, cycle=%s, slots=%s, phev=%s",
                path,
                data.get("strategy", "surplus"),
                data.get("cycle_seconds", 300),
                data.get("slot_duration_min", 15),
                data.get("phev_enabled", False))

    # HA add-on options can deliver values as strings — cast explicitly
    return Config(
        strategy=str(data.get("strategy", "surplus")),
        cycle_seconds=int(data.get("cycle_seconds", 300)),
        slot_duration_min=int(data.get("slot_duration_min", 15)),
        battery_capacity_kwh=float(data.get("battery_capacity_kwh", 30.0)),
        min_soc_percent=int(data.get("min_soc_percent", 15)),
        max_soc_percent=int(data.get("max_soc_percent", 95)),
        round_trip_efficiency=float(data.get("round_trip_efficiency", 0.85)),
        phev_enabled=bool(data.get("phev_enabled", False)),
        phev_min_charge_w=int(data.get("phev_min_charge_w", 1380)),
        phev_max_charge_w=int(data.get("phev_max_charge_w", 3680)),
        phev_battery_kwh=float(data.get("phev_battery_kwh", 14.0)),
        phev_voltage=int(data.get("phev_voltage", 230)),
        entity_phev_soc=str(data.get("entity_phev_soc", "sensor.psa_battery_level")),
        entity_phev_charging_power=str(data.get("entity_phev_charging_power", "sensor.garage_wallbox_power")),
        entity_phev_connected=str(data.get("entity_phev_connected", "sensor.psa_charging_status")),
        entity_phev_ampere_limit=str(data.get("entity_phev_ampere_limit", "number.go_echarger_403613_set_max_ampere_limit")),
        entity_battery_soc=str(data.get("entity_battery_soc", "sensor.inverter_battery")),
        entity_battery_power=str(data.get("entity_battery_power", "sensor.inverter_battery_power")),
        entity_pv_power=str(data.get("entity_pv_power", "sensor.inverter_pv_power")),
        entity_grid_power=str(data.get("entity_grid_power", "sensor.inverter_grid_power")),
        entity_load_power=str(data.get("entity_load_power", "sensor.inverter_load_power")),
        entity_epex_prices=str(data.get("entity_epex_prices", "sensor.epex_spot_data_total_price_3")),
        entity_solcast_forecast=str(data.get("entity_solcast_forecast", "sensor.solcast_pv_forecast_prognose_heute")),
        entity_solcast_forecast_tomorrow=str(data.get("entity_solcast_forecast_tomorrow", "sensor.solcast_pv_forecast_prognose_morgen")),
        entity_grid_charge_current=str(data.get("entity_grid_charge_current", "number.inverter_battery_grid_charging_current")),
        min_price_spread_eur=float(data.get("min_price_spread_eur", 0.04)),
        price_threshold_eur=float(data.get("price_threshold_eur", 0.15)),
        estimated_daily_load_kwh=float(data.get("estimated_daily_load_kwh", 12.0)),
        dry_run=bool(data.get("dry_run", False)),
        max_grid_charge_soc=int(data.get("max_grid_charge_soc", 80)),
        sungrow_tou_enabled=bool(data.get("sungrow_tou_enabled", False)),
        emhass_url=str(data.get("emhass_url", "http://localhost:5000")),
        export_price_eur=float(data.get("export_price_eur", 0.10)),
        emhass_optimization_time_step=int(data.get("emhass_optimization_time_step", 30)),
        emhass_battery_charge_power_max=int(data.get("emhass_battery_charge_power_max", 5000)),
        emhass_battery_discharge_power_max=int(data.get("emhass_battery_discharge_power_max", 5000)),
        mode_hold_seconds=int(data.get("mode_hold_seconds", 120)),
    )


def validate_config(config: Config) -> bool:
    """Validate config values. Returns True if valid."""
    errors = []
    if config.min_soc_percent >= config.max_soc_percent:
        errors.append(f"min_soc ({config.min_soc_percent}) >= max_soc ({config.max_soc_percent})")
    if config.max_grid_charge_soc > config.max_soc_percent:
        errors.append(f"max_grid_charge_soc ({config.max_grid_charge_soc}) > max_soc ({config.max_soc_percent})")
    if config.max_grid_charge_soc < config.min_soc_percent:
        errors.append(f"max_grid_charge_soc ({config.max_grid_charge_soc}) < min_soc ({config.min_soc_percent})")
    if not config.entity_battery_soc:
        errors.append("entity_battery_soc is empty")
    if config.round_trip_efficiency < 0.5 or config.round_trip_efficiency > 1.0:
        errors.append(f"round_trip_efficiency ({config.round_trip_efficiency}) outside [0.5, 1.0]")
    # EMHASS-specific validation
    if config.strategy == "emhass":
        step = config.emhass_optimization_time_step
        slot = config.slot_duration_min
        if step % slot != 0 and slot % step != 0:
            errors.append(f"emhass_optimization_time_step ({step}) must be a multiple "
                          f"of slot_duration_min ({slot}) or vice versa")
        if config.emhass_battery_charge_power_max <= 0:
            errors.append(f"emhass_battery_charge_power_max must be > 0")
        if config.emhass_battery_discharge_power_max <= 0:
            errors.append(f"emhass_battery_discharge_power_max must be > 0")
    for e in errors:
        logger.error("Config validation: %s", e)
    return len(errors) == 0
