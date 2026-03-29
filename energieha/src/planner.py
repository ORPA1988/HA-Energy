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

# Lazy-loaded to avoid import error if EMHASS not configured
_emhass_loaded = False


def _load_emhass():
    global _emhass_loaded
    try:
        from .strategies.emhass import plan_emhass
        STRATEGIES["emhass"] = plan_emhass
        _emhass_loaded = True
    except ImportError as e:
        logger.warning("EMHASS strategy not available: %s", e)


def _fallback_plan(snapshot, prices, pv_forecast, config, error_msg):
    """Create a fallback plan using price strategy (or surplus if no prices)."""
    if prices:
        logger.warning("Fallback to PRICE strategy: %s", error_msg)
        plan = plan_price_optimized(snapshot, prices, pv_forecast, config)
        plan.strategy_error = error_msg
    else:
        logger.warning("Fallback to SURPLUS (no prices): %s", error_msg)
        plan = plan_surplus(snapshot, prices, pv_forecast, config)
        plan.strategy_error = error_msg
    return plan


def create_plan(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
    sunrise_hour: int = 6,
    sunset_hour: int = 20,
) -> Plan:
    """Create an energy plan using the configured strategy.

    Fallback chain: configured strategy → price → surplus.
    Price strategy is preferred over surplus because it optimizes costs.
    """
    strategy_name = config.strategy

    # Lazy-load EMHASS strategy
    if strategy_name == "emhass" and not _emhass_loaded:
        _load_emhass()

    # Validate data availability
    if strategy_name == "price" and not prices:
        logger.warning("No price data, falling back to surplus")
        strategy_name = "surplus"
    elif strategy_name == "forecast" and not pv_forecast:
        logger.warning("No PV forecast, falling back to price")
        strategy_name = "price" if prices else "surplus"
    elif strategy_name == "emhass" and "emhass" not in STRATEGIES:
        logger.warning("EMHASS not available, falling back to price")
        strategy_name = "price" if prices else "surplus"

    strategy_fn = STRATEGIES.get(strategy_name)
    if not strategy_fn:
        logger.error("Unknown strategy '%s', falling back to price", strategy_name)
        strategy_fn = plan_price_optimized if prices else plan_surplus

    try:
        if strategy_name == "forecast":
            plan = strategy_fn(snapshot, prices, pv_forecast, config,
                               sunrise_hour=sunrise_hour, sunset_hour=sunset_hour)
        else:
            plan = strategy_fn(snapshot, prices, pv_forecast, config)

        _enforce_soc_limits(plan, config)

        logger.info("Plan created: strategy=%s, slots=%d",
                     plan.strategy, len(plan.slots))
        return plan
    except Exception as e:
        logger.error("Strategy '%s' failed: %s", strategy_name, e, exc_info=True)
        plan = _fallback_plan(snapshot, prices, pv_forecast, config,
                              f"{strategy_name}: {e}")
        _enforce_soc_limits(plan, config)
        return plan


def _enforce_soc_limits(plan: Plan, config: Config) -> None:
    """Post-process: clip any SOC violations as safety net."""
    violations = 0
    for slot in plan.slots:
        if slot.projected_soc < config.min_soc_percent:
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0
            slot.projected_soc = config.min_soc_percent
            violations += 1
        elif slot.projected_soc > config.max_soc_percent:
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0
            slot.projected_soc = config.max_soc_percent
            violations += 1
    if violations:
        logger.warning("SOC safety net: clipped %d slots", violations)
