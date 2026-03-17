"""PV power forecast — Solcast (via HA entity) with Open-Meteo fallback."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from config import get_config
from ha_client import get_ha_client
from models import PVForecastResult

logger = logging.getLogger(__name__)


class PVForecast:
    """
    Provides 48h hourly PV power forecast in watts.

    Sources (selected via pv_forecast_source config):
      - "auto": Try Solcast first, fall back to Open-Meteo
      - "solcast": Use Solcast HA integration entity (detailedHourly attribute)
      - "open_meteo": Use Open-Meteo solar irradiance API (free, no key)
    """

    # PV efficiency adjustment for temperature (typical -0.35%/°C above 25°C)
    TEMP_COEFF = 0.0035

    def __init__(self):
        cfg = get_config()
        self.lat = cfg.pv_latitude
        self.lon = cfg.pv_longitude
        self.kwp = cfg.pv_forecast_kwp
        self.tilt = cfg.pv_tilt
        self.orientation = cfg.pv_orientation
        self.efficiency = cfg.pv_efficiency

        self._cache: Optional[PVForecastResult] = None
        self._cache_ts: Optional[datetime] = None

    async def get_forecast_48h(self, force_refresh: bool = False) -> PVForecastResult:
        """Return 48h hourly PV power forecast (watts)."""
        if (
            not force_refresh
            and self._cache
            and self._cache_ts
            and (datetime.now() - self._cache_ts).total_seconds() < 3600
        ):
            return self._cache

        cfg = get_config()
        source = cfg.pv_forecast_source

        try:
            if source == "solcast":
                result = await self._fetch_solcast()
            elif source == "open_meteo":
                result = await self._fetch_open_meteo()
            else:
                # "auto": try Solcast first, fall back to Open-Meteo
                result = await self._fetch_auto()

            self._cache = result
            self._cache_ts = datetime.now()
            return result
        except Exception as e:
            logger.error("PV forecast fetch failed: %s", e)
            return self._fallback_forecast()

    async def _fetch_auto(self) -> PVForecastResult:
        """Try Solcast first; fall back to Open-Meteo if unavailable or insufficient."""
        cfg = get_config()
        if cfg.solcast_entity:
            try:
                result = await self._fetch_solcast()
                # Check if Solcast provides enough future data (at least 12h)
                non_zero = sum(1 for w in result.power_w if w > 0)
                if non_zero >= 4:
                    return result
                logger.info("Solcast has only %d non-zero hours, supplementing with Open-Meteo", non_zero)
                return await self._merge_solcast_openmeteo(result)
            except Exception as e:
                logger.warning("Solcast fetch failed, falling back to Open-Meteo: %s", e)

        return await self._fetch_open_meteo()

    # ------------------------------------------------------------------
    # Solcast (via HA entity attributes)
    # ------------------------------------------------------------------

    async def _fetch_solcast(self) -> PVForecastResult:
        """
        Read PV forecast from Solcast HA integration entity.

        Solcast stores detailed hourly data in the 'detailedHourly' attribute
        of forecast sensors (e.g. sensor.solcast_pv_forecast_forecast_today).
        Each entry has: period_start, pv_estimate (kW), pv_estimate10, pv_estimate90.

        The available time span is auto-detected from the data.
        """
        cfg = get_config()
        ha = get_ha_client()
        entity_id = cfg.solcast_entity
        estimate_key = cfg.solcast_estimate_type

        state = await ha.get_state(entity_id)
        if not state:
            raise ValueError(f"Solcast entity '{entity_id}' not found in HA")

        attrs = state.get("attributes", {})

        # Try multiple attribute names used by different Solcast integration versions
        hourly_data = (
            attrs.get("detailedHourly")
            or attrs.get("detailedForecast")
            or attrs.get("forecast")
            or []
        )

        if not hourly_data:
            raise ValueError(f"Solcast entity '{entity_id}' has no forecast data in attributes")

        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        hours: list[datetime] = []
        powers: list[float] = []

        for entry in hourly_data:
            try:
                period_start = entry.get("period_start", "")
                t = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
                # Convert to local time (naive) for consistency with rest of system
                t = t.astimezone().replace(tzinfo=None).replace(minute=0, second=0, microsecond=0)
            except (ValueError, TypeError):
                continue

            if t < now:
                continue
            if len(hours) >= 48:
                break

            # Get estimate value (kW) and convert to W
            kw_value = entry.get(estimate_key, entry.get("pv_estimate", 0))
            try:
                power_w = max(0.0, float(kw_value) * 1000.0)
            except (ValueError, TypeError):
                power_w = 0.0

            hours.append(t)
            powers.append(power_w)

        available_hours = len(hours)
        if available_hours > 0:
            last_hour = hours[-1]
            logger.info(
                "Solcast: %d hours available (until %s), estimate_type=%s",
                available_hours, last_hour.strftime("%Y-%m-%d %H:%M"), estimate_key,
            )
        else:
            logger.warning("Solcast: no future forecast data found in entity '%s'", entity_id)

        # Pad to 48h with zeros
        while len(powers) < 48:
            hours.append(now + timedelta(hours=len(hours)))
            powers.append(0.0)

        return PVForecastResult(
            hours=hours[:48],
            power_w=powers[:48],
            total_kwh=sum(powers[:48]) / 1000.0,
        )

    async def _merge_solcast_openmeteo(self, solcast: PVForecastResult) -> PVForecastResult:
        """Merge Solcast data with Open-Meteo for hours where Solcast has no data."""
        try:
            openmeteo = await self._fetch_open_meteo()
        except Exception:
            return solcast

        merged_powers = []
        merged_hours = []
        for i in range(48):
            sc_w = solcast.power_w[i] if i < len(solcast.power_w) else 0.0
            om_w = openmeteo.power_w[i] if i < len(openmeteo.power_w) else 0.0
            om_h = openmeteo.hours[i] if i < len(openmeteo.hours) else (
                datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=i)
            )
            # Use Solcast where available, Open-Meteo as supplement
            merged_powers.append(sc_w if sc_w > 0 else om_w)
            merged_hours.append(solcast.hours[i] if i < len(solcast.hours) else om_h)

        return PVForecastResult(
            hours=merged_hours,
            power_w=merged_powers,
            total_kwh=sum(merged_powers) / 1000.0,
        )

    # ------------------------------------------------------------------
    # Open-Meteo (free, no API key)
    # ------------------------------------------------------------------

    async def _fetch_open_meteo(self) -> PVForecastResult:
        """
        Fetch solar irradiance from Open-Meteo and apply PV model.

        PV model:
          G_tilt = direct_radiation * cos(incidence_angle) + diffuse_radiation
          P = G_tilt * kwp * efficiency * (1 - TEMP_COEFF * (T - 25))
        """
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "hourly": "direct_radiation,diffuse_radiation,temperature_2m,direct_normal_irradiance",
            "forecast_days": 3,
            "timezone": "auto",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()

        data = r.json()
        hourly = data.get("hourly", {})

        times_raw = hourly.get("time", [])
        direct = hourly.get("direct_radiation", [])
        diffuse = hourly.get("diffuse_radiation", [])
        temps = hourly.get("temperature_2m", [])

        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        hours: list[datetime] = []
        powers: list[float] = []

        tilt_rad = math.radians(self.tilt)
        orientation_rad = math.radians(self.orientation - 180)

        for i, t_str in enumerate(times_raw):
            t = datetime.fromisoformat(t_str)
            if t < now:
                continue
            if len(hours) >= 48:
                break

            d = direct[i] if i < len(direct) else 0.0
            df = diffuse[i] if i < len(diffuse) else 0.0
            temp = temps[i] if i < len(temps) else 25.0

            hour_angle = _solar_hour_angle(t, self.lon)
            declination = _solar_declination(t)
            lat_rad = math.radians(self.lat)
            decl_rad = math.radians(declination)

            cos_zenith = (
                math.sin(lat_rad) * math.sin(decl_rad)
                + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(math.radians(hour_angle))
            )
            cos_zenith = max(0.0, cos_zenith)

            cos_incidence = (
                cos_zenith * math.cos(tilt_rad)
                + math.sin(math.acos(max(0.001, cos_zenith))) * math.sin(tilt_rad) * math.cos(orientation_rad)
            )
            cos_incidence = max(0.0, cos_incidence)

            g_tilt = d * cos_incidence + df * (1 + math.cos(tilt_rad)) / 2.0
            temp_factor = max(0.0, 1.0 - self.TEMP_COEFF * max(0.0, temp - 25.0))

            # PV power: g_tilt(W/m²) * kwp(kWp) * efficiency * temp_factor
            power_w = g_tilt * self.kwp * self.efficiency * temp_factor

            hours.append(t)
            powers.append(max(0.0, power_w))

        # Pad to 48h if needed
        while len(powers) < 48:
            hours.append(now + timedelta(hours=len(hours)))
            powers.append(0.0)

        return PVForecastResult(
            hours=hours[:48],
            power_w=powers[:48],
            total_kwh=sum(powers[:48]) / 1000.0,
        )

    def _fallback_forecast(self) -> PVForecastResult:
        """Simple sun-based fallback — no external call needed."""
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        hours = [now + timedelta(hours=i) for i in range(48)]
        powers = []
        for h in hours:
            hour = h.hour
            if 6 <= hour <= 20:
                frac = math.sin(math.pi * (hour - 6) / 14.0)
                powers.append(frac * self.kwp * 0.7 * 1000.0)
            else:
                powers.append(0.0)
        return PVForecastResult(
            hours=hours,
            power_w=powers,
            total_kwh=sum(powers) / 1000.0,
        )


def _solar_declination(dt: datetime) -> float:
    """Solar declination in degrees for a given date."""
    day_of_year = dt.timetuple().tm_yday
    return 23.45 * math.sin(math.radians(360.0 / 365.0 * (day_of_year - 81)))


def _solar_hour_angle(dt: datetime, longitude: float) -> float:
    """Solar hour angle in degrees."""
    local_solar_noon = 12.0 - (longitude / 15.0)
    return 15.0 * (dt.hour + dt.minute / 60.0 - local_solar_noon)


# Global singleton
_pv_forecast: Optional[PVForecast] = None


def get_pv_forecast() -> PVForecast:
    global _pv_forecast
    if _pv_forecast is None:
        _pv_forecast = PVForecast()
    return _pv_forecast
