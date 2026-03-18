"""Configuration management — reads /data/options.json provided by HA Supervisor."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

OPTIONS_FILE = Path("/data/options.json")
CONFIG_JSON_FILE = Path("/data/ha_energy_config.json")


@dataclass
class EVChargingWindow:
    name: str
    available_from: str
    available_until: str
    target_soc_percent: int
    must_finish_by: str
    priority: str = "cost"


@dataclass
class EVConfig:
    """Configuration for a single EV/wallbox pair."""
    name: str = "EV 1"
    wallbox_type: str = "goe"  # "goe", "ha_entity", "ocpp"
    soc_sensor: str = ""
    battery_capacity_kwh: float = 60.0
    min_charge_current_a: int = 6
    max_charge_current_a: int = 16
    phases: int = 1
    charge_mode: str = "smart"  # solar/min_solar/fast/smart/off
    # HA entity wallbox fields
    switch_entity: str = ""
    power_sensor: str = ""
    current_number: str = ""
    session_sensor: str = ""
    car_state_sensor: str = ""
    # OCPP fields
    ocpp_entity_prefix: str = ""
    # go-e fields (uses global goe_* config if this is the primary EV)
    use_global_goe: bool = True
    # Charging windows specific to this EV
    charging_windows: list[EVChargingWindow] = field(default_factory=list)
    target_soc: int = 80
    allow_grid_charging: bool = True


@dataclass
class DeferrableLoad:
    name: str
    switch: str
    power_w: int
    duration_h: float
    latest_end_h: int
    earliest_start_h: int
    min_soc_battery: int = 20
    price_limit_ct_kwh: float = 999.0
    power_sensor: str = ""  # HA sensor for actual power (overrides power_w)
    subtract_from_total: bool = False  # Subtract from total consumption for base load calc


@dataclass
class Config:
    # PV
    pv_power_sensor: str = "sensor.solar_power"
    pv_forecast_kwp: float = 10.0
    pv_orientation: int = 180
    pv_tilt: int = 30
    pv_latitude: float = 48.0
    pv_longitude: float = 11.0
    pv_efficiency: float = 0.18
    pv_forecast_source: str = "auto"  # "auto" | "solcast" | "open_meteo"
    solcast_entity: str = ""  # e.g. "sensor.solcast_pv_forecast_forecast_today"
    solcast_estimate_type: str = "pv_estimate"  # "pv_estimate" | "pv_estimate10" | "pv_estimate90"

    # Battery
    battery_soc_sensor: str = "sensor.battery_soc"
    battery_power_sensor: str = "sensor.battery_power"
    battery_capacity_kwh: float = 10.0
    battery_charge_switch: str = "switch.battery_charge"
    battery_discharge_switch: str = "switch.battery_discharge"
    battery_max_charge_w: int = 3000
    battery_max_discharge_w: int = 3000
    battery_min_soc: int = 10
    battery_reserve_soc: int = 20
    battery_efficiency: float = 0.95

    # Battery Balancing
    battery_balancing_enabled: bool = True
    battery_balancing_mode: str = "auto"
    battery_balancing_frequency: str = "monthly"
    battery_balancing_custom_days: int = 30
    battery_balancing_target_soc: int = 100
    battery_balancing_hold_duration_h: int = 2
    battery_balancing_preferred_time: str = "10:00"
    battery_balancing_auto_trigger_soc_deviation: int = 5
    battery_balancing_use_solar_only: bool = True

    # Grid / Inverter
    grid_power_sensor: str = "sensor.grid_power"
    grid_max_import_w: int = 0
    total_power_sensor: str = ""  # Total house consumption sensor for load decomposition
    inverter_powerloss_sensor: str = ""  # Optional: inverter power loss sensor (added to total consumption)

    # Prices
    price_source: str = "entso-e"
    entso_e_token: str = ""
    entso_e_area: str = "10YDE-EON------1"
    tibber_token: str = ""
    awattar_country: str = "AT"
    epex_spot_area: str = "DE-LU"
    price_sensor_entity: str = ""
    fixed_price_ct_kwh: float = 25.0
    # EPEX entity support (reads directly from HA entities like sensor.epex_spot_de_price)
    epex_import_entity: str = ""
    epex_export_entity: str = ""
    epex_unit: str = "ct/kWh"  # "ct/kWh", "EUR/MWh", "EUR/kWh"

    # Price calculation
    price_input_is_netto: bool = True  # Deprecated: use price_input_mode instead
    price_input_mode: str = "netto"  # "netto", "brutto", "total_gross"
    price_vat_percent: float = 19.0
    price_grid_fee_source: str = "fixed"
    price_grid_fee_fixed_ct_kwh: float = 7.5
    price_grid_fee_entity: str = ""
    price_supplier_markup_ct_kwh: float = 2.0
    price_other_taxes_ct_kwh: float = 0.0
    price_feed_in_ct_kwh: float = 8.0

    # go-e
    goe_enabled: bool = False
    goe_connection_type: str = "local"
    goe_local_ip: str = ""
    goe_cloud_serial: str = ""
    goe_cloud_token: str = ""
    goe_max_current_a: int = 16
    goe_phases: int = 1

    # EV
    ev_soc_sensor: str = "sensor.ev_battery_soc"
    ev_battery_capacity_kwh: float = 60.0
    ev_charge_mode: str = "smart"
    ev_min_charge_current_a: int = 6
    ev_max_charge_current_a: int = 16
    ev_allow_battery_to_charge_ev: bool = True
    ev_allow_grid_to_charge_ev: bool = True
    ev_combined_charge_threshold_ct: float = 15.0

    # EV Charging Windows
    ev_charging_windows: list[EVChargingWindow] = field(default_factory=list)

    # Multi-EV configurations
    ev_configs: list[EVConfig] = field(default_factory=list)

    # Deferrable Loads
    deferrable_loads: list[DeferrableLoad] = field(default_factory=list)

    # EV surplus thresholds (hysteresis to prevent flapping)
    ev_surplus_start_threshold_w: int = 1400  # ~6A at 230V, start solar charging
    ev_surplus_stop_threshold_w: int = 1000   # stop solar charging below this

    # Operation mode
    read_only: bool = True  # Default: safe mode on first start, no switching/control
    operation_mode: str = "stopped"  # "stopped" = no optimization/scheduler, "running" = full operation

    # Optimization
    optimizer_backend: str = "builtin"  # "builtin" (scipy LP) or "emhass"
    optimization_goal: str = "cost"
    optimization_interval_minutes: int = 60
    long_term_plan_interval_hours: int = 6
    peak_shaving_limit_w: int = 0

    # Notifications
    notify_target: str = "notify.mobile_app"
    notify_on_balancing: bool = True
    notify_on_cheap_window: bool = True
    notify_on_ev_charged: bool = True

    # Runtime (not from config file)
    ha_url: str = field(default_factory=lambda: os.environ.get("HA_URL", "http://supervisor/core"))
    supervisor_token: str = field(default_factory=lambda: os.environ.get("SUPERVISOR_TOKEN", os.environ.get("HASSIO_TOKEN", "")))


def load_config() -> Config:
    """Load configuration from JSON config file, falling back to /data/options.json."""
    cfg = Config()

    # Priority: 1. ha_energy_config.json (web GUI), 2. options.json (HA Supervisor)
    opts = {}
    if CONFIG_JSON_FILE.exists():
        try:
            opts = json.loads(CONFIG_JSON_FILE.read_text())
            logger.info("Configuration loaded from %s", CONFIG_JSON_FILE)
        except Exception as e:
            logger.error("Failed to parse %s: %s", CONFIG_JSON_FILE, e)

    if not opts and OPTIONS_FILE.exists():
        try:
            opts = json.loads(OPTIONS_FILE.read_text())
            logger.info("Configuration loaded from %s", OPTIONS_FILE)
        except Exception as e:
            logger.error("Failed to parse options.json: %s", e)
            return cfg

    if not opts:
        logger.warning("No config files found, using defaults")
        return cfg

    # Map flat options to dataclass fields
    simple_fields = [
        "pv_power_sensor", "pv_forecast_kwp", "pv_orientation", "pv_tilt",
        "pv_latitude", "pv_longitude", "pv_efficiency",
        "pv_forecast_source", "solcast_entity", "solcast_estimate_type",
        "battery_soc_sensor", "battery_power_sensor", "battery_capacity_kwh",
        "battery_charge_switch", "battery_discharge_switch",
        "battery_max_charge_w", "battery_max_discharge_w",
        "battery_min_soc", "battery_reserve_soc", "battery_efficiency",
        "battery_balancing_enabled", "battery_balancing_mode",
        "battery_balancing_frequency", "battery_balancing_custom_days",
        "battery_balancing_target_soc", "battery_balancing_hold_duration_h",
        "battery_balancing_preferred_time",
        "battery_balancing_auto_trigger_soc_deviation",
        "battery_balancing_use_solar_only",
        "grid_power_sensor", "grid_max_import_w", "total_power_sensor",
        "inverter_powerloss_sensor",
        "price_source", "entso_e_token", "entso_e_area",
        "tibber_token", "awattar_country", "epex_spot_area",
        "price_sensor_entity", "fixed_price_ct_kwh",
        "epex_import_entity", "epex_export_entity", "epex_unit",
        "price_input_is_netto", "price_input_mode", "price_vat_percent",
        "price_grid_fee_source", "price_grid_fee_fixed_ct_kwh",
        "price_grid_fee_entity", "price_supplier_markup_ct_kwh",
        "price_other_taxes_ct_kwh", "price_feed_in_ct_kwh",
        "goe_enabled", "goe_connection_type", "goe_local_ip",
        "goe_cloud_serial", "goe_cloud_token", "goe_max_current_a", "goe_phases",
        "ev_soc_sensor", "ev_battery_capacity_kwh", "ev_charge_mode",
        "ev_min_charge_current_a", "ev_max_charge_current_a",
        "ev_allow_battery_to_charge_ev", "ev_allow_grid_to_charge_ev",
        "ev_combined_charge_threshold_ct",
        "ev_surplus_start_threshold_w", "ev_surplus_stop_threshold_w",
        "read_only", "operation_mode",
        "optimizer_backend", "optimization_goal", "optimization_interval_minutes",
        "long_term_plan_interval_hours", "peak_shaving_limit_w",
        "notify_target", "notify_on_balancing",
        "notify_on_cheap_window", "notify_on_ev_charged",
    ]
    for f in simple_fields:
        if f in opts:
            setattr(cfg, f, opts[f])

    # EV charging windows
    if "ev_charging_windows" in opts:
        try:
            cfg.ev_charging_windows = [
                EVChargingWindow(**w) for w in opts["ev_charging_windows"]
            ]
        except Exception as e:
            logger.error("Invalid EV charging window configuration: %s", e)
            cfg.ev_charging_windows = []

    # EV configurations (multi-EV)
    if "ev_configs" in opts:
        try:
            cfg.ev_configs = [EVConfig(**ev) for ev in opts["ev_configs"]]
        except Exception as e:
            logger.error("Invalid EV config: %s", e)
            cfg.ev_configs = []

    # Deferrable loads
    if "deferrable_loads" in opts:
        try:
            cfg.deferrable_loads = [
                DeferrableLoad(**dl) for dl in opts["deferrable_loads"]
            ]
        except Exception as e:
            logger.error("Invalid deferrable load configuration: %s", e)
            cfg.deferrable_loads = []

    # Migrate deprecated price_input_is_netto to price_input_mode
    if "price_input_mode" not in opts and "price_input_is_netto" in opts:
        cfg.price_input_mode = "netto" if cfg.price_input_is_netto else "brutto"
    if cfg.price_input_mode not in ("netto", "brutto", "total_gross"):
        cfg.price_input_mode = "netto"

    # Validate critical value ranges
    cfg.battery_min_soc = max(0, min(100, cfg.battery_min_soc))
    cfg.battery_reserve_soc = max(0, min(100, cfg.battery_reserve_soc))
    cfg.price_vat_percent = max(0.0, min(100.0, cfg.price_vat_percent))
    cfg.battery_efficiency = max(0.1, min(1.0, cfg.battery_efficiency))
    cfg.goe_max_current_a = max(6, min(32, cfg.goe_max_current_a))
    cfg.goe_phases = max(1, min(3, cfg.goe_phases))
    cfg.ev_min_charge_current_a = max(0, min(32, cfg.ev_min_charge_current_a))
    cfg.ev_max_charge_current_a = max(cfg.ev_min_charge_current_a, min(32, cfg.ev_max_charge_current_a))
    cfg.battery_capacity_kwh = max(0.1, cfg.battery_capacity_kwh)
    cfg.ev_battery_capacity_kwh = max(0.1, cfg.ev_battery_capacity_kwh)

    logger.info("Configuration loaded successfully")
    return cfg



def save_config(cfg: Config) -> bool:
    """Save current config to JSON file for persistence across restarts."""
    try:
        data = {}
        for f in cfg.__dataclass_fields__:
            val = getattr(cfg, f)
            if isinstance(val, list):
                if val and hasattr(val[0], '__dataclass_fields__'):
                    data[f] = [
                        {k: getattr(item, k) for k in item.__dataclass_fields__}
                        for item in val
                    ]
                else:
                    data[f] = val
            else:
                data[f] = val
        CONFIG_JSON_FILE.write_text(json.dumps(data, indent=2, default=str))
        logger.info("Config saved to %s", CONFIG_JSON_FILE)
        return True
    except Exception as e:
        logger.error("Failed to save config: %s", e)
        return False


def reload_config() -> Config:
    """Force reload config from disk."""
    global _config
    _config = load_config()
    return _config


def update_config(updates: dict) -> Config:
    """Apply partial updates to config and save."""
    cfg = get_config()

    for key, value in updates.items():
        if key == "deferrable_loads" and isinstance(value, list):
            try:
                cfg.deferrable_loads = [DeferrableLoad(**dl) for dl in value]
            except Exception as e:
                logger.error("Invalid deferrable load update: %s", e)
            continue
        if key == "ev_configs" and isinstance(value, list):
            try:
                cfg.ev_configs = [EVConfig(**ev) for ev in value]
            except Exception as e:
                logger.error("Invalid EV config update: %s", e)
            continue
        if key == "ev_charging_windows" and isinstance(value, list):
            try:
                cfg.ev_charging_windows = [EVChargingWindow(**w) for w in value]
            except Exception as e:
                logger.error("Invalid EV window update: %s", e)
            continue
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    save_config(cfg)
    return cfg


# Global singleton
_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config
