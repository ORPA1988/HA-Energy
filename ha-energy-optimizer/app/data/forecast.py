"""PV power forecast using Open-Meteo API (free, no API key required)."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from config import get_config
from models import PVForecastResult

logger = logging.getLogger(__name__)


class PVForecast:
    """Calculates hourly PV power forecast using solar irradiance data from Open-Meteo."""

    # PV efficiency adjustment for temperature (typical -0.35%/°C above 25°C)
    TEMP_COEFF = 0.0035

    def __init__(self):
        cfg = get_config()
        self.lat = cfg.pv_latitude
        self.lon = cfg.pv_longitude
        self.kwp = cfg.pv_forecast_kwp
        self.tilt = cfg.pv_tilt         # panel tilt in degrees (0=horizontal, 90=vertical)
        self.orientation = cfg.pv_orientation  # azimuth: 180=south, 90=east, 270=west
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

        try:
            result = await self._fetch_open_meteo()
            self._cache = result
            self._cache_ts = datetime.now()
            return result
        except Exception as e:
            logger.error("PV forecast fetch failed: %s", e)
            return self._fallback_forecast()

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

        # Open-Meteo API typically responds in 1-3 seconds
        # 5s timeout prevents blocking on slow networks while avoiding excessive waits
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()

        data = r.json()
        hourly = data.get("hourly", {})

        times_raw = hourly.get("time", [])
        direct = hourly.get("direct_radiation", [])
        diffuse = hourly.get("diffuse_radiation", [])
        dni = hourly.get("direct_normal_irradiance", [])
        temps = hourly.get("temperature_2m", [])

        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        hours: list[datetime] = []
        powers: list[float] = []

        tilt_rad = math.radians(self.tilt)
        orientation_rad = math.radians(self.orientation - 180)  # offset from south

        for i, t_str in enumerate(times_raw):
            t = datetime.fromisoformat(t_str)
            if t < now:
                continue
            if len(hours) >= 48:
                break

            d = direct[i] if i < len(direct) else 0.0
            df = diffuse[i] if i < len(diffuse) else 0.0
            temp = temps[i] if i < len(temps) else 25.0

            # Solar zenith/azimuth for incidence angle on tilted surface
            hour_angle = _solar_hour_angle(t, self.lon)
            declination = _solar_declination(t)
            lat_rad = math.radians(self.lat)
            decl_rad = math.radians(declination)

            cos_zenith = (
                math.sin(lat_rad) * math.sin(decl_rad)
                + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(math.radians(hour_angle))
            )
            cos_zenith = max(0.0, cos_zenith)

            # Incidence angle on tilted surface
            cos_incidence = (
                cos_zenith * math.cos(tilt_rad)
                + math.sin(math.acos(max(0.001, cos_zenith))) * math.sin(tilt_rad) * math.cos(orientation_rad)
            )
            cos_incidence = max(0.0, cos_incidence)

            # Irradiance on tilted surface
            g_tilt = d * cos_incidence + df * (1 + math.cos(tilt_rad)) / 2.0

            # Temperature correction
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
            # Simple bell curve: peak at noon (hour 12), zero before 6 and after 20
            if 6 <= hour <= 20:
                frac = math.sin(math.pi * (hour - 6) / 14.0)
                powers.append(frac * self.kwp * 0.7 * 1000.0)  # 70% of peak kWp
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
