"""Price-optimized strategy: charge at cheapest total grid cost.

Algorithm:
1. Read dynamic price threshold from HA (input_number.epex_preisschwelle_netzladung)
2. For each slot below threshold, calculate actual grid cost:
   grid_cost = price × max(0, charge_power + load - pv) × hours
   This accounts for PV offset: charging during sun costs less grid import.
3. Calculate how many slots of grid charging are needed for target SOC
4. Select the N slots with lowest actual grid cost
5. PV surplus → battery charge (free)
6. Expensive hours → discharge

Battery charge power is determined by the inverter (not configurable here).
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
    """Create a 24h plan optimized for lowest total grid charging cost."""
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
        eff_charge = config.round_trip_efficiency ** 0.5
        energy_from_grid_wh = energy_needed_wh / eff_charge
        energy_per_slot_wh = charge_power_w * (slot_minutes / 60.0)
        slots_needed = max(1, int(energy_from_grid_wh / energy_per_slot_wh + 0.99))
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

    # --- Calculate actual grid cost per slot for charging ---
    # grid_cost = price × max(0, charge_power + load - pv) × hours / 1000
    # Slots with PV offset are cheaper because less grid import needed
    hours = slot_minutes / 60.0
    candidate_slots = []
    for s in slots:
        if s.price_eur_kwh >= threshold:
            continue
        grid_import_w = max(0, charge_power_w + s.load_estimate_w - s.pv_forecast_w)
        grid_cost_eur = s.price_eur_kwh * (grid_import_w / 1000.0) * hours
        candidate_slots.append((grid_cost_eur, grid_import_w, s))

    # Sort by actual grid cost (cheapest total cost first)
    candidate_slots.sort(key=lambda x: x[0])

    # --- Assign grid charging to the N cheapest-cost slots ---
    charge_count = 0
    for grid_cost, grid_import_w, slot in candidate_slots:
        if charge_count >= slots_needed:
            break
        slot.planned_battery_mode = "charge"
        slot.planned_battery_w = charge_power_w
        charge_count += 1
        logger.debug("Grid charge slot %s: price=%.4f, PV=%.0fW, "
                     "grid_import=%.0fW, cost=%.4f EUR",
                     slot.start.strftime("%H:%M"), slot.price_eur_kwh,
                     slot.pv_forecast_w, grid_import_w, grid_cost)

    if candidate_slots and charge_count > 0:
        assigned = candidate_slots[:charge_count]
        total_planned = sum(c[0] for c in assigned)
        logger.info("Assigned %d/%d slots for grid charging "
                    "(threshold %.4f, %d below threshold, est. cost %.2f EUR)",
                    charge_count, slots_needed, threshold,
                    len(candidate_slots), total_planned)

    # --- PV surplus → charge battery (free energy) ---
    for slot in slots:
        if slot.planned_battery_mode != "idle":
            continue
        surplus_w = slot.pv_forecast_w - slot.load_estimate_w
        slot.planned_phev_w = calc_phev_power(surplus_w, config, snapshot)
        remaining = surplus_w - slot.planned_phev_w
        if remaining > 50:
            slot.planned_battery_mode = "charge"
            slot.planned_battery_w = remaining

    # --- Expensive slots → discharge battery ---
    if prices:
        median_price = sorted(s.price_eur_kwh for s in slots)[len(slots) // 2]
        for slot in slots:
            if slot.planned_battery_mode != "idle":
                continue
            if slot.price_eur_kwh > median_price and slot.pv_forecast_w < slot.load_estimate_w:
                deficit_w = slot.load_estimate_w - slot.pv_forecast_w
                slot.planned_battery_mode = "discharge"
                slot.planned_battery_w = -deficit_w

    # --- Forward SOC simulation with constraint clipping ---
    soc = snapshot.battery_soc
    total_charge_cost = 0.0
    for slot in slots:
        if (slot.planned_battery_mode == "charge"
                and is_grid_charging(slot.pv_forecast_w, slot.load_estimate_w, slot.planned_battery_w)
                and soc >= config.grid_charge_target_soc):
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0

        if slot.planned_battery_w > 0 and soc >= config.max_soc_percent:
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0

        if slot.planned_battery_w < 0 and soc <= config.min_soc_percent:
            slot.planned_battery_mode = "idle"
            slot.planned_battery_w = 0

        soc = update_soc(soc, slot.planned_battery_w, slot_minutes, config)

        slot.planned_grid_w = calc_grid_balance(
            slot.pv_forecast_w, slot.load_estimate_w,
            slot.planned_phev_w, slot.planned_battery_w)
        slot.projected_soc = soc

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
