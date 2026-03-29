# EnergieHA - Home Assistant Energy Management Add-on

Energiemanagement-Add-on fuer Home Assistant mit integrierter EMHASS LP-Optimierung. Steuert Hausbatterie ueber Sungrow TOU-Programme, optimiert Netzladung nach guenstigstem Gesamtstrompreis und bietet ein Web-GUI direkt im HA Panel.

## Features

- **EMHASS LP-Optimierung** direkt integriert (kein separates Addon noetig)
- **4 Strategien**: EMHASS (LP), Price (guenstigste Gesamtkosten), Surplus, Forecast
- **Web-GUI** mit 4 Seiten: Dashboard, Planung, Wechselrichter, Konfiguration
- **48h Planungshorizont** (heute + morgen)
- **Sungrow TOU-Steuerung**: Separate PV-only und Grid-Charge Programme
- **Dynamische Preisschwelle** via GUI-Slider
- **Entlade-Sperre**: Keine Entladung wenn Netzpreis < effektive Ladekosten + Spread
- **Stuendliches Lastprofil** aus 7-Tage HA History
- **EPEX Spot** + **Solcast PV Forecast** Integration
- **Dark/Light Theme**, Mobile responsive
- Dry-Run-Modus fuer sicheres Testen

## Installation

1. **Einstellungen → Add-ons → Add-on Store → ⋮ → Repositories**
2. URL hinzufuegen: `https://github.com/ORPA1988/HA-Energy`
3. "EnergieHA" installieren
4. Konfiguration anpassen
5. Add-on starten → EnergieHA Panel erscheint in der Sidebar

## Web-GUI

Das Addon hat eine eigene Web-Oberflaeche (HA Ingress):

| Seite | Inhalt |
|-------|--------|
| **Dashboard** | Power-Flow Animation, SOC Gauge, 48h Preischart, PV Forecast, Slider-Steuerung, 7-Tage Verlauf |
| **Planung** | SOC+Leistung+Preis Chart (heute+morgen), Planungstabelle, Kosten-Zusammenfassung |
| **Wechselrichter** | Live-Werte, BMS Details, TOU-Programme, Quick Actions |
| **Konfiguration** | Alle Einstellungen, Entity-Validation, EMHASS Betriebsmodi |

## Strategien

### EMHASS (Empfohlen)
Lineare Programmierung mit cvxpy. Minimiert Gesamtkosten ueber 48h unter Beruecksichtigung von PV-Forecast, Strompreisen, Batterie-Effizienz und SOC-Grenzen.

**3 Kostenfunktionen:**
- `profit`: Gewinn maximieren (Import min + Export max)
- `cost`: Import-Kosten minimieren
- `self-consumption`: Eigenverbrauch maximieren

**Optionen:** Netzladung verbieten, Netzeinspeisung verbieten, Batterie-Zykluskosten

### Price
Berechnet fuer jede Stunde die tatsaechlichen Grid-Kosten:
```
grid_cost = Preis × max(0, Ladeleistung + Last - PV) / 1000 × Stunden
```
Waehlt die billigsten Stunden fuer Netzladung. Fallback mit Effizienz-Spread-Pruefung wenn alle Preise ueber der Schwelle liegen. Entlade-Sperre wenn Netzpreis < Ladekosten/η + Spread.

### Surplus
PV-Ueberschuss → Batterie. Defizit → Batterie entladen. Einfach und zuverlaessig.

### Forecast
Solcast-basierte Vorausplanung mit dynamischem SOC-Ziel.

## Sungrow TOU-Steuerung

Das Addon programmiert die 6 TOU-Programme des Sungrow Wechselrichters:

| Programm | Funktion |
|----------|----------|
| P1 | Vor Ladung: Disabled (Batterie versorgt Haus) |
| P2 | PV-Ladung: Disabled + hoher SOC (PV laedt Batterie) |
| P3 | Grid-Ladung: Grid + SOC-Ziel (Netz laedt bei billigen Preisen) |
| P4 | Nach Ladung: Disabled (Batterie versorgt Haus) |
| P5-P6 | Dummy-Programme |

**Wichtig:** "Disabled" = Batterie entlaedt (Load First Modus). "Grid" = Netz laedt Batterie.

## Konfiguration

### Batterie
| Parameter | Default | Beschreibung |
|-----------|---------|-------------|
| `battery_capacity_kwh` | 30.0 | Nennkapazitaet kWh |
| `min_soc_percent` | 20 | Minimaler SOC % |
| `max_soc_percent` | 95 | Maximaler SOC % |
| `round_trip_efficiency` | 0.90 | Wirkungsgrad (hin+rueck) |
| `max_grid_charge_soc` | 80 | Max SOC fuer Netzladung % |
| `grid_charge_target_soc` | 90 | Ziel-SOC Netzladung % |

### EMHASS
| Parameter | Default | Beschreibung |
|-----------|---------|-------------|
| `emhass_costfun` | profit | profit / cost / self-consumption |
| `emhass_optimization_time_step` | 30 | Zeitschritt Min (15/30/60) |
| `emhass_nocharge_from_grid` | false | Netzladung verbieten |
| `emhass_nodischarge_to_grid` | true | Netzeinspeisung verbieten |
| `emhass_weight_battery_discharge` | 0.0 | Entlade-Zykluskosten EUR/kWh |
| `emhass_weight_battery_charge` | 0.0 | Lade-Zykluskosten EUR/kWh |
| `maximum_power_from_grid` | 9500 | Max Grid Import W |
| `maximum_power_to_grid` | 9500 | Max Grid Export W |
| `export_price_eur` | 0.10 | Einspeiseverguetung EUR/kWh |

### Price Strategy
| Parameter | Default | Beschreibung |
|-----------|---------|-------------|
| `price_threshold_eur` | 0.25 | Preisschwelle EUR/kWh (auch via GUI-Slider) |
| `min_price_spread_eur` | 0.04 | Mindest-Spread fuer Arbitrage |
| `load_planning_reserve_pct` | 10 | Reserve auf Durchschnittsverbrauch % |

### Allgemein
| Parameter | Default | Beschreibung |
|-----------|---------|-------------|
| `strategy` | surplus | emhass / price / forecast / surplus |
| `cycle_seconds` | 300 | Planungszyklus Sekunden |
| `slot_duration_min` | 15 | Zeitraster Minuten |
| `dry_run` | true | Nur planen, nicht steuern |
| `direct_control` | false | Wechselrichter direkt steuern |
| `sungrow_tou_enabled` | false | TOU-Programme schreiben |

## Publizierte Entitaeten

| Entity | Beschreibung |
|--------|-------------|
| `sensor.energieha_status` | Strategie, Version, TOU-Reason |
| `sensor.energieha_battery_mode` | charge/discharge/idle + Leistung |
| `sensor.energieha_battery_plan` | 48h Plan als JSON |
| `sensor.energieha_planned_soc` | Projizierter End-SOC |
| `sensor.energieha_savings` | Kosten mit/ohne Batterie |
| `sensor.energieha_emhass_diag` | EMHASS Diagnose (Kostenfunktion, Laufzeit) |
| `sensor.p_batt_forecast` | EMHASS Batterie-Forecast |
| `sensor.soc_batt_forecast` | EMHASS SOC-Forecast |
| `sensor.optim_status` | EMHASS Optimierungsstatus |

## Hardware-Setup

| Komponente | Typ |
|------------|-----|
| Wechselrichter | Sungrow (ha-sungrow Integration) |
| Hausbatterie | 30 kWh (~24 kWh nutzbar) |
| PV-Anlage | Peak ~1.6 kW (Solcast Forecast) |
| Stromtarif | Dynamisch EPEX Spot (Brutto) |
| HA Host | Raspberry Pi 4, HA OS, aarch64 |

## Lizenz

MIT
