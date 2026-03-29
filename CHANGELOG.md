# Changelog

## [1.5.3] - 2026-03-29

### Fixed
- **Logs-Reiter entfernt** - Logs sind ueber das HA Addon-Menu zugaenglich

## [1.5.2] - 2026-03-29

### Fixed
- **Navigation funktioniert** - Alle URLs auf relative Pfade umgestellt (`./planning` statt `{{ ingress_path }}/planning`)
- HA Ingress laedt Addons in einem iframe → absolute Pfade verliessen den iframe-Kontext

## [1.5.1] - 2026-03-29

### Fixed
- **Flask Startup Crash** behoben: `app.url_rules` → `app.url_map.iter_rules()` (AttributeError toetete Flask beim Start)
- Variable-Name Fix in `/api/prices` Route

## [1.5.0] - 2026-03-29

### Fixed
- **Fehlende API-Endpoints** hinzugefuegt: `/api/forecast` und `/api/savings` (Dashboard JS rief diese auf aber sie existierten nicht)
- **Globaler Error Handler** `@app.errorhandler(Exception)` verhindert permanente Flask-Crashes
- Jede Route in try/except gewrappt
- JS fetch()-Aufrufe in try/catch (priceChart, forecastChart)
- Alte Blueprint-Dateien durch Deprecation-Hinweise ersetzt
- Flask Version gepinnt: `>=3.0,<4.0`

## [1.4.0] - 2026-03-29

### Changed
- **Alle Routes inline in app.py** - Blueprints entfernt (funktionierten nicht mit HA Ingress)

## [1.3.3] - 2026-03-29

### Changed
- **Price Strategy: Guenstigste Gesamtkosten** statt billigster Roh-Preis
  - Sortiert nach `Preis × max(0, Ladeleistung + Last - PV)`
  - PV-reiche Stunden werden bevorzugt (weniger Grid-Import)
- Preischart: Lade-Stunden per ISO-Datum gematcht (nicht nur Stunde)
- Labels zeigen Datum-Prefix fuer zweiten Tag

## [1.2.1] - 2026-03-29

### Fixed
- **TOU PV/Grid Trennung** - PV-Surplus und Grid-Ladung in separate TOU-Programme
  - Vorher: Ein "charge" Block → Grid fuer alles (zu fruehe Netzladung)
  - Nachher: PV-Block → Disabled+SOC, Grid-Block → Grid+SOC (getrennt)

## [1.1.0] - 2026-03-29

### Added
- **Smart Grid Charging** - Neue Price Strategy
  - Dynamische Preisschwelle aus HA Entity (`input_number.epex_preisschwelle_netzladung`)
  - Ladeleistung aus Wechselrichter gelesen (grid_charging_current × battery_voltage)
  - Ziel-SOC konfigurierbar (`grid_charge_target_soc`)
  - Berechnet benoetigte Ladestunden automatisch

### Changed
- Config: Neue Felder `entity_price_threshold`, `grid_charge_target_soc`

## [1.0.0] - 2026-03-28

### Added
- **Web GUI mit HA Ingress** - Addon GUI ueber HA Sidebar
  - Dashboard: Power-Flow, SOC-Gauge, Status-Cards, EMHASS-Indikator
  - Planung: 24h Timeline-Tabelle mit Chart.js Charts
  - Wechselrichter: TOU-Editor, Quick Actions, PHEV-Steuerung
  - Konfiguration: Formular-basierter Config-Editor
- **Flask + HTMX + Alpine.js + Chart.js** (kein Node.js, RPi4-freundlich)
- **Thread-safe AppState** fuer Planning-Loop ↔ Web-Server
- **Direct Inverter Control** via HA Services
- **Circuit Breaker** im Inverter Controller
- **Dark Theme UI** mit responsive Design

## [0.9.0] - 2026-03-28

### Fixed
- EMHASS Integration: Stale-Data-Check, publish-data Fallback

## [0.1.0] - 2026-03-26

### Added
- Erstveroeffentlichung
- 3 Strategien: Surplus, Price (3-Pass Greedy), Forecast (Solcast)
- Batterie-Steuerung, PHEV-Ladung, EPEX/Solcast Integration
- Dry-Run Modus, Change-Detection, Graceful Shutdown
