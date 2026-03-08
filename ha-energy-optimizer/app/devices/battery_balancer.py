"""Battery balancing controller — periodic full-charge cycle for cell equalization."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config import get_config
from ha_client import get_ha_client
from models import BalancingDecision, BalancingState

logger = logging.getLogger(__name__)

STATE_FILE = Path("/data/balancing_state.json")


class BatteryBalancer:
    """
    Executes periodic full-charge cycles to equalize battery cells.

    Modes:
      auto      — triggers automatically when interval elapsed or cell deviation detected
      scheduled — triggers at configured time/frequency
      manual    — only triggers on explicit API call
    """

    def __init__(self):
        cfg = get_config()
        self._cfg = cfg
        self._ha = get_ha_client()
        self._state = BalancingState.IDLE
        self._balance_start: Optional[datetime] = None
        self._last_balance: Optional[datetime] = None
        self._load_state()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        data = {
            "state": self._state.value,
            "balance_start": self._balance_start.isoformat() if self._balance_start else None,
            "last_balance": self._last_balance.isoformat() if self._last_balance else None,
        }
        STATE_FILE.write_text(json.dumps(data))

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            self._state = BalancingState(data.get("state", "idle"))
            if data.get("balance_start"):
                self._balance_start = datetime.fromisoformat(data["balance_start"])
            if data.get("last_balance"):
                self._last_balance = datetime.fromisoformat(data["last_balance"])
        except Exception as e:
            logger.warning("Could not load balancing state: %s", e)

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------

    def _interval_days(self) -> int:
        freq = self._cfg.battery_balancing_frequency
        if freq == "daily":
            return 1
        elif freq == "weekly":
            return 7
        elif freq == "monthly":
            return 30
        else:  # custom
            return self._cfg.battery_balancing_custom_days

    def should_balance_now(
        self,
        battery_soc: float,
        pv_forecast_kwh_today: float,
        battery_max_soc: float = 100.0,
    ) -> BalancingDecision:
        """Evaluate whether balancing should start now."""
        if not self._cfg.battery_balancing_enabled:
            return BalancingDecision(should_start=False, reason="balancing disabled")

        if self._cfg.battery_balancing_mode == "manual":
            return BalancingDecision(should_start=False, reason="manual mode — use API")

        if self._state in (BalancingState.CHARGING, BalancingState.HOLDING):
            return BalancingDecision(should_start=False, reason="balancing already running")

        # Check interval
        interval_elapsed = False
        if self._last_balance is None:
            interval_elapsed = True
            reason = "first run — no previous balance recorded"
        else:
            days_since = (datetime.now() - self._last_balance).days
            if days_since >= self._interval_days():
                interval_elapsed = True
                reason = f"interval elapsed ({days_since}d since last balance)"
            else:
                reason = f"next balance in {self._interval_days() - days_since}d"

        # Auto trigger: check SOC deviation (simplified: trigger if battery isn't reaching expected max)
        soc_deviation_trigger = (
            self._cfg.battery_balancing_mode == "auto"
            and battery_max_soc < (self._cfg.battery_balancing_target_soc
                                   - self._cfg.battery_balancing_auto_trigger_soc_deviation)
        )
        if soc_deviation_trigger:
            interval_elapsed = True
            reason = f"SOC deviation detected (max {battery_max_soc:.0f}% < target {self._cfg.battery_balancing_target_soc}%)"

        if not interval_elapsed:
            return BalancingDecision(should_start=False, reason=reason)

        # Check preferred time window
        preferred_hour = int(self._cfg.battery_balancing_preferred_time.split(":")[0])
        now_hour = datetime.now().hour
        if abs(now_hour - preferred_hour) > 2 and self._cfg.battery_balancing_mode == "scheduled":
            return BalancingDecision(
                should_start=False,
                reason=f"outside preferred window (prefer ~{preferred_hour:02d}:00)",
                scheduled_time=datetime.now().replace(hour=preferred_hour, minute=0, second=0),
            )

        # Solar-only check
        if self._cfg.battery_balancing_use_solar_only:
            required_kwh = (self._cfg.battery_balancing_target_soc - battery_soc) / 100.0 * self._cfg.battery_capacity_kwh
            if pv_forecast_kwh_today < required_kwh:
                return BalancingDecision(
                    should_start=False,
                    reason=f"insufficient solar ({pv_forecast_kwh_today:.1f} kWh forecast < {required_kwh:.1f} kWh needed)",
                    estimated_cost_eur=0.0,
                )
            estimated_cost_eur = 0.0
        else:
            required_kwh = (self._cfg.battery_balancing_target_soc - battery_soc) / 100.0 * self._cfg.battery_capacity_kwh
            estimated_cost_eur = required_kwh * self._cfg.price_feed_in_ct_kwh / 100.0

        return BalancingDecision(
            should_start=True,
            reason=reason,
            estimated_cost_eur=estimated_cost_eur,
        )

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def start_balancing(self, reason: str = "manual") -> None:
        """Initiate balancing cycle: force battery to charge to target SOC."""
        if self._state in (BalancingState.CHARGING, BalancingState.HOLDING):
            logger.info("Balancing already in progress, skipping")
            return

        logger.info("Starting battery balancing: %s", reason)
        self._state = BalancingState.CHARGING
        self._balance_start = datetime.now()
        self._save_state()

        # Enable full charge via HA
        await self._ha.turn_on(self._cfg.battery_charge_switch)

        if self._cfg.notify_on_balancing:
            await self._ha.notify(
                self._cfg.notify_target,
                "Battery Balancing Started",
                f"Balancing started. Target: {self._cfg.battery_balancing_target_soc}% "
                f"Hold: {self._cfg.battery_balancing_hold_duration_h}h. Reason: {reason}",
            )
        await self._ha.publish_sensor("balancing_status", "charging", "")

    async def tick(self, battery_soc: float) -> None:
        """Called periodically (every 30s) to manage the balancing cycle."""
        if self._state == BalancingState.IDLE:
            return

        if self._state == BalancingState.CHARGING:
            if battery_soc >= self._cfg.battery_balancing_target_soc - 1:
                logger.info("Battery reached target SOC %.1f%%, holding for %dh",
                            battery_soc, self._cfg.battery_balancing_hold_duration_h)
                self._state = BalancingState.HOLDING
                self._save_state()
                await self._ha.publish_sensor("balancing_status", "holding", "")

        elif self._state == BalancingState.HOLDING:
            if self._balance_start:
                hold_elapsed = datetime.now() - self._balance_start
                target_hold = timedelta(hours=self._cfg.battery_balancing_hold_duration_h)
                if hold_elapsed >= target_hold:
                    await self.stop_balancing()

    async def stop_balancing(self) -> None:
        """Complete the balancing cycle and resume normal operation."""
        logger.info("Battery balancing complete")
        self._state = BalancingState.IDLE
        self._last_balance = datetime.now()
        self._balance_start = None
        self._save_state()

        await self._ha.publish_sensor("balancing_status", "idle", "",
                                      {"last_balance": self._last_balance.isoformat()})

        if self._cfg.notify_on_balancing:
            await self._ha.notify(
                self._cfg.notify_target,
                "Battery Balancing Complete",
                f"Balancing finished at {self._last_balance.strftime('%H:%M')}. "
                f"Next balance in {self._interval_days()} day(s).",
            )

    @property
    def current_state(self) -> BalancingState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in (BalancingState.CHARGING, BalancingState.HOLDING)


# Global singleton
_balancer: Optional[BatteryBalancer] = None


def get_battery_balancer() -> BatteryBalancer:
    global _balancer
    if _balancer is None:
        _balancer = BatteryBalancer()
    return _balancer
