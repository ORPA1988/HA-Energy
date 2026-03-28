"""EMHASS REST API client for linear-programming-based optimization."""

import logging

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 60  # EMHASS optimization can take a while on RPi

# HA add-ons run in separate Docker containers.
# localhost doesn't work — must use Docker hostname (slug with hyphens).
EMHASS_URLS = [
    "http://5b918bf2-emhass:5000",      # Docker hostname (slug with hyphens)
    "http://5b918bf2_emhass:5000",       # Alternative with underscores
    "http://addon_5b918bf2_emhass:5000", # Supervisor proxy format
    "http://localhost:5000",              # Fallback if running on same host
]


class EmhassClient:
    """Calls EMHASS day-ahead optimization and reads results."""

    def __init__(self, url: str = None):
        if url and url != "http://localhost:5000":
            # User provided a specific URL
            self.url = url.rstrip("/")
        else:
            # Auto-detect working URL
            self.url = self._find_working_url()

    def _find_working_url(self) -> str:
        """Try known EMHASS URLs and return the first one that works."""
        for url in EMHASS_URLS:
            try:
                resp = requests.get(f"{url}/", timeout=3)
                if resp.status_code < 500:
                    logger.info("EMHASS: found at %s", url)
                    return url
            except requests.RequestException:
                continue
        logger.warning("EMHASS: not reachable at any known URL, using default")
        return EMHASS_URLS[0]

    def is_available(self) -> bool:
        """Check if EMHASS API is reachable."""
        try:
            resp = requests.get(f"{self.url}/", timeout=5)
            return resp.status_code < 500
        except requests.RequestException:
            return False

    def dayahead_optim(self, pv_forecast_w: list[float],
                       load_forecast_w: list[float],
                       prices_eur: list[float],
                       export_price_eur: float = 0.10,
                       battery_params: dict = None,
                       optimization_time_step: int = None) -> dict:
        """Run day-ahead optimization and return result.

        Args:
            battery_params: Dict with EMHASS battery runtime params (decimal SOC 0-1).
                Keys: set_use_battery, battery_nominal_energy_capacity,
                battery_minimum/maximum/target_state_of_charge,
                battery_charge/discharge_power_max,
                battery_charge/discharge_efficiency.
            optimization_time_step: EMHASS time step in minutes (passed as runtime param).
        """
        payload = {
            "pv_power_forecast": pv_forecast_w,
            "load_power_forecast": load_forecast_w,
            "load_cost_forecast": prices_eur,
            "prod_price_forecast": [export_price_eur] * len(prices_eur),
        }

        # Add battery parameters (EMHASS uses decimal SOC 0.0-1.0)
        if battery_params:
            payload.update(battery_params)

        if optimization_time_step:
            payload["optimization_time_step"] = optimization_time_step

        target_soc = battery_params.get("battery_target_state_of_charge", 0) if battery_params else 0
        logger.info("EMHASS: calling dayahead-optim with %d intervals, target SOC %.1f%%, time_step=%s",
                     len(pv_forecast_w), target_soc * 100,
                     optimization_time_step or "default")

        resp = requests.post(f"{self.url}/action/dayahead-optim",
                             json=payload, timeout=TIMEOUT)

        logger.info("EMHASS: response status=%d, content-type=%s, body=%s",
                     resp.status_code,
                     resp.headers.get("content-type", "unknown"),
                     resp.text[:500] if resp.text else "(empty)")

        resp.raise_for_status()

        if not resp.text or not resp.text.strip():
            logger.warning("EMHASS: empty response body — triggering publish-data anyway")
            # Still try publish-data, then let sensor freshness check validate
            try:
                requests.post(f"{self.url}/action/publish-data", json={}, timeout=30)
            except requests.RequestException:
                pass
            return {"status": "ok", "warning": "empty response"}

        try:
            result = resp.json()
        except ValueError:
            logger.warning("EMHASS: non-JSON response: %s", resp.text[:200])
            # Still trigger publish-data for non-JSON success responses
            try:
                requests.post(f"{self.url}/action/publish-data", json={}, timeout=30)
            except requests.RequestException:
                pass
            return {"status": "ok", "raw": resp.text[:200]}

        logger.info("EMHASS: optimization complete, result keys: %s",
                    list(result.keys()) if isinstance(result, dict) else type(result).__name__)

        # Try to trigger publish-data (may not work in all EMHASS versions)
        try:
            pub_resp = requests.post(f"{self.url}/action/publish-data",
                                      json={}, timeout=30)
            logger.info("EMHASS: publish-data status=%d", pub_resp.status_code)
        except requests.RequestException as e:
            logger.warning("EMHASS: publish-data failed: %s", e)

        return result

    def get_optimization_results(self) -> dict | None:
        """Read cached optimization results directly from EMHASS API.

        Returns dict with 'optim_status', 'P_batt', 'SOC_opt', etc.
        Falls back to None if not available.
        """
        try:
            resp = requests.get(f"{self.url}/action/get-data", timeout=10)
            if resp.status_code == 200 and resp.text:
                try:
                    return resp.json()
                except ValueError:
                    pass
        except requests.RequestException:
            pass
        return None

    @staticmethod
    def validate_inputs(pv_forecast: list[float], load_forecast: list[float],
                        prices: list[float], target_soc: float) -> list[str]:
        """Validate EMHASS inputs, return list of error messages (empty = OK).

        Args:
            target_soc: Target SOC as decimal (0.0-1.0), matching EMHASS convention.
        """
        errors = []

        if len(pv_forecast) != len(load_forecast):
            errors.append(f"PV ({len(pv_forecast)}) != Load ({len(load_forecast)}) length mismatch")

        if len(prices) < len(pv_forecast):
            errors.append(f"Too few price points: {len(prices)} < {len(pv_forecast)}")

        # EMHASS uses decimal SOC (0.0-1.0)
        if not (0.0 <= target_soc <= 1.0):
            errors.append(f"Target SOC {target_soc} outside [0.0-1.0] (EMHASS decimal format)")

        if any(p < 0 for p in pv_forecast):
            errors.append("Negative PV forecast values")

        if len(pv_forecast) < 4:
            errors.append(f"Too few forecast points: {len(pv_forecast)}")

        return errors
