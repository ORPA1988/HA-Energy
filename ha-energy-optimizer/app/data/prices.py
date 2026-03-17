"""Multi-source electricity price fetcher + netto/brutto calculator."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree

import httpx

from config import get_config
from ha_client import get_ha_client
from models import PriceForecast, PriceResult, TimeWindow

logger = logging.getLogger(__name__)


class PriceCalculator:
    """Converts raw market prices to all-in consumer prices."""

    def __init__(self):
        cfg = get_config()
        self.input_is_netto = cfg.price_input_is_netto
        self.vat_percent = cfg.price_vat_percent
        self.grid_fee_source = cfg.price_grid_fee_source
        self.grid_fee_fixed = cfg.price_grid_fee_fixed_ct_kwh
        self.grid_fee_entity = cfg.price_grid_fee_entity
        self.supplier_markup = cfg.price_supplier_markup_ct_kwh
        self.other_taxes = cfg.price_other_taxes_ct_kwh

    async def _get_grid_fee(self) -> float:
        if self.grid_fee_source == "entity" and self.grid_fee_entity:
            ha = get_ha_client()
            return await ha.get_state_value(self.grid_fee_entity, self.grid_fee_fixed)
        return self.grid_fee_fixed

    def calculate(self, raw_ct: float, grid_fee_ct: float) -> PriceResult:
        """
        Convert raw market price to all-in consumer price.

        Formula:
          net_ct  = raw_ct (if input already netto)
          gross_ct = net_ct * (1 + vat/100)   [if input is netto]
                   or net_ct                   [if already brutto]
          total_ct = gross_ct + grid_fee + supplier_markup + other_taxes
        """
        if self.input_is_netto:
            net_ct = raw_ct
            gross_ct = raw_ct * (1.0 + self.vat_percent / 100.0)
        else:
            gross_ct = raw_ct
            net_ct = raw_ct / (1.0 + self.vat_percent / 100.0)

        total_ct = gross_ct + grid_fee_ct + self.supplier_markup + self.other_taxes

        return PriceResult(
            raw_ct=raw_ct,
            net_ct=net_ct,
            gross_ct=gross_ct,
            total_ct=total_ct,
            breakdown={
                "raw_market": raw_ct,
                "vat": gross_ct - net_ct,
                "grid_fee": grid_fee_ct,
                "supplier_markup": self.supplier_markup,
                "other_taxes": self.other_taxes,
            },
        )

    def get_cheap_windows(
        self,
        prices: list[float],
        hours: list[datetime],
        threshold_ct: float,
        min_duration_h: int = 1,
    ) -> list[TimeWindow]:
        """Return consecutive time windows where price < threshold."""
        windows: list[TimeWindow] = []
        i = 0
        while i < len(prices):
            if prices[i] < threshold_ct:
                j = i
                while j < len(prices) and prices[j] < threshold_ct:
                    j += 1
                if j - i >= min_duration_h:
                    window_prices = prices[i:j]
                    windows.append(TimeWindow(
                        start=hours[i],
                        end=hours[j - 1] + timedelta(hours=1),
                        avg_price_ct=sum(window_prices) / len(window_prices),
                        min_price_ct=min(window_prices),
                    ))
                i = j
            else:
                i += 1
        return windows


class PriceFetcher:
    """Fetch day-ahead hourly prices from various sources."""

    def __init__(self):
        self._cfg = get_config()
        self._calc = PriceCalculator()
        self._cache: Optional[PriceForecast] = None
        self._cache_ts: Optional[datetime] = None

    async def get_prices_48h(self, force_refresh: bool = False) -> PriceForecast:
        """Return 48h price forecast, using cache if < 1h old."""
        if (
            not force_refresh
            and self._cache
            and self._cache_ts
            and (datetime.now() - self._cache_ts).seconds < 3600
        ):
            return self._cache

        raw_prices = await self._dispatch()
        grid_fee = await self._calc._get_grid_fee()
        hours = [datetime.now().replace(minute=0, second=0, microsecond=0)
                 + timedelta(hours=i) for i in range(len(raw_prices))]

        net_prices = []
        total_prices = []
        for rp in raw_prices:
            result = self._calc.calculate(rp, grid_fee)
            net_prices.append(result.net_ct)
            total_prices.append(result.total_ct)

        # Identify cheap windows (below 20th percentile of total prices)
        if total_prices:
            sorted_p = sorted(total_prices)
            threshold = sorted_p[max(0, len(sorted_p) // 5)]
            cheap_windows = self._calc.get_cheap_windows(
                total_prices, hours, threshold, min_duration_h=1
            )
        else:
            cheap_windows = []

        self._cache = PriceForecast(
            hours=hours,
            raw_ct=raw_prices,
            net_ct=net_prices,
            total_ct=total_prices,
            cheap_windows=cheap_windows,
        )
        self._cache_ts = datetime.now()
        return self._cache

    async def _dispatch(self) -> list[float]:
        source = self._cfg.price_source
        try:
            if source == "entso-e":
                return await self._fetch_entso_e()
            elif source == "awattar":
                return await self._fetch_awattar()
            elif source == "tibber":
                return await self._fetch_tibber()
            elif source == "epex_spot":
                return await self._fetch_epex_spot()
            elif source == "epex_entity":
                return await self._fetch_epex_entity()
            elif source == "sensor":
                return await self._fetch_ha_sensor()
            else:
                return await self._fetch_fixed()
        except Exception as e:
            logger.error("Price fetch failed for source %s: %s", source, e)
            return await self._fetch_fixed()

    async def _fetch_entso_e(self) -> list[float]:
        """ENTSO-E Transparency Platform — XML REST API."""
        token = self._cfg.entso_e_token
        area = self._cfg.entso_e_area
        if not token:
            logger.warning("ENTSO-E token not set, falling back to fixed price")
            return await self._fetch_fixed()

        now = datetime.now(timezone.utc)
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_end = period_start + timedelta(days=3)

        url = "https://web-api.tp.entsoe.eu/api"
        params = {
            "securityToken": token,
            "documentType": "A44",
            "in_Domain": area,
            "out_Domain": area,
            "periodStart": period_start.strftime("%Y%m%d%H00"),
            "periodEnd": period_end.strftime("%Y%m%d%H00"),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()

        root = ElementTree.fromstring(r.text)
        ns = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0"}
        prices_ct: list[float] = []

        for ts in root.findall(".//ns:TimeSeries", ns):
            unit = ts.findtext("ns:currency_Unit.name", namespaces=ns)
            for period in ts.findall("ns:Period", ns):
                resolution = ts.findtext("ns:resolution", namespaces=ns, default="PT60M")
                for pt in period.findall("ns:Point", ns):
                    price_raw = float(pt.findtext("ns:price.amount", default="0", namespaces=ns))
                    # ENTSO-E gives EUR/MWh → convert to ct/kWh
                    prices_ct.append(price_raw / 10.0)

        if not prices_ct:
            logger.warning("ENTSO-E returned no price data")
            return await self._fetch_fixed()

        # Return 48h worth
        return prices_ct[:48] if len(prices_ct) >= 48 else prices_ct + [prices_ct[-1]] * (48 - len(prices_ct))

    async def _fetch_awattar(self) -> list[float]:
        """aWATTar API — free, no auth, AT+DE."""
        country = self._cfg.awattar_country.lower()
        base = "https://api.awattar.at" if country == "at" else "https://api.awattar.de"

        now = datetime.now(timezone.utc)
        start_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        end_ms = start_ms + 48 * 3600 * 1000

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{base}/v1/marketdata",
                params={"start": start_ms, "end": end_ms},
            )
            r.raise_for_status()

        data = r.json()
        # aWATTar gives EUR/MWh, positive only during cheap hours
        prices_ct = []
        for entry in data.get("data", []):
            # marketprice in EUR/MWh → ct/kWh
            prices_ct.append(entry["marketprice"] / 10.0)

        if not prices_ct:
            return await self._fetch_fixed()

        prices_ct = prices_ct[:48]
        while len(prices_ct) < 48:
            prices_ct.append(prices_ct[-1] if prices_ct else self._cfg.fixed_price_ct_kwh)
        return prices_ct

    async def _fetch_tibber(self) -> list[float]:
        """Tibber GraphQL API."""
        token = self._cfg.tibber_token
        if not token:
            return await self._fetch_fixed()

        query = """
        {
          viewer {
            homes {
              currentSubscription {
                priceInfo {
                  today { total startsAt }
                  tomorrow { total startsAt }
                }
              }
            }
          }
        }
        """
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.tibber.com/v1-beta/gql",
                json={"query": query},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()

        data = r.json()
        price_info = (
            data.get("data", {})
            .get("viewer", {})
            .get("homes", [{}])[0]
            .get("currentSubscription", {})
            .get("priceInfo", {})
        )
        prices_ct = []
        for day in ["today", "tomorrow"]:
            for entry in price_info.get(day, []):
                # Tibber gives EUR/kWh → ct/kWh
                prices_ct.append(entry["total"] * 100.0)

        if not prices_ct:
            return await self._fetch_fixed()
        return prices_ct[:48]

    async def _fetch_epex_spot(self) -> list[float]:
        """SMARD.de public API — EPEX Spot prices for DE-LU / AT / CH."""
        # SMARD uses a week-based timestamp index
        # Fetch Day-ahead prices, series ID 4169 (DE-LU EPEX Spot)
        area_map = {
            "DE-LU": 4169,
            "AT": 5078468,
            "CH": 5078476,
        }
        series_id = area_map.get(self._cfg.epex_spot_area, 4169)

        # Get available timestamps
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://www.smard.de/app/chart_data/{series_id}/DE/index_quarterhour.json"
            )
            r.raise_for_status()
            timestamps = r.json().get("timestamps", [])

        if not timestamps:
            return await self._fetch_fixed()

        # Use the latest available period
        latest_ts = timestamps[-1]
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://www.smard.de/app/chart_data/{series_id}/DE/"
                f"{series_id}_DE_quarterhour_{latest_ts}.json"
            )
            r.raise_for_status()
            raw_data = r.json().get("series", [])

        # Each entry: [timestamp_ms, price_eur_mwh or None]
        # Average 4 quarter-hours to get hourly price
        hourly: list[float] = []
        quarter_batch: list[float] = []

        now_ms = datetime.now(timezone.utc).timestamp() * 1000

        for entry in raw_data:
            ts_ms, price = entry
            if ts_ms < now_ms - 3600 * 1000:
                continue
            if price is None:
                price = self._cfg.fixed_price_ct_kwh * 10  # estimate as EUR/MWh
            quarter_batch.append(price)
            if len(quarter_batch) == 4:
                hourly.append(sum(quarter_batch) / 4.0 / 10.0)  # EUR/MWh → ct/kWh
                quarter_batch = []

        if not hourly:
            return await self._fetch_fixed()

        hourly = hourly[:48]
        while len(hourly) < 48:
            hourly.append(hourly[-1])
        return hourly

    @staticmethod
    def _convert_price_to_ct_kwh(value: float, unit: str) -> float:
        """Convert price value to ct/kWh from various units."""
        if unit == "EUR/MWh":
            return value / 10.0
        elif unit == "EUR/kWh":
            return value * 100.0
        # Default: already ct/kWh
        return value

    async def _fetch_epex_entity(self) -> list[float]:
        """
        Read electricity prices directly from HA entities.

        Supports EPEX Spot, Nordpool, and similar integrations that expose
        price forecasts via entity attributes 'today' and 'tomorrow'.
        Falls back to reading 'raw_today'/'raw_tomorrow' or 'prices_today'/'prices_tomorrow'.
        """
        entity_id = self._cfg.epex_import_entity
        if not entity_id:
            logger.warning("epex_import_entity not set, falling back to fixed price")
            return await self._fetch_fixed()

        ha = get_ha_client()
        state_data = await ha.get_state(entity_id)
        if not state_data:
            logger.warning("EPEX entity %s not available", entity_id)
            return await self._fetch_fixed()

        attrs = state_data.get("attributes", {})
        unit = self._cfg.epex_unit

        prices_ct: list[float] = []

        # Try various attribute names used by different integrations
        # EPEX Spot integration: 'data' attribute with list of {start_time, end_time, price_ct_per_kwh}
        epex_data = attrs.get("data", [])
        if epex_data and isinstance(epex_data, list):
            for entry in epex_data:
                if isinstance(entry, dict):
                    price = entry.get("price_ct_per_kwh") or entry.get("price") or 0.0
                    prices_ct.append(self._convert_price_to_ct_kwh(float(price), unit))
            if prices_ct:
                logger.info("EPEX entity: read %d prices from 'data' attribute", len(prices_ct))

        # Nordpool integration: 'today' and 'tomorrow' attributes (list of floats)
        if not prices_ct:
            today = attrs.get("today") or attrs.get("raw_today") or attrs.get("prices_today") or []
            tomorrow = attrs.get("tomorrow") or attrs.get("raw_tomorrow") or attrs.get("prices_tomorrow") or []

            if today and isinstance(today, list):
                for p in today:
                    if isinstance(p, dict):
                        val = p.get("value", p.get("price", 0.0))
                    else:
                        val = p
                    try:
                        prices_ct.append(self._convert_price_to_ct_kwh(float(val), unit))
                    except (ValueError, TypeError):
                        prices_ct.append(self._cfg.fixed_price_ct_kwh)

                for p in (tomorrow if isinstance(tomorrow, list) else []):
                    if isinstance(p, dict):
                        val = p.get("value", p.get("price", 0.0))
                    else:
                        val = p
                    try:
                        prices_ct.append(self._convert_price_to_ct_kwh(float(val), unit))
                    except (ValueError, TypeError):
                        prices_ct.append(self._cfg.fixed_price_ct_kwh)

                if prices_ct:
                    logger.info("EPEX entity: read %d prices from today/tomorrow attributes", len(prices_ct))

        # Fallback: read single current price from state
        if not prices_ct:
            try:
                current_price = float(state_data.get("state", 0))
                prices_ct = [self._convert_price_to_ct_kwh(current_price, unit)] * 48
                logger.info("EPEX entity: using single current price %.2f", prices_ct[0])
            except (ValueError, TypeError):
                return await self._fetch_fixed()

        # Pad to 48h
        if not prices_ct:
            return await self._fetch_fixed()
        while len(prices_ct) < 48:
            prices_ct.append(prices_ct[-1])
        return prices_ct[:48]

    async def _fetch_ha_sensor(self) -> list[float]:
        """Read current price from HA sensor — duplicated for 48h (no forecast)."""
        ha = get_ha_client()
        current = await ha.get_state_value(
            self._cfg.price_sensor_entity, self._cfg.fixed_price_ct_kwh
        )
        return [current] * 48

    async def _fetch_fixed(self) -> list[float]:
        return [self._cfg.fixed_price_ct_kwh] * 48


# Global singleton
_price_fetcher: Optional[PriceFetcher] = None


def get_price_fetcher() -> PriceFetcher:
    global _price_fetcher
    if _price_fetcher is None:
        _price_fetcher = PriceFetcher()
    return _price_fetcher
