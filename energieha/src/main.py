"""EnergieHA main loop: orchestrates collect → plan → execute → publish."""

import logging
import signal
import sys
import time

from . import __version__
from .collector import Collector
from .config import load_config
from .models import Config
from .entities import EntityPublisher
from .executor import Executor
from .ha_client import HaClient
from .planner import create_plan

# Compact log format: timestamp, level, module, message
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("energieha")

# Global flag for graceful shutdown
_running = True


def _handle_signal(signum, frame):
    global _running
    logger.info("Received signal %d, shutting down...", signum)
    _running = False


def main():
    """Main entry point for EnergieHA."""
    global _running

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("=== EnergieHA v%s starting ===", __version__)
    logger.info("Python %s", sys.version)

    # Load configuration
    try:
        config = load_config()
    except Exception as e:
        logger.error("Failed to load config: %s", e, exc_info=True)
        config = Config()

    logger.info("Strategy: %s | Cycle: %ds | Slots: %dmin | Battery: %.0f kWh (SOC %d%%–%d%%)",
                config.strategy, config.cycle_seconds, config.slot_duration_min,
                config.battery_capacity_kwh, config.min_soc_percent, config.max_soc_percent)

    if config.dry_run:
        logger.info("*** DRY RUN MODE – no control commands will be sent ***")

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
        logger.error("Could not reach HA API after 10 attempts. Exiting.")
        sys.exit(1)

    # Publish startup heartbeat so we can see the add-on is alive
    try:
        client.set_state("sensor.energieha_status", "starting", {
            "friendly_name": "EnergieHA Status",
            "icon": "mdi:battery-sync",
            "version": __version__,
        })
        logger.info("Heartbeat entity published")
    except Exception as e:
        logger.error("Failed to publish heartbeat: %s", e)

    # Fetch timezone from HA config
    ha_cfg = client.get_ha_config()
    config.timezone = ha_cfg.get("time_zone", "Europe/Vienna")
    logger.info("Timezone: %s", config.timezone)

    # Validate critical entities
    for entity_id in [config.entity_battery_soc, config.entity_pv_power,
                       config.entity_epex_prices]:
        state = client.get_state(entity_id)
        if state is None:
            logger.warning("Entity %s not found — check config", entity_id)
        else:
            logger.info("Entity %s: OK (%s)", entity_id, state.get("state", "?"))

    collector = Collector(client, config)
    executor = Executor(client, config)
    publisher = EntityPublisher(client, config)

    # Sungrow TOU adapter (optional)
    tou_adapter = None
    if config.sungrow_tou_enabled:
        from .sungrow_tou import SungrowTouAdapter
        tou_adapter = SungrowTouAdapter(client, config)
        logger.info("Sungrow TOU adapter enabled")

    # Main loop
    cycle = 0
    while _running:
        cycle += 1
        cycle_start = time.monotonic()

        try:
            _run_cycle(collector, executor, publisher, config, cycle, tou_adapter)
        except Exception as e:
            logger.error("Cycle %d failed: %s", cycle, e, exc_info=True)
            # Write error to status entity so it's visible in HA
            try:
                client.set_state("sensor.energieha_status", "error", {
                    "friendly_name": "EnergieHA Status",
                    "icon": "mdi:alert-circle",
                    "error": str(e),
                    "cycle": cycle,
                    "version": __version__,
                })
            except Exception:
                pass

        # Sleep until next cycle
        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0, config.cycle_seconds - elapsed)
        if sleep_time > 0 and _running:
            logger.debug("Cycle %d done in %.1fs, sleeping %.0fs",
                         cycle, elapsed, sleep_time)
            # Sleep in small increments to respond to signals promptly
            end_time = time.monotonic() + sleep_time
            while _running and time.monotonic() < end_time:
                time.sleep(min(5.0, end_time - time.monotonic()))

    logger.info("=== EnergieHA stopped ===")


def _run_cycle(collector, executor, publisher, config, cycle_num, tou_adapter=None):
    """Execute one planning cycle."""
    # 1. Collect data
    snapshot = collector.get_snapshot()
    if snapshot is None:
        logger.warning("Cycle %d: No snapshot available, skipping", cycle_num)
        return

    prices = collector.get_prices()
    pv_forecast = collector.get_pv_forecast()
    sunrise_hour, sunset_hour = collector.get_sun_times()

    logger.info("Cycle %d: SOC=%.1f%% PV=%.0fW Load=%.0fW Grid=%.0fW | "
                "Prices=%d Forecast=%d | Sun %d:00-%d:00",
                cycle_num, snapshot.battery_soc, snapshot.pv_power_w,
                snapshot.load_power_w, snapshot.grid_power_w,
                len(prices), len(pv_forecast), sunrise_hour, sunset_hour)

    # 2. Create plan
    plan = create_plan(snapshot, prices, pv_forecast, config,
                       sunrise_hour=sunrise_hour, sunset_hour=sunset_hour)

    # 3. Execute current slot (publish sensor entities)
    executor.execute(plan)

    # 4. Apply Sungrow TOU programs (if enabled)
    if tou_adapter is not None:
        tou_adapter.apply(plan, snapshot)

    # 5. Publish plan entities
    publisher.publish(plan, snapshot)


if __name__ == "__main__":
    main()
