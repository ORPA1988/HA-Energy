"""HA Energy Optimizer — FastAPI application entry point."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import logging.handlers
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Add app directory to path
sys.path.insert(0, str(Path(__file__).parent))

LOG_FILE = Path("/data/energy_optimizer.log")
_log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter(_log_format))
    _handlers.append(_file_handler)
except (OSError, PermissionError):
    pass  # No file logging in dev environments
logging.basicConfig(level=logging.INFO, format=_log_format, handlers=_handlers)
logger = logging.getLogger(__name__)

from config import get_config, save_config, update_config, reload_config
from data.collector import DataCollector
from data.forecast import get_pv_forecast
from data.prices import get_price_fetcher
from devices.battery_balancer import get_battery_balancer
from devices.goe import get_goe_charger
from ha_client import get_ha_client
from models import (
    DailySchedule,
    EnergyState,
    EVStrategyResult,
    LongTermPlan,
    OptimizationGoal,
)
from optimizer.coordinator import get_coordinator
from optimizer.ev_strategy import get_ev_strategy_solver
from optimizer.genetic import get_genetic_planner
from optimizer.emhass_backend import get_emhass_optimizer, is_emhass_available
from optimizer.linear import get_linear_optimizer
from optimizer.realtime import get_realtime_controller
from scheduler import get_scheduler, setup_jobs, pause_jobs, resume_jobs


class AppState:
    """Central application state shared across all modules."""

    def __init__(self):
        self.cfg = get_config()
        self.ha = get_ha_client()
        self.collector = DataCollector()
        self.goe = get_goe_charger()
        self.balancer = get_battery_balancer()
        self.price_fetcher = get_price_fetcher()
        self.pv_forecast = get_pv_forecast()
        self.realtime = get_realtime_controller()
        # Select optimizer backend
        if self.cfg.optimizer_backend == "emhass" and is_emhass_available():
            self.lp = get_emhass_optimizer()
            logger.info("Using EMHASS optimizer backend")
        else:
            self.lp = get_linear_optimizer()
            if self.cfg.optimizer_backend == "emhass":
                logger.warning("EMHASS requested but not installed, using built-in LP")
            logger.info("Using built-in LP optimizer backend")
        self.genetic = get_genetic_planner()
        self.ev_solver = get_ev_strategy_solver()
        self.coordinator = get_coordinator()

        # Cached results
        self.current_state: Optional[EnergyState] = None
        self.current_schedule: Optional[DailySchedule] = None
        self.current_plan: Optional[LongTermPlan] = None
        self.current_ev_strategy: Optional[EVStrategyResult] = None

        # WebSocket connections
        self._ws_clients: list[WebSocket] = []
        self._ws_client_limit: int = 100  # Prevent memory leak
        self._history: collections.deque[dict] = collections.deque(maxlen=2880)  # Last 24h

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _estimate_house_load_profile(self) -> list[float]:
        """
        Estimate 24h house load profile from recent history.
        
        Uses last 7 days of history to compute hourly average load.
        Falls back to sensible defaults if insufficient data.
        """
        # Need at least 2 hours of data (240 samples at 30s intervals)
        if len(self._history) < 240:
            # Fallback: typical household consumption pattern
            # Higher during day (600-800W), lower at night (300-400W)
            return [
                400, 350, 350, 350, 400, 500,  # 00-05: Night (low)
                600, 700, 750, 700, 650, 600,  # 06-11: Morning peak, then moderate
                550, 600, 650, 700, 800, 850,  # 12-17: Afternoon rise
                900, 850, 750, 650, 550, 450,  # 18-23: Evening peak, then decline
            ]
        
        # Group history by hour of day and calculate average load
        # Note: History timestamps are local time (datetime.now() without tzinfo).
        # All samples are grouped by hour (0-23), averaging across multiple days.
        # Hours without samples default to 500W (e.g., DST spring-forward, or new deployment).
        hourly_loads = [[] for _ in range(24)]
        for entry in self._history:
            try:
                ts = datetime.fromisoformat(entry["ts"])
                hour = ts.hour
                # Retrieve pre-calculated house load from history
                # (calculated by collector.py using energy balance equation)
                house_w = entry.get("house_w", 500.0)  # Fallback for old entries
                hourly_loads[hour].append(house_w)
            except (ValueError, KeyError):
                continue
        
        # Calculate average for each hour, fallback to 500W if no data
        profile = []
        for hour_samples in hourly_loads:
            if hour_samples:
                profile.append(sum(hour_samples) / len(hour_samples))
            else:
                profile.append(500.0)
        
        return profile

    # ------------------------------------------------------------------
    # Scheduled jobs
    # ------------------------------------------------------------------

    async def run_realtime_loop(self) -> None:
        """Runs every 30s — EVCC-style real-time EV control."""
        try:
            prices = await self.price_fetcher.get_prices_48h()
            current_price = prices.total_ct[0] if prices.total_ct else 0.0
            current_price_raw = prices.raw_ct[0] if prices.raw_ct else 0.0
            current_price_net = prices.net_ct[0] if prices.net_ct else 0.0

            state = await self.collector.get_current_state(
                current_price_raw, current_price_net, current_price
            )
            self.current_state = state

            # Merge wallbox data (go-e native or generic wallbox)
            goe_status = await self.goe.get_status()
            if goe_status:
                state.ev_car_state = goe_status.car_state
                state.ev_charge_power_w = goe_status.power_w
                state.ev_session_kwh = goe_status.energy_kwh_session
                state.ev_charge_current_a = goe_status.current_a
                await self.goe.publish_to_ha(goe_status)
            else:
                # Try generic wallbox
                wb = self.realtime._get_wallbox()
                if wb:
                    wb_status = await wb.get_status()
                    if wb_status:
                        state.ev_charge_power_w = wb_status.power_w
                        state.ev_session_kwh = wb_status.energy_kwh_session
                        state.ev_charge_current_a = wb_status.current_a
                        if hasattr(wb, 'publish_to_ha'):
                            await wb.publish_to_ha(wb_status)

            # Balancing tick
            await self.balancer.tick(state.battery_soc_percent)
            state.balancing_status = self.balancer.current_state
            state.battery_is_balancing = self.balancer.is_active

            # Get coordinator actions
            actions = self.coordinator.get_actions(
                state, self.current_schedule, self.current_plan
            )

            # Run realtime EV control (injects LP smart setpoints)
            target_soc = 80
            if self.cfg.ev_charging_windows:
                target_soc = self.cfg.ev_charging_windows[0].target_soc_percent

            await self.realtime.run(
                state=state,
                goe_status=goe_status,
                lp_current_a=actions.ev_charge_current_a,
                lp_enabled=actions.ev_enabled,
                target_soc=target_soc,
            )

            # Apply load schedule
            await self.coordinator.apply_load_actions(actions)

            # Publish all sensors
            await self.coordinator.publish_summary(state, actions, self.current_schedule)

            # Archive snapshot
            self._add_history(state, actions)

            # Push to WebSocket clients
            await self._broadcast_state(state)

        except Exception as e:
            logger.error("Realtime loop error: %s", e, exc_info=True)

    async def run_linear_optimization(self) -> None:
        """Runs hourly — EMHASS-style LP optimization."""
        try:
            logger.info("Running LP optimization...")
            prices = await self.price_fetcher.get_prices_48h()
            pv = await self.pv_forecast.get_forecast_48h()
            state = self.current_state

            # Guard: skip LP if no sensor data available (sensors not configured yet)
            if not state or (state.pv_power_w == 0 and state.grid_power_w == 0
                             and state.battery_power_w == 0 and state.house_load_w == 0):
                logger.warning("No sensor data available — skipping LP optimization")
                return

            bat_soc = state.battery_soc_percent if state else 50.0
            ev_soc = state.ev_soc_percent if state else None

            # Read Multi-EV SOCs from HA sensors
            ev_soc_map = {}
            for ev in self.cfg.ev_configs:
                if ev.soc_sensor:
                    try:
                        soc_val = await self.ha.get_state_value(ev.soc_sensor, default=-1.0)
                        if soc_val >= 0:
                            ev_soc_map[ev.name] = soc_val
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Could not read EV SOC for %s: %s", ev.name, e)

            target_soc = 80
            departure_h = 7
            if self.cfg.ev_charging_windows:
                # Note: Currently only first window is used
                # Multi-window support requires LP/genetic algorithm enhancement
                w = self.cfg.ev_charging_windows[0]
                target_soc = w.target_soc_percent
                try:
                    departure_h = int(w.must_finish_by.split(":")[0])
                except (ValueError, IndexError):
                    logger.warning("Invalid must_finish_by '%s', using default 7", w.must_finish_by)
                    departure_h = 7
                if len(self.cfg.ev_charging_windows) > 1:
                    logger.info("Multiple EV windows configured, using first: %s", w.name)

            self.current_schedule = self.lp.optimize(
                prices_ct=prices.total_ct,
                pv_forecast_w=pv.power_w,
                house_load_w=self._estimate_house_load_profile(),
                battery_soc=bat_soc,
                ev_soc=ev_soc,
                ev_target_soc=target_soc,
                ev_departure_h=departure_h,
                goal=OptimizationGoal(self.cfg.optimization_goal) if self.cfg.optimization_goal in OptimizationGoal._value2member_map_ else OptimizationGoal.COST,
                ev_soc_map=ev_soc_map,
            )
            logger.info("LP optimization complete. Cost: €%.3f", self.current_schedule.total_cost_eur)

        except Exception as e:
            logger.error("LP optimization error: %s", e, exc_info=True)

    async def run_genetic_planning(self) -> None:
        """Runs every 6h — EOS-style 48h genetic algorithm planning."""
        try:
            logger.info("Running 48h genetic planner...")
            prices = await self.price_fetcher.get_prices_48h()
            pv = await self.pv_forecast.get_forecast_48h()
            state = self.current_state

            bat_soc = state.battery_soc_percent if state else 50.0
            ev_soc = state.ev_soc_percent if state else None
            target_soc = 80
            if self.cfg.ev_charging_windows:
                # Note: Using first window's target SOC for genetic planning
                target_soc = self.cfg.ev_charging_windows[0].target_soc_percent

            self.current_plan = self.genetic.optimize_48h(
                pv_forecast_w=pv.power_w,
                price_forecast_ct=prices.total_ct,
                battery_soc=bat_soc,
                ev_soc=ev_soc,
                ev_target_soc=target_soc,
            )
            logger.info("Genetic plan complete. 48h cost: €%.3f", self.current_plan.total_cost_eur)

        except Exception as e:
            logger.error("Genetic planner error: %s", e, exc_info=True)

    async def refresh_prices(self) -> None:
        """Refresh price data from configured source."""
        try:
            await self.price_fetcher.get_prices_48h(force_refresh=True)
            logger.info("Prices refreshed")
        except Exception as e:
            logger.warning("Price refresh failed: %s", e)

    async def run_ev_strategy(self) -> None:
        """Evaluate EV charging strategy for tonight's window."""
        try:
            if not self.cfg.ev_charging_windows:
                return
            # Note: Currently evaluates first window only
            w = self.cfg.ev_charging_windows[0]
            now = datetime.now()
            try:
                start_h = int(w.available_from.split(":")[0])
                end_h = int(w.available_until.split(":")[0])
            except (ValueError, IndexError):
                logger.warning("Invalid time format in EV window, skipping strategy eval")
                return

            window_start = now.replace(hour=start_h, minute=0, second=0)
            if window_start < now:
                window_start += timedelta(days=1)
            window_end = window_start + timedelta(days=1)
            must_finish = window_start.replace(hour=end_h)
            if must_finish <= window_start:
                must_finish += timedelta(days=1)

            prices = await self.price_fetcher.get_prices_48h()
            pv = await self.pv_forecast.get_forecast_48h()
            state = self.current_state

            bat_soc = state.battery_soc_percent if state else 50.0
            ev_soc = state.ev_soc_percent if state else 20.0

            self.current_ev_strategy = self.ev_solver.solve(
                ev_soc=ev_soc or 20.0,
                battery_soc=bat_soc,
                price_forecast_ct=prices.total_ct,
                pv_forecast_w=pv.power_w,
                window_start=window_start,
                window_end=window_end,
                target_soc_percent=w.target_soc_percent,
                must_finish_by=must_finish,
            )

            best = self.current_ev_strategy.recommended
            recommended_strategy = next(
                (s for s in self.current_ev_strategy.strategies
                 if s.strategy_type == best), None
            )
            cost = recommended_strategy.total_cost_eur if recommended_strategy else 0.0

            await self.ha.publish_sensor("ev_strategy", best.value, "",
                                         {"cost_eur": cost})
            await self.ha.publish_sensor("ev_cost_tonight", round(cost, 2), "EUR")

        except Exception as e:
            logger.error("EV strategy evaluation error: %s", e, exc_info=True)

    async def check_balancing(self) -> None:
        """Check if battery balancing should start."""
        try:
            state = self.current_state
            if not state:
                return
            pv = await self.pv_forecast.get_forecast_48h()
            decision = self.balancer.should_balance_now(
                battery_soc=state.battery_soc_percent,
                pv_forecast_kwh_today=sum(pv.power_w[:24]) / 1000.0,
            )
            if decision.should_start:
                logger.info("Auto-starting balancing: %s", decision.reason)
                await self.balancer.start_balancing(decision.reason)
        except Exception as e:
            logger.error("Balancing check error: %s", e, exc_info=True)

    async def notify_cheap_window(self) -> None:
        """Notify user of upcoming cheap price windows."""
        try:
            if not self.cfg.notify_on_cheap_window:
                return
            prices = await self.price_fetcher.get_prices_48h()
            if prices.cheap_windows:
                next_window = prices.cheap_windows[0]
                # Only notify if window starts within next 3 hours
                minutes_until = int((next_window.start - datetime.now()).total_seconds() / 60)
                if 0 < minutes_until <= 180:
                    await self.ha.notify(
                        self.cfg.notify_target,
                        "Günstiger Strompreis kommt",
                        f"In {minutes_until} Minuten: Preis ∅ {next_window.avg_price_ct:.1f} ct/kWh "
                        f"bis {next_window.end.strftime('%H:%M')} Uhr",
                    )
        except Exception as e:
            logger.warning("Notification error: %s", e)

    def _add_history(self, state: EnergyState, actions) -> None:
        """Store hourly snapshot for history charts."""
        self._history.append({
            "ts": state.timestamp.isoformat(),
            "pv_w": state.pv_power_w,
            "bat_soc": state.battery_soc_percent,
            "grid_w": state.grid_power_w,
            "ev_w": state.ev_charge_power_w,
            "house_w": state.house_load_w,  # Store for load profile estimation
            "price_ct": state.price_total_ct_kwh,
            "savings_eur": actions.estimated_savings_eur,
        })
        # deque with maxlen=2880 auto-discards oldest entries

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _broadcast_state(self, state: EnergyState) -> None:
        if not self._ws_clients:
            return

        # Multi-EV status from wallbox instances
        ev_list = []
        wallboxes = self.realtime._get_wallboxes()
        for name, wb in wallboxes.items():
            try:
                st = await wb.get_status()
                if st:
                    ev_list.append({
                        "name": name,
                        "power_w": round(st.power_w, 0),
                        "current_a": st.current_a,
                        "car_state": st.car_state.value,
                        "session_kwh": round(st.energy_kwh_session, 2),
                    })
            except Exception as e:
                logger.debug("Failed to read wallbox status for '%s': %s", name, e)

        # Load decomposition summary
        decomp = None
        try:
            from data.load_decomposition import get_load_decomposer
            decomposer = get_load_decomposer()
            decomp = await decomposer.get_decomposition()
        except Exception as e:
            logger.debug("Load decomposition unavailable: %s", e)

        data = {
            "type": "state",
            "pv_w": round(state.pv_power_w, 0),
            "battery_soc": round(state.battery_soc_percent, 1),
            "battery_power_w": round(state.battery_power_w, 0),
            "grid_w": round(state.grid_power_w, 0),
            "surplus_w": round(state.surplus_w, 0),
            "house_load_w": round(state.house_load_w, 0),
            "ev_car_state": state.ev_car_state.value,
            "ev_power_w": round(state.ev_charge_power_w, 0),
            "ev_soc": state.ev_soc_percent,
            "ev_charge_current_a": state.ev_charge_current_a,
            "ev_session_kwh": round(state.ev_session_kwh, 2),
            "ev_list": ev_list,
            "price_ct": round(state.price_total_ct_kwh, 2),
            "is_balancing": state.battery_is_balancing,
            "read_only": self.cfg.read_only,
            "operation_mode": self.cfg.operation_mode,
            "optimizer_backend": self.cfg.optimizer_backend,
            "ts": state.timestamp.isoformat(),
        }
        if decomp:
            data["decomposition"] = {
                "total_w": round(decomp.get("total_power_w", 0), 0),
                "base_w": round(decomp.get("base_load_w", 0), 0),
                "controllable_w": round(decomp.get("controllable_total_w", 0), 0),
                "loads": {k: round(v, 0) for k, v in decomp.get("loads", {}).items()},
            }
        msg = json.dumps(data)
        dead = []
        for ws in list(self._ws_clients):  # Copy to avoid mutation during iteration
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._ws_clients:
                self._ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app_state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await app_state.ha.start()
    cfg = app_state.cfg

    # First-start auto-detection
    from config import CONFIG_JSON_FILE
    is_first_start = not CONFIG_JSON_FILE.exists()
    if is_first_start:
        logger.info("First start detected — running auto-configuration")
        try:
            detected = await _run_auto_detect()
            if detected:
                updates = {field: info["entity_id"] for field, info in detected.items()}
                updates["operation_mode"] = "stopped"
                updates["read_only"] = True
                from config import update_config as _update_cfg
                _update_cfg(updates)
                app_state.cfg = get_config()
                cfg = app_state.cfg
                logger.info("Auto-detected %d entities on first start", len(detected))
        except Exception as e:
            logger.warning("Auto-detection on first start failed: %s", e)
        app_state.is_first_start = True
    else:
        app_state.is_first_start = False

    # Register scheduler jobs
    scheduler = get_scheduler()
    setup_jobs(app_state)
    scheduler.start()

    # Validate config at startup
    if cfg.battery_capacity_kwh <= 0:
        logger.warning("battery_capacity_kwh is 0 — battery optimization disabled")
    if cfg.battery_min_soc >= 100:
        logger.error("battery_min_soc >= 100%% — battery can never discharge!")
    if cfg.goe_enabled and not cfg.goe_local_ip and cfg.goe_connection_type == "local":
        logger.warning("go-e enabled (local) but no IP configured")
    if cfg.price_source == "entso-e" and not cfg.entso_e_token:
        logger.warning("ENTSO-E selected but no token configured — will use fixed price")

    # Only run optimizations if operation_mode is "running"
    if cfg.operation_mode == "running":
        logger.info("Operation mode: running — starting optimizations")
        await app_state.refresh_prices()
        await app_state.run_linear_optimization()
        await app_state.run_genetic_planning()
        await app_state.run_ev_strategy()
    else:
        logger.info("Operation mode: stopped — skipping optimizations, pausing scheduler")
        pause_jobs()

    try:
        yield
    finally:
        # Shutdown
        scheduler.shutdown(wait=False)
        await app_state.ha.stop()
        logger.info("HA Energy Optimizer stopped")


app = FastAPI(
    title="HA Energy Optimizer",
    version="0.2.0",
    lifespan=lifespan,
)

# Serve static dashboard
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    """Health check endpoint for Docker HEALTHCHECK and monitoring."""
    state = app_state.current_state
    return {
        "status": "ok",
        "has_state": state is not None,
        "has_schedule": app_state.current_schedule is not None,
        "has_plan": app_state.current_plan is not None,
        "read_only": app_state.cfg.read_only,
        "uptime_samples": len(app_state._history),
        "ws_clients": len(app_state._ws_clients),
    }


@app.get("/")
async def dashboard():
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse("<h1>HA Energy Optimizer</h1><p>Dashboard not found.</p>")


@app.get("/api/state")
async def get_state():
    state = app_state.current_state
    if not state:
        return JSONResponse({"error": "No state available yet"}, status_code=503)
    return state.model_dump(mode="json")


@app.get("/api/schedule")
async def get_schedule():
    schedule = app_state.current_schedule
    if not schedule:
        return JSONResponse({"error": "No schedule computed yet"}, status_code=503)
    return schedule.model_dump(mode="json")


@app.get("/api/plan")
async def get_plan():
    plan = app_state.current_plan
    if not plan:
        return JSONResponse({"error": "No 48h plan computed yet"}, status_code=503)
    return plan.model_dump(mode="json")


@app.get("/api/prices")
async def get_prices():
    prices = await app_state.price_fetcher.get_prices_48h()
    return prices.model_dump(mode="json")


@app.get("/api/ev/strategy")
async def get_ev_strategy():
    strategy = app_state.current_ev_strategy
    if not strategy:
        return JSONResponse({"error": "No EV strategy computed yet"}, status_code=503)
    return strategy.model_dump(mode="json")


@app.post("/api/ev/mode")
async def set_ev_mode(body: dict):
    mode = body.get("mode", "smart")
    valid_modes = {"solar", "min_solar", "fast", "smart", "off"}
    if mode not in valid_modes:
        return JSONResponse(
            {"error": f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid_modes))}"},
            status_code=400,
        )
    cfg = get_config()
    cfg.ev_charge_mode = mode
    return {"status": "ok", "mode": mode}


def _log_task_exception(task: asyncio.Task) -> None:
    """Callback to log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Background task %s failed: %s", task.get_name(), exc, exc_info=exc)


@app.post("/api/optimize")
async def trigger_optimization():
    """Trigger immediate re-optimization."""
    t1 = asyncio.create_task(app_state.run_linear_optimization(), name="lp_optimize")
    t2 = asyncio.create_task(app_state.run_ev_strategy(), name="ev_strategy")
    t1.add_done_callback(_log_task_exception)
    t2.add_done_callback(_log_task_exception)
    return {"status": "triggered"}


@app.get("/api/mode")
async def get_mode():
    """Get current operation mode."""
    cfg = get_config()
    return {
        "read_only": cfg.read_only,
        "operation_mode": cfg.operation_mode,
        "is_first_start": getattr(app_state, "is_first_start", False),
        "mode": cfg.operation_mode,
    }


@app.post("/api/mode")
async def set_mode_api(body: dict):
    """Toggle read-only mode. Body: {"read_only": true/false}"""
    read_only = body.get("read_only", False)
    cfg = update_config({"read_only": read_only})
    mode = "read_only" if cfg.read_only else "active"
    logger.info("Read-only mode changed to: %s", mode)
    return {"status": "ok", "read_only": cfg.read_only, "mode": mode}


def _validate_config_internal() -> tuple[list[str], list[str]]:
    """Validate config and return (errors, warnings)."""
    cfg = get_config()
    warnings = []
    errors = []

    if not cfg.pv_power_sensor:
        errors.append("PV-Leistungssensor nicht konfiguriert")
    if not cfg.battery_soc_sensor:
        warnings.append("Batterie-SOC-Sensor nicht konfiguriert")
    if not cfg.grid_power_sensor:
        warnings.append("Netz-Leistungssensor nicht konfiguriert")
    if cfg.price_source == "epex_entity" and not cfg.epex_import_entity:
        errors.append("EPEX-Entity als Preisquelle gewählt aber keine Import-Entität konfiguriert")
    if cfg.price_source == "entso-e" and not cfg.entso_e_token:
        errors.append("ENTSO-E als Preisquelle gewählt aber kein API-Token konfiguriert")
    if cfg.price_source == "tibber" and not cfg.tibber_token:
        errors.append("Tibber als Preisquelle gewählt aber kein Token konfiguriert")
    if cfg.battery_capacity_kwh <= 0:
        warnings.append("Batteriekapazität ist 0 — Batterieoptimierung deaktiviert")
    if cfg.battery_min_soc >= 100:
        errors.append("Minimaler SOC >= 100% — Batterie kann nie entladen werden")
    if cfg.goe_enabled and not cfg.goe_local_ip and cfg.goe_connection_type == "local":
        warnings.append("go-e Charger aktiviert aber keine lokale IP konfiguriert")
    for dl in cfg.deferrable_loads:
        if not dl.switch:
            errors.append(f"Steuerbare Last '{dl.name}' hat keine Switch-Entität")
        if dl.power_w <= 0 and not dl.power_sensor:
            warnings.append(f"Steuerbare Last '{dl.name}' hat weder Leistung noch Sensor")
    if any(dl.subtract_from_total for dl in cfg.deferrable_loads) and not cfg.total_power_sensor:
        warnings.append("Lastzerlegung aktiviert aber kein Gesamtverbrauch-Sensor konfiguriert")
    if cfg.optimizer_backend == "emhass" and not is_emhass_available():
        warnings.append("EMHASS als Backend gewählt aber nicht installiert — Fallback auf eingebauten LP")

    return errors, warnings


@app.post("/api/mode/start")
async def start_optimizer():
    """Start optimizer: validate config first, then enable scheduler."""
    errors, warnings = _validate_config_internal()
    if errors:
        return JSONResponse({
            "status": "error",
            "message": "Konfiguration enthält Fehler — Start nicht möglich",
            "errors": errors,
            "warnings": warnings,
        }, status_code=400)

    cfg = update_config({"operation_mode": "running"})
    app_state.cfg = cfg
    resume_jobs()

    # Run initial optimizations
    asyncio.create_task(app_state.refresh_prices(), name="initial_prices")
    asyncio.create_task(app_state.run_linear_optimization(), name="initial_lp")
    asyncio.create_task(app_state.run_genetic_planning(), name="initial_genetic")
    asyncio.create_task(app_state.run_ev_strategy(), name="initial_ev")

    logger.info("Optimizer started — all jobs resumed")
    return {
        "status": "ok",
        "operation_mode": "running",
        "warnings": warnings,
    }


@app.post("/api/mode/stop")
async def stop_optimizer():
    """Stop optimizer: pause all scheduler jobs."""
    cfg = update_config({"operation_mode": "stopped"})
    app_state.cfg = cfg
    pause_jobs()
    logger.info("Optimizer stopped — all jobs paused")
    return {"status": "ok", "operation_mode": "stopped"}


@app.post("/api/balance/start")
async def start_balancing():
    await app_state.balancer.start_balancing("manual via API")
    return {"status": "started"}


@app.post("/api/balance/stop")
async def stop_balancing():
    await app_state.balancer.stop_balancing()
    return {"status": "stopped"}


@app.get("/api/history")
async def get_history(hours: int = Query(24, ge=1, le=168)):
    cutoff_count = hours * 120  # 30s intervals → 120 per hour
    return {"history": list(app_state._history)[-cutoff_count:]}


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def get_config_api():
    """Return current configuration as JSON."""
    cfg = get_config()
    data = {}
    for f in cfg.__dataclass_fields__:
        val = getattr(cfg, f)
        if isinstance(val, list) and val and hasattr(val[0], '__dataclass_fields__'):
            data[f] = [
                {k: getattr(item, k) for k in item.__dataclass_fields__}
                for item in val
            ]
        else:
            data[f] = val
    # Remove sensitive fields from response, but indicate if they are set
    data.pop("supervisor_token", None)
    data["_has_goe_cloud_token"] = bool(cfg.goe_cloud_token)
    data.pop("goe_cloud_token", None)
    data["_has_tibber_token"] = bool(cfg.tibber_token)
    data.pop("tibber_token", None)
    data["_has_entso_e_token"] = bool(cfg.entso_e_token)
    data.pop("entso_e_token", None)
    # Add runtime info
    data["_emhass_available"] = is_emhass_available()
    data["_optimizer_active"] = "emhass" if (cfg.optimizer_backend == "emhass" and is_emhass_available()) else "builtin"
    return data


@app.post("/api/config")
async def update_config_api(body: dict):
    """Update configuration. Accepts partial updates."""
    # Don't allow updating sensitive runtime fields
    body.pop("supervisor_token", None)
    body.pop("ha_url", None)
    cfg = update_config(body)
    app_state.cfg = cfg
    return {"status": "ok", "message": "Config updated"}


@app.get("/api/config/validate")
async def validate_config():
    """Validate current configuration and return warnings/errors."""
    errors, warnings = _validate_config_internal()
    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Auto-Detection API
# ---------------------------------------------------------------------------

# Mapping: config_field -> (domain, device_classes, keywords_in_entity_id)
_AUTO_DETECT_RULES: list[tuple[str, str, list[str], list[str]]] = [
    ("pv_power_sensor", "sensor", ["power", "energy"],
     ["solar", "pv", "photovoltaic", "solarpower", "pv_power"]),
    ("battery_soc_sensor", "sensor", ["battery"],
     ["battery_soc", "akku_soc", "battery_state_of_charge", "battery_level",
      "batt_soc", "speicher_soc"]),
    ("battery_power_sensor", "sensor", ["power"],
     ["battery_power", "akku_power", "battery_charging_power",
      "speicher_leistung", "batt_power"]),
    ("battery_charge_switch", "switch", [],
     ["battery_charge", "akku_laden", "battery_grid_charging",
      "batt_charge", "speicher_laden"]),
    ("battery_discharge_switch", "switch", [],
     ["battery_discharge", "akku_entladen", "batt_discharge",
      "speicher_entladen"]),
    ("grid_power_sensor", "sensor", ["power"],
     ["grid_power", "netz", "meter_power", "stromzaehler", "grid_import",
      "netzbezug", "house_meter"]),
    ("total_power_sensor", "sensor", ["power"],
     ["total_power", "house_power", "gesamtverbrauch", "hausverbrauch",
      "total_consumption", "load_power"]),
    ("ev_soc_sensor", "sensor", ["battery"],
     ["ev_battery", "car_soc", "fahrzeug", "ev_soc", "vehicle_battery",
      "auto_soc", "car_battery"]),
    ("epex_import_entity", "sensor", ["monetary"],
     ["epex_spot", "nordpool", "electricity_price", "strompreis",
      "energy_price", "spot_price"]),
    ("price_sensor_entity", "sensor", ["monetary"],
     ["electricity_price", "strompreis", "energy_price", "power_price",
      "current_price"]),
]


def _match_entity(entity: dict, domain: str, device_classes: list[str],
                   keywords: list[str]) -> tuple[bool, str]:
    """Score an entity against detection rules. Returns (matched, confidence)."""
    eid = entity.get("entity_id", "")
    attrs = entity.get("attributes", {})
    dc = attrs.get("device_class", "")
    fname = (attrs.get("friendly_name", "") or "").lower()

    if not eid.startswith(domain + "."):
        return False, ""

    eid_lower = eid.lower()
    kw_match = any(kw in eid_lower or kw in fname for kw in keywords)
    dc_match = dc in device_classes if device_classes else False

    if kw_match and dc_match:
        return True, "high"
    if kw_match:
        return True, "medium"
    if dc_match and device_classes:
        return True, "low"
    return False, ""


async def _run_auto_detect() -> dict:
    """Run auto-detection logic, returns dict of field_name -> suggestion info."""
    all_states = await app_state.ha.get_all_states()
    suggestions = {}

    for field_name, domain, device_classes, keywords in _AUTO_DETECT_RULES:
        best = None
        best_conf_rank = 0  # high=3, medium=2, low=1
        conf_ranks = {"high": 3, "medium": 2, "low": 1}

        for entity in all_states:
            matched, confidence = _match_entity(
                entity, domain, device_classes, keywords)
            if matched and conf_ranks.get(confidence, 0) > best_conf_rank:
                attrs = entity.get("attributes", {})
                unit = attrs.get("unit_of_measurement", "")
                best = {
                    "entity_id": entity["entity_id"],
                    "confidence": confidence,
                    "friendly_name": attrs.get("friendly_name", ""),
                    "current_value": f"{entity.get('state', '')} {unit}".strip(),
                }
                best_conf_rank = conf_ranks[confidence]

        if best:
            suggestions[field_name] = best

    return suggestions


@app.get("/api/config/auto-detect")
async def auto_detect_entities():
    """Auto-detect HA entities for configuration fields."""
    suggestions = await _run_auto_detect()
    return {
        "suggestions": suggestions,
        "found_count": len(suggestions),
        "total_fields": len(_AUTO_DETECT_RULES),
    }


# ---------------------------------------------------------------------------
# HA Entity API (for entity picker in GUI)
# ---------------------------------------------------------------------------

@app.get("/api/ha/entities")
async def get_ha_entities(domain: str = ""):
    """List all HA entities, optionally filtered by domain."""
    entities = await app_state.ha.get_entities_by_domain(domain)
    return {"entities": entities}


@app.get("/api/ha/entity/{entity_id:path}")
async def get_ha_entity_state(entity_id: str):
    """Get current state of a specific HA entity."""
    import re
    if not re.match(r"^[a-z_]+\.[a-z0-9_]+$", entity_id):
        return JSONResponse({"error": "Invalid entity_id format"}, status_code=400)
    state = await app_state.ha.get_state(entity_id)
    if not state:
        return JSONResponse({"error": "Entity not found"}, status_code=404)
    return state


# ---------------------------------------------------------------------------
# Deferrable Loads API
# ---------------------------------------------------------------------------

@app.get("/api/config/loads")
async def get_loads():
    """List all configured deferrable loads."""
    cfg = get_config()
    loads = []
    for i, dl in enumerate(cfg.deferrable_loads):
        loads.append({
            "index": i,
            **{k: getattr(dl, k) for k in dl.__dataclass_fields__},
        })
    return {"loads": loads}


@app.post("/api/config/loads")
async def add_load(body: dict):
    """Add a new deferrable load."""
    cfg = get_config()
    from config import DeferrableLoad
    try:
        load = DeferrableLoad(**body)
        cfg.deferrable_loads.append(load)
        save_config(cfg)
        return {"status": "ok", "index": len(cfg.deferrable_loads) - 1}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.put("/api/config/loads/{index}")
async def update_load(index: int, body: dict):
    """Update a deferrable load by index."""
    cfg = get_config()
    if index < 0 or index >= len(cfg.deferrable_loads):
        return JSONResponse({"error": "Invalid index"}, status_code=404)
    from config import DeferrableLoad
    try:
        cfg.deferrable_loads[index] = DeferrableLoad(**body)
        save_config(cfg)
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/config/loads/{index}")
async def delete_load(index: int):
    """Delete a deferrable load by index."""
    cfg = get_config()
    if index < 0 or index >= len(cfg.deferrable_loads):
        return JSONResponse({"error": "Invalid index"}, status_code=404)
    cfg.deferrable_loads.pop(index)
    save_config(cfg)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# EV / Wallbox Config API
# ---------------------------------------------------------------------------

@app.get("/api/config/evs")
async def get_evs():
    """List all configured EV charging windows."""
    cfg = get_config()
    evs = []
    for i, w in enumerate(cfg.ev_charging_windows):
        evs.append({
            "index": i,
            **{k: getattr(w, k) for k in w.__dataclass_fields__},
        })
    return {"evs": evs}


@app.post("/api/config/evs")
async def add_ev(body: dict):
    """Add a new EV charging window."""
    cfg = get_config()
    from config import EVChargingWindow
    try:
        window = EVChargingWindow(**body)
        cfg.ev_charging_windows.append(window)
        save_config(cfg)
        return {"status": "ok", "index": len(cfg.ev_charging_windows) - 1}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.put("/api/config/evs/{index}")
async def update_ev(index: int, body: dict):
    """Update an EV charging window by index."""
    cfg = get_config()
    if index < 0 or index >= len(cfg.ev_charging_windows):
        return JSONResponse({"error": "Invalid index"}, status_code=404)
    from config import EVChargingWindow
    try:
        cfg.ev_charging_windows[index] = EVChargingWindow(**body)
        save_config(cfg)
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/config/evs/{index}")
async def delete_ev(index: int):
    """Delete an EV charging window by index."""
    cfg = get_config()
    if index < 0 or index >= len(cfg.ev_charging_windows):
        return JSONResponse({"error": "Invalid index"}, status_code=404)
    cfg.ev_charging_windows.pop(index)
    save_config(cfg)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# EV/Wallbox Configuration API (Multi-EV)
# ---------------------------------------------------------------------------

@app.get("/api/config/ev-configs")
async def get_ev_configs():
    """List all configured EV/wallbox pairs."""
    cfg = get_config()
    evs = []
    for i, ev in enumerate(cfg.ev_configs):
        evs.append({
            "index": i,
            **{k: getattr(ev, k) for k in ev.__dataclass_fields__},
        })
    return {"ev_configs": evs}


@app.post("/api/config/ev-configs")
async def add_ev_config(body: dict):
    """Add a new EV/wallbox configuration."""
    cfg = get_config()
    from config import EVConfig
    try:
        ev = EVConfig(**body)
        cfg.ev_configs.append(ev)
        save_config(cfg)
        return {"status": "ok", "index": len(cfg.ev_configs) - 1}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.put("/api/config/ev-configs/{index}")
async def update_ev_config(index: int, body: dict):
    """Update an EV/wallbox configuration by index."""
    cfg = get_config()
    if index < 0 or index >= len(cfg.ev_configs):
        return JSONResponse({"error": "Invalid index"}, status_code=404)
    from config import EVConfig
    try:
        cfg.ev_configs[index] = EVConfig(**body)
        save_config(cfg)
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/config/ev-configs/{index}")
async def delete_ev_config(index: int):
    """Delete an EV/wallbox configuration by index."""
    cfg = get_config()
    if index < 0 or index >= len(cfg.ev_configs):
        return JSONResponse({"error": "Invalid index"}, status_code=404)
    cfg.ev_configs.pop(index)
    save_config(cfg)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Load Decomposition API
# ---------------------------------------------------------------------------

@app.get("/api/load-decomposition")
async def get_load_decomposition():
    """Get current load decomposition (base load vs controllable loads)."""
    from data.load_decomposition import get_load_decomposer
    decomposer = get_load_decomposer()
    return await decomposer.get_decomposition()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    
    # Enforce client limit to prevent memory leak
    if len(app_state._ws_clients) >= app_state._ws_client_limit:
        logger.warning("WebSocket client limit reached (%d), closing oldest connection", 
                      app_state._ws_client_limit)
        try:
            oldest = app_state._ws_clients.pop(0)
            await oldest.close()
        except Exception as e:
            logger.debug("Error closing oldest WebSocket: %s", e)
    
    app_state._ws_clients.append(ws)
    try:
        # Send current state immediately on connect
        if app_state.current_state:
            await app_state._broadcast_state(app_state.current_state)
        while True:
            await ws.receive_text()  # Keep alive, handle pings
    except WebSocketDisconnect:
        if ws in app_state._ws_clients:
            app_state._ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,
    )
