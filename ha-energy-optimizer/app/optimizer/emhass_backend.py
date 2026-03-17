"""Optional EMHASS-based optimizer backend.

Uses EMHASS (Energy Management for Home Assistant Solar Systems) as an
alternative optimization engine. EMHASS provides ML-based forecasting and
CVXPY solver with HiGHS backend.

This module wraps EMHASS's core optimization to produce the same DailySchedule
output as the built-in scipy LP optimizer, so it can be used as a drop-in
replacement.

Requirements:
    pip install emhass  (optional dependency, not required for built-in LP)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from config import get_config
from models import DailySchedule, EVSlotData, HourlySlot, OptimizationGoal

logger = logging.getLogger(__name__)

N_HOURS = 24

# Check if EMHASS is available
_emhass_available = False
try:
    from emhass.optimize import Optimize
    from emhass.retrieve_hass import RetrieveHass
    _emhass_available = True
except ImportError:
    logger.info("EMHASS not installed — using built-in LP optimizer")


def is_emhass_available() -> bool:
    """Check if EMHASS package is installed."""
    return _emhass_available


class EMHASSOptimizer:
    """
    EMHASS-based optimization backend.

    When EMHASS is not installed, falls back to the built-in LP optimizer
    transparently. This allows users to optionally install EMHASS for
    advanced features (ML forecasting, more constraint types).

    The optimizer accepts the same inputs as LinearOptimizer.optimize()
    and returns a DailySchedule.
    """

    def __init__(self):
        self._cfg = get_config()
        self._last_schedule: Optional[DailySchedule] = None
        self._fallback = None  # Lazy-loaded LinearOptimizer

    def _get_fallback(self):
        """Get built-in LP optimizer as fallback."""
        if self._fallback is None:
            from optimizer.linear import LinearOptimizer
            self._fallback = LinearOptimizer()
        return self._fallback

    def optimize(
        self,
        prices_ct: list[float],
        pv_forecast_w: list[float],
        house_load_w: list[float],
        battery_soc: float,
        ev_soc: Optional[float],
        ev_target_soc: int = 80,
        ev_departure_h: int = 7,
        goal: OptimizationGoal = OptimizationGoal.COST,
    ) -> DailySchedule:
        """Run EMHASS optimization, falling back to built-in LP if unavailable."""
        if not _emhass_available:
            logger.debug("EMHASS not available, using built-in LP optimizer")
            return self._get_fallback().optimize(
                prices_ct=prices_ct,
                pv_forecast_w=pv_forecast_w,
                house_load_w=house_load_w,
                battery_soc=battery_soc,
                ev_soc=ev_soc,
                ev_target_soc=ev_target_soc,
                ev_departure_h=ev_departure_h,
                goal=goal,
            )

        try:
            return self._run_emhass(
                prices_ct, pv_forecast_w, house_load_w,
                battery_soc, ev_soc, ev_target_soc, ev_departure_h, goal,
            )
        except Exception as e:
            logger.error("EMHASS optimization failed, falling back to LP: %s", e)
            return self._get_fallback().optimize(
                prices_ct=prices_ct,
                pv_forecast_w=pv_forecast_w,
                house_load_w=house_load_w,
                battery_soc=battery_soc,
                ev_soc=ev_soc,
                ev_target_soc=ev_target_soc,
                ev_departure_h=ev_departure_h,
                goal=goal,
            )

    def _run_emhass(
        self,
        prices_ct: list[float],
        pv_forecast_w: list[float],
        house_load_w: list[float],
        battery_soc: float,
        ev_soc: Optional[float],
        ev_target_soc: int,
        ev_departure_h: int,
        goal: OptimizationGoal,
    ) -> DailySchedule:
        """Execute EMHASS day-ahead optimization."""
        cfg = self._cfg
        N = N_HOURS

        # Pad inputs to 24h
        prices = _pad(prices_ct, N, cfg.fixed_price_ct_kwh)
        pv = _pad(pv_forecast_w, N, 0.0)
        house = _pad(house_load_w, N, 500.0)

        # Build EMHASS configuration
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        freq = 60  # 60-minute intervals

        # Create time index for EMHASS
        import pandas as pd
        time_index = pd.date_range(start=now, periods=N, freq=f"{freq}min")

        # Convert prices from ct/kWh to EUR/kWh (EMHASS expects EUR/kWh)
        prices_eur_kwh = [p / 100.0 for p in prices]

        # Build EMHASS input DataFrames
        df_input = pd.DataFrame({
            "pv_power_forecast": pv,
            "load_power_forecast": house,
            "price_forecast": prices_eur_kwh,
        }, index=time_index)

        # EMHASS optimization parameters
        params = {
            "num_def_loads": len(cfg.deferrable_loads),
            "P_deferrable_nom": [dl.power_w for dl in cfg.deferrable_loads] or [0],
            "def_total_hours": [dl.duration_h for dl in cfg.deferrable_loads] or [0],
            "set_def_constant": [True] * max(1, len(cfg.deferrable_loads)),
            "treat_def_as_semi_cont": [False] * max(1, len(cfg.deferrable_loads)),
            "set_nocharge_from_grid": not cfg.ev_allow_grid_to_charge_ev,
            "set_nodischarge_to_grid": True,
            "set_total_pv_sell": False,
            "lp_solver": "COIN_CMD",  # Fallback solver; HiGHS preferred
            "lp_solver_path": "cbc",
        }

        # Battery parameters
        bat_params = {
            "set_use_battery": cfg.battery_capacity_kwh > 0,
            "Pd_max": cfg.battery_max_discharge_w / 1000.0,  # kW
            "Pc_max": cfg.battery_max_charge_w / 1000.0,  # kW
            "eta_disch": cfg.battery_efficiency,
            "eta_ch": cfg.battery_efficiency,
            "Enom": cfg.battery_capacity_kwh,
            "SOCmin": cfg.battery_min_soc / 100.0,
            "SOCmax": 1.0,
            "SOCinit": battery_soc / 100.0,
        }

        # Objective weight based on goal
        if goal == OptimizationGoal.COST:
            w_cost = 1.0
            w_self = 0.0
        elif goal == OptimizationGoal.SELF_CONSUMPTION:
            w_cost = 0.2
            w_self = 0.8
        else:  # BALANCED
            w_cost = 0.5
            w_self = 0.5

        # Run EMHASS Optimize
        opt = Optimize(
            retrieve_hass_conf={},
            optim_conf=params,
            plant_conf=bat_params,
            var_load_cost="price_forecast",
            var_prod_price="price_forecast",
            freq=freq,
            days_list=time_index,
        )

        # Day-ahead optimization
        opt_res = opt.perform_dayahead_forecast_optim(
            df_input, df_input["pv_power_forecast"], df_input["load_power_forecast"]
        )

        # Convert EMHASS results to DailySchedule
        return self._emhass_to_schedule(
            opt_res, prices, pv, house, battery_soc, ev_soc,
            ev_target_soc, goal, now,
        )

    def _emhass_to_schedule(
        self,
        opt_res,
        prices: list[float],
        pv: list[float],
        house: list[float],
        battery_soc: float,
        ev_soc: Optional[float],
        ev_target_soc: int,
        goal: OptimizationGoal,
        now: datetime,
    ) -> DailySchedule:
        """Convert EMHASS optimization result to DailySchedule."""
        cfg = self._cfg
        N = min(N_HOURS, len(opt_res))
        slots = []
        bat_soc = battery_soc / 100.0
        bat_cap_wh = cfg.battery_capacity_kwh * 1000.0
        bat_eff = cfg.battery_efficiency
        total_cost = 0.0

        for t in range(N):
            row = opt_res.iloc[t] if hasattr(opt_res, 'iloc') else {}

            # Extract EMHASS results (column names may vary)
            grid_import = max(0.0, _get_col(row, "P_grid_import", "P_grid_pos", 0.0) * 1000)
            grid_export = max(0.0, _get_col(row, "P_grid_export", "P_grid_neg", 0.0) * 1000)
            bat_charge = max(0.0, _get_col(row, "P_batt_charge", "P_batt_pos", 0.0) * 1000)
            bat_discharge = max(0.0, _get_col(row, "P_batt_discharge", "P_batt_neg", 0.0) * 1000)

            # SOC tracking
            bat_soc += (bat_charge * bat_eff - bat_discharge / bat_eff) / bat_cap_wh
            bat_soc = max(cfg.battery_min_soc / 100.0, min(1.0, bat_soc))

            # Deferrable load states
            load_states = {}
            for li, load in enumerate(cfg.deferrable_loads):
                col_name = f"P_deferrable{li}"
                load_power = _get_col(row, col_name, f"P_def_{li}", 0.0) * 1000
                load_states[load.switch] = load_power > 50  # Threshold

            cost_eur = grid_import * prices[t] / 100.0 / 1000.0
            cost_eur -= grid_export * cfg.price_feed_in_ct_kwh / 100.0 / 1000.0
            total_cost += cost_eur

            slots.append(HourlySlot(
                hour=now + timedelta(hours=t),
                battery_charge_w=bat_charge,
                battery_discharge_w=bat_discharge,
                ev_charge_w=0.0,  # EMHASS doesn't natively optimize EV
                ev_current_a=0,
                grid_import_w=grid_import,
                grid_export_w=grid_export,
                battery_soc_end=bat_soc * 100.0,
                deferrable_loads=load_states,
                cost_eur=cost_eur,
                price_ct=prices[t],
            ))

        schedule = DailySchedule(
            slots=slots,
            total_cost_eur=total_cost,
            optimization_goal=goal,
            solver_status="emhass_ok",
        )
        self._last_schedule = schedule
        logger.info("EMHASS optimization complete. Total cost: €%.3f", total_cost)
        return schedule

    def get_current_slot(self) -> Optional[HourlySlot]:
        """Return the schedule slot for the current hour."""
        if not self._last_schedule:
            return None
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        for slot in self._last_schedule.slots:
            if slot.hour == now:
                return slot
        return None


def _pad(lst: list[float], n: int, default: float) -> list[float]:
    if len(lst) >= n:
        return lst[:n]
    return lst + [default] * (n - len(lst))


def _get_col(row, *names, default=0.0):
    """Try multiple column names to extract a value from a DataFrame row."""
    for name in names:
        try:
            val = row.get(name, None) if hasattr(row, 'get') else getattr(row, name, None)
            if val is not None:
                return float(val)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return default


# Global singleton
_emhass_optimizer: Optional[EMHASSOptimizer] = None


def get_emhass_optimizer() -> EMHASSOptimizer:
    global _emhass_optimizer
    if _emhass_optimizer is None:
        _emhass_optimizer = EMHASSOptimizer()
    return _emhass_optimizer
