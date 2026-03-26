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

    logger.info("Loaded config: strategy=%s, cycle=%ds, slots=%dmin, phev=%s",
                data.get("strategy", "surplus"),
                data.get("cycle_seconds", 300),
                data.get("slot_duration_min", 15),
                data.get("phev_enabled", False))

    return Config(
        strategy=data.get("strategy", "surplus"),
        cycle_seconds=data.get("cycle_seconds", 300),
        slot_duration_min=data.get("slot_duration_min", 15),
        battery_capacity_kwh=data.get("battery_capacity_kwh", 30.0),
        min_soc_percent=data.get("min_soc_percent", 15),
        max_soc_percent=data.get("max_soc_percent", 95),
        round_trip_efficiency=data.get("round_trip_efficiency", 0.85),
        phev_enabled=data.get("phev_enabled", False),
        phev_min_charge_w=data.get("phev_min_charge_w", 1380),
        phev_max_charge_w=data.get("phev_max_charge_w", 3680),
        phev_battery_kwh=data.get("phev_battery_kwh", 14.0),
        phev_voltage=data.get("phev_voltage", 230),
        entity_phev_soc=data.get("entity_phev_soc", "sensor.psa_battery_level"),
        entity_phev_charging_power=data.get("entity_phev_charging_power", "sensor.garage_wallbox_power"),
        entity_phev_connected=data.get("entity_phev_connected", "sensor.psa_charging_status"),
        entity_phev_ampere_limit=data.get("entity_phev_ampere_limit", "number.go_echarger_403613_set_max_ampere_limit"),
        entity_battery_soc=data.get("entity_battery_soc", "sensor.inverter_battery"),
        entity_battery_power=data.get("entity_battery_power", "sensor.inverter_battery_power"),
        entity_pv_power=data.get("entity_pv_power", "sensor.inverter_pv_power"),
        entity_grid_power=data.get("entity_grid_power", "sensor.inverter_grid_power"),
        entity_load_power=data.get("entity_load_power", "sensor.inverter_load_power"),
        entity_epex_prices=data.get("entity_epex_prices", "sensor.epex_spot_data_total_price"),
        entity_solcast_forecast=data.get("entity_solcast_forecast", "sensor.solcast_pv_forecast_prognose_heute"),
        entity_solcast_forecast_tomorrow=data.get("entity_solcast_forecast_tomorrow", "sensor.solcast_pv_forecast_prognose_morgen"),
        min_price_spread_eur=data.get("min_price_spread_eur", 0.04),
        price_threshold_eur=data.get("price_threshold_eur", 0.15),
        estimated_daily_load_kwh=data.get("estimated_daily_load_kwh", 12.0),
        dry_run=data.get("dry_run", False),
        sungrow_tou_enabled=data.get("sungrow_tou_enabled", False),
    )
