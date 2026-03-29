"""EnergieHA main loop: orchestrates collect -> plan -> execute -> publish.

v1.0.0: Runs planning loop as background thread, Flask web server as main thread.
"""

import logging
import signal
import sys
import threading
import time

from . import __version__
from .collector import Collector
from .config import load_config, validate_config
from .models import Config
from .entities import EntityPublisher
from .executor import Executor
from .ha_client import HaClient
from .planner import create_plan
from .state import AppState, CycleSummary

# Compact log format: timestamp, level, module, message
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("energieha")


def _handle_signal(signum, frame):
    logger.info("Received signal %d, shutting down...", signum)
    AppState().running = False


def planning_loop():
    """Background thread: runs the energy management planning loop."""
    state = AppState()

    logger.info("=== EnergieHA v%s planning loop starting ===", __version__)

    # Load configuration
    try:
        config = load_config()
    except Exception as e:
        logger.error("Failed to load config: %s", e, exc_info=True)
        config = Config()

    state.config = config

    # Restore persistent state from last run
    state.load_state()

    logger.info("Strategy: %s | Cycle: %ds | Slots: %dmin | Battery: %.0f kWh (SOC %d%%-%d%%)",
                config.strategy, config.cycle_seconds, config.slot_duration_min,
                config.battery_capacity_kwh, config.min_soc_percent, config.max_soc_percent)

    if not validate_config(config):
        logger.warning("Config validation failed - using values as-is, check settings")

    if config.dry_run:
        logger.info("*** DRY RUN MODE - no control commands will be sent ***")

    if config.direct_control:
        logger.info("*** DIRECT CONTROL MODE - inverter will be controlled directly ***")

    # Initialize components
    client = HaClient()
    logger.info("HA client: %s", client._base_url)

    # Wait for HA to be available
    for attempt in range(10):
        if client.is_available():
            logger.info("Home Assistant API is reachable")
            break
        logger.warning("HA API not available (attempt %d/10), waiting 10s...", attempt + 1)
        time.sleep(10)
    else:
        logger.error("Could not reach HA API after 10 attempts.")
        state.add_error("Could not reach HA API after 10 attempts")
        return

    # Publish startup heartbeat
    try:
        client.set_state("sensor.energieha_status", "starting", {
            "friendly_name": "EnergieHA Status",
            "icon": "mdi:battery-sync",
            "version": __version__,
        })
    except Exception as e:
        logger.error("Failed to publish heartbeat: %s", e)

    # Fetch timezone from HA config
    ha_cfg = client.get_ha_config()
    config.timezone = ha_cfg.get("time_zone", "Europe/Vienna")
    logger.info("Timezone: %s", config.timezone)

    # Validate critical entities
    for entity_id in [config.entity_battery_soc, config.entity_pv_power,
                       config.entity_epex_prices]:
        entity_state = client.get_state(entity_id)
        if entity_state is None:
            logger.warning("Entity %s not found - check config", entity_id)
        else:
            logger.info("Entity %s: OK (%s)", entity_id, entity_state.get("state", "?"))

    collector = Collector(client, config)
    executor = Executor(client, config)
    publisher = EntityPublisher(client, config)

    # Sungrow TOU adapter (optional)
    tou_adapter = None
    if config.sungrow_tou_enabled:
        from .sungrow_tou import SungrowTouAdapter
        tou_adapter = SungrowTouAdapter(client, config)
        logger.info("Sungrow TOU adapter enabled")

    # Inverter controller (optional)
    inverter_ctrl = None
    if config.direct_control:
        from .inverter_control import InverterController
        inverter_ctrl = InverterController(client, config)
        logger.info("Direct inverter control enabled")

    # Main loop with circuit-breaker
    cycle = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 3

    while state.running:
        cycle += 1
        cycle_start = time.monotonic()

        # Check for forced replan
        force_replan = getattr(state, '_force_replan', False)
        if force_replan:
            state._force_replan = False
            logger.info("Forced replan triggered")

        try:
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.warning("SAFE MODE: %d consecutive failures - publishing idle only",
                               consecutive_failures)
                executor._publish_idle()
            else:
                _run_cycle(collector, executor, publisher, config, cycle,
                           tou_adapter, inverter_ctrl, state)
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.error("Cycle %d failed (%d/%d): %s", cycle,
                         consecutive_failures, MAX_CONSECUTIVE_FAILURES,
                         e, exc_info=True)
            state.add_error(f"Cycle {cycle}: {e}")
            try:
                client.set_state("sensor.energieha_status", "error", {
                    "friendly_name": "EnergieHA Status",
                    "icon": "mdi:alert-circle",
                    "error": str(e),
                    "cycle": cycle,
                    "consecutive_failures": consecutive_failures,
                    "version": __version__,
                })
            except Exception:
                pass

        # Sleep until next cycle
        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0, config.cycle_seconds - elapsed)
        if sleep_time > 0 and state.running:
            logger.debug("Cycle %d done in %.1fs, sleeping %.0fs",
                         cycle, elapsed, sleep_time)
            end_time = time.monotonic() + sleep_time
            while state.running and time.monotonic() < end_time:
                # Wake up early if replan requested
                if getattr(state, '_force_replan', False):
                    break
                time.sleep(min(5.0, end_time - time.monotonic()))

    logger.info("=== EnergieHA planning loop stopped ===")


def _run_cycle(collector, executor, publisher, config, cycle_num,
               tou_adapter=None, inverter_ctrl=None, state=None):
    """Execute one planning cycle."""
    from datetime import datetime

    # 1. Collect data
    snapshot = collector.get_snapshot()
    if snapshot is None:
        logger.warning("Cycle %d: No snapshot available, skipping", cycle_num)
        return

    prices = collector.get_prices()
    pv_forecast = collector.get_pv_forecast()
    sunrise_hour, sunset_hour = collector.get_sun_times()

    # Update average load from history (once per hour, not every 5min cycle)
    if cycle_num == 1 or cycle_num % 12 == 0:  # Every 12 cycles = 1 hour
        avg_load = collector.get_average_load_w(days=7)
        if avg_load > 0:
            config.estimated_daily_load_kwh = avg_load * 24 / 1000.0
            logger.info("Updated estimated daily load: %.1f kWh (%.0f W avg from 7d history)",
                        config.estimated_daily_load_kwh, avg_load)

    logger.info("Cycle %d: SOC=%.1f%% PV=%.0fW Load=%.0fW Grid=%.0fW GridCharge=%.0fW | "
                "Prices=%d Forecast=%d | Sun %d:00-%d:00",
                cycle_num, snapshot.battery_soc, snapshot.pv_power_w,
                snapshot.load_power_w, snapshot.grid_power_w,
                snapshot.grid_charge_power_w,
                len(prices), len(pv_forecast), sunrise_hour, sunset_hour)

    # 2. Create plan
    plan = create_plan(snapshot, prices, pv_forecast, config,
                       sunrise_hour=sunrise_hour, sunset_hour=sunset_hour)

    # 3. Execute current slot (publish sensor entities)
    executor.execute(plan)

    # 4. Apply Sungrow TOU programs (if enabled)
    if tou_adapter is not None:
        tou_adapter.apply(plan, snapshot)

    # 5. Publish TOU explanation to status entity
    if tou_adapter is not None and hasattr(tou_adapter, "last_tou_reason"):
        plan._tou_reason = getattr(tou_adapter, "last_tou_reason", "")

    # 6. Publish plan entities
    publisher.publish(plan, snapshot)

    # 7. Update shared state for web GUI
    if state:
        state.plan = plan
        state.snapshot = snapshot
        # Cache prices and forecast for web GUI charts
        state.prices = [{"start": p.start.isoformat(), "end": p.end.isoformat(),
                         "price": p.price_eur_kwh} for p in prices] if prices else []
        state.pv_forecast = [{"start": f.start.isoformat(), "end": f.end.isoformat(),
                              "power_w": f.power_w, "power_w_10": getattr(f, 'power_w_10', 0),
                              "power_w_90": getattr(f, 'power_w_90', 0)} for f in pv_forecast] if pv_forecast else []
        slot = plan.current_slot
        state.add_cycle(CycleSummary(
            timestamp=datetime.now(),
            strategy=plan.strategy,
            battery_soc=snapshot.battery_soc,
            battery_mode=slot.planned_battery_mode if slot else "idle",
            pv_power_w=snapshot.pv_power_w,
            grid_power_w=snapshot.grid_power_w,
            load_power_w=snapshot.load_power_w,
        ))
        # Update EMHASS health status
        if plan.strategy == "emhass":
            state.emhass_available = True
            state.emhass_last_ok = datetime.now()
        elif config.strategy == "emhass" and hasattr(plan, 'strategy_error'):
            state.emhass_available = False

        # Record daily statistics
        state.record_daily_stats(snapshot, plan)

        # Save state to disk (survives addon restart)
        state.save_state()


def main():
    """Main entry point: starts planning loop thread + Flask web server."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("=== EnergieHA v%s starting ===", __version__)
    logger.info("Python %s", sys.version)

    state = AppState()

    # Start planning loop in background thread
    loop_thread = threading.Thread(target=planning_loop, name="planning-loop", daemon=True)
    loop_thread.start()
    logger.info("Planning loop thread started")

    # Start Flask web server in main thread
    try:
        from .web.app import start_server
        start_server()
    except ImportError:
        logger.warning("Flask not available, running planning loop only")
        loop_thread.join()
    except Exception as e:
        logger.error("Web server failed: %s", e, exc_info=True)
        # Keep planning loop running even if web server fails
        loop_thread.join()

    state.running = False
    logger.info("=== EnergieHA stopped ===")


if __name__ == "__main__":
    main()
