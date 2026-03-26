"""Shared helper functions for all planning strategies."""

from ..models import Config, ForecastPoint, PricePoint, Snapshot


def get_forecast_for_time(forecast: list[ForecastPoint], t) -> float:
    """Return PV forecast power (W) for the given time, or 0."""
    for fp in forecast:
        if fp.start <= t < fp.end:
            return fp.power_w
    return 0.0


def get_price_for_time(prices: list[PricePoint], t) -> float:
    """Return electricity price (EUR/kWh) for the given time, or 0."""
    for pp in prices:
        if pp.start <= t < pp.end:
            return pp.price_eur_kwh
    return 0.0


def calc_phev_power(surplus_w: float, config: Config, snapshot: Snapshot) -> float:
    """Calculate PHEV charge power from PV surplus. Returns 0 if below minimum."""
    if not config.phev_enabled or not snapshot.phev_connected or surplus_w <= 0:
        return 0.0
    if surplus_w >= config.phev_min_charge_w:
        return min(surplus_w, config.phev_max_charge_w)
    return 0.0


def update_soc(soc: float, battery_w: float, slot_minutes: int,
               config: Config) -> float:
    """Forward-simulate SOC for one slot, applying round-trip efficiency."""
    energy_wh = battery_w * (slot_minutes / 60.0)
    if energy_wh < 0:
        # Discharge: account for losses
        energy_wh *= config.round_trip_efficiency
    soc += (energy_wh / config.battery_capacity_wh) * 100.0
    return max(config.min_soc_percent, min(config.max_soc_percent, soc))


def calc_grid_balance(pv_w: float, load_w: float, phev_w: float,
                      battery_w: float) -> float:
    """Calculate grid power. Positive = import, negative = export."""
    return -(pv_w - load_w - phev_w - battery_w)


def is_grid_charging(pv_w: float, load_w: float, battery_w: float) -> bool:
    """True if battery charges from grid (not just from PV surplus)."""
    surplus = pv_w - load_w
    return battery_w > 0 and battery_w > max(0, surplus)
