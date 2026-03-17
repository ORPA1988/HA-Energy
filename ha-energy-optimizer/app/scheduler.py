"""APScheduler job management for all optimization loops."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from main import AppState

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


def setup_jobs(app_state: "AppState") -> None:
    """Register all scheduled optimization jobs."""
    from config import get_config
    cfg = get_config()
    scheduler = get_scheduler()

    # 1. Realtime control loop (every 30s)
    scheduler.add_job(
        app_state.run_realtime_loop,
        trigger=IntervalTrigger(seconds=30),
        id="realtime",
        name="EV Realtime Control",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 2. LP optimization (hourly, configurable)
    scheduler.add_job(
        app_state.run_linear_optimization,
        trigger=IntervalTrigger(minutes=cfg.optimization_interval_minutes),
        id="lp_optimize",
        name="LP Cost Optimization",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 3. Genetic 48h planner (every 6h, configurable)
    scheduler.add_job(
        app_state.run_genetic_planning,
        trigger=IntervalTrigger(hours=cfg.long_term_plan_interval_hours),
        id="genetic_plan",
        name="48h Genetic Planner",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 4. Price refresh (every hour, slightly offset)
    scheduler.add_job(
        app_state.refresh_prices,
        trigger=IntervalTrigger(hours=1, start_date=datetime.now().replace(minute=5, second=0)),
        id="price_refresh",
        name="Price Data Refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 5. EV strategy evaluation (every 15 minutes)
    scheduler.add_job(
        app_state.run_ev_strategy,
        trigger=IntervalTrigger(minutes=15),
        id="ev_strategy",
        name="EV Strategy Evaluator",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 6. Battery balancing check (every 15 minutes)
    scheduler.add_job(
        app_state.check_balancing,
        trigger=IntervalTrigger(minutes=15),
        id="balancing_check",
        name="Battery Balancing Check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 7. Cheap window notification (every hour)
    scheduler.add_job(
        app_state.notify_cheap_window,
        trigger=IntervalTrigger(hours=1),
        id="cheap_window_notify",
        name="Cheap Window Notifications",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    logger.info("All scheduler jobs registered")


_JOB_IDS = [
    "realtime", "lp_optimize", "genetic_plan", "price_refresh",
    "ev_strategy", "balancing_check", "cheap_window_notify",
]


def pause_jobs() -> None:
    """Pause all optimization jobs (stopped mode)."""
    scheduler = get_scheduler()
    for job_id in _JOB_IDS:
        try:
            scheduler.pause_job(job_id)
        except Exception:
            pass
    logger.info("All scheduler jobs paused")


def resume_jobs() -> None:
    """Resume all optimization jobs (running mode)."""
    scheduler = get_scheduler()
    for job_id in _JOB_IDS:
        try:
            scheduler.resume_job(job_id)
        except Exception:
            pass
    logger.info("All scheduler jobs resumed")
