"""HA Energy Optimizer — FastAPI application entry point."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Add app directory to path
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from config import get_config
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
from optimizer.linear import get_linear_optimizer
from optimizer.realtime import get_realtime_controller
from scheduler import get_scheduler, setup_jobs


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
        self.lp = get_linear_optimizer()
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
        self._history: list[dict] = []  # Last 24h snapshots

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _estimate_house_load_profile(self) -> list[float]:
        """
        Estimate 24h house load profile from recent history.
        
        Uses last 7 days of history to compute hourly average load.
        Falls back to sensible defaults if insufficient data.
        """
        if len(self._history) < 240:  # Need at least 2 hours of data (240 * 30s)
            # Fallback: typical household consumption pattern
            # Higher during day (600-800W), lower at night (300-400W)
            return [
                400, 350, 350, 350, 400, 500,  # 00-05: Night (low)
                600, 700, 750, 700, 650, 600,  # 06-11: Morning peak, then moderate
                550, 600, 650, 700, 800, 850,  # 12-17: Afternoon rise
                900, 850, 750, 650, 550, 450,  # 18-23: Evening peak, then decline
            ]
        
        # Group history by hour of day and calculate average load
        hourly_loads = [[] for _ in range(24)]
        for entry in self._history:
            try:
                ts = datetime.fromisoformat(entry["ts"])
                hour = ts.hour
                # Approximate house load from history (grid + battery discharge - battery charge)
                # This is a simplification; the actual calculation is in collector.py
                house_w = entry.get("house_w", 500.0)  # May not be stored
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

            # Merge go-e data
            goe_status = await self.goe.get_status()
            if goe_status:
                state.ev_car_state = goe_status.car_state
                state.ev_charge_power_w = goe_status.power_w
                state.ev_session_kwh = goe_status.energy_kwh_session
                state.ev_charge_current_a = goe_status.current_a
                await self.goe.publish_to_ha(goe_status)

            # Balancing tick
            await self.balancer.tick(state.battery_soc_percent)
            state.balancing_status = self.balancer.current_state
            state.is_balancing = self.balancer.is_active

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

            bat_soc = state.battery_soc_percent if state else 50.0
            ev_soc = state.ev_soc_percent if state else None

            target_soc = 80
            departure_h = 7
            if self.cfg.ev_charging_windows:
                # Note: Currently only first window is used
                # Multi-window support requires LP/genetic algorithm enhancement
                w = self.cfg.ev_charging_windows[0]
                target_soc = w.target_soc_percent
                departure_h = int(w.must_finish_by.split(":")[0])
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
                goal=OptimizationGoal(self.cfg.optimization_goal),
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
            start_h = int(w.available_from.split(":")[0])
            end_h = int(w.available_until.split(":")[0])

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
        # Keep last 24h = 2880 entries at 30s interval
        if len(self._history) > 2880:
            self._history = self._history[-2880:]

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _broadcast_state(self, state: EnergyState) -> None:
        if not self._ws_clients:
            return
        msg = json.dumps({
            "type": "state",
            "pv_w": round(state.pv_power_w, 0),
            "battery_soc": round(state.battery_soc_percent, 1),
            "grid_w": round(state.grid_power_w, 0),
            "surplus_w": round(state.surplus_w, 0),
            "ev_car_state": state.ev_car_state.value,
            "ev_power_w": round(state.ev_charge_power_w, 0),
            "ev_soc": state.ev_soc_percent,
            "price_ct": round(state.price_total_ct_kwh, 2),
            "is_balancing": state.is_balancing,
            "ts": state.timestamp.isoformat(),
        })
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app_state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await app_state.ha.start()
    scheduler = get_scheduler()
    setup_jobs(app_state)
    scheduler.start()

    # Run initial optimizations
    await app_state.refresh_prices()
    await app_state.run_linear_optimization()
    await app_state.run_genetic_planning()
    await app_state.run_ev_strategy()

    yield

    # Shutdown
    scheduler.shutdown()
    await app_state.ha.stop()


app = FastAPI(
    title="HA Energy Optimizer",
    version="1.0.0",
    lifespan=lifespan,
)

# Serve static dashboard
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

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
    cfg = get_config()
    cfg.ev_charge_mode = mode
    return {"status": "ok", "mode": mode}


@app.post("/api/optimize")
async def trigger_optimization():
    """Trigger immediate re-optimization."""
    asyncio.create_task(app_state.run_linear_optimization())
    asyncio.create_task(app_state.run_ev_strategy())
    return {"status": "triggered"}


@app.post("/api/balance/start")
async def start_balancing():
    await app_state.balancer.start_balancing("manual via API")
    return {"status": "started"}


@app.post("/api/balance/stop")
async def stop_balancing():
    await app_state.balancer.stop_balancing()
    return {"status": "stopped"}


@app.get("/api/history")
async def get_history(hours: int = 24):
    cutoff_count = hours * 120  # 30s intervals → 120 per hour
    return {"history": app_state._history[-cutoff_count:]}


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
        except Exception:
            pass
    
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
