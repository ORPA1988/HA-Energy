"""Thread-safe shared application state between planning loop and web server."""

import threading
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class CycleSummary:
    """Summary of one planning cycle."""
    timestamp: datetime
    strategy: str
    battery_soc: float
    battery_mode: str
    pv_power_w: float
    grid_power_w: float
    load_power_w: float
    error: str = ""


class AppState:
    """Thread-safe singleton holding shared state between planning loop and Flask."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._data_lock = threading.Lock()
        self._plan = None
        self._snapshot = None
        self._config = None
        self._cycle_history = deque(maxlen=100)
        self._error_log = deque(maxlen=50)
        self._emhass_last_ok = None
        self._emhass_available = False
        self._running = True
        self._cycle_count = 0
        self._prices = []       # list of PricePoint-like dicts
        self._pv_forecast = []  # list of ForecastPoint-like dicts
        self._savings = {}      # savings summary dict

    @property
    def plan(self):
        with self._data_lock:
            return self._plan

    @plan.setter
    def plan(self, value):
        with self._data_lock:
            self._plan = value

    @property
    def snapshot(self):
        with self._data_lock:
            return self._snapshot

    @snapshot.setter
    def snapshot(self, value):
        with self._data_lock:
            self._snapshot = value

    @property
    def config(self):
        with self._data_lock:
            return self._config

    @config.setter
    def config(self, value):
        with self._data_lock:
            self._config = value

    @property
    def running(self):
        return self._running

    @running.setter
    def running(self, value):
        self._running = value

    @property
    def cycle_count(self):
        return self._cycle_count

    @cycle_count.setter
    def cycle_count(self, value):
        self._cycle_count = value

    def add_cycle(self, summary: CycleSummary):
        with self._data_lock:
            self._cycle_history.appendleft(summary)
            self._cycle_count += 1

    def add_error(self, error_msg: str):
        with self._data_lock:
            self._error_log.appendleft({
                "timestamp": datetime.now().isoformat(),
                "error": error_msg,
            })

    def get_cycle_history(self, limit=20):
        with self._data_lock:
            return list(self._cycle_history)[:limit]

    def get_error_log(self, limit=20):
        with self._data_lock:
            return list(self._error_log)[:limit]

    @property
    def prices(self):
        with self._data_lock:
            return list(self._prices)

    @prices.setter
    def prices(self, value):
        with self._data_lock:
            self._prices = value or []

    @property
    def pv_forecast(self):
        with self._data_lock:
            return list(self._pv_forecast)

    @pv_forecast.setter
    def pv_forecast(self, value):
        with self._data_lock:
            self._pv_forecast = value or []

    @property
    def savings(self):
        with self._data_lock:
            return dict(self._savings) if self._savings else {}

    @savings.setter
    def savings(self, value):
        with self._data_lock:
            self._savings = value or {}

    @property
    def emhass_last_ok(self):
        with self._data_lock:
            return self._emhass_last_ok

    @emhass_last_ok.setter
    def emhass_last_ok(self, value):
        with self._data_lock:
            self._emhass_last_ok = value

    @property
    def emhass_available(self):
        with self._data_lock:
            return self._emhass_available

    @emhass_available.setter
    def emhass_available(self, value):
        with self._data_lock:
            self._emhass_available = value

    def get_status_dict(self) -> dict:
        """Return a summary dict for the API."""
        with self._data_lock:
            plan = self._plan
            snap = self._snapshot
            config = self._config

        result = {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "emhass_available": self._emhass_available,
            "emhass_last_ok": self._emhass_last_ok.isoformat() if self._emhass_last_ok else None,
        }

        if snap:
            result["snapshot"] = {
                "battery_soc": snap.battery_soc,
                "pv_power_w": snap.pv_power_w,
                "grid_power_w": snap.grid_power_w,
                "load_power_w": snap.load_power_w,
                "battery_power_w": snap.battery_power_w,
                "phev_connected": snap.phev_connected,
                "phev_soc": snap.phev_soc,
                "timestamp": snap.timestamp.isoformat() if snap.timestamp else None,
            }

        if plan:
            slot = plan.current_slot
            result["plan"] = {
                "strategy": plan.strategy,
                "created_at": plan.created_at.isoformat(),
                "total_slots": len(plan.slots),
                "current_mode": slot.planned_battery_mode if slot else "idle",
                "current_price": slot.price_eur_kwh if slot else 0,
                "projected_soc": slot.projected_soc if slot else 0,
            }

        if config:
            result["config"] = {
                "strategy": config.strategy,
                "dry_run": config.dry_run,
                "direct_control": config.direct_control,
                "battery_capacity_kwh": config.battery_capacity_kwh,
                "min_soc_percent": config.min_soc_percent,
                "max_soc_percent": config.max_soc_percent,
                "sungrow_tou_enabled": config.sungrow_tou_enabled,
                "phev_enabled": config.phev_enabled,
            }

        return result
