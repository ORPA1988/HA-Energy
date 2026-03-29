# EnergieHA - Claude Code Projektkontext

> Dieses Dokument enthaelt den vollstaendigen Kontext fuer Claude Code, damit in einem neuen Chat nahtlos weitergearbeitet werden kann.

## Projekt-Ueberblick

**EnergieHA** ist ein Home Assistant Add-on fuer Energiemanagement mit integriertem Web-GUI. Es steuert eine Hausbatterie ueber Sungrow TOU-Programme, optimiert Netzladung nach guenstigstem Gesamtstrompreis (EPEX Spot), nutzt PV-Prognosen (Solcast) und bietet 4 Optimierungsstrategien.

**Repository**: https://github.com/ORPA1988/HA-Energy
**Version**: 1.5.3 (stable branch: `Version_1.5.3_stable`)
**Sprache**: Python + Flask (Web-GUI), Jinja2 + HTMX + Alpine.js + Chart.js (Frontend)
**Deployment**: HA Add-on Container (Alpine + Python + Flask)

## Architektur

### Zwei Threads in einem Prozess
- **Main Thread**: Flask Web-Server (Port 5050, HA Ingress)
- **Background Thread**: Planning Loop (5-Minuten-Zyklus)
- Kommunikation ueber `AppState` Singleton (Thread-safe mit `threading.Lock`)

### Batterie: Modus + Sungrow TOU-Steuerung
Die Ladeleistung wird vom Sungrow-Wechselrichter bestimmt. Das Add-on gibt den Modus vor und programmiert die 6 TOU-Programme:

**Sungrow TOU-Verhalten (Load First Modus):**
- `"Disabled"` + beliebiger SOC = Batterie versorgt Haus (Load First). Strom wird direkt aus Batterie gezogen, egal welcher SOC eingestellt ist. Bei voller Batterie (SOC_ist >= SOC_soll) wird nur aus Netz gezogen.
- `"Grid"` + SOC-Ziel = Netz laedt Batterie aktiv bis zum SOC-Ziel (bei guenstigen Preisen)
- `"Disabled"` + hoher SOC = PV-Ueberschuss laedt Batterie, kein Grid-Import fuer Batterie

**TOU-Strategie (PV und Grid getrennt!):**
- Guenstige Stunden → `"Grid"` + SOC=80% (Netz laedt Batterie)
- Teure Stunden → `"Disabled"` + SOC=min (Batterie versorgt Haus → kein teurer Netz-Import)
- PV-Stunden → `"Disabled"` + SOC=85% (PV-Ueberschuss laedt Batterie, kein Grid)
- Die TOU-Konsolidierung trennt PV-only und Grid-Charge in separate Programme

**Beispiel-Layout bei PV + Grid-Ladung:**
```
P1: 00:00-10:00  Disabled SOC=15% (entladen)
P2: 10:00-13:00  Disabled SOC=85% (PV-only Ladung)
P3: 13:00-16:00  Grid     SOC=80% (Netzladung bei billigsten Preisen)
P4: 16:00-23:50  Disabled SOC=15% (entladen)
P5-P6: Dummy-Programme
```

### PHEV
PHEV wird NICHT von EnergieHA gesteuert. Das Auto wird ueber **evcc** gemanagt.
PHEV-Code bleibt im Add-on (phev_enabled=false), wird aber nicht genutzt.

### Ressourcenschonung
- Change-Detection: API-Writes nur bei geaenderten Werten
- Keine Datenbank, keine persistente Speicherung
- 5-Minuten-Zyklus, ~30 MB RAM, <5% CPU

## Web-GUI (HA Ingress)

Flask-basiert, erreichbar ueber HA Sidebar ("EnergieHA" Panel).

**WICHTIG fuer Entwicklung:**
- Alle URLs muessen **relativ** sein (`./planning`, `./api/state`, `./static/style.css`)
- HA Ingress laeuft in einem iframe → absolute Pfade verlassen den iframe und geben 404
- Alle Routes sind direkt in `app.py` definiert (keine Blueprints - die funktionierten nicht mit Ingress)
- Jede Route ist in try/except gewrappt → Flask crasht nie permanent
- `@app.errorhandler(Exception)` faengt alle Fehler global ab

**4 Seiten:**
| Seite | Route | Inhalt |
|-------|-------|--------|
| Dashboard | `/` | Power-Flow SVG, SOC-Gauge, Preischart 48h, PV-Forecast, Status |
| Planung | `/planning` | 24h Chart (SOC+Leistung+Preis), Planungstabelle, Replan-Button |
| Wechselrichter | `/inverter` | TOU-Programme, Quick Actions, PHEV-Steuerung |
| Konfiguration | `/config` | Alle Addon-Einstellungen, Price-Strategy Felder |

**API-Endpoints:**
```
GET  /api/state     → Aktueller Status (JSON)
GET  /api/plan      → 24h Plan-Slots (JSON)
GET  /api/prices    → EPEX Preise + Lade-Ranges (JSON)
GET  /api/forecast  → PV Solcast Forecast (JSON)
GET  /api/savings   → Kostenvergleich (JSON)
GET  /api/cycles    → Zyklusverlauf (JSON)
POST /api/replan    → Sofortige Neuplanung triggern
POST /api/inverter/reset-tou     → Alle TOU auf Disabled
POST /api/inverter/emergency-idle → Notfall-Idle + Replan
GET  /debug         → Alle registrierten Routes (JSON)
```

## Strategien

### 1. Price (Empfohlen) - Guenstigste Gesamtkosten
Aktive Strategie. Berechnet die **tatsaechlichen Grid-Kosten** pro Stunde:
```
grid_cost = Preis × max(0, Ladeleistung + Last - PV) / 1000 × Stunden
```
- Liest Preisschwelle aus HA: `input_number.epex_preisschwelle_netzladung`
- Liest Ladeleistung aus WR: `grid_charging_current × battery_voltage`
- Berechnet benoetigte Ladestunden fuer Ziel-SOC
- Waehlt die N Stunden mit niedrigstem `grid_cost` (nicht Roh-Preis!)
- PV-reiche Stunden sind billiger weil weniger Grid-Import noetig

### 2. Surplus - PV-Ueberschuss
Einfachster Modus. Laedt nur aus PV-Ueberschuss, entlaedt bei Bedarf.

### 3. Forecast - PV-Prognose
Plant basierend auf Solcast-Prognose. Nacht-Ladung bei niedrigem erwarteten PV.

### 4. EMHASS - LP-Optimierung
Ruft EMHASS REST API auf. **Aktuell problematisch**: EMHASS v0.17.1 publish-data funktioniert nicht, Sensoren werden nicht aktualisiert. Workaround: 48h Staleness-Limit.

## Hardware / HA-Instanz

| Komponente | Details |
|---|---|
| **HA Host** | Raspberry Pi 4, HA OS 17.1, aarch64 |
| **HA Version** | 2026.3.4 |
| **Wechselrichter** | Sungrow (ha-sungrow Integration) |
| **Hausbatterie** | 30 kWh Nenn, ~24 kWh nutzbar, SOH 99.9% |
| **PV-Anlage** | Peak ca. 1.6 kW (Solcast) |
| **Wallbox** | go-eCharger (go-e Integration + Shelly) |
| **PHEV** | Peugeot 308 SW Hybrid, 14 kWh (PSA Car Controller) |
| **Stromzaehler** | ESP01S + SML (ESPHome) |
| **Stromtarif** | Dynamisch EPEX Spot (Brutto inkl. Steuern) |
| **PV-Prognose** | Solcast PV Forecast Integration |
| **Timezone** | Europe/Vienna |

## Entity-Mapping (Stand 2026-03-29)

### Eingabe-Sensoren
```
sensor.inverter_battery                  SOC in % (Attr: BMS Voltage, BMS Current, etc.)
sensor.inverter_battery_power            Batterieleistung W (+charge/-discharge)
sensor.inverter_pv_power                 PV-Leistung W
sensor.inverter_grid_power               Netzleistung W (+import/-export)
sensor.inverter_load_power               Hausverbrauch W
number.inverter_battery_grid_charging_current  Grid-Ladestrom A (aktuell 125A)

sensor.epex_spot_data_total_price_3      Aktueller Brutto-Preis EUR/kWh
  -> Attribut "data": [{start_time, end_time, price_per_kwh}, ...] (72h)

sensor.solcast_pv_forecast_prognose_heute    PV heute
sensor.solcast_pv_forecast_prognose_morgen   PV morgen
  -> Attribut "detailedForecast": [{period_start, pv_estimate (kW!), pv_estimate10, pv_estimate90}]

input_number.epex_preisschwelle_netzladung   Dynamische Preisschwelle (0.18 EUR/kWh)
```

### Ausgabe-Entitaeten
```
sensor.energieha_battery_mode       "charge"/"discharge"/"idle" + estimated_power_w, projected_soc
sensor.energieha_status             Strategie, Version, tou_reason, strategy_error
sensor.energieha_battery_plan       JSON: [{t, mode, soc, batt, pv, load, grid, gridload, price, cost, total}]
sensor.energieha_planned_soc        Projizierter End-SOC
sensor.energieha_savings            Geschaetzte Ersparnis (grid_import, self_consumption, cost_with/without)
sensor.energieha_grid_setpoint      Geplanter Netzfluss W
sensor.energieha_emhass_diag        EMHASS API Diagnose (result_type, result_keys, emhass_url)
```

### Sungrow TOU Entitaeten (6 Programme)
```
time.inverter_program_{1-6}_time           Startzeit
input_datetime.inverter_program_{1-6}_end  Endzeit
select.inverter_program_{1-6}_charging     "Grid" / "Disabled"
number.inverter_program_{1-6}_soc          SOC-Ziel %
```

## Bekannte Datenformate

### EPEX Spot Preise
```json
{"start_time": "2026-03-29T14:00:00+02:00", "end_time": "2026-03-29T15:00:00+02:00", "price_per_kwh": 0.1135}
```
WICHTIG: Feld heisst `price_per_kwh`, Entity ist `sensor.epex_spot_data_total_price_3` (Brutto!).

### Solcast Forecast
```json
{"period_start": "2026-03-29T12:00:00+02:00", "pv_estimate": 1.55, "pv_estimate10": 1.2, "pv_estimate90": 1.9}
```
WICHTIG: `pv_estimate` ist in **kW**. Collector konvertiert automatisch (< 100 → ×1000).

## Status (v1.5.3, Stand 2026-03-29)

**LIVE-Betrieb** mit `strategy=price`, `sungrow_tou_enabled=true`, `dry_run=false`.

### Funktionierende Features
- [x] Web-GUI mit 4 Seiten (Flask + HTMX + Alpine.js + Chart.js, HA Ingress)
- [x] Price Strategy: Guenstigste Gesamtkosten (Grid-Import × Preis, PV-Offset beruecksichtigt)
- [x] Dynamische Preisschwelle aus HA input_number
- [x] Ladeleistung aus WR gelesen (grid_charging_current × battery_voltage)
- [x] TOU-Konsolidierung: Separate PV-only und Grid-Charge Programme
- [x] 48h Preischart mit Lade-Stunden-Markierung (blau)
- [x] PV Forecast Chart mit Konfidenzband (P10/P90)
- [x] Sungrow TOU: 4 aktive + 2 Dummy-Programme
- [x] Quick Actions: Reset-TOU, Emergency-Idle, Replan
- [x] SOC Safety Net + max_grid_charge_soc
- [x] Modus-Hysterese (120s)
- [x] Globaler Error Handler (Flask crasht nie permanent)

### Bekannte Probleme
- [ ] EMHASS publish-data funktioniert nicht (Sensoren stale seit 25.03)
- [ ] EMHASS Strategy faellt auf Surplus zurueck (48h Staleness-Workaround)

## Dateistruktur

```
HA-Energy/
|-- .gitignore
|-- README.md
|-- DEVELOPMENT.md
|-- CHANGELOG.md
|-- CLAUDE.md              <- DIESES DOKUMENT
|-- repository.json
|-- energieha/             <- HA Add-on Ordner
    |-- config.yaml        <- Add-on Definition + Schema
    |-- Dockerfile         <- Alpine + Python + Flask
    |-- build.json         <- Multi-Arch (aarch64, amd64, armv7, i386)
    |-- run.sh             <- Startup Script
    |-- DOCS.md            <- User-Dokumentation
    |-- src/
        |-- __init__.py    <- __version__
        |-- __main__.py    <- Entry Point
        |-- main.py        <- Hauptschleife + Flask Start (2 Threads)
        |-- config.py      <- Config-Loader + Validation
        |-- models.py      <- TimeSlot, Snapshot, Plan, Config Dataclasses
        |-- ha_client.py   <- HA REST API Client
        |-- collector.py   <- Sensordaten + EPEX + Solcast einlesen
        |-- planner.py     <- Strategie-Dispatcher + Fallback
        |-- executor.py    <- Steuer-Entitaeten publizieren
        |-- entities.py    <- Info-Entitaeten publizieren
        |-- sungrow_tou.py <- TOU-Adapter (PV/Grid getrennt)
        |-- emhass_client.py <- EMHASS REST API Client
        |-- inverter_control.py <- Direkte WR-Steuerung via HA Services
        |-- state.py       <- Thread-safe AppState (Plan, Snapshot, Prices, Forecast)
        |-- strategies/
            |-- __init__.py
            |-- helpers.py  <- Gemeinsame Funktionen
            |-- surplus.py  <- PV-Ueberschuss
            |-- price.py    <- Guenstigste Gesamtkosten (AKTIV)
            |-- forecast.py <- PV-Prognose-basiert
            |-- emhass.py   <- EMHASS LP-Optimierung
        |-- web/
            |-- app.py      <- Flask App (ALLE Routes inline, keine Blueprints!)
            |-- templates/  <- Jinja2 (dashboard, planning, inverter, config, base)
            |-- static/     <- CSS + JS (style.css, htmx, alpine, chart.js)
            |-- routes/     <- DEPRECATED (leere Dateien, nicht importiert)
```

## Versionierung

Beide Stellen synchron halten:
- `energieha/config.yaml` → `version: "X.Y.Z"`
- `energieha/src/__init__.py` → `__version__ = "X.Y.Z"`

```bash
git tag vX.Y.Z && git push origin main --tags
```

## Bekannte Fallstricke (Lessons Learned)

1. **HA Ingress iframe**: Alle URLs muessen relativ sein (`./path`), nicht `{{ ingress_path }}/path`
2. **Flask Blueprints**: Funktionieren NICHT mit HA Ingress. Alle Routes inline in app.py.
3. **`app.url_rules`**: Existiert nicht in Flask! Korrekt: `app.url_map.iter_rules()`
4. **Flask Error Handling**: JEDE Route braucht try/except, sonst toetet ein Fehler Flask permanent
5. **EMHASS v0.17.1**: API gibt Text zurueck, keine JSON-Daten. publish-data aktualisiert Sensoren nicht.
6. **TOU PV/Grid**: Muessen getrennt werden. Ein gemeinsamer "charge" Block setzt Grid fuer alles.
7. **EPEX Entity**: `sensor.epex_spot_data_total_price_3` (Brutto!), nicht `_total_price` (Netto)
