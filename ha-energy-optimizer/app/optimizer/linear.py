"""EMHASS-style LP optimization using scipy.optimize.linprog (HiGHS solver)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
from scipy.optimize import linprog

from config import get_config
from models import DailySchedule, EVSlotData, HourlySlot, OptimizationGoal

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

    def _get_ev_params(self, ev_soc: Optional[float], ev_target_soc: int,
                        ev_departure_h: int,
                        ev_soc_map: Optional[dict[str, float]] = None) -> list[dict]:
        """Build EV parameter list from ev_configs or legacy single-EV config."""
        cfg = self._cfg
        soc_map = ev_soc_map or {}
        if cfg.ev_configs:
            params = []
            for ev in cfg.ev_configs:
                params.append({
                    "name": ev.name,
                    "cap_wh": ev.battery_capacity_kwh * 1000.0,
                    "init_soc": soc_map.get(ev.name, 0.0) / 100.0,
                    "max_ch_w": ev.max_charge_current_a * ev.phases * 230.0,
                    "target_soc": ev.target_soc / 100.0,
                    "departure_h": ev_departure_h,
                    "allow_grid": ev.allow_grid_charging,
                    "phases": ev.phases,
                })
            return params
        # Legacy single-EV fallback
        return [{
            "name": "EV",
            "cap_wh": cfg.ev_battery_capacity_kwh * 1000.0,
            "init_soc": (ev_soc or 0.0) / 100.0,
            "max_ch_w": cfg.ev_max_charge_current_a * cfg.goe_phases * 230.0,
            "target_soc": ev_target_soc / 100.0,
            "departure_h": ev_departure_h,
            "allow_grid": cfg.ev_allow_grid_to_charge_ev,
            "phases": cfg.goe_phases,
        }]

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
        ev_soc_map: Optional[dict[str, float]] = None,
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
        house = self._pad(house_load_w, N, 500.0)
        feed_in = cfg.price_feed_in_ct_kwh

        bat_cap_wh = cfg.battery_capacity_kwh * 1000.0
        bat_min_soc = cfg.battery_min_soc / 100.0
        bat_init_soc = battery_soc / 100.0
        bat_eff = cfg.battery_efficiency
        bat_max_ch = float(cfg.battery_max_charge_w)
        bat_max_dis = float(cfg.battery_max_discharge_w)

        # Multi-EV parameters
        ev_params = self._get_ev_params(ev_soc, ev_target_soc, ev_departure_h, ev_soc_map)
        n_evs = len(ev_params)
        n_loads = len(cfg.deferrable_loads)

        # Variable layout:
        # [bat_ch(N), bat_dis(N), ev_ch_0(N), ..., ev_ch_k(N), grid_imp(N), grid_exp(N), load_on(n_loads*N)]
        ev_start = 2 * N  # First EV block starts at index 2*N
        grid_imp_start = ev_start + n_evs * N
        grid_exp_start = grid_imp_start + N
        load_start = grid_exp_start + N
        n_total = load_start + n_loads * N

        # ---- Cost vector (minimize) ----
        c = np.zeros(n_total)
        c[grid_imp_start:grid_imp_start + N] = [p / 100.0 for p in prices]
        c[grid_exp_start:grid_exp_start + N] = -feed_in / 100.0

        if goal == OptimizationGoal.SELF_CONSUMPTION:
            c[N:2 * N] -= 0.001
        elif goal == OptimizationGoal.BALANCED:
            c[grid_imp_start:grid_imp_start + N] *= 0.7

        # ---- Bounds ----
        bounds = (
            [(0, bat_max_ch)] * N
            + [(0, bat_max_dis)] * N
        )
        for ev in ev_params:
            bounds += [(0, ev["max_ch_w"])] * N
        bounds += (
            [(0, float(cfg.grid_max_import_w or 1e7))] * N
            + [(0, None)] * N
            + [(0, 1)] * (n_loads * N)
        )

        # ---- Equality constraints (energy balance per hour) ----
        A_eq = np.zeros((N, n_total))
        b_eq = np.zeros(N)
        for t in range(N):
            A_eq[t, t] = 1.0                         # bat_charge
            A_eq[t, N + t] = -1.0                     # bat_discharge
            for ei in range(n_evs):
                A_eq[t, ev_start + ei * N + t] = 1.0  # ev_charge (each EV)
            A_eq[t, grid_imp_start + t] = 1.0          # grid_import
            A_eq[t, grid_exp_start + t] = -1.0         # grid_export
            for li, load in enumerate(cfg.deferrable_loads):
                A_eq[t, load_start + li * N + t] = load.power_w
            b_eq[t] = house[t] - pv[t]

        # ---- Inequality constraints ----
        ineq_rows = []
        ineq_b = []

        # Battery SOC dynamics
        for t in range(N):
            row_min = np.zeros(n_total)
            for i in range(t + 1):
                row_min[i] = -bat_eff
                row_min[N + i] = 1.0 / bat_eff
            ineq_rows.append(row_min)
            ineq_b.append((bat_init_soc - bat_min_soc) * bat_cap_wh)

            row_max = np.zeros(n_total)
            for i in range(t + 1):
                row_max[i] = bat_eff
                row_max[N + i] = -1.0 / bat_eff
            ineq_rows.append(row_max)
            ineq_b.append((1.0 - bat_init_soc) * bat_cap_wh)

        # Per-EV: must reach target SOC by departure hour
        for ei, ev in enumerate(ev_params):
            if not ev["allow_grid"]:
                continue
            ev_needed_wh = max(0.0, (ev["target_soc"] - ev["init_soc"]) * ev["cap_wh"])
            if ev_needed_wh <= 0:
                continue
            row_ev = np.zeros(n_total)
            for t in range(min(ev["departure_h"], N)):
                row_ev[ev_start + ei * N + t] = -1.0
            ineq_rows.append(row_ev)
            ineq_b.append(-ev_needed_wh)

        # Grid import limit
        if cfg.grid_max_import_w > 0:
            for t in range(N):
                row = np.zeros(n_total)
                row[grid_imp_start + t] = 1.0
                ineq_rows.append(row)
                ineq_b.append(float(cfg.grid_max_import_w))

        # Deferrable load constraints
        for li, load in enumerate(cfg.deferrable_loads):
            base = load_start + li * N
            duration_slots = load.duration_h
            row_dur = np.zeros(n_total)
            for t in range(N):
                hour = (datetime.now().hour + t) % 24
                # Handle midnight wrap-around (e.g. earliest=22, latest=8)
                if load.earliest_start_h <= load.latest_end_h:
                    in_window = load.earliest_start_h <= hour < load.latest_end_h
                else:
                    in_window = hour >= load.earliest_start_h or hour < load.latest_end_h
                if in_window:
                    row_dur[base + t] = -1.0
            ineq_rows.append(row_dur)
            ineq_b.append(-duration_slots)

            for t in range(N):
                if prices[t] > load.price_limit_ct_kwh:
                    row_price = np.zeros(n_total)
                    row_price[base + t] = 1.0
                    ineq_rows.append(row_price)
                    ineq_b.append(0.0)

        # Assemble inequality matrix
        if ineq_rows:
            A_ub = np.array(ineq_rows)
            b_ub = np.array(ineq_b)
        else:
            A_ub = None
            b_ub = None

        # ---- Solve ----
        try:
            result = linprog(
                c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                bounds=bounds, method="highs",
            )
        except Exception as e:
            logger.error("LP solver failed: %s", e)
            return self._empty_schedule()

        if not result.success:
            logger.warning("LP solver did not converge: %s", result.message)

        x = result.x if result.x is not None else np.zeros(n_total)
        return self._build_schedule(
            x, prices, pv, house, bat_init_soc, bat_cap_wh, bat_eff,
            ev_params, n_loads, ev_start, grid_imp_start, grid_exp_start,
            load_start, result.message, goal,
        )

    def _build_schedule(self, x, prices, pv, house, bat_init_soc, bat_cap_wh,
                        bat_eff, ev_params, n_loads, ev_start, grid_imp_start,
                        grid_exp_start, load_start, solver_msg, goal) -> DailySchedule:
        N = N_HOURS
        cfg = self._cfg
        n_evs = len(ev_params)
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        slots = []
        bat_soc = bat_init_soc
        ev_socs = [ev["init_soc"] for ev in ev_params]
        total_cost = 0.0

        for t in range(N):
            ch = max(0.0, x[t])
            dis = max(0.0, x[N + t])
            imp = max(0.0, x[grid_imp_start + t])
            exp = max(0.0, x[grid_exp_start + t])

            bat_soc += (ch * bat_eff - dis / bat_eff) / bat_cap_wh
            bat_soc = max(cfg.battery_min_soc / 100.0, min(1.0, bat_soc))

            # Per-EV charge data
            ev_slot_data = []
            total_ev_ch = 0.0
            for ei, ev in enumerate(ev_params):
                ev_ch = max(0.0, x[ev_start + ei * N + t])
                total_ev_ch += ev_ch
                ev_socs[ei] += ev_ch / ev["cap_wh"]
                ev_socs[ei] = min(1.0, ev_socs[ei])
                ev_slot_data.append(EVSlotData(
                    name=ev["name"],
                    charge_w=ev_ch,
                    current_a=int(ev_ch / (ev["phases"] * 230)) if ev_ch > 0 else 0,
                    soc_end=ev_socs[ei] * 100.0,
                ))

            load_states = {}
            for li, load in enumerate(cfg.deferrable_loads):
                load_states[load.switch] = x[load_start + li * N + t] > 0.5

            cost_eur = imp * prices[t] / 100.0 / 1000.0
            cost_eur -= exp * cfg.price_feed_in_ct_kwh / 100.0 / 1000.0
            total_cost += cost_eur

            # For backward compat, ev_charge_w/ev_current_a = sum of all EVs
            primary_phases = ev_params[0]["phases"] if ev_params else cfg.goe_phases
            slots.append(HourlySlot(
                hour=now + timedelta(hours=t),
                battery_charge_w=ch,
                battery_discharge_w=dis,
                ev_charge_w=total_ev_ch,
                ev_current_a=int(total_ev_ch / (primary_phases * 230)) if total_ev_ch > 0 else 0,
                grid_import_w=imp,
                grid_export_w=exp,
                battery_soc_end=bat_soc * 100.0,
                ev_soc_end=ev_socs[0] * 100.0 if ev_socs else None,
                ev_slots=ev_slot_data,
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
        logger.info("LP optimization complete. %d EVs, Total cost: €%.3f, status: %s",
                    n_evs, total_cost, solver_msg[:30])
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
