"""EOS-style 48h genetic algorithm planner."""
from __future__ import annotations

import logging
import random
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from config import get_config
from models import HourlySlot, LongTermPlan, TimeWindow

logger = logging.getLogger(__name__)

# Genetic algorithm parameters - tuned for Raspberry Pi 4 performance
N_HOURS = 48
POPULATION_SIZE = 50        # Small enough for RPi4 (50 * 100 = 5000 fitness evaluations)
N_GENERATIONS = 100          # ~15-20 seconds total runtime on RPi4
MUTATION_RATE = 0.05        # 5% gene mutation rate for exploration
TOURNAMENT_SIZE = 5         # Tournament selection size
ELITE_FRACTION = 0.2        # Keep top 20% (10 chromosomes) unchanged


class Chromosome:
    """
    Represents one complete 48h control scenario.

    Each hour has 3 genes:
      [0] battery_mode: 0=idle, 1=charge, 2=discharge
      [1] ev_charge:    0=off, 1=on
      [2] load_fraction: 0.0..1.0 fraction of loads active
    """

    def __init__(self, n_hours: int = N_HOURS):
        self.genes = [
            [
                random.randint(0, 2),   # battery_mode
                random.randint(0, 1),   # ev_charge
                random.uniform(0, 1),   # load_fraction
            ]
            for _ in range(n_hours)
        ]
        self.fitness: float = float("inf")  # lower = better (cost)

    def mutate(self) -> None:
        for t in range(len(self.genes)):
            if random.random() < MUTATION_RATE:
                self.genes[t][0] = random.randint(0, 2)
            if random.random() < MUTATION_RATE:
                self.genes[t][1] = random.randint(0, 1)
            if random.random() < MUTATION_RATE:
                self.genes[t][2] = random.uniform(0, 1)

    @staticmethod
    def crossover(a: "Chromosome", b: "Chromosome") -> "Chromosome":
        point = random.randint(1, len(a.genes) - 1)
        child = Chromosome(len(a.genes))
        child.genes = deepcopy(a.genes[:point]) + deepcopy(b.genes[point:])
        return child


class GeneticPlanner:
    """
    EOS-style 48h genetic algorithm optimizer.

    Finds the lowest-cost battery + EV + load schedule over the next 48 hours
    by simulating many random scenarios and evolving towards the best solution.
    """

    def __init__(self):
        self._cfg = get_config()
        self._last_plan: Optional[LongTermPlan] = None

    def optimize_48h(
        self,
        pv_forecast_w: list[float],
        price_forecast_ct: list[float],
        battery_soc: float,
        ev_soc: Optional[float] = None,
        ev_target_soc: int = 80,
    ) -> LongTermPlan:
        """
        Run the genetic algorithm and return the optimal 48h plan.
        """
        cfg = self._cfg

        pv = self._pad(pv_forecast_w, N_HOURS, 0.0)
        prices = self._pad(price_forecast_ct, N_HOURS, cfg.fixed_price_ct_kwh)

        # Initialize population
        population = [Chromosome() for _ in range(POPULATION_SIZE)]

        # Evaluate initial fitness
        for chrom in population:
            chrom.fitness = self._evaluate_fitness(
                chrom, pv, prices, battery_soc, ev_soc, ev_target_soc
            )

        # Evolve
        best_fitness_history = []
        for gen in range(N_GENERATIONS):
            population.sort(key=lambda c: c.fitness)
            best_fitness_history.append(population[0].fitness)
            
            # Log convergence every 20 generations (skip gen 0, start at 20)
            if gen > 0 and gen % 20 == 0:
                improvement = best_fitness_history[0] - population[0].fitness
                logger.debug("Generation %d: best fitness %.3f EUR (improved %.3f EUR from start)",
                           gen, population[0].fitness, improvement)
            
            n_elite = max(1, int(POPULATION_SIZE * ELITE_FRACTION))
            next_gen = deepcopy(population[:n_elite])

            while len(next_gen) < POPULATION_SIZE:
                parent_a = self._tournament_select(population)
                parent_b = self._tournament_select(population)
                child = Chromosome.crossover(parent_a, parent_b)
                child.mutate()
                child.fitness = self._evaluate_fitness(
                    child, pv, prices, battery_soc, ev_soc, ev_target_soc
                )
                next_gen.append(child)

            population = next_gen

        best = min(population, key=lambda c: c.fitness)
        plan = self._build_plan(best, pv, prices, battery_soc, ev_soc)
        self._last_plan = plan

        # Log final convergence stats
        initial_best = best_fitness_history[0]
        final_best = best.fitness
        improvement = initial_best - final_best
        logger.info("Genetic planner complete. Best fitness: %.3f EUR over 48h (improved %.3f EUR from initial)",
                   best.fitness, improvement)
        return plan

    def _evaluate_fitness(
        self,
        chrom: Chromosome,
        pv: list[float],
        prices: list[float],
        bat_soc_init: float,
        ev_soc_init: Optional[float],
        ev_target_soc: int,
    ) -> float:
        """Simulate energy flows and compute total cost for this chromosome."""
        cfg = self._cfg
        bat_soc = bat_soc_init / 100.0
        ev_soc = (ev_soc_init or 0.0) / 100.0
        bat_cap = cfg.battery_capacity_kwh * 1000.0
        bat_eff = cfg.battery_efficiency
        bat_min = cfg.battery_min_soc / 100.0
        bat_max = 1.0
        bat_max_ch = cfg.battery_max_charge_w
        bat_max_dis = cfg.battery_max_discharge_w
        ev_cap = cfg.ev_battery_capacity_kwh * 1000.0
        ev_max_ch = cfg.ev_max_charge_current_a * cfg.goe_phases * 230.0
        feed_in = cfg.price_feed_in_ct_kwh

        total_cost = 0.0
        ev_deadline_penalty = 0.0

        for t in range(N_HOURS):
            pv_w = pv[t]
            price_ct = prices[t]
            gene = chrom.genes[t]
            bat_mode = gene[0]  # 0=idle, 1=charge, 2=discharge
            ev_on = gene[1]
            load_frac = gene[2]

            # House load (simplified: 300W base + load fraction)
            house_load = 300.0 + load_frac * sum(
                dl.power_w for dl in cfg.deferrable_loads
            ) / max(1, len(cfg.deferrable_loads))

            # EV charging
            ev_ch_w = 0.0
            if ev_on and ev_soc < ev_target_soc / 100.0:
                ev_ch_w = min(ev_max_ch, (ev_target_soc / 100.0 - ev_soc) * ev_cap)
                ev_soc += ev_ch_w / ev_cap

            # Battery
            bat_ch_w = 0.0
            bat_dis_w = 0.0
            if bat_mode == 1 and bat_soc < bat_max:  # charge
                bat_ch_w = min(bat_max_ch, (bat_max - bat_soc) * bat_cap / bat_eff)
                bat_soc += bat_ch_w * bat_eff / bat_cap
            elif bat_mode == 2 and bat_soc > bat_min:  # discharge
                bat_dis_w = min(bat_max_dis, (bat_soc - bat_min) * bat_cap * bat_eff)
                bat_soc -= bat_dis_w / bat_eff / bat_cap
            bat_soc = max(bat_min, min(bat_max, bat_soc))

            # Grid balance
            net_demand = house_load + bat_ch_w + ev_ch_w - pv_w - bat_dis_w
            if net_demand > 0:
                grid_import = net_demand
                total_cost += grid_import * price_ct / 100.0 / 1000.0
            else:
                grid_export = -net_demand
                total_cost -= grid_export * feed_in / 100.0 / 1000.0

        # Penalty: EV didn't reach target SOC by t=24h (departure)
        if ev_soc_init is not None and ev_soc < ev_target_soc / 100.0 - 0.05:
            ev_deadline_penalty = 10.0  # heavy penalty

        return total_cost + ev_deadline_penalty

    def _tournament_select(self, population: list[Chromosome]) -> Chromosome:
        contestants = random.sample(population, min(TOURNAMENT_SIZE, len(population)))
        return min(contestants, key=lambda c: c.fitness)

    def _build_plan(
        self,
        chrom: Chromosome,
        pv: list[float],
        prices: list[float],
        bat_soc_init: float,
        ev_soc_init: Optional[float],
    ) -> LongTermPlan:
        cfg = self._cfg
        bat_soc = bat_soc_init / 100.0
        bat_cap = cfg.battery_capacity_kwh * 1000.0
        bat_eff = cfg.battery_efficiency
        bat_min = cfg.battery_min_soc / 100.0
        ev_soc = (ev_soc_init or 0.0) / 100.0
        ev_cap = cfg.ev_battery_capacity_kwh * 1000.0
        ev_max_ch = cfg.ev_max_charge_current_a * cfg.goe_phases * 230.0

        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        slots = []
        total_cost = 0.0
        cheap_windows: list[TimeWindow] = []
        in_cheap = False
        cheap_start: Optional[datetime] = None
        avg_prices: list[float] = []

        for t in range(N_HOURS):
            gene = chrom.genes[t]
            bat_mode = gene[0]
            ev_on = gene[1]
            price_ct = prices[t]

            ev_ch_w = ev_max_ch * ev_on if ev_on and ev_soc < 1.0 else 0.0
            ev_soc = min(1.0, ev_soc + ev_ch_w / ev_cap)

            bat_ch_w = 0.0
            bat_dis_w = 0.0
            if bat_mode == 1 and bat_soc < 1.0:
                bat_ch_w = min(float(cfg.battery_max_charge_w),
                               (1.0 - bat_soc) * bat_cap / bat_eff)
                bat_soc = min(1.0, bat_soc + bat_ch_w * bat_eff / bat_cap)
            elif bat_mode == 2 and bat_soc > bat_min:
                bat_dis_w = min(float(cfg.battery_max_discharge_w),
                                (bat_soc - bat_min) * bat_cap * bat_eff)
                bat_soc = max(bat_min, bat_soc - bat_dis_w / bat_eff / bat_cap)

            house = 300.0
            net = house + bat_ch_w + ev_ch_w - pv[t] - bat_dis_w
            imp = max(0.0, net)
            exp = max(0.0, -net)

            cost_eur = imp * price_ct / 100.0 / 1000.0 - exp * cfg.price_feed_in_ct_kwh / 100.0 / 1000.0
            total_cost += cost_eur

            # Track cheap windows
            threshold = sorted(prices)[len(prices) // 4]  # 25th percentile
            slot_hour = now + timedelta(hours=t)
            if price_ct <= threshold:
                if not in_cheap:
                    in_cheap = True
                    cheap_start = slot_hour
                    avg_prices = [price_ct]
                else:
                    avg_prices.append(price_ct)
            else:
                if in_cheap and cheap_start:
                    cheap_windows.append(TimeWindow(
                        start=cheap_start,
                        end=slot_hour,
                        avg_price_ct=sum(avg_prices) / len(avg_prices),
                        min_price_ct=min(avg_prices),
                    ))
                in_cheap = False

            slots.append(HourlySlot(
                hour=slot_hour,
                battery_charge_w=bat_ch_w,
                battery_discharge_w=bat_dis_w,
                ev_charge_w=ev_ch_w,
                ev_current_a=int(ev_ch_w / (cfg.goe_phases * 230)) if ev_ch_w > 0 else 0,
                grid_import_w=imp,
                grid_export_w=exp,
                battery_soc_end=bat_soc * 100.0,
                ev_soc_end=ev_soc * 100.0,
                cost_eur=cost_eur,
                price_ct=price_ct,
            ))

        # Recommended battery reserve: keep enough for EV charging at next cheap window
        battery_reserve = cfg.battery_reserve_soc

        return LongTermPlan(
            slots=slots,
            total_cost_eur=total_cost,
            battery_reserve_soc=float(battery_reserve),
            recommended_battery_charge_windows=cheap_windows[:5],
        )

    @staticmethod
    def _pad(lst: list[float], n: int, default: float) -> list[float]:
        if len(lst) >= n:
            return lst[:n]
        return lst + [default] * (n - len(lst))

    @property
    def last_plan(self) -> Optional[LongTermPlan]:
        return self._last_plan


# Global singleton
_planner: Optional[GeneticPlanner] = None


def get_genetic_planner() -> GeneticPlanner:
    global _planner
    if _planner is None:
        _planner = GeneticPlanner()
    return _planner
