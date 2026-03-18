"""Pydantic data models for HA Energy Optimizer."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EVChargeMode(str, Enum):
    SOLAR = "solar"
    MIN_SOLAR = "min_solar"
    FAST = "fast"
    SMART = "smart"
    OFF = "off"


class OptimizationGoal(str, Enum):
    COST = "cost"
    SELF_CONSUMPTION = "self_consumption"
    BALANCED = "balanced"


class BalancingState(str, Enum):
    IDLE = "idle"
    WAITING = "waiting"
    CHARGING = "charging"
    HOLDING = "holding"


class CarState(int, Enum):
    NONE = 0
    CHARGING = 1
    DONE = 2
    READY = 3


class EVStrategyType(str, Enum):
    GRID_TO_EV = "grid_to_ev"
    GRID_TO_BATTERY_TO_EV = "grid_battery_ev"
    SOLAR_TO_EV = "solar_ev"
    COMBINED = "combined"
    SOLAR_TO_BATTERY_TO_EV = "solar_battery_ev"


# ---------------------------------------------------------------------------
# State models
# ---------------------------------------------------------------------------

class EnergyState(BaseModel):
    """Current snapshot of the home energy system."""
    timestamp: datetime = Field(default_factory=datetime.now)
    # PV
    pv_power_w: float = 0.0
    pv_forecast_today_kwh: float = 0.0
    # Battery
    battery_soc_percent: float = 50.0
    battery_power_w: float = 0.0  # positive=charging, negative=discharging
    battery_capacity_kwh: float = 10.0
    battery_is_balancing: bool = False
    # Grid / Inverter
    grid_power_w: float = 0.0  # positive=import, negative=export
    inverter_powerloss_w: float = 0.0  # inverter conversion losses
    # House load
    house_load_w: float = 0.0
    surplus_w: float = 0.0  # solar - house_load - battery_charging
    # EV (from go-e or HA sensor)
    ev_soc_percent: Optional[float] = None
    ev_car_state: CarState = CarState.NONE
    ev_charge_power_w: float = 0.0
    ev_session_kwh: float = 0.0
    ev_charge_current_a: int = 0
    ev_charge_mode: EVChargeMode = EVChargeMode.SMART
    ev_temperature_c: Optional[float] = None
    # Prices
    price_raw_ct_kwh: float = 0.0
    price_net_ct_kwh: float = 0.0
    price_total_ct_kwh: float = 0.0
    feed_in_ct_kwh: float = 8.0
    # Balancing
    balancing_status: BalancingState = BalancingState.IDLE
    last_balance_time: Optional[datetime] = None


class GoeStatus(BaseModel):
    """Status from go-e charger."""
    car_state: CarState = CarState.NONE
    current_a: int = 0
    power_w: float = 0.0
    energy_kwh_session: float = 0.0
    phases_active: int = 1
    temperature_c: float = 0.0
    error_code: int = 0
    enabled: bool = False
    max_current_a: int = 16
    firmware_version: str = ""


# ---------------------------------------------------------------------------
# Price models
# ---------------------------------------------------------------------------

class PriceResult(BaseModel):
    raw_ct: float
    net_ct: float
    gross_ct: float
    total_ct: float
    breakdown: dict[str, float]


class TimeWindow(BaseModel):
    start: datetime
    end: datetime
    avg_price_ct: float
    min_price_ct: float


class PriceForecast(BaseModel):
    hours: list[datetime]
    raw_ct: list[float]
    net_ct: list[float]
    total_ct: list[float]
    cheap_windows: list[TimeWindow] = []


# ---------------------------------------------------------------------------
# PV Forecast models
# ---------------------------------------------------------------------------

class PVForecastResult(BaseModel):
    hours: list[datetime]
    power_w: list[float]
    total_kwh: float


# ---------------------------------------------------------------------------
# Optimization schedule models
# ---------------------------------------------------------------------------

class EVSlotData(BaseModel):
    """Per-EV data within an hourly slot (for multi-EV support)."""
    name: str = ""
    charge_w: float = 0.0
    current_a: int = 0
    soc_end: Optional[float] = None


class HourlySlot(BaseModel):
    """One-hour slot in the optimization schedule."""
    hour: datetime
    battery_charge_w: float = 0.0
    battery_discharge_w: float = 0.0
    ev_charge_w: float = 0.0
    ev_current_a: int = 0
    grid_import_w: float = 0.0
    grid_export_w: float = 0.0
    battery_soc_end: float = 0.0
    ev_soc_end: Optional[float] = None
    ev_slots: list[EVSlotData] = []
    deferrable_loads: dict[str, bool] = {}
    cost_eur: float = 0.0
    price_ct: float = 0.0


class DailySchedule(BaseModel):
    """24h LP optimization result."""
    created_at: datetime = Field(default_factory=datetime.now)
    slots: list[HourlySlot] = []
    total_cost_eur: float = 0.0
    estimated_savings_eur: float = 0.0
    optimization_goal: OptimizationGoal = OptimizationGoal.COST
    solver_status: str = "unknown"


class LongTermPlan(BaseModel):
    """48h genetic algorithm planning result."""
    created_at: datetime = Field(default_factory=datetime.now)
    slots: list[HourlySlot] = []
    total_cost_eur: float = 0.0
    battery_reserve_soc: float = 20.0
    recommended_battery_charge_windows: list[TimeWindow] = []


# ---------------------------------------------------------------------------
# EV Strategy models
# ---------------------------------------------------------------------------

class EVStrategy(BaseModel):
    strategy_type: EVStrategyType
    name: str
    description: str
    feasible: bool
    total_cost_eur: float
    charging_timeline: list[HourlySlot] = []
    achieves_target_soc: bool
    target_soc_at_deadline: float = 0.0
    notes: str = ""


class EVStrategyResult(BaseModel):
    created_at: datetime = Field(default_factory=datetime.now)
    recommended: EVStrategyType
    strategies: list[EVStrategy]
    window_start: datetime
    window_end: datetime
    target_soc_percent: int
    must_finish_by: datetime


# ---------------------------------------------------------------------------
# Balancing models
# ---------------------------------------------------------------------------

class BalancingDecision(BaseModel):
    should_start: bool
    reason: str
    estimated_cost_eur: float = 0.0
    scheduled_time: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Control actions
# ---------------------------------------------------------------------------

class Actions(BaseModel):
    """Final merged actions to execute."""
    ev_charge_current_a: int = 0
    ev_enabled: bool = False
    battery_charge_limit_w: float = 3000.0
    battery_discharge_limit_w: float = 3000.0
    deferrable_loads_state: dict[str, bool] = {}
    estimated_savings_eur: float = 0.0
    active_strategy: Optional[EVStrategyType] = None
