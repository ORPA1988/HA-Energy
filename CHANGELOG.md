# Changelog

## [0.1.0] - 2026-03-26

### Added
- Initial release
- Three energy management strategies: surplus, price-optimized, PV-forecast
- Battery mode control (charge/discharge/idle) - inverter determines power
- PHEV surplus charging via go-eCharger with W->A conversion
- EPEX Spot price integration (reads `data` attribute with `price_per_kwh`)
- Solcast PV forecast integration (reads `detailedForecast` attribute)
- PSA Car Controller integration for PHEV status detection
- Published control entities: battery_mode, phev_charge_w, phev_target_ampere, grid_setpoint
- Published info entities: status, battery_plan (timeline), planned_soc, savings
- Dry-run mode (default: enabled) for safe testing
- Automatic fallback to surplus strategy when data is missing
- Change-detection to minimize API writes
- Graceful shutdown via SIGTERM/SIGINT
- HA Supervisor API communication with retry logic

### Hardware Configuration
- Sungrow inverter via ha-sungrow integration
- 30 kWh house battery (usable ~24 kWh)
- go-eCharger wallbox for PHEV charging
- Peugeot 308 SW Hybrid via PSA Car Controller
- ESP01S smart meter for grid power
- EPEX Spot dynamic pricing
- Solcast PV forecast
- Raspberry Pi 4, Home Assistant OS, aarch64
