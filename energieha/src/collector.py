"""Data collector: reads HA entities and builds structured data for planning."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .ha_client import HaClient
from .models import Config, ForecastPoint, PricePoint, Snapshot

logger = logging.getLogger(__name__)


class Collector:
    """Reads all relevant HA entity data for the planning cycle."""

    def __init__(self, client: HaClient, config: Config):
        self._client = client
        self._config = config

    def get_snapshot(self) -> Snapshot | None:
        """Read current system state from HA sensors."""
        soc = self._client.get_state_value(self._config.entity_battery_soc)
        bat_power = self._client.get_state_value(self._config.entity_battery_power)
        pv = self._client.get_state_value(self._config.entity_pv_power)
        grid = self._client.get_state_value(self._config.entity_grid_power)
        load = self._client.get_state_value(self._config.entity_load_power)

        if soc is None:
            logger.error("Battery SOC unavailable – cannot plan")
            return None

        # PHEV data (optional)
        phev_connected = False
        phev_soc = 0.0
        phev_power = 0.0
        if self._config.phev_enabled:
            conn_state = self._client.get_state(self._config.entity_phev_connected)
            if conn_state:
                raw = str(conn_state.get("state", "off")).lower()
                # PSA integration: "Disconnected"/"InProgress"/"Connected" etc.
                phev_connected = raw in (
                    "on", "true", "connected",
                    "inprogress", "charging", "waitscheduled",
                )
            phev_soc = self._client.get_state_value(self._config.entity_phev_soc) or 0.0
            phev_power = self._client.get_state_value(self._config.entity_phev_charging_power) or 0.0

        # Grid charging power: current × battery voltage
        grid_charge_w = 5000.0  # default
        grid_charge_current = self._client.get_state_value(
            self._config.entity_grid_charge_current)
        if grid_charge_current and grid_charge_current > 0:
            bat_state = self._client.get_state(self._config.entity_battery_soc)
            bat_voltage = 52.0  # nominal (mid-range for 48V system)
            if bat_state:
                bat_voltage = float(bat_state.get("attributes", {}).get(
                    "BMS Voltage", 52.0))
            grid_charge_w = grid_charge_current * bat_voltage
            # Plausibility check
            if grid_charge_w < 1000 or grid_charge_w > 10000:
                logger.warning("Grid charge power %.0fW seems implausible "
                               "(%.0fA × %.1fV)", grid_charge_w,
                               grid_charge_current, bat_voltage)
            logger.debug("Grid charge power: %.0fA × %.1fV = %.0fW",
                         grid_charge_current, bat_voltage, grid_charge_w)

        # Dynamic price threshold from HA input_number
        dyn_price_threshold = 0.0
        if self._config.entity_price_threshold:
            val = self._client.get_state_value(self._config.entity_price_threshold)
            if val is not None and val > 0:
                dyn_price_threshold = val
                logger.debug("Dynamic price threshold: %.4f EUR/kWh", val)

        return Snapshot(
            timestamp=datetime.now(timezone.utc),
            battery_soc=soc,
            battery_power_w=bat_power or 0.0,
            pv_power_w=pv or 0.0,
            grid_power_w=grid or 0.0,
            load_power_w=load or 0.0,
            phev_connected=phev_connected,
            phev_soc=phev_soc,
            phev_power_w=phev_power,
            grid_charge_power_w=grid_charge_w,
            dynamic_price_threshold=dyn_price_threshold,
        )

    def get_prices(self) -> list[PricePoint]:
        """Read EPEX spot prices from HA entity attributes.

        Supports common EPEX integration formats:
        - Attribute 'prices' or 'data' as list of {start, end, price}
        - Attribute 'forecast' as list of {start_time, price}
        """
        attrs = self._client.get_attributes(self._config.entity_epex_prices)
        if not attrs:
            logger.warning("No price data available from %s",
                           self._config.entity_epex_prices)
            return []

        prices = []

        # Try common attribute formats
        raw = attrs.get("data") or attrs.get("prices") or attrs.get("forecast") or []
        if not raw and "raw_today" in attrs:
            raw = attrs.get("raw_today", []) + attrs.get("raw_tomorrow", [])

        for item in raw:
            try:
                start = self._parse_timestamp(
                    item.get("start") or item.get("start_time") or item.get("startsAt", ""))
                end = self._parse_timestamp(
                    item.get("end") or item.get("end_time") or item.get("endsAt", ""))
                price = float(
                    item.get("price_per_kwh")  # EPEX Spot Data integration
                    or item.get("price")
                    or item.get("value")
                    or item.get("total", 0))

                if start and end:
                    # If no explicit end, assume 1 hour slot
                    if end <= start:
                        end = start + timedelta(hours=1)
                    prices.append(PricePoint(start=start, end=end, price_eur_kwh=price))
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Skipping price item: %s", e)
                continue

        if not prices:
            # Fallback: try to use the main state as current price
            current = self._client.get_state_value(self._config.entity_epex_prices)
            if current is not None:
                now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                prices.append(PricePoint(
                    start=now, end=now + timedelta(hours=1), price_eur_kwh=current))

        # Freshness check
        if prices:
            now = datetime.now(timezone.utc)
            newest = max(p.end for p in prices)
            age_hours = (now - newest).total_seconds() / 3600
            if age_hours > 24:
                logger.warning("Price data is %.0fh old — may be stale", age_hours)

        logger.info("Collected %d price points", len(prices))
        return prices

    def get_pv_forecast(self) -> list[ForecastPoint]:
        """Read Solcast PV forecast from HA entity attributes.

        Supports common Solcast integration formats (oziee/ha-solcast-solar):
        - Attribute 'detailedForecast' or 'forecast' as list of dicts
        - Attribute 'forecasts' as list of {period_start, pv_estimate}
        """
        forecasts = []

        for entity_id in [self._config.entity_solcast_forecast,
                          self._config.entity_solcast_forecast_tomorrow]:
            attrs = self._client.get_attributes(entity_id)
            if not attrs:
                continue

            raw = (attrs.get("detailedForecast")
                   or attrs.get("detailed_forecast")
                   or attrs.get("forecasts")
                   or attrs.get("forecast")
                   or [])

            for item in raw:
                try:
                    start = self._parse_timestamp(
                        item.get("period_start")
                        or item.get("datetime")
                        or item.get("start")
                        or item.get("time", ""))
                    # Power in watts (Solcast gives kW, some integrations convert)
                    power = float(
                        item.get("pv_estimate", 0)
                        or item.get("pv_estimate50", 0)
                        or item.get("power", 0)
                        or item.get("value", 0))

                    # Solcast typically gives kW – convert to W if < 100
                    if 0 < power < 100:
                        power *= 1000.0

                    duration = int(item.get("period", 30))
                    if start:
                        end = start + timedelta(minutes=duration)
                        forecasts.append(ForecastPoint(
                            start=start, end=end, power_w=power))
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Skipping forecast item: %s", e)
                    continue

        logger.info("Collected %d PV forecast points", len(forecasts))
        return forecasts

    def get_sun_times(self) -> tuple[int, int]:
        """Read sunrise/sunset hours from sun.sun entity."""
        state = self._client.get_state("sun.sun")
        if state:
            attrs = state.get("attributes", {})
            sunrise = self._parse_timestamp(attrs.get("next_rising"))
            sunset = self._parse_timestamp(attrs.get("next_setting"))
            if sunrise and sunset:
                tz = ZoneInfo(self._config.timezone)
                return sunrise.astimezone(tz).hour, sunset.astimezone(tz).hour
        return 6, 20  # Fallback for Central Europe

    @staticmethod
    def _parse_timestamp(value) -> datetime | None:
        """Parse a timestamp string to timezone-aware datetime."""
        if not value:
            return None
        if isinstance(value, datetime):
            return value

        value = str(value)

        # Fast path: fromisoformat handles most HA timestamp formats
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass

        # Fallback: manual format parsing
        for fmt in ("%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S.%f%z",
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S%z",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        return None
