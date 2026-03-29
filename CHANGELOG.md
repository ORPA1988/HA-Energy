# Changelog

## [2.2.3] - 2026-03-29

### Added
- **48h Planungshorizont** - Plant bis Ende morgen (statt nur 24h)
- **Fallback mit Effizienz-Pruefung** - Laedt oberhalb Schwelle nur wenn Spread nach Wirkungsgrad lohnt
- **Entlade-Sperre** - Batterie entlaedt nicht wenn Netzpreis < effektive Ladekosten + Spread
- **Dashboard Slider** - Preisschwelle, Ziel-SOC, Planungsreserve direkt einstellbar
- **Naechste-Aktion Countdown** - Zeigt wann naechste Netzladung geplant ist
- **Wechselrichter BMS Details** - Spannung, Strom, Temperatur, Zyklen
- **TOU-Reason Anzeige** - Erklaert warum TOU-Programme so gesetzt sind
- **Strategie-Sofort-Wechsel** - Button in Config mit Replan
- **Entity-Validation** - Prueft ob konfigurierte Entities in HA existieren
- **Planning Summary Cards** - Grid Import kWh, Kosten, Lade-/Entlade-Slots
- **7-Tage Verbrauchs-History** - Durchschnittslast aus HA History API
- **Planungsreserve** - Konfigurierbarer Aufschlag auf Durchschnittsverbrauch

### Changed
- **EMHASS Fallback → Price** statt Surplus (immer bestmoegliche Optimierung)
- **HA Helper entfernt** - input_number.epex_preisschwelle_netzladung durch GUI-Slider ersetzt
- **Chart: Heute + Morgen** mit dynamischer Skalierung aller Achsen
- **SOC-Simulation** realistisch: Idle-Slots simulieren Sungrow Load-First Verhalten

### Fixed
- **Chart Skalierung** - Alle Leistungswerte (PV, Last, Batterie) auf einer kW-Achse

## [2.0.0] - 2026-03-29

### Added
- **Dashboard Quick Controls** - Slider fuer Preisschwelle, Ziel-SOC, Reserve
- **POST /api/settings** - Live-Aenderung von Config-Werten via GUI

## [1.9.1] - 2026-03-29

### Added
- **Dynamischer Verbrauch** aus 7-Tage HA History (sum/7/24h + Reserve)
- **Planungsreserve %** als Config-Option

## [1.9.0] - 2026-03-29

### Fixed
- **SOC-Simulation** - Idle-Slots simulieren Batterie-Entladung (Load First)
- **Dynamische Last** aus HA History statt statischem Config-Wert

## [1.8.0] - 2026-03-29

### Added
- **Mobile responsive** - Hamburger Menu, responsive Charts/Tabellen
- **Persistent State** - Plan/Preise/Forecast ueberleben Addon-Restart

## [1.7.0] - 2026-03-29

### Added
- **Wechselrichter Live-Werte** - SOC, PV, Grid, Load direkt vom WR
- **Savings-Anzeige** auf Dashboard (Kosten, Eigenverbrauch)
- **Entity-Validation** - Prueft ob Entities in HA existieren

## [1.6.0] - 2026-03-29

### Added
- **Animierter Power-Flow** - Dynamische Strichstaerke, farbcodierte Pfade, Glow
- **JETZT-Markierung** im Planungschart

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
