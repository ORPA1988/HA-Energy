"""Data models for EnergieHA."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass
class TimeSlot:
    """A single planning time slot."""
    start: datetime
    duration_min: int
    pv_forecast_w: float = 0.0
    price_eur_kwh: float = 0.0
    load_estimate_w: float = 0.0
    # Battery: positive = charge, negative = discharge (power set by inverter)
    planned_battery_mode: str = "idle"  # "charge" / "discharge" / "idle"
    planned_battery_w: float = 0.0     # estimated power (informational)
    # PHEV: target charge power in W (0 = off)
    planned_phev_w: float = 0.0
    # Grid: positive = import, negative = export
    planned_grid_w: float = 0.0
    projected_soc: float = 0.0

    @property
    def energy_wh(self) -> float:
        """Battery energy delta for this slot in Wh."""
        return self.planned_battery_w * (self.duration_min / 60.0)


@dataclass
class Snapshot:
    """Current system state read from HA sensors."""
    timestamp: datetime
    battery_soc: float          # 0-100%
    battery_power_w: float      # current charge(+)/discharge(-)
    pv_power_w: float
    grid_power_w: float         # import(+)/export(-)
    load_power_w: float
    # PHEV
    phev_connected: bool = False
    phev_soc: float = 0.0      # 0-100%
    phev_power_w: float = 0.0  # current charging power


@dataclass
class PricePoint:
    """A single price data point."""
    start: datetime
    end: datetime
    price_eur_kwh: float


@dataclass
class ForecastPoint:
    """A single PV forecast data point."""
    start: datetime
    end: datetime
    power_w: float


@dataclass
class Plan:
    """Complete scheduling plan."""
    created_at: datetime
    strategy: str
    slots: list = field(default_factory=list)  # list[TimeSlot]
    tz: str = "Europe/Vienna"

    @property
    def current_slot(self) -> "TimeSlot | None":
        """Return the slot that covers the current time."""
        now = datetime.now(ZoneInfo(self.tz))
        for slot in self.slots:
            end = slot.start + timedelta(minutes=slot.duration_min)
            if slot.start <= now < end:
                return slot
        return self.slots[0] if self.slots else None


@dataclass
class Config:
    """Add-on configuration (loaded from /data/options.json)."""
    strategy: str = "surplus"
    cycle_seconds: int = 300
    slot_duration_min: int = 15

    # Hausbatterie (Leistung wird vom WR vorgegeben, nur Modus steuerbar)
    battery_capacity_kwh: float = 30.0
    min_soc_percent: int = 15
    max_soc_percent: int = 95
    round_trip_efficiency: float = 0.85

    # PHEV (Peugeot 308 SW Hybrid via go-eCharger)
    phev_enabled: bool = False
    phev_min_charge_w: int = 1380   # ~6A einphasig (230V)
    phev_max_charge_w: int = 3680   # ~16A einphasig (230V)
    phev_battery_kwh: float = 14.0
    phev_voltage: int = 230         # Netzspannung für A→W Umrechnung
    entity_phev_soc: str = "sensor.psa_battery_level"
    entity_phev_charging_power: str = "sensor.garage_wallbox_power"
    entity_phev_connected: str = "sensor.psa_charging_status"
    entity_phev_ampere_limit: str = "number.go_echarger_403613_set_max_ampere_limit"

    # Sensor-Entitäten (Inverter / Netz / PV)
    entity_battery_soc: str = "sensor.inverter_battery"
    entity_battery_power: str = "sensor.inverter_battery_power"
    entity_pv_power: str = "sensor.inverter_pv_power"
    entity_grid_power: str = "sensor.inverter_grid_power"
    entity_load_power: str = "sensor.inverter_load_power"
    entity_epex_prices: str = "sensor.epex_spot_data_total_price"
    entity_solcast_forecast: str = "sensor.solcast_pv_forecast_prognose_heute"
    entity_solcast_forecast_tomorrow: str = "sensor.solcast_pv_forecast_prognose_morgen"

    # Strategieparameter
    min_price_spread_eur: float = 0.04
    price_threshold_eur: float = 0.15
    estimated_daily_load_kwh: float = 12.0

    dry_run: bool = False

    # Sungrow TOU Steuerung
    sungrow_tou_enabled: bool = False

    # Timezone (set from HA config at startup)
    timezone: str = "Europe/Vienna"

    @property
    def battery_capacity_wh(self) -> float:
        return self.battery_capacity_kwh * 1000.0

    @property
    def usable_capacity_wh(self) -> float:
        """Usable capacity between min and max SOC."""
        soc_range = (self.max_soc_percent - self.min_soc_percent) / 100.0
        return self.battery_capacity_wh * soc_range

    @property
    def slots_per_day(self) -> int:
        return (24 * 60) // self.slot_duration_min

    @property
    def load_per_slot_w(self) -> float:
        """Average load per slot based on estimated daily consumption."""
        return (self.estimated_daily_load_kwh * 1000.0) / 24.0
