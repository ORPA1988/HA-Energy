"""Price-optimized strategy: charge cheap, discharge expensive.

Battery: only mode control (charge/discharge/idle), power set by inverter.
PHEV: charge power tracks PV surplus, clamped to min/max charge limits.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..models import Config, ForecastPoint, Plan, PricePoint, Snapshot, TimeSlot
from .helpers import (calc_grid_balance, calc_phev_power, get_forecast_for_time,
                      get_price_for_time, is_grid_charging, update_soc)

logger = logging.getLogger(__name__)


def plan_price_optimized(
    snapshot: Snapshot,
    prices: list[PricePoint],
    pv_forecast: list[ForecastPoint],
    config: Config,
) -> Plan:
    """Create a 24h plan optimized for electricity cost.

    Three-pass greedy algorithm:
    1. Assign free PV surplus → PHEV first, then battery
    2. Pair cheapest grid-charge slots with most expensive discharge slots
    3. Forward-simulate SOC to enforce constraints
    """
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    slot_minutes = config.slot_duration_min
    num_slots = (24 * 60) // slot_minutes
    slots = []

    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = get_forecast_for_time(pv_forecast, slot_start)
        price = get_price_for_time(prices, slot_start)
        load_w = snapshot.load_power_w if i == 0 else config.load_per_slot_w
        slots.append(TimeSlot(
            start=slot_start, duration_min=slot_minutes,
            pv_forecast_w=pv_w, price_eur_kwh=price, load_estimate_w=load_w,
        ))

    # --- PASS 1: Assign PV surplus → PHEV first, then battery ---
    soc = snapshot.battery_soc
    for slot in slots:
        surplus_w = slot.pv_forecast_w - slot.load_estimate_w
        slot.planned_phev_w = calc_phev_power(surplus_w, config, snapshot)
        remaining = surplus_w - slot.planned_phev_w

        if remaining > 0 and soc < config.max_soc_percent:
            slot.planned_battery_mode = "charge"
            headroom_wh = (config.max_soc_percent - soc) / 100.0 * config.battery_capacity_wh
            max_w = headroom_wh / (slot_minutes / 60.0)
            slot.planned_battery_w = min(remaining, max(0, max_w))
            soc += (slot.planned_battery_w * slot_minutes / 60.0) / config.battery_capacity_wh * 100.0
            slot.planned_grid_w = -(remaining - slot.planned_battery_w)

    # --- PASS 2: Greedy grid-charge / discharge pairing ---
    charge_candidates = []
    discharge_candidates = []

    if prices:
        median_price = sorted(s.price_eur_kwh for s in slots)[len(slots) // 2]

        for slot in slots:
            if slot.planned_battery_mode == "idle" and slot.price_eur_kwh <= config.price_threshold_eur:
                charge_candidates.append(slot)
            if slot.price_eur_kwh > median_price and slot.pv_forecast_w < slot.load_estimate_w:
                discharge_candidates.append(slot)

        charge_candidates.sort(key=lambda s: s.price_eur_kwh)
        discharge_candidates.sort(key=lambda s: s.price_eur_kwh, reverse=True)

        discharge_iter = iter(discharge_candidates)
        for cheap in charge_candidates:
            discharge_slot = next(discharge_iter, None)
            if discharge_slot is None:
                break

            spread = discharge_slot.price_eur_kwh - cheap.price_eur_kwh
            if spread < config.min_price_spread_eur:
                continue

            cheap.planned_battery_mode = "charge"
            cheap.planned_battery_w = config.battery_capacity_wh / 4

            if discharge_slot.planned_battery_mode != "charge":
                discharge_slot.planned_battery_mode = "discharge"
                deficit_w = discharge_slot.load_estimate_w - discharge_slot.pv_forecast_w
                discharge_slot.planned_battery_w = -deficit_w

    # --- PASS 3: Forward SOC simulation with constraint clipping ---
    soc = snapshot.battery_soc
    for slot in slots:
        # Grid-charge limit
        if (is_grid_charging(slot.pv_forecast_w, slot.load_estimate_w, slot.planned_battery_w)
                and soc >= config.max_grid_charge_soc):
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0

        soc = update_soc(soc, slot.planned_battery_w, slot_minutes, config)

        if slot.planned_battery_w > 0 and soc >= config.max_soc_percent:
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0
        elif slot.planned_battery_w < 0 and soc <= config.min_soc_percent:
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0

        slot.planned_grid_w = calc_grid_balance(
            slot.pv_forecast_w, slot.load_estimate_w,
            slot.planned_phev_w, slot.planned_battery_w)
        slot.projected_soc = soc

    logger.info("Price plan: %d slots, SOC %.1f%%→%.1f%%, %d charge/%d discharge",
                len(slots), snapshot.battery_soc,
                slots[-1].projected_soc if slots else soc,
                sum(1 for s in slots if s.planned_battery_mode == "charge"),
                sum(1 for s in slots if s.planned_battery_mode == "discharge"))

    return Plan(created_at=now, strategy="price", slots=slots, tz=config.timezone)
