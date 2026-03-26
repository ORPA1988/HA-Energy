# EnergieHA - Home Assistant Energy Management Add-on

Leichtgewichtiges Energiemanagement-Add-on fuer Home Assistant. Steuert Hausbatterie und PHEV-Ladung basierend auf PV-Prognose, Strompreisen und aktuellem Verbrauch.

## Features

- **Hausbatterie-Steuerung** (nur Modus: charge/discharge/idle - Leistung bestimmt der Wechselrichter)
- **PHEV-Ueberschussladen** via go-eCharger (Ladeleistung folgt PV-Ueberschuss)
- **3 Strategien**: Surplus, Preisoptimiert, PV-Forecast
- **EPEX Spot** Strompreis-Integration
- **Solcast** PV-Prognose-Integration
- Publiziert Plan-Entitaeten fuer HA-Dashboards
- Dry-Run-Modus fuer sicheres Testen

## Architektur

```
Main Loop (alle 5 min)
  |
  +-- Collector  --> liest HA-Sensoren (SOC, PV, Grid, Load, EPEX, Solcast, PHEV)
  +-- Planner    --> waehlt Strategie, erstellt 24h-Plan (96 Slots a 15 min)
  +-- Executor   --> publiziert Steuer-Entitaeten (battery_mode, phev_target_ampere)
  +-- Publisher   --> publiziert Info-Entitaeten (plan, soc_projection, savings)
```

### Modul-Uebersicht

| Modul | Funktion |
|---|---|
| `main.py` | Hauptschleife, Signal-Handling, Orchestrierung |
| `config.py` | Laedt `/data/options.json` in Config-Dataclass |
| `models.py` | Datenmodelle: TimeSlot, Snapshot, Plan, Config |
| `ha_client.py` | REST-Client fuer HA Supervisor API |
| `collector.py` | Liest Sensoren, parst EPEX/Solcast-Attribute |
| `planner.py` | Strategie-Dispatcher mit Fallback |
| `executor.py` | Publiziert Steuerentitaeten (battery_mode, phev_ampere) |
| `entities.py` | Publiziert Info-Entitaeten (Plan-Timeline, SOC, Savings) |
| `strategies/surplus.py` | Einfacher PV-Ueberschuss-Modus |
| `strategies/price.py` | Preisoptimierter Modus (3-Pass Greedy) |
| `strategies/forecast.py` | PV-Prognose-basierter Modus |

## Installation

1. In Home Assistant: **Einstellungen > Add-ons > Add-on Store > ... > Repositories**
2. Repository-URL hinzufuegen: `https://github.com/ORPA1988/HA-Energy`
3. "EnergieHA" suchen und installieren
4. Konfiguration anpassen (siehe unten)
5. Add-on starten

## Konfiguration

### Hausbatterie

| Parameter | Default | Beschreibung |
|---|---|---|
| `battery_capacity_kwh` | 30.0 | Nennkapazitaet in kWh |
| `min_soc_percent` | 15 | Minimaler SOC (Reserve) |
| `max_soc_percent` | 95 | Maximaler SOC |
| `round_trip_efficiency` | 0.85 | Wirkungsgrad Laden/Entladen |

### PHEV (Plug-in Hybrid)

| Parameter | Default | Beschreibung |
|---|---|---|
| `phev_enabled` | false | PHEV-Steuerung aktivieren |
| `phev_min_charge_w` | 1380 | Minimale Ladeleistung (~6A) |
| `phev_max_charge_w` | 3680 | Maximale Ladeleistung (~16A) |
| `phev_voltage` | 230 | Netzspannung fuer W->A Umrechnung |

### Strategie

| Parameter | Default | Beschreibung |
|---|---|---|
| `strategy` | surplus | `surplus` / `price` / `forecast` |
| `cycle_seconds` | 300 | Update-Intervall (60-900s) |
| `slot_duration_min` | 15 | Planungsraster (15 oder 60 min) |
| `min_price_spread_eur` | 0.04 | Mindest-Preisspanne fuer Arbitrage |
| `price_threshold_eur` | 0.15 | Schwelle fuer guenstige Netzladung |
| `dry_run` | true | Nur lesen+planen, keine Steuerung |

### Entity-Mapping (voreingestellt fuer diese Anlage)

```yaml
# Inverter (Sungrow via ha-sungrow)
entity_battery_soc: "sensor.inverter_battery"
entity_battery_power: "sensor.inverter_battery_power"
entity_pv_power: "sensor.inverter_pv_power"
entity_grid_power: "sensor.inverter_grid_power"
entity_load_power: "sensor.inverter_load_power"

# EPEX Spot
entity_epex_prices: "sensor.epex_spot_data_total_price"

# Solcast PV Forecast
entity_solcast_forecast: "sensor.solcast_pv_forecast_prognose_heute"
entity_solcast_forecast_tomorrow: "sensor.solcast_pv_forecast_prognose_morgen"

# PHEV (PSA + go-eCharger)
entity_phev_soc: "sensor.psa_battery_level"
entity_phev_charging_power: "sensor.garage_wallbox_power"
entity_phev_connected: "sensor.psa_charging_status"
entity_phev_ampere_limit: "number.go_echarger_403613_set_max_ampere_limit"
```

## Publizierte Entitaeten

### Steuer-Entitaeten (Executor)

| Entity | Typ | Beschreibung |
|---|---|---|
| `sensor.energieha_battery_mode` | charge/discharge/idle | Batteriemodus-Sollwert |
| `sensor.energieha_phev_charge_w` | W | PHEV Ziel-Ladeleistung |
| `sensor.energieha_phev_target_ampere` | A | PHEV Ziel-Strom fuer go-eCharger |
| `sensor.energieha_grid_setpoint` | W | Geplanter Netzfluss |

### Info-Entitaeten (Publisher)

| Entity | Typ | Beschreibung |
|---|---|---|
| `sensor.energieha_status` | charge/discharge/idle | Aktueller Status |
| `sensor.energieha_battery_plan` | W | Aktuelle Plan-Leistung + Timeline |
| `sensor.energieha_planned_soc` | % | Projizierter End-SOC + Verlauf |
| `sensor.energieha_savings` | EUR | Geschaetzte Ersparnis vs. ohne Batterie |

### HA-Automation fuer PHEV (manuell erstellen)

```yaml
automation:
  - alias: "EnergieHA PHEV Ampere setzen"
    trigger:
      - platform: state
        entity_id: sensor.energieha_phev_target_ampere
    action:
      - service: number.set_value
        target:
          entity_id: number.go_echarger_403613_set_max_ampere_limit
        data:
          value: "{{ states('sensor.energieha_phev_target_ampere') | int }}"
```

## Strategien

### Surplus (Standard)
Reine Eigenverbrauchsoptimierung:
- PV-Ueberschuss -> PHEV laden (wenn angeschlossen) -> Batterie laden -> Grid-Export
- Defizit -> Batterie entladen -> Grid-Import

### Price (Preisoptimiert)
Dreistufiger Greedy-Algorithmus:
1. **Pass 1**: PV-Ueberschuss zuweisen (PHEV-Prioritaet, dann Batterie)
2. **Pass 2**: Guenstige Grid-Lade-Slots mit teuren Entlade-Slots paaren
3. **Pass 3**: SOC-Simulation mit Constraint-Clipping

### Forecast (PV-Prognose)
Nutzt Solcast-Prognose fuer intelligente Vorausplanung:
- Sonniger Tag: SOC niedrig halten fuer PV-Aufnahme
- Bewoelkter Tag: Nacht-Netzladung auf guenstigen SOC-Zielwert
- Abendspitze: Entladen zur Deckung teurer Stunden

## Hardware-Setup

| Komponente | Typ |
|---|---|
| Wechselrichter | Sungrow (via ha-sungrow Integration) |
| Hausbatterie | 30 kWh (nutzbar ~24 kWh, vom WR gemeldet: 23.8 kWh) |
| PV-Anlage | Solcast-Prognose, Peak ~1.6 kW |
| Stromzaehler | ESP01S mit SML-Anbindung |
| Wallbox | go-eCharger (go-e Integration) |
| PHEV | Peugeot 308 SW Hybrid (PSA Car Controller) |
| Stromtarif | Dynamisch via EPEX Spot |
| HA Host | Raspberry Pi 4, HA OS, aarch64 |

## Datenformate

### EPEX Spot Preise
Entity `sensor.epex_spot_data_total_price` mit Attribut `data`:
```json
[{"start_time": "2026-03-26T00:00:00+01:00",
  "end_time": "2026-03-26T01:00:00+01:00",
  "price_per_kwh": 0.1385}]
```

### Solcast Forecast
Entity `sensor.solcast_pv_forecast_prognose_heute` mit Attribut `detailedForecast`:
```json
[{"period_start": "2026-03-26T06:00:00+01:00",
  "pv_estimate": 0.0154,
  "pv_estimate10": 0.0123,
  "pv_estimate90": 0.0185}]
```
Werte in **kW** (werden automatisch in W umgerechnet).

## Lizenz

MIT
