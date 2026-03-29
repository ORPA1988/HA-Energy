"""Price-optimized strategy: charge at cheapest prices below threshold.

Algorithm:
1. Read dynamic price threshold from HA (input_number.epex_preisschwelle_netzladung)
2. Filter hours where price < threshold
3. Calculate how many hours of grid charging are needed to reach target SOC
   (using the actual inverter charge power: grid_charging_current × battery_voltage)
4. Select the cheapest N hours for grid charging
5. Remaining hours: discharge from battery or use PV surplus

Battery charge power is determined by the inverter (not configurable here).
PHEV charge power tracks PV surplus, clamped to min/max charge limits.
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

    1. Determine price threshold (from HA entity or config)
    2. Find hours below threshold
    3. Calculate needed charge hours for target SOC
    4. Pick cheapest hours for grid charging
    5. PV surplus → battery charge (free)
    6. Expensive hours → discharge
    """
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    slot_minutes = config.slot_duration_min
    num_slots = (24 * 60) // slot_minutes

    # --- Price threshold: dynamic from HA, fallback to config ---
    threshold = snapshot.dynamic_price_threshold
    if threshold <= 0:
        threshold = config.price_threshold_eur
    logger.info("Price threshold: %.4f EUR/kWh (dynamic=%s)",
                threshold, snapshot.dynamic_price_threshold > 0)

    # --- Charge power from inverter (W) ---
    charge_power_w = snapshot.grid_charge_power_w
    logger.info("Grid charge power from inverter: %.0f W", charge_power_w)

    # --- Target SOC for grid charging ---
    target_soc = config.grid_charge_target_soc
    current_soc = snapshot.battery_soc

    # --- Energy needed to reach target SOC (accounting for efficiency) ---
    if current_soc < target_soc:
        soc_delta = target_soc - current_soc
        energy_needed_wh = (soc_delta / 100.0) * config.battery_capacity_wh
        # Account for charge efficiency loss
        eff_charge = config.round_trip_efficiency ** 0.5
        energy_from_grid_wh = energy_needed_wh / eff_charge
        # How many slots at the inverter's charge power?
        energy_per_slot_wh = charge_power_w * (slot_minutes / 60.0)
        slots_needed = max(1, int(energy_from_grid_wh / energy_per_slot_wh + 0.99))  # round up
    else:
        energy_from_grid_wh = 0
        slots_needed = 0
        logger.info("SOC %.1f%% >= target %d%% - no grid charging needed",
                    current_soc, target_soc)

    logger.info("Need %.0f Wh from grid (%.1f%% → %d%%), %d slots at %.0f W",
                energy_from_grid_wh, current_soc, target_soc,
                slots_needed, charge_power_w)

    # --- Build slot structure ---
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

    # --- Find slots below price threshold, sorted by price (cheapest first) ---
    cheap_slots = [s for s in slots if s.price_eur_kwh < threshold]
    cheap_slots.sort(key=lambda s: s.price_eur_kwh)

    # --- Assign grid charging to the N cheapest slots ---
    charge_count = 0
    for slot in cheap_slots:
        if charge_count >= slots_needed:
            break
        slot.planned_battery_mode = "charge"
        slot.planned_battery_w = charge_power_w
        charge_count += 1

    logger.info("Assigned %d/%d cheap slots for grid charging (threshold %.4f, "
                "found %d slots below threshold)",
                charge_count, slots_needed, threshold, len(cheap_slots))

    # --- PV surplus → charge battery (free energy) ---
    for slot in slots:
        if slot.planned_battery_mode != "idle":
            continue  # Already assigned (grid charge)
        surplus_w = slot.pv_forecast_w - slot.load_estimate_w
        # PHEV first, then battery
        slot.planned_phev_w = calc_phev_power(surplus_w, config, snapshot)
        remaining = surplus_w - slot.planned_phev_w
        if remaining > 50:  # 50W minimum to avoid noise
            slot.planned_battery_mode = "charge"
            slot.planned_battery_w = remaining

    # --- Expensive slots → discharge battery ---
    if prices:
        median_price = sorted(s.price_eur_kwh for s in slots)[len(slots) // 2]
        for slot in slots:
            if slot.planned_battery_mode != "idle":
                continue
            # Discharge when: price above median AND no PV surplus
            if slot.price_eur_kwh > median_price and slot.pv_forecast_w < slot.load_estimate_w:
                deficit_w = slot.load_estimate_w - slot.pv_forecast_w
                slot.planned_battery_mode = "discharge"
                slot.planned_battery_w = -deficit_w

    # --- Forward SOC simulation with constraint clipping ---
    soc = snapshot.battery_soc
    total_charge_cost = 0.0
    for slot in slots:
        # Grid-charge SOC limit: stop charging when target reached
        if (slot.planned_battery_mode == "charge"
                and is_grid_charging(slot.pv_forecast_w, slot.load_estimate_w, slot.planned_battery_w)
                and soc >= config.grid_charge_target_soc):
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0

        # Max SOC limit
        if slot.planned_battery_w > 0 and soc >= config.max_soc_percent:
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0

        # Min SOC limit
        if slot.planned_battery_w < 0 and soc <= config.min_soc_percent:
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0

        soc = update_soc(soc, slot.planned_battery_w, slot_minutes, config)

        # Calculate grid balance and cost
        slot.planned_grid_w = calc_grid_balance(
            slot.pv_forecast_w, slot.load_estimate_w,
            slot.planned_phev_w, slot.planned_battery_w)
        slot.projected_soc = soc

        # Track grid import cost
        if slot.planned_grid_w > 0:
            import_kwh = slot.planned_grid_w * (slot_minutes / 60.0) / 1000.0
            total_charge_cost += import_kwh * slot.price_eur_kwh

    charge_slots = sum(1 for s in slots if s.planned_battery_mode == "charge")
    discharge_slots = sum(1 for s in slots if s.planned_battery_mode == "discharge")
    grid_charge_slots = sum(1 for s in slots
                            if s.planned_battery_mode == "charge"
                            and is_grid_charging(s.pv_forecast_w, s.load_estimate_w, s.planned_battery_w))

    logger.info("Price plan: %d slots, SOC %.1f%%→%.1f%%, "
                "%d charge (%d grid/%d PV), %d discharge, "
                "est. grid cost %.2f EUR",
                len(slots), snapshot.battery_soc,
                slots[-1].projected_soc if slots else soc,
                charge_slots, grid_charge_slots, charge_slots - grid_charge_slots,
                discharge_slots, total_charge_cost)

    return Plan(created_at=now, strategy="price", slots=slots, tz=config.timezone)
