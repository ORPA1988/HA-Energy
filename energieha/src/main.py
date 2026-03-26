"""EnergieHA main loop: orchestrates collect → plan → execute → publish."""

import logging
import signal
import sys
import time

from .collector import Collector
from .config import load_config
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

    logger.info("=== EnergieHA v0.1.0 starting ===")

    # Load configuration
    config = load_config()
    logger.info("Strategy: %s | Cycle: %ds | Slots: %dmin | Battery: %.0f kWh (SOC %d%%–%d%%)",
                config.strategy, config.cycle_seconds, config.slot_duration_min,
                config.battery_capacity_kwh, config.min_soc_percent, config.max_soc_percent)

    if config.dry_run:
        logger.info("*** DRY RUN MODE – no control commands will be sent ***")

    # Initialize components
    client = HaClient()
    collector = Collector(client, config)
    executor = Executor(client, config)
    publisher = EntityPublisher(client, config)

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

    # Main loop
    cycle = 0
    while _running:
        cycle += 1
        cycle_start = time.monotonic()

        try:
            _run_cycle(collector, executor, publisher, config, cycle)
        except Exception as e:
            logger.error("Cycle %d failed: %s", cycle, e, exc_info=True)

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


def _run_cycle(collector, executor, publisher, config, cycle_num):
    """Execute one planning cycle."""
    # 1. Collect data
    snapshot = collector.get_snapshot()
    if snapshot is None:
        logger.warning("Cycle %d: No snapshot available, skipping", cycle_num)
        return

    prices = collector.get_prices()
    pv_forecast = collector.get_pv_forecast()

    logger.info("Cycle %d: SOC=%.1f%% PV=%.0fW Load=%.0fW Grid=%.0fW | "
                "Prices=%d Forecast=%d",
                cycle_num, snapshot.battery_soc, snapshot.pv_power_w,
                snapshot.load_power_w, snapshot.grid_power_w,
                len(prices), len(pv_forecast))

    # 2. Create plan
    plan = create_plan(snapshot, prices, pv_forecast, config)

    # 3. Execute current slot
    executor.execute(plan)

    # 4. Publish plan entities
    publisher.publish(plan, snapshot)


if __name__ == "__main__":
    main()
