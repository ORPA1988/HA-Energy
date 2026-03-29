"""Thread-safe shared application state between planning loop and web server."""

import json
import os
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
        self._daily_stats = []  # list of daily stat dicts (last 30 days)

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

    # ---- Persistent State (survives addon restart) ----

    STATE_FILE = "/data/energieha_state.json"

    STATS_FILE = "/data/energieha_daily_stats.json"

    def save_state(self):
        """Save prices, forecast, savings, cycle_count, daily_stats to disk."""
        try:
            data = {
                "prices": self._prices,
                "pv_forecast": self._pv_forecast,
                "savings": self._savings,
                "cycle_count": self._cycle_count,
                "saved_at": datetime.now().isoformat(),
            }
            path = os.environ.get("ENERGIEHA_STATE_PATH", self.STATE_FILE)
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning("Failed to save state: %s", e)

    def load_state(self):
        """Restore saved state from disk (called on startup)."""
        try:
            path = os.environ.get("ENERGIEHA_STATE_PATH", self.STATE_FILE)
            if not os.path.exists(path):
                return
            with open(path) as f:
                data = json.load(f)
            self._prices = data.get("prices", [])
            self._pv_forecast = data.get("pv_forecast", [])
            self._savings = data.get("savings", {})
            self._cycle_count = data.get("cycle_count", 0)
            logger.info("State restored from %s (%d prices, %d forecast)",
                        path, len(self._prices), len(self._pv_forecast))
        except Exception as e:
            logger.warning("Failed to load state: %s", e)
        # Load daily stats separately
        self._load_daily_stats()

    def record_daily_stats(self, snapshot, plan):
        """Record today's stats. Called once per cycle, accumulates over the day."""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            # Find or create today's entry
            entry = None
            for s in self._daily_stats:
                if s.get("date") == today:
                    entry = s
                    break
            if not entry:
                entry = {"date": today, "grid_import_wh": 0, "grid_export_wh": 0,
                         "pv_wh": 0, "load_wh": 0, "cost_eur": 0, "cycles": 0}
                self._daily_stats.append(entry)

            # Accumulate energy for this 5-minute cycle (W × 5min/60 = Wh)
            interval_h = 5.0 / 60.0  # 5 minutes
            if snapshot:
                grid = snapshot.grid_power_w
                if grid > 0:
                    entry["grid_import_wh"] += grid * interval_h
                else:
                    entry["grid_export_wh"] += abs(grid) * interval_h
                entry["pv_wh"] += max(0, snapshot.pv_power_w) * interval_h
                entry["load_wh"] += max(0, snapshot.load_power_w) * interval_h
                # Cost for this interval
                if plan and plan.current_slot and grid > 0:
                    entry["cost_eur"] += grid / 1000.0 * interval_h * plan.current_slot.price_eur_kwh
            entry["cycles"] += 1

            # Keep only last 30 days
            self._daily_stats = [s for s in self._daily_stats
                                 if s.get("date", "") >= (datetime.now().replace(day=1)).strftime("%Y-%m-%d")][-30:]

            # Save periodically (every 12 cycles = 1 hour)
            if entry["cycles"] % 12 == 0:
                self._save_daily_stats()
        except Exception as e:
            logger.warning("Daily stats error: %s", e)

    def get_daily_stats(self, days: int = 7) -> list:
        """Return last N days of stats as list of dicts."""
        return self._daily_stats[-days:]

    def _save_daily_stats(self):
        try:
            path = os.environ.get("ENERGIEHA_STATS_PATH", self.STATS_FILE)
            with open(path, "w") as f:
                json.dump(self._daily_stats, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save daily stats: %s", e)

    def _load_daily_stats(self):
        try:
            path = os.environ.get("ENERGIEHA_STATS_PATH", self.STATS_FILE)
            if os.path.exists(path):
                with open(path) as f:
                    self._daily_stats = json.load(f)
                logger.info("Loaded %d days of daily stats", len(self._daily_stats))
        except Exception as e:
            logger.warning("Failed to load daily stats: %s", e)
            self._daily_stats = []

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
            # Add savings from the plan
            savings = self._savings
            if savings:
                result["snapshot"]["savings_eur"] = savings.get("cost_with_battery_eur", 0)
                result["snapshot"]["self_consumption"] = savings.get("self_consumption_percent", 0)

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
                "max_grid_charge_soc": config.max_grid_charge_soc,
                "grid_charge_target_soc": config.grid_charge_target_soc,
                "price_threshold_eur": config.price_threshold_eur,
                "load_planning_reserve_pct": getattr(config, 'load_planning_reserve_pct', 10),
                "estimated_daily_load_kwh": config.estimated_daily_load_kwh,
                "sungrow_tou_enabled": config.sungrow_tou_enabled,
                "phev_enabled": config.phev_enabled,
            }

        # Next action countdown from plan
        if plan and plan.slots:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            try:
                now = datetime.now(ZoneInfo(plan.tz))
                next_charge = None
                for s in plan.slots:
                    if s.planned_battery_mode == "charge" and s.planned_battery_w > 50:
                        surplus = s.pv_forecast_w - s.load_estimate_w
                        if s.planned_battery_w > max(0, surplus):  # grid charge
                            if s.start > now:
                                next_charge = s
                                break
                if next_charge:
                    mins = int((next_charge.start - now).total_seconds() / 60)
                    result["next_action"] = {
                        "type": "grid_charge",
                        "start": next_charge.start.strftime("%H:%M"),
                        "minutes_until": max(0, mins),
                        "label": f"Netzladung in {mins // 60}h {mins % 60}min ({next_charge.start.strftime('%H:%M')})"
                            if mins > 0 else f"Netzladung JETZT ({next_charge.start.strftime('%H:%M')})",
                    }
            except Exception:
                pass

        return result
