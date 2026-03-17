"""Smart EV charging strategy solver — evaluates and ranks 5 charging strategies."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from config import get_config
from models import (
    EVStrategy,
    EVStrategyResult,
    EVStrategyType,
    HourlySlot,
)

logger = logging.getLogger(__name__)


class EVChargeStrategySolver:
    """
    Given a charging window (from → until) and a target SOC,
    computes and ranks all feasible strategies:

    A. Grid → EV directly in cheapest hours
    B. Grid → Battery → EV (pre-charge battery at cheap prices, then discharge to EV)
    C. Solar → EV (use next-day/same-day solar forecast directly)
    D. Combined (Grid→EV + Battery→EV simultaneously)
    E. Solar → Battery → EV (indirect solar storage path)
    """

    def __init__(self):
        self._cfg = get_config()

    def solve(
        self,
        ev_soc: float,
        battery_soc: float,
        price_forecast_ct: list[float],
        pv_forecast_w: list[float],
        window_start: datetime,
        window_end: datetime,
        target_soc_percent: int,
        must_finish_by: datetime,
    ) -> EVStrategyResult:
        cfg = self._cfg
        now = datetime.now().replace(minute=0, second=0, microsecond=0)

        # Compute hours in window
        hours: list[datetime] = []
        h = window_start.replace(minute=0, second=0, microsecond=0)
        while h <= must_finish_by:
            hours.append(h)
            h += timedelta(hours=1)

        # Guard against empty forecast lists
        if not price_forecast_ct:
            price_forecast_ct = [cfg.fixed_price_ct_kwh]
        if not pv_forecast_w:
            pv_forecast_w = [0.0]

        # Map hours to price/PV forecast indices
        def hour_idx(dt: datetime) -> int:
            delta = int((dt - now).total_seconds() / 3600)
            return max(0, min(delta, len(price_forecast_ct) - 1))

        prices = {h: price_forecast_ct[hour_idx(h)] for h in hours}
        pv = {h: pv_forecast_w[hour_idx(h)] for h in hours}

        ev_needed_wh = max(0.0, (target_soc_percent / 100.0 - ev_soc / 100.0)
                           * cfg.ev_battery_capacity_kwh * 1000.0)
        bat_available_wh = max(0.0, (battery_soc / 100.0 - cfg.battery_min_soc / 100.0)
                               * cfg.battery_capacity_kwh * 1000.0 * cfg.battery_efficiency)
        ev_max_ch_w = cfg.ev_max_charge_current_a * cfg.goe_phases * 230.0
        bat_max_dis_w = float(cfg.battery_max_discharge_w)

        strategies: list[EVStrategy] = []

        # Strategy A: Grid → EV directly
        if cfg.ev_allow_grid_to_charge_ev:
            strategy_a = self._strategy_grid_to_ev(
                hours, prices, ev_needed_wh, ev_max_ch_w, ev_soc, target_soc_percent
            )
            strategies.append(strategy_a)

        # Strategy B: Grid → Battery → EV
        if cfg.ev_allow_grid_to_charge_ev and cfg.ev_allow_battery_to_charge_ev:
            strategy_b = self._strategy_grid_battery_ev(
                hours, prices, ev_needed_wh, bat_available_wh,
                ev_max_ch_w, bat_max_dis_w, battery_soc, ev_soc, target_soc_percent
            )
            strategies.append(strategy_b)

        # Strategy C: Solar → EV
        strategy_c = self._strategy_solar_ev(
            hours, prices, pv, ev_needed_wh, ev_max_ch_w, ev_soc, target_soc_percent
        )
        strategies.append(strategy_c)

        # Strategy D: Combined (Grid + Battery → EV simultaneously)
        if cfg.ev_allow_grid_to_charge_ev and cfg.ev_allow_battery_to_charge_ev:
            strategy_d = self._strategy_combined(
                hours, prices, ev_needed_wh, bat_available_wh,
                ev_max_ch_w, bat_max_dis_w, battery_soc, ev_soc, target_soc_percent
            )
            strategies.append(strategy_d)

        # Strategy E: Solar → Battery → EV
        strategy_e = self._strategy_solar_battery_ev(
            hours, prices, pv, ev_needed_wh, bat_available_wh,
            ev_max_ch_w, bat_max_dis_w, ev_soc, target_soc_percent
        )
        strategies.append(strategy_e)

        # Rank by: feasibility first, then cost
        feasible = [s for s in strategies if s.feasible]
        recommended = (
            min(feasible, key=lambda s: s.total_cost_eur).strategy_type
            if feasible
            else EVStrategyType.GRID_TO_EV
        )

        return EVStrategyResult(
            recommended=recommended,
            strategies=strategies,
            window_start=window_start,
            window_end=window_end,
            target_soc_percent=target_soc_percent,
            must_finish_by=must_finish_by,
        )

    def _strategy_grid_to_ev(
        self, hours, prices, ev_needed_wh, ev_max_ch_w, ev_soc, target_soc
    ) -> EVStrategy:
        """Charge EV from grid in cheapest available hours."""
        sorted_hours = sorted(hours, key=lambda h: prices[h])
        timeline: list[HourlySlot] = []
        remaining_wh = ev_needed_wh
        current_ev_soc = ev_soc
        total_cost = 0.0

        for h in sorted_hours:
            if remaining_wh <= 0:
                break
            ch_w = min(ev_max_ch_w, remaining_wh)
            cost_eur = ch_w * prices[h] / 100.0 / 1000.0
            remaining_wh -= ch_w
            current_ev_soc = min(100.0, current_ev_soc + ch_w / (self._cfg.ev_battery_capacity_kwh * 10))
            total_cost += cost_eur
            timeline.append(HourlySlot(
                hour=h, ev_charge_w=ch_w,
                ev_current_a=int(ch_w / (self._cfg.goe_phases * 230)),
                grid_import_w=ch_w, cost_eur=cost_eur, price_ct=prices[h],
                ev_soc_end=current_ev_soc,
            ))

        return EVStrategy(
            strategy_type=EVStrategyType.GRID_TO_EV,
            name="Netz → E-Auto",
            description="Direktes Laden aus dem Netz in den günstigsten Stunden",
            feasible=remaining_wh <= 0,
            total_cost_eur=total_cost,
            charging_timeline=sorted(timeline, key=lambda s: s.hour),
            achieves_target_soc=remaining_wh <= 0,
            target_soc_at_deadline=current_ev_soc,
        )

    def _strategy_grid_battery_ev(
        self, hours, prices, ev_needed_wh, bat_available_wh,
        ev_max_ch_w, bat_max_dis_w, battery_soc, ev_soc, target_soc
    ) -> EVStrategy:
        """Pre-charge battery from grid when cheap, then discharge to EV."""
        cfg = self._cfg
        bat_soc = battery_soc
        current_ev_soc = ev_soc
        remaining_wh = ev_needed_wh
        total_cost = 0.0
        timeline = []
        threshold = cfg.ev_combined_charge_threshold_ct

        # Phase 1: Charge battery during cheapest hours
        sorted_hours = sorted(hours, key=lambda h: prices[h])
        bat_charged_wh = 0.0
        bat_ch_max_wh = (100.0 - battery_soc) / 100.0 * cfg.battery_capacity_kwh * 1000.0

        for h in sorted_hours:
            if prices[h] > threshold or bat_charged_wh >= bat_ch_max_wh:
                break
            ch_w = min(float(cfg.battery_max_charge_w), bat_ch_max_wh - bat_charged_wh)
            cost = ch_w * prices[h] / 100.0 / 1000.0
            bat_charged_wh += ch_w
            bat_soc = min(100.0, bat_soc + ch_w / (cfg.battery_capacity_kwh * 10))
            total_cost += cost
            timeline.append(HourlySlot(
                hour=h, battery_charge_w=ch_w, grid_import_w=ch_w,
                cost_eur=cost, price_ct=prices[h], battery_soc_end=bat_soc,
            ))

        # Phase 2: Discharge battery to charge EV
        discharge_wh = min(bat_available_wh + bat_charged_wh, remaining_wh)
        for h in sorted(hours):
            if discharge_wh <= 0 or remaining_wh <= 0:
                break
            dis_w = min(bat_max_dis_w, discharge_wh)
            ev_ch_w = min(ev_max_ch_w, dis_w * cfg.battery_efficiency)
            discharge_wh -= dis_w
            remaining_wh -= ev_ch_w
            current_ev_soc = min(100.0, current_ev_soc + ev_ch_w / (cfg.ev_battery_capacity_kwh * 10))
            timeline.append(HourlySlot(
                hour=h, battery_discharge_w=dis_w, ev_charge_w=ev_ch_w,
                ev_current_a=int(ev_ch_w / (cfg.goe_phases * 230)),
                cost_eur=0.0, price_ct=prices[h],
                battery_soc_end=max(cfg.battery_min_soc, bat_soc - dis_w / (cfg.battery_capacity_kwh * 10)),
                ev_soc_end=current_ev_soc,
            ))

        return EVStrategy(
            strategy_type=EVStrategyType.GRID_TO_BATTERY_TO_EV,
            name="Netz → Akku → E-Auto",
            description="Akku günstig aus Netz laden, dann E-Auto aus Akku laden",
            feasible=remaining_wh <= 0,
            total_cost_eur=total_cost,
            charging_timeline=sorted(timeline, key=lambda s: s.hour),
            achieves_target_soc=remaining_wh <= 0,
            target_soc_at_deadline=current_ev_soc,
        )

    def _strategy_solar_ev(
        self, hours, prices, pv, ev_needed_wh, ev_max_ch_w, ev_soc, target_soc
    ) -> EVStrategy:
        """Charge EV directly from solar during daytime hours."""
        remaining_wh = ev_needed_wh
        current_ev_soc = ev_soc
        total_cost = 0.0
        timeline = []

        for h in sorted(hours):
            if remaining_wh <= 0:
                break
            pv_w = pv.get(h, 0.0)
            if pv_w < 200:
                continue  # not enough solar
            ch_w = min(ev_max_ch_w, pv_w * 0.8, remaining_wh)  # 80% of PV to EV
            remaining_wh -= ch_w
            current_ev_soc = min(100.0, current_ev_soc + ch_w / (self._cfg.ev_battery_capacity_kwh * 10))
            timeline.append(HourlySlot(
                hour=h, ev_charge_w=ch_w,
                ev_current_a=int(ch_w / (self._cfg.goe_phases * 230)),
                cost_eur=0.0, price_ct=prices[h], ev_soc_end=current_ev_soc,
            ))

        return EVStrategy(
            strategy_type=EVStrategyType.SOLAR_TO_EV,
            name="Solar → E-Auto",
            description="E-Auto laden wenn Solarüberschuss vorhanden",
            feasible=remaining_wh <= 0,
            total_cost_eur=total_cost,
            charging_timeline=timeline,
            achieves_target_soc=remaining_wh <= 0,
            target_soc_at_deadline=current_ev_soc,
            notes="Kostenlos, aber wetterabhängig",
        )

    def _strategy_combined(
        self, hours, prices, ev_needed_wh, bat_available_wh,
        ev_max_ch_w, bat_max_dis_w, battery_soc, ev_soc, target_soc
    ) -> EVStrategy:
        """Simultaneous grid + battery → EV in cheapest hours."""
        cfg = self._cfg
        remaining_wh = ev_needed_wh
        current_ev_soc = ev_soc
        bat_soc = battery_soc
        total_cost = 0.0
        timeline = []

        sorted_hours = sorted(hours, key=lambda h: prices[h])
        bat_remaining = bat_available_wh

        for h in sorted_hours:
            if remaining_wh <= 0:
                break
            bat_ch_w = min(bat_max_dis_w, bat_remaining) if bat_remaining > 0 else 0.0
            bat_ev_w = bat_ch_w * cfg.battery_efficiency
            grid_ev_w = min(ev_max_ch_w - bat_ev_w, remaining_wh - bat_ev_w)
            grid_ev_w = max(0.0, grid_ev_w)
            total_ev_w = bat_ev_w + grid_ev_w

            bat_remaining -= bat_ch_w
            remaining_wh -= total_ev_w
            current_ev_soc = min(100.0, current_ev_soc + total_ev_w / (cfg.ev_battery_capacity_kwh * 10))
            bat_soc = max(cfg.battery_min_soc, bat_soc - bat_ch_w / (cfg.battery_capacity_kwh * 10))
            cost_eur = grid_ev_w * prices[h] / 100.0 / 1000.0
            total_cost += cost_eur

            timeline.append(HourlySlot(
                hour=h, ev_charge_w=total_ev_w, battery_discharge_w=bat_ch_w,
                ev_current_a=int(total_ev_w / (cfg.goe_phases * 230)),
                grid_import_w=grid_ev_w, cost_eur=cost_eur,
                price_ct=prices[h], battery_soc_end=bat_soc, ev_soc_end=current_ev_soc,
            ))

        return EVStrategy(
            strategy_type=EVStrategyType.COMBINED,
            name="Kombiniert (Netz + Akku → E-Auto)",
            description="E-Auto gleichzeitig aus Netz und Akku laden",
            feasible=remaining_wh <= 0,
            total_cost_eur=total_cost,
            charging_timeline=sorted(timeline, key=lambda s: s.hour),
            achieves_target_soc=remaining_wh <= 0,
            target_soc_at_deadline=current_ev_soc,
        )

    def _strategy_solar_battery_ev(
        self, hours, prices, pv, ev_needed_wh, bat_available_wh,
        ev_max_ch_w, bat_max_dis_w, ev_soc, target_soc
    ) -> EVStrategy:
        """Solar charges battery, battery charges EV at night."""
        cfg = self._cfg
        remaining_wh = ev_needed_wh
        current_ev_soc = ev_soc
        bat_soc_charged = bat_available_wh / (cfg.battery_capacity_kwh * 10)
        total_cost = 0.0
        timeline = []

        for h in sorted(hours):
            if remaining_wh <= 0:
                break
            pv_w = pv.get(h, 0.0)
            if pv_w > 200:
                # Charge battery from solar
                bat_ch_w = min(float(cfg.battery_max_charge_w), pv_w * 0.9)
                bat_soc_charged += bat_ch_w * cfg.battery_efficiency / (cfg.battery_capacity_kwh * 10)
                timeline.append(HourlySlot(
                    hour=h, battery_charge_w=bat_ch_w,
                    cost_eur=0.0, price_ct=prices[h],
                ))
            elif h.hour < 8 or h.hour >= 20:
                # Discharge battery to EV at night
                dis_w = min(bat_max_dis_w, bat_soc_charged * cfg.battery_capacity_kwh * 10)
                ev_ch_w = min(ev_max_ch_w, dis_w * cfg.battery_efficiency, remaining_wh)
                bat_soc_charged -= dis_w / (cfg.battery_capacity_kwh * 10)
                remaining_wh -= ev_ch_w
                current_ev_soc = min(100.0, current_ev_soc + ev_ch_w / (cfg.ev_battery_capacity_kwh * 10))
                timeline.append(HourlySlot(
                    hour=h, battery_discharge_w=dis_w, ev_charge_w=ev_ch_w,
                    ev_current_a=int(ev_ch_w / (cfg.goe_phases * 230)),
                    cost_eur=0.0, price_ct=prices[h], ev_soc_end=current_ev_soc,
                ))

        return EVStrategy(
            strategy_type=EVStrategyType.SOLAR_TO_BATTERY_TO_EV,
            name="Solar → Akku → E-Auto",
            description="Solar lädt Akku tagsüber, Akku lädt E-Auto nachts",
            feasible=remaining_wh <= 0,
            total_cost_eur=total_cost,
            charging_timeline=sorted(timeline, key=lambda s: s.hour),
            achieves_target_soc=remaining_wh <= 0,
            target_soc_at_deadline=current_ev_soc,
            notes="Vollständig kostenfrei wenn Solar ausreicht",
        )


# Global singleton
_solver: Optional[EVChargeStrategySolver] = None


def get_ev_strategy_solver() -> EVChargeStrategySolver:
    global _solver
    if _solver is None:
        _solver = EVChargeStrategySolver()
    return _solver
