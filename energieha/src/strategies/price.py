"""Price-optimized strategy: charge at cheapest total grid cost.

Plans for today + tomorrow (up to 48h, depending on EPEX data availability).
Finds the cheapest hours by actual grid cost (price × grid import, PV offset).

Fallback logic: If NO prices are below threshold, charge at the N cheapest
hours anyway - the battery must be charged regardless of price level.
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
    """Create a today+tomorrow plan optimized for lowest total grid cost."""
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    slot_minutes = config.slot_duration_min

    # --- Plan horizon: today + tomorrow (up to 48h) ---
    # Calculate hours until end of tomorrow (23:59)
    tomorrow_end = (now + timedelta(days=1)).replace(hour=23, minute=59, second=0)
    hours_until_end = (tomorrow_end - now).total_seconds() / 3600.0
    num_slots = min(int(hours_until_end * 60 / slot_minutes) + 1, 48 * 60 // slot_minutes)
    logger.info("Planning horizon: %d slots (%.0fh, until tomorrow end)", num_slots, hours_until_end)

    # --- Price threshold ---
    threshold = snapshot.dynamic_price_threshold
    if threshold <= 0:
        threshold = config.price_threshold_eur
    logger.info("Price threshold: %.2f ct/kWh", threshold * 100)

    # --- Charge power from inverter (W) ---
    charge_power_w = snapshot.grid_charge_power_w
    logger.info("Grid charge power from inverter: %.0f W", charge_power_w)

    # --- Target SOC for grid charging ---
    target_soc = config.grid_charge_target_soc
    current_soc = snapshot.battery_soc

    # --- Energy needed to reach target SOC ---
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

    logger.info("Need %.0f Wh (%.1f%% → %d%%), %d slots at %.0f W",
                energy_from_grid_wh, current_soc, target_soc,
                slots_needed, charge_power_w)

    # --- Build slot structure (today + tomorrow) ---
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
    hours = slot_minutes / 60.0
    all_candidates = []
    below_threshold = []
    for s in slots:
        grid_import_w = max(0, charge_power_w + s.load_estimate_w - s.pv_forecast_w)
        grid_cost_eur = s.price_eur_kwh * (grid_import_w / 1000.0) * hours
        entry = (grid_cost_eur, grid_import_w, s)
        all_candidates.append(entry)
        if s.price_eur_kwh < threshold:
            below_threshold.append(entry)

    # --- Select charge slots ---
    if below_threshold:
        # Normal: enough cheap hours below threshold
        below_threshold.sort(key=lambda x: x[0])
        candidates = below_threshold
        logger.info("Found %d slots below threshold %.2f ct (%d needed)",
                    len(below_threshold), threshold * 100, slots_needed)
    elif slots_needed > 0:
        # FALLBACK: No prices below threshold - charge at cheapest anyway!
        # The battery must be charged regardless of price level.
        all_candidates.sort(key=lambda x: x[0])
        candidates = all_candidates
        logger.warning("NO prices below %.2f ct threshold! "
                       "Fallback: charging at %d cheapest slots anyway",
                       threshold * 100, slots_needed)
    else:
        candidates = []

    # Assign grid charging to the N cheapest-cost slots
    charge_count = 0
    for grid_cost, grid_import_w, slot in candidates:
        if charge_count >= slots_needed:
            break
        slot.planned_battery_mode = "charge"
        slot.planned_battery_w = charge_power_w
        charge_count += 1
        logger.debug("Grid charge: %s price=%.2fct PV=%.0fW grid=%.0fW cost=%.3fEUR",
                     slot.start.strftime("%d.%m %H:%M"), slot.price_eur_kwh * 100,
                     slot.pv_forecast_w, grid_import_w, grid_cost)

    if charge_count > 0:
        assigned = candidates[:charge_count]
        total_planned = sum(c[0] for c in assigned)
        cheapest = assigned[0][2].price_eur_kwh * 100
        most_exp = assigned[-1][2].price_eur_kwh * 100
        logger.info("Assigned %d grid-charge slots (%.1f-%.1f ct, est. %.2f EUR total)",
                    charge_count, cheapest, most_exp, total_planned)

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

    # --- Forward SOC simulation with realistic battery behavior ---
    soc = snapshot.battery_soc
    total_charge_cost = 0.0
    for slot in slots:
        # Grid-charge SOC limit
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

        # Simulate real Sungrow Load First behavior for idle slots:
        # battery covers load deficit, PV surplus charges battery
        if slot.planned_battery_mode == "idle":
            deficit_w = slot.load_estimate_w - slot.pv_forecast_w
            if deficit_w > 0 and soc > config.min_soc_percent:
                slot.planned_battery_w = -deficit_w
                slot.planned_battery_mode = "discharge"
            elif deficit_w < -50:
                slot.planned_battery_w = -deficit_w
                slot.planned_battery_mode = "charge"

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

    logger.info("Price plan: %d slots (%.0fh), SOC %.1f%%→%.1f%%, "
                "%d charge (%d grid/%d PV), %d discharge, cost %.2f EUR",
                len(slots), len(slots) * slot_minutes / 60,
                snapshot.battery_soc,
                slots[-1].projected_soc if slots else soc,
                charge_slots, grid_charge_slots, charge_slots - grid_charge_slots,
                discharge_slots, total_charge_cost)

    return Plan(created_at=now, strategy="price", slots=slots, tz=config.timezone)
