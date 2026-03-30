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
    hourly_profile = snapshot.hourly_load_profile
    has_profile = len(hourly_profile) >= 12

    slots = []
    for i in range(num_slots):
        slot_start = now + timedelta(minutes=i * slot_minutes)
        pv_w = get_forecast_for_time(pv_forecast, slot_start)
        price = get_price_for_time(prices, slot_start)
        # Load: real hourly profile from 7d history
        if i == 0:
            load_w = snapshot.load_power_w
        elif has_profile:
            load_w = hourly_profile.get(slot_start.hour, config.load_per_slot_w)
        else:
            load_w = config.load_per_slot_w
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
        # Normal: cheap hours below threshold available
        below_threshold.sort(key=lambda x: x[0])
        candidates = below_threshold
        logger.info("Found %d slots below threshold %.2f ct (%d needed)",
                    len(below_threshold), threshold * 100, slots_needed)
    elif slots_needed > 0:
        # FALLBACK: No prices below threshold.
        # Only charge if the price spread (accounting for efficiency losses)
        # makes it worthwhile: effective_cheap / η < expensive → net savings.
        eff = config.round_trip_efficiency
        all_prices = [s.price_eur_kwh for s in slots if s.price_eur_kwh > 0]
        if all_prices:
            cheapest_price = min(all_prices)
            most_expensive = max(all_prices)
            # Effective cost of charging at cheapest price, accounting for losses
            effective_charge_cost = cheapest_price / eff
            net_spread = most_expensive - effective_charge_cost

            logger.info("Fallback check: cheapest=%.2fct, effective=%.2fct (÷%.0f%% eff), "
                        "most_expensive=%.2fct, net_spread=%.2fct, min_spread=%.2fct",
                        cheapest_price * 100, effective_charge_cost * 100, eff * 100,
                        most_expensive * 100, net_spread * 100,
                        config.min_price_spread_eur * 100)

            if net_spread >= config.min_price_spread_eur:
                # Spread is large enough → charge at cheapest, discharge at expensive
                all_candidates.sort(key=lambda x: x[0])
                candidates = all_candidates
                logger.info("Fallback ACTIVE: net spread %.2f ct >= %.2f ct min → "
                            "charging at %d cheapest slots above threshold",
                            net_spread * 100, config.min_price_spread_eur * 100, slots_needed)
            else:
                # Spread too small → not worth charging with efficiency losses
                candidates = []
                logger.info("Fallback SKIPPED: net spread %.2f ct < %.2f ct min → "
                            "grid charging not profitable after efficiency losses",
                            net_spread * 100, config.min_price_spread_eur * 100)
        else:
            candidates = []
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

    # Calculate average grid charge price (for discharge lock logic)
    avg_grid_charge_price = 0.0
    if charge_count > 0:
        assigned = candidates[:charge_count]
        total_planned = sum(c[0] for c in assigned)
        charge_prices = [c[2].price_eur_kwh for c in assigned]
        avg_grid_charge_price = sum(charge_prices) / len(charge_prices)
        logger.info("Assigned %d grid-charge slots (avg %.2f ct, est. %.2f EUR total)",
                    charge_count, avg_grid_charge_price * 100, total_planned)

    # Minimum price for profitable discharge:
    # Only discharge if grid price > (avg_charge_price / efficiency) + min_spread
    # Below this, it's cheaper to use grid directly than stored battery energy.
    eff = config.round_trip_efficiency
    if avg_grid_charge_price > 0:
        min_discharge_price = avg_grid_charge_price / eff + config.min_price_spread_eur
    else:
        min_discharge_price = 0  # No grid charging → PV-only energy → always profitable to discharge
    logger.info("Discharge lock: avg charge %.2f ct / %.0f%% eff + %.2f ct spread = min %.2f ct",
                avg_grid_charge_price * 100, eff * 100,
                config.min_price_spread_eur * 100, min_discharge_price * 100)

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
                # Discharge lock: don't discharge if grid price is below
                # the effective charge cost (avg_charge/eff + spread).
                # It's cheaper to import from grid than use stored energy.
                if min_discharge_price > 0 and slot.price_eur_kwh < min_discharge_price:
                    # Lock battery: don't discharge, import from grid instead
                    slot.planned_battery_w = 0
                    slot.planned_battery_mode = "idle"
                else:
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
