# Changelog

## [1.0.0] - 2026-03-28

### Added
- **Web GUI with HA Ingress** - Full addon GUI accessible via HA sidebar
  - Dashboard: Power flow diagram, SOC gauge, live status cards, EMHASS indicator
  - Planning: 24h timeline table with Chart.js charts (SOC, power, price)
  - Inverter Control: TOU program editor, battery mode controls, PHEV charging
  - Configuration: Full form-based config editor with grouped sections
  - Logs: Real-time log viewer with SSE streaming, cycle history
- **Flask + HTMX + Alpine.js + Chart.js** frontend stack (no Node.js needed, RPi4-friendly)
- **Thread-safe AppState** singleton for communication between planning loop and web server
- **Direct Inverter Control** module (`inverter_control.py`) for Sungrow TOU programs, battery modes, grid charging, PHEV via HA service calls
- **`direct_control` config option** - enables direct inverter control via HA services (default: off for backward compatibility)
- **Circuit breaker** in inverter controller (3 failures -> safe mode)
- **Replan trigger** via GUI button
- **Dark theme UI** with responsive design

### Changed
- Architecture: Single process with two threads (Flask web server + planning loop)
- `main.py` refactored from standalone loop to threaded architecture
- Dockerfile: Added `py3-flask` dependency
- `run.sh`: Added Flask availability check with pip fallback

### Infrastructure
- HA Ingress support (ingress_port: 5050, panel in sidebar)
- Static JS libraries bundled: HTMX 1.9, Alpine.js 3.14, Chart.js 4.4

## [0.9.0] - 2026-03-28

### Fixed
- EMHASS integration: stale data check, discharge efficiency direction, publish-data fallback
- EMHASS config sync for v0.17.1 compatibility

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
