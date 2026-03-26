# EnergieHA - Claude Code Projektkontext

> Dieses Dokument enthaelt den vollstaendigen Kontext fuer Claude Code, damit in einem neuen Chat nahtlos weitergearbeitet werden kann.

## Projekt-Ueberblick

**EnergieHA** ist ein leichtgewichtiges Home Assistant Add-on fuer Energiemanagement. Es steuert eine Hausbatterie ueber Sungrow TOU-Programme, basierend auf EMHASS LP-Optimierung, PV-Prognose (Solcast), Strompreisen (EPEX Spot) und aktuellem Verbrauch. PHEV-Ladung wird von evcc gesteuert.

**Repository**: https://github.com/ORPA1988/HA-Energy
**Version**: 0.3.9
**Sprache**: Python (kein Framework, nur `requests`)
**Deployment**: HA Add-on Container (Alpine + Python)

## Architektur-Entscheidungen

### Batterie: Modus + Sungrow TOU-Steuerung
Die Ladeleistung der Hausbatterie wird durch den Sungrow-Wechselrichter bestimmt. Das Add-on gibt den Modus vor (charge/discharge/idle) und programmiert optional die 6 TOU-Programme des Wechselrichters direkt (`sungrow_tou_enabled: true`).

**Sungrow TOU-Mapping:**
- `charge` → `select.inverter_program_N_charging = "Grid"`, SOC-Ziel = geplanter SOC%
- `discharge`/`idle` → `charging = "Disabled"`, SOC = min_soc% (Load First entlaedt automatisch)
- Der 24h-Plan (96 Slots) wird auf max. 6 Programme konsolidiert
- Change-Detection: Programme werden nur geschrieben wenn sich Werte aendern

### PHEV: Leistung folgt PV-Ueberschuss
Die PHEV-Ladeleistung wird aktiv gesteuert. Das Add-on berechnet die optimale Leistung (W), konvertiert zu Ampere (W / 230V, clamp 6-16A) und publiziert `sensor.energieha_phev_target_ampere`. Die HA-Automation `automation.energieha_phev_ampere_go_echarger` uebertraegt diesen Wert automatisch auf die go-eCharger Wallbox.

### Steuerung: Hybrid (indirekt + direkt)
- **Sensoren**: Publiziert `sensor.energieha_*` Entitaeten fuer Dashboard-Sichtbarkeit
- **Sungrow TOU**: Programmiert die WR TOU-Programme direkt (opt-in via Config)
- **PHEV**: HA-Automation uebertraegt Ampere-Wert auf go-eCharger

### Ressourcenschonung
- Change-Detection: API-Writes nur bei geaenderten Werten
- Keine Datenbank, keine persistente Speicherung
- Kompakte Logs (nur Entscheidungen, keine Sensorwerte pro Zyklus)
- 5-Minuten-Zyklus (nicht jede Sekunde)

## Hardware / HA-Instanz

| Komponente | Details |
|---|---|
| **HA Host** | Raspberry Pi 4, HA OS 17.1, aarch64 |
| **HA Version** | 2026.3.4 |
| **Wechselrichter** | Sungrow (ha-sungrow Integration) |
| **Hausbatterie** | 30 kWh Nennkapazitaet, ~24 kWh nutzbar, SOH 99.9% |
| **PV-Anlage** | Peak ca. 1.6 kW (laut Solcast) |
| **Wallbox** | go-eCharger (go-e Integration + Shelly Garage_Wallbox) |
| **PHEV** | Peugeot 308 SW Hybrid, 14 kWh Batterie (PSA Car Controller Add-on) |
| **Stromzaehler** | ESP01S mit SML-Anbindung (ESPHome) |
| **Stromtarif** | Dynamisch via EPEX Spot (EPEX Spot Integration) |
| **PV-Prognose** | Solcast PV Forecast Integration |
| **Timezone** | Europe/Vienna |

## Entity-Mapping (echte HA-Instanz, Stand 2026-03-26)

### Eingabe-Sensoren (lesen)
```
sensor.inverter_battery            SOC in % (aktuell: 10%)
sensor.inverter_battery_power      Batterieleistung in W (+charge/-discharge)
sensor.inverter_battery_state      "charging"/"idle"/"discharging" (enum)
sensor.inverter_battery_capacity   Nutzbare Kapazitaet in kWh (23.8)
sensor.inverter_pv_power           PV-Leistung in W (PV1+PV2 kombiniert)
sensor.inverter_grid_power         Netzleistung in W (+import/-export)
sensor.inverter_load_power         Hausverbrauch in W

sensor.epex_spot_data_total_price  Aktueller Preis in EUR/kWh
  -> Attribut "data": Liste mit {start_time, end_time, price_per_kwh}
  -> 48 Datenpunkte (heute + morgen)

sensor.solcast_pv_forecast_prognose_heute   PV-Prognose heute in kWh
  -> Attribut "detailedForecast": Liste mit {period_start, pv_estimate (kW!), pv_estimate10, pv_estimate90}
  -> 30-Minuten-Intervalle

sensor.solcast_pv_forecast_prognose_morgen  PV-Prognose morgen

sensor.psa_battery_level           PHEV SOC in % (88%)
sensor.psa_charging_status         "Disconnected"/"InProgress"/"Connected"/"WaitScheduled"
sensor.garage_wallbox_power        Wallbox aktuelle Leistung in W
number.go_echarger_403613_set_max_ampere_limit  Wallbox Ampere-Limit (0-16A)
```

### Ausgabe-Entitaeten (publiziert vom Add-on)
```
sensor.energieha_battery_mode      "charge"/"discharge"/"idle"
sensor.energieha_phev_charge_w     Ziel-Ladeleistung PHEV in W
sensor.energieha_phev_target_ampere  Ziel-Strom PHEV in A (fuer go-eCharger)
sensor.energieha_grid_setpoint     Geplanter Netzfluss in W
sensor.energieha_status            Status + Strategie-Info
sensor.energieha_battery_plan      Plan-Timeline als JSON
sensor.energieha_planned_soc       SOC-Projektion
sensor.energieha_savings           Geschaetzte Ersparnis
```

### Weitere relevante HA-Entitaeten
```
sensor.inverter_battery_soh        99.9% (Batteriegesundheit)
sensor.inverter_pv1_power          PV String 1 in W
sensor.inverter_pv2_power          PV String 2 in W
sensor.soc_batt_forecast           Batterie-SOC-Forecast (von EMHASS?)
sensor.p_pv_forecast               PV Power Forecast in W (von EMHASS?)
sensor.p_load_forecast             Load Power Forecast in W
sensor.p_grid_forecast             Grid Power Forecast in W
sensor.p_batt_forecast             Battery Power Forecast in W
binary_sensor.go_echarger_403613_allowed_to_charge  Wallbox Ladefreigabe
select.go_echarger_403613_phase_switch_mode  Phasenmodus (aktuell: Force_1)
input_number.epex_preisschwelle_netzladung  Benutzer-Preisschwelle (0.18 EUR/kWh)
```

### Installierte Add-ons (relevant)
- EMHASS v0.17.1 (gestoppt) - Alternative Energieoptimierung
- evcc 0.303.2 (gestoppt) - EV-Lademanagement
- PSA Car Controller v3.6.3 (laeuft) - Peugeot-Anbindung
- Solcast PV Forecast (Integration, kein Add-on)
- EPEX Spot (Integration, kein Add-on)

## Bekannte Datenformate

### EPEX Spot Preise
Entity-State = aktueller Preis. Attribut `data` = Array:
```json
{"start_time": "2026-03-26T00:00:00+01:00", "end_time": "2026-03-26T01:00:00+01:00", "price_per_kwh": 0.1385}
```
WICHTIG: Feld heisst `price_per_kwh` (nicht `price` oder `value`).

### Solcast Forecast
Attribut `detailedForecast` = Array:
```json
{"period_start": "2026-03-26T06:00:00+01:00", "pv_estimate": 0.0154, "pv_estimate10": 0.0123, "pv_estimate90": 0.0185}
```
WICHTIG: `pv_estimate` ist in **kW** (nicht W). Collector konvertiert automatisch (< 100 -> *1000).

### PSA Charging Status
String-Werte: `"Disconnected"`, `"InProgress"`, `"Connected"`, `"WaitScheduled"` etc.
Collector erkennt `inprogress`, `charging`, `waitscheduled`, `connected` als "angeschlossen".

## Status (v0.3.9, Stand 2026-03-26)

**LIVE-Betrieb** mit `strategy=emhass`, `sungrow_tou_enabled=true`, `dry_run=false`.
Batterie wird aktiv gesteuert: Netzladung bei guenstigen Preisen (EMHASS LP-Optimierung).
PHEV-Steuerung deaktiviert (wird ueber evcc gehandhabt).

### Funktionierende Features
- [x] EMHASS LP-Optimierung via REST API (auto-detect Docker URL)
- [x] Sungrow TOU: 3 aktive + 3 Dummy-Programme, flexibel, Mitternacht-safe
- [x] TOU P2: Grid nur bei Netzladung, Disabled+SOC fuer PV-Laden
- [x] Surplus/Price/Forecast als Fallback-Strategien
- [x] Round-trip efficiency (85%)
- [x] Dynamische Sunrise/Sunset von sun.sun
- [x] Modus-Hysterese (120s)
- [x] SOC Safety Net + max_grid_charge_soc (80%)
- [x] Startup Entity-Validierung + Error-Reporting in Status-Entity
- [x] EMHASS Sensor-Format (battery_scheduled_power/soc), Vorzeichen, Stunden→15min

### PHEV
PHEV wird NICHT von EnergieHA gesteuert. Das Auto wird ueber **evcc** (separates Add-on) gemanagt.
PHEV-Code bleibt im Add-on (phev_enabled=false), wird aber nicht genutzt.

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
    |-- config.yaml        <- Add-on Definition (Version hier erhoehen!)
    |-- Dockerfile
    |-- run.sh
    |-- src/               <- Python-Paket -> /app/energieha/ im Container
        |-- __init__.py    <- __version__ (hier auch erhoehen!)
        |-- main.py        <- Hauptschleife, Orchestrierung
        |-- config.py      <- Config-Loader (/data/options.json)
        |-- models.py      <- TimeSlot, Snapshot, Plan, Config Dataclasses
        |-- ha_client.py   <- HA REST API (GET/POST states, services)
        |-- collector.py   <- Sensordaten + EPEX + Solcast einlesen
        |-- planner.py     <- Strategie-Dispatcher + Fallback
        |-- executor.py    <- Steuer-Entitaeten publizieren
        |-- entities.py    <- Info-Entitaeten publizieren
        |-- sungrow_tou.py <- Sungrow TOU-Adapter (6 Programme)
        |-- emhass_client.py <- EMHASS REST API Client
        |-- strategies/
            |-- __init__.py
            |-- helpers.py    <- Gemeinsame Funktionen (SOC, Grid, PHEV)
            |-- surplus.py    <- PV-Ueberschuss-Modus
            |-- price.py      <- Preisoptimiert (3-Pass Greedy)
            |-- forecast.py   <- PV-Prognose-basiert
            |-- emhass.py     <- EMHASS LP-Optimierung
```

## Versionierung

HA erkennt Updates wenn `version` in `energieha/config.yaml` sich aendert.
Beide Stellen synchron halten:
- `energieha/config.yaml` -> `version: "X.Y.Z"`
- `energieha/src/__init__.py` -> `__version__ = "X.Y.Z"`

Git-Tag erstellen: `git tag vX.Y.Z && git push origin main --tags`
