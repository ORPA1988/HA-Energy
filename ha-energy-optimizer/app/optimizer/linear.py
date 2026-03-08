"""EMHASS-style LP optimization using scipy.optimize.linprog (HiGHS solver)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
from scipy.optimize import linprog

from config import get_config
from models import DailySchedule, HourlySlot, OptimizationGoal

logger = logging.getLogger(__name__)

N_HOURS = 24


class LinearOptimizer:
    """
    Solves the home energy management problem as a linear program over 24h.

    Decision variables per hour t (total = N_HOURS * n_vars):
      x[0..N]   battery_charge_w     [0, bat_max_charge]
      x[N..2N]  battery_discharge_w  [0, bat_max_discharge]
      x[2N..3N] ev_charge_w          [0, ev_max_charge]
      x[3N..4N] grid_import_w        [0, grid_max_import]
      x[4N..5N] grid_export_w        [0, inf]
      x[5N..5N+n_loads*N] load_on    [0, 1] (relaxed binary for each deferrable load)

    Objective (minimize):
      sum_t( grid_import_w[t] * price[t] ) - sum_t( grid_export_w[t] * feed_in )

    Constraints:
      1. Energy balance per hour
      2. Battery SOC dynamics
      3. SOC bounds [min_soc, 100]
      4. EV must reach target SOC by departure
      5. Each deferrable load must run for required duration within allowed window
      6. Grid import limit (if set)
    """

    def __init__(self):
        self._cfg = get_config()
        self._last_schedule: Optional[DailySchedule] = None

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
        """Run LP optimization and return a 24h schedule."""
        cfg = self._cfg
        N = N_HOURS

        # Validate inputs
        if len(prices_ct) < N:
            logger.warning("Price forecast has only %d hours, expected %d. Padding with fallback.", 
                         len(prices_ct), N)
        if len(pv_forecast_w) < N:
            logger.warning("PV forecast has only %d hours, expected %d. Padding with zeros.", 
                         len(pv_forecast_w), N)
        if len(house_load_w) < N:
            logger.warning("House load profile has only %d hours, expected %d. Padding with 500W.", 
                         len(house_load_w), N)

        # Extend/pad inputs to 24h
        prices = self._pad(prices_ct, N, cfg.fixed_price_ct_kwh)
        pv = self._pad(pv_forecast_w, N, 0.0)
        house = self._pad(house_load_w, N, 500.0)  # default 500W baseline load
        feed_in = cfg.price_feed_in_ct_kwh

        bat_cap_wh = cfg.battery_capacity_kwh * 1000.0
        bat_min_soc = cfg.battery_min_soc / 100.0
        bat_init_soc = battery_soc / 100.0
        bat_eff = cfg.battery_efficiency
        bat_max_ch = float(cfg.battery_max_charge_w)
        bat_max_dis = float(cfg.battery_max_discharge_w)

        ev_cap_wh = cfg.ev_battery_capacity_kwh * 1000.0
        ev_init_soc = (ev_soc or 0.0) / 100.0
        ev_max_ch_w = cfg.ev_max_charge_current_a * cfg.goe_phases * 230.0

        n_loads = len(cfg.deferrable_loads)

        # Variable layout:
        # [bat_ch(N), bat_dis(N), ev_ch(N), grid_imp(N), grid_exp(N), load_on(n_loads*N)]
        n_base = 5 * N
        n_total = n_base + n_loads * N

        # ---- Cost vector (minimize) ----
        c = np.zeros(n_total)
        # Grid import cost
        c[3 * N:4 * N] = [p / 100.0 for p in prices]  # ct → EUR per 1W·h
        # Grid export revenue (negative cost)
        c[4 * N:5 * N] = -feed_in / 100.0

        # Self-consumption bonus: slightly prefer battery discharge over grid import
        if goal == OptimizationGoal.SELF_CONSUMPTION:
            c[N:2 * N] -= 0.001  # tiny incentive to discharge
        elif goal == OptimizationGoal.BALANCED:
            c[3 * N:4 * N] *= 0.7  # reduce weight on cost vs self-consumption

        # ---- Bounds ----
        bounds = (
            [(0, bat_max_ch)] * N          # battery charge
            + [(0, bat_max_dis)] * N       # battery discharge
            + [(0, ev_max_ch_w)] * N       # EV charge
            + [(0, float(cfg.grid_max_import_w or 1e7))] * N  # grid import
            + [(0, None)] * N              # grid export
            + [(0, 1)] * (n_loads * N)     # deferrable load fractions
        )

        # ---- Equality constraints (energy balance per hour) ----
        A_eq = np.zeros((N, n_total))
        b_eq = np.zeros(N)
        for t in range(N):
            A_eq[t, t] = 1.0           # bat_charge
            A_eq[t, N + t] = -1.0      # bat_discharge
            A_eq[t, 2 * N + t] = 1.0  # ev_charge
            A_eq[t, 3 * N + t] = 1.0  # grid_import
            A_eq[t, 4 * N + t] = -1.0 # grid_export
            # Load power included in house_load, so it's already accounted for
            b_eq[t] = house[t] - pv[t]  # net demand (positive = need from grid/battery)

        # ---- Inequality constraints ----
        ineq_rows = []
        ineq_b = []

        # Battery SOC dynamics (rolling constraint):
        # soc[t] = soc_init + sum_{i<=t}(ch[i]*eff - dis[i]/eff) / bat_cap
        # soc[t] >= bat_min_soc  →  -sum(ch*eff - dis/eff) <= soc_init - bat_min_soc) * bat_cap
        # soc[t] <= 1.0          →  +sum(ch*eff - dis/eff) <= (1 - soc_init) * bat_cap
        for t in range(N):
            # SOC minimum constraint
            row_min = np.zeros(n_total)
            for i in range(t + 1):
                row_min[i] = -bat_eff          # -ch[i]*eff
                row_min[N + i] = 1.0 / bat_eff # +dis[i]/eff
            ineq_rows.append(row_min)
            ineq_b.append((bat_init_soc - bat_min_soc) * bat_cap_wh)

            # SOC maximum constraint
            row_max = np.zeros(n_total)
            for i in range(t + 1):
                row_max[i] = bat_eff
                row_max[N + i] = -1.0 / bat_eff
            ineq_rows.append(row_max)
            ineq_b.append((1.0 - bat_init_soc) * bat_cap_wh)

        # EV must reach target SOC by departure hour
        if ev_soc is not None and cfg.ev_allow_grid_to_charge_ev:
            ev_needed_wh = max(0.0, (ev_target_soc / 100.0 - ev_init_soc) * ev_cap_wh)
            row_ev = np.zeros(n_total)
            for t in range(min(ev_departure_h, N)):
                row_ev[2 * N + t] = -1.0
            ineq_rows.append(row_ev)
            ineq_b.append(-ev_needed_wh)

        # Grid import limit
        if cfg.grid_max_import_w > 0:
            for t in range(N):
                row = np.zeros(n_total)
                row[3 * N + t] = 1.0
                ineq_rows.append(row)
                ineq_b.append(float(cfg.grid_max_import_w))

        # Deferrable load constraints
        for li, load in enumerate(cfg.deferrable_loads):
            base = n_base + li * N
            # Must run for required total duration
            duration_slots = load.duration_h
            row_dur = np.zeros(n_total)
            for t in range(N):
                hour = (datetime.now().hour + t) % 24
                # Restrict to allowed window [earliest_start, latest_end)
                if load.earliest_start_h <= hour or hour < load.latest_end_h:
                    row_dur[base + t] = -1.0
            ineq_rows.append(row_dur)
            ineq_b.append(-duration_slots)  # must run at least duration_slots hours

            # Price limit: don't run when price > limit
            for t in range(N):
                if prices[t] > load.price_limit_ct_kwh:
                    row_price = np.zeros(n_total)
                    row_price[base + t] = 1.0
                    ineq_rows.append(row_price)
                    ineq_b.append(0.0)  # load_on[t] <= 0

        # Assemble inequality matrix
        if ineq_rows:
            A_ub = np.array(ineq_rows)
            b_ub = np.array(ineq_b)
        else:
            A_ub = None
            b_ub = None

        # ---- Solve ----
        # Using HiGHS method (Interior Point) - memory efficient for RPi4
        # Alternative methods: 'simplex' (slower), 'revised simplex' (more memory)
        try:
            result = linprog(
                c,
                A_ub=A_ub,
                b_ub=b_ub,
                A_eq=A_eq,
                b_eq=b_eq,
                bounds=bounds,
                method="highs",  # Best for RPi4: fast and low memory
            )
        except Exception as e:
            logger.error("LP solver failed: %s", e)
            return self._empty_schedule()

        if not result.success:
            logger.warning("LP solver did not converge: %s", result.message)

        x = result.x if result.x is not None else np.zeros(n_total)
        return self._build_schedule(x, prices, pv, house, bat_init_soc, bat_cap_wh,
                                    bat_eff, ev_init_soc, ev_cap_wh, n_loads,
                                    result.message, goal)

    def _build_schedule(self, x, prices, pv, house, bat_init_soc, bat_cap_wh,
                        bat_eff, ev_init_soc, ev_cap_wh, n_loads, solver_msg, goal) -> DailySchedule:
        N = N_HOURS
        cfg = self._cfg
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        slots = []
        bat_soc = bat_init_soc
        ev_soc = ev_init_soc
        total_cost = 0.0

        for t in range(N):
            ch = max(0.0, x[t])
            dis = max(0.0, x[N + t])
            ev_ch = max(0.0, x[2 * N + t])
            imp = max(0.0, x[3 * N + t])
            exp = max(0.0, x[4 * N + t])

            bat_soc += (ch * bat_eff - dis / bat_eff) / bat_cap_wh
            bat_soc = max(cfg.battery_min_soc / 100.0, min(1.0, bat_soc))

            ev_soc += ev_ch / ev_cap_wh
            ev_soc = min(1.0, ev_soc)

            load_states = {}
            for li, load in enumerate(cfg.deferrable_loads):
                base = 5 * N + li * N
                load_states[load.switch] = x[base + t] > 0.5

            cost_eur = imp * prices[t] / 100.0 / 1000.0  # W * ct/kWh → EUR (1h interval)
            cost_eur -= exp * cfg.price_feed_in_ct_kwh / 100.0 / 1000.0
            total_cost += cost_eur

            slots.append(HourlySlot(
                hour=now + timedelta(hours=t),
                battery_charge_w=ch,
                battery_discharge_w=dis,
                ev_charge_w=ev_ch,
                ev_current_a=int(ev_ch / (cfg.goe_phases * 230)) if ev_ch > 0 else 0,
                grid_import_w=imp,
                grid_export_w=exp,
                battery_soc_end=bat_soc * 100.0,
                ev_soc_end=ev_soc * 100.0,
                deferrable_loads=load_states,
                cost_eur=cost_eur,
                price_ct=prices[t],
            ))

        schedule = DailySchedule(
            slots=slots,
            total_cost_eur=total_cost,
            optimization_goal=goal,
            solver_status=solver_msg[:50] if solver_msg else "ok",
        )
        self._last_schedule = schedule
        logger.info("LP optimization complete. Total cost: €%.3f, status: %s",
                    total_cost, solver_msg[:30])
        return schedule

    def _empty_schedule(self) -> DailySchedule:
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        return DailySchedule(
            slots=[HourlySlot(hour=now + timedelta(hours=t)) for t in range(N_HOURS)],
            solver_status="failed",
        )

    @staticmethod
    def _pad(lst: list[float], n: int, default: float) -> list[float]:
        if len(lst) >= n:
            return lst[:n]
        return lst + [default] * (n - len(lst))

    def get_current_slot(self) -> Optional[HourlySlot]:
        """Return the schedule slot for the current hour."""
        if not self._last_schedule:
            return None
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        for slot in self._last_schedule.slots:
            if slot.hour == now:
                return slot
        return None


# Global singleton
_optimizer: Optional[LinearOptimizer] = None


def get_linear_optimizer() -> LinearOptimizer:
    global _optimizer
    if _optimizer is None:
        _optimizer = LinearOptimizer()
    return _optimizer
