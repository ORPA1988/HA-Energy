"""Planning dispatcher: selects and runs the configured strategy."""

import logging

from .models import Config, ForecastPoint, Plan, PricePoint, Snapshot
from .strategies.forecast import plan_forecast
from .strategies.price import plan_price_optimized
from .strategies.surplus import plan_surplus

logger = logging.getLogger(__name__)

STRATEGIES = {
    "surplus": plan_surplus,
    "price": plan_price_optimized,
    "forecast": plan_forecast,
}


def create_plan(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
) -> Plan:
    """Create an energy plan using the configured strategy.

    Falls back to surplus mode if the selected strategy fails
    or if required data is missing.
    """
    strategy_name = config.strategy

    # Validate data availability for advanced strategies
    if strategy_name == "price" and not prices:
        logger.warning("No price data available, falling back to surplus mode")
        strategy_name = "surplus"
    elif strategy_name == "forecast" and not pv_forecast:
        logger.warning("No PV forecast available, falling back to surplus mode")
        strategy_name = "surplus"

    strategy_fn = STRATEGIES.get(strategy_name)
    if not strategy_fn:
        logger.error("Unknown strategy '%s', falling back to surplus", strategy_name)
        strategy_fn = plan_surplus

    try:
        plan = strategy_fn(snapshot, prices, pv_forecast, config)
        logger.info("Plan created: strategy=%s, slots=%d",
                     plan.strategy, len(plan.slots))
        return plan
    except Exception as e:
        logger.error("Strategy '%s' failed: %s – falling back to surplus",
                      strategy_name, e)
        return plan_surplus(snapshot, prices, pv_forecast, config)
