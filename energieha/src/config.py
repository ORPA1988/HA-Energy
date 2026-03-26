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
        entity_epex_prices=str(data.get("entity_epex_prices", "sensor.epex_spot_data_total_price")),
        entity_solcast_forecast=str(data.get("entity_solcast_forecast", "sensor.solcast_pv_forecast_prognose_heute")),
        entity_solcast_forecast_tomorrow=str(data.get("entity_solcast_forecast_tomorrow", "sensor.solcast_pv_forecast_prognose_morgen")),
        min_price_spread_eur=float(data.get("min_price_spread_eur", 0.04)),
        price_threshold_eur=float(data.get("price_threshold_eur", 0.15)),
        estimated_daily_load_kwh=float(data.get("estimated_daily_load_kwh", 12.0)),
        dry_run=bool(data.get("dry_run", False)),
        sungrow_tou_enabled=bool(data.get("sungrow_tou_enabled", False)),
    )
