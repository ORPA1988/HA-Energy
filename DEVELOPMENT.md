# EnergieHA - Entwicklungs- und Debug-Plan

## Status: v0.1.0 - Erster lauffaehiger Prototyp

### Was funktioniert
- [x] Add-on-Struktur (Dockerfile, config.yaml, run.sh)
- [x] Config-Laden aus /data/options.json
- [x] HA REST API Client mit Retry-Logik
- [x] Collector: Sensordaten lesen (SOC, PV, Grid, Load)
- [x] Collector: EPEX-Preise parsen (Attribut `data` -> `price_per_kwh`)
- [x] Collector: Solcast-Forecast parsen (Attribut `detailedForecast` -> `pv_estimate` kW->W)
- [x] Collector: PHEV-Status lesen (PSA charging_status)
- [x] 3 Strategien: surplus, price, forecast
- [x] PHEV-Ueberschussladen in allen Strategien
- [x] Executor: Batteriemodus publizieren (nur Modus, keine Leistung)
- [x] Executor: PHEV-Ampere berechnen und publizieren (W->A Konvertierung)
- [x] EntityPublisher: Plan-Timeline, SOC-Projektion, Savings
- [x] Planner-Fallback: bei fehlenden Daten -> surplus
- [x] Dry-Run-Modus (Default: aktiviert)
- [x] GitHub-Repository: ORPA1988/HA-Energy

### Was NICHT funktioniert / noch fehlt

#### P0 - Blocker fuer ersten Testlauf
- [ ] **Add-on in HA installieren** - Repository-Cache muss aktualisiert werden
  - Aktion: In HA unter Einstellungen > Add-ons > Store > Repositories das Repo entfernen und neu hinzufuegen
  - Dann "EnergieHA" suchen und installieren
- [ ] **Erster Dry-Run-Test** - Add-on starten und Logs pruefen
  - Erwartung: Sensoren werden gelesen, Plan wird erstellt, Entitaeten publiziert
  - Keine Steuerung (dry_run: true)

#### P1 - Wichtige Verbesserungen
- [ ] **Zeitzone-Handling** - Aktuell UTC, HA-Zeitzone (Europe/Vienna) nicht beruecksichtigt
  - Betrifft: SUNRISE_HOUR/SUNSET_HOUR in forecast.py (fest 6/20 UTC)
  - Fix: HA-Zeitzone aus `/api/config` lesen und in Strategien nutzen
- [ ] **Batteriemodus-Steuerung tatsaechlich ausfuehren**
  - Aktuell: Nur `sensor.energieha_battery_mode` publiziert
  - Fehlt: HA-Automation oder direkte API-Calls um den WR-Modus zu setzen
  - Abhaengig von: Welche Entities bietet der Sungrow-WR fuer Modus-Steuerung?
- [ ] **PHEV-Steuerung tatsaechlich ausfuehren**
  - Aktuell: Nur `sensor.energieha_phev_target_ampere` publiziert
  - Fehlt: HA-Automation oder direkter call_service auf go-eCharger
  - Beispiel-Automation ist im README dokumentiert
- [ ] **Solcast Forecast-Morgen Entity**
  - Pruefen ob `sensor.solcast_pv_forecast_prognose_morgen` existiert und Daten hat
  - Eventuell anderer Entity-Name

#### P2 - Robustheit
- [ ] **Fehlerbehandlung bei fehlenden EPEX-Daten**
  - Aktuell: Fallback auf aktuelle Stunde wenn keine Zeitreihe
  - Verbesserung: Letzten bekannten Plan beibehalten wenn keine neuen Preise
- [ ] **Sensor-Validierung beim Start**
  - Beim ersten Zyklus alle konfigurierten Entities pruefen und warnen wenn nicht vorhanden
- [ ] **Retry bei Supervisor-Token-Problemen**
  - SUPERVISOR_TOKEN kann beim Container-Start kurz nicht verfuegbar sein
- [ ] **Cycle-Skipping bei hoher Last**
  - Wenn ein Zyklus laenger als cycle_seconds dauert, nicht aufstauen

#### P3 - Features
- [ ] **Steuerbare Verbraucher** (z.B. Warmwasser-Boiler, Waschmaschine)
  - Konfigurierbare Liste von switch-Entities mit Zeitfenstern
- [ ] **Batteriegesundheit** - Zyklenminimierung
  - Unnoetige Lade/Entlade-Wechsel vermeiden
  - SOC-Spread begrenzen (z.B. nicht von 15% auf 95% in einem Zyklus)
- [ ] **Einspeiseverguetung** beruecksichtigen
  - Wenn Export > 0 EUR/kWh, in Kostenberechnung einbeziehen
- [ ] **Netzlastspitzen-Management** (Peak Shaving)
  - Max Grid-Import begrenzen
- [ ] **Mehrere PV-Strings** unterstuetzen
  - Aktuell: sensor.inverter_pv_power (Summe)
  - Optional: PV1 + PV2 separat fuer bessere Prognose

## Debug-Anleitung

### Logs pruefen
Im Add-on-Tab von HA: **EnergieHA > Protokoll**

Erwartete Log-Eintraege bei erfolgreichem Start:
```
EnergieHA v0.1.0 starting
Strategy: surplus | Cycle: 300s | Slots: 15min | Battery: 30 kWh (SOC 15%-95%)
DRY RUN MODE
Home Assistant API is reachable
Loaded config: strategy=surplus, cycle=300s, slots=15min, phev=False
Cycle 1: SOC=10.0% PV=0W Load=2800W Grid=2890W | Prices=48 Forecast=48
Surplus plan: 96 slots, SOC 10.0%->15.0%, PHEV=off
Control: battery=discharge | PHEV=0W | grid=2800W | price=0.1944 EUR/kWh | SOC->10.0%
```

### Haeufige Fehler

| Fehler | Ursache | Loesung |
|---|---|---|
| `Battery SOC unavailable` | Entity falsch oder Inverter offline | Entity-Name in Config pruefen |
| `No price data available` | EPEX-Entity hat kein `data` Attribut | Entity-Name pruefen, ggf. `sensor.epex_spot_data_market_price` testen |
| `HA API not available` | Supervisor-Token fehlt | Pruefen ob `homeassistant_api: true` in config.yaml |
| `Strategy 'price' failed` | Keine Preisdaten -> automatischer Fallback auf surplus | EPEX-Integration pruefen |

### Entity-Werte manuell pruefen
In HA Entwicklerwerkzeuge > Status:
```
sensor.inverter_battery          -> SOC in %
sensor.inverter_battery_power    -> Leistung in W (+laden/-entladen)
sensor.inverter_pv_power         -> PV in W
sensor.inverter_grid_power       -> Netz in W (+import/-export)
sensor.inverter_load_power       -> Hausverbrauch in W
sensor.epex_spot_data_total_price -> Preis EUR/kWh (Attribut 'data' fuer Zeitreihe)
sensor.psa_charging_status       -> "Disconnected" / "InProgress" / "Connected"
```

## Versionierung

- **Semantic Versioning**: MAJOR.MINOR.PATCH
- HA erkennt neue Versionen automatisch wenn `version` in `energieha/config.yaml` erhoeht wird
- Jede Version bekommt einen Git-Tag: `v0.1.0`, `v0.2.0`, etc.
- CHANGELOG.md dokumentiert alle Aenderungen

### Versions-Erhoehung
1. `energieha/config.yaml`: `version: "X.Y.Z"` aendern
2. `energieha/src/__init__.py`: `__version__ = "X.Y.Z"` aendern
3. `CHANGELOG.md` aktualisieren
4. Commit + Tag: `git tag vX.Y.Z && git push origin main --tags`

## Projektstruktur

```
HA-Energy/                        <- GitHub Repo Root
|-- .gitignore
|-- README.md                     <- Dieses Dokument
|-- DEVELOPMENT.md                <- Entwicklungsplan (dieses Dokument)
|-- CHANGELOG.md                  <- Versionshistorie
|-- CLAUDE.md                     <- Kontext fuer Claude Code (naechster Chat)
|-- repository.json               <- HA Add-on Repository Metadaten
|-- energieha/                    <- Add-on Ordner (slug = energieha)
|   |-- config.yaml               <- Add-on Definition + Schema + Defaults
|   |-- Dockerfile                <- Alpine + Python + py3-requests
|   |-- run.sh                    <- Startskript (bashio + python3 -m energieha.main)
|   |-- src/                      <- Python-Paket (wird zu /app/energieha/ im Container)
|       |-- __init__.py
|       |-- main.py               <- Hauptschleife
|       |-- config.py             <- Config-Loader
|       |-- models.py             <- Dataclasses
|       |-- ha_client.py          <- HA REST Client
|       |-- collector.py          <- Sensor-Daten sammeln
|       |-- planner.py            <- Strategie-Dispatcher
|       |-- executor.py           <- Steuer-Entitaeten publizieren
|       |-- entities.py           <- Info-Entitaeten publizieren
|       |-- strategies/
|           |-- __init__.py
|           |-- surplus.py        <- PV-Ueberschuss-Modus
|           |-- price.py          <- Preisoptimierter Modus
|           |-- forecast.py       <- PV-Prognose-Modus
```

## Technische Details

### HA Kommunikation
- **REST API** ueber `http://supervisor/core/api`
- **Auth**: `SUPERVISOR_TOKEN` Environment-Variable (automatisch von HA injiziert)
- **Berechtigungen**: `homeassistant_api: true` in config.yaml
- Entitaeten werden via `POST /api/states/sensor.energieha_*` erstellt/aktualisiert

### Docker-Container
- Base Image: HA Add-on Base (Alpine Linux)
- Installiert: `python3`, `py3-requests` (via apk, kein pip noetig)
- Working Directory: `/app`
- Python-Paket: `/app/energieha/` (kopiert aus `src/`)
- Start: `python3 -m energieha.main`

### Ressourcen-Verbrauch (Ziel)
- CPU: < 1% im Idle, < 5% waehrend Planung
- RAM: < 30 MB
- Disk: Nur Logs, keine persistente Datenbank
- I/O: ~10 API-Calls pro Zyklus (alle 5 min)
