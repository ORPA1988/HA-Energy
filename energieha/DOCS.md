# EnergieHA — Dokumentation

## Was macht EnergieHA?

EnergieHA ist ein leichtgewichtiges Energiemanagement-Add-on fuer Home Assistant. Es optimiert automatisch, **wann** deine Hausbatterie aus dem Netz geladen wird und **wann** sie entlaedt, um die Stromkosten zu minimieren.

### So funktioniert es

1. **Daten sammeln**: Alle 5 Minuten liest EnergieHA PV-Prognose (Solcast), Strompreise (EPEX Spot), Batterie-SOC und Hausverbrauch.
2. **Optimieren**: EMHASS berechnet per linearer Programmierung den kostenoptimalen 24h-Batterie-Fahrplan.
3. **Steuern**: Der Plan wird in die 6 TOU-Programme des Sungrow-Wechselrichters uebersetzt.
4. **Anzeigen**: Alle Daten werden als HA-Sensoren publiziert und im Dashboard dargestellt.

---

## Konfiguration

### Strategie

| Strategie | Beschreibung |
|-----------|-------------|
| `emhass` | **Empfohlen.** Nutzt EMHASS LP-Optimierung fuer kostenoptimalen Plan. Benoetigt laufendes EMHASS Add-on. |
| `surplus` | Einfach: PV-Ueberschuss in Batterie, Batterie entlaedt bei Bedarf. Kein Preisbezug. |
| `price` | Greedy-Algorithmus: Guenstig laden, teuer entladen. Einfacher als EMHASS. |
| `forecast` | PV-Prognose-basiert: Plant SOC-Ziel bei Sonnenaufgang, naechtliches Grid-Charging bei wenig PV. |

Bei Fehlern faellt EnergieHA automatisch auf `surplus` zurueck.

### Wichtige Parameter

| Parameter | Default | Beschreibung |
|-----------|---------|-------------|
| `strategy` | surplus | Optimierungsstrategie (surplus/price/forecast/emhass) |
| `cycle_seconds` | 300 | Planungszyklus in Sekunden (5 Minuten) |
| `battery_capacity_kwh` | 30.0 | Nennkapazitaet der Hausbatterie in kWh |
| `min_soc_percent` | 15 | Absolutes Minimum — SOC faellt nie darunter |
| `max_soc_percent` | 95 | Absolutes Maximum — Batterie wird nie hoeher geladen |
| `max_grid_charge_soc` | 80 | Max SOC fuer Netzladung. PV darf hoeher laden. |
| `round_trip_efficiency` | 0.85 | Batterie-Wirkungsgrad (Lade- und Entladeverluste) |
| `sungrow_tou_enabled` | false | Sungrow TOU-Programme direkt programmieren |
| `dry_run` | true | Testmodus: Entities publizieren, aber WR nicht steuern |
| `export_price_eur` | 0.10 | Einspeisung (EUR/kWh), fix unabhaengig von EPEX |
| `mode_hold_seconds` | 120 | Mindestzeit in Sekunden bevor Modus wechselt (Hysterese) |
| `price_threshold_eur` | 0.25 | Max Preis fuer Grid-Charging (Brutto, nur fuer price-Strategie) |

### Entity-Konfiguration

Alle Sensor-Entity-IDs sind konfigurierbar. Die Defaults passen fuer eine Sungrow-Installation mit EPEX Spot und Solcast:

- `entity_battery_soc`: `sensor.inverter_battery`
- `entity_epex_prices`: `sensor.epex_spot_data_total_price_3` (Brutto inkl. Steuern)
- `entity_grid_charge_current`: `number.inverter_battery_grid_charging_current`

---

## Sungrow TOU-Steuerung

### Wie die Programme funktionieren

Der Sungrow-Wechselrichter hat 6 TOU-Programme. EnergieHA nutzt 3 aktive + 3 Dummy-Programme:

| Programm | Funktion |
|----------|----------|
| **P1** (00:00 → Ladebeginn) | `Disabled` SOC=min — Batterie entlaedt fuer Hausverbrauch (Load First) |
| **P2** (Ladebeginn → Ladeende) | Lademodus + SOC-Ziel — PV oder Grid laedt Batterie |
| **P3** (Ladeende → 23:50) | `Disabled` SOC=min — Batterie entlaedt fuer Hausverbrauch |
| P4-P6 (23:50-23:56) | Dummy-Programme (letzte Minuten, inaktiv) |

### Lademodus von P2

| Modus | Wann | Bedeutung |
|-------|------|-----------|
| `Disabled` + SOC-Ziel | PV-Laden | PV laedt Batterie exklusiv bis SOC-Ziel. Haus aus Netz. |
| `Grid` + SOC-Ziel | Netzladung | Netz + PV laden Batterie bis SOC-Ziel. Fuer guenstige Nachtstunden. |
| `Disabled` + SOC=min | Kein Laden | Normalbetrieb: Batterie dient Hausverbrauch. |

### Zeiten

Die Start-/Endzeiten werden **flexibel** aus dem 24h-Plan berechnet (erster/letzter Charge-Slot). Programme sind lueckenlos und chronologisch.

---

## EMHASS Integration

### Voraussetzungen

- EMHASS Add-on muss installiert und gestartet sein
- EMHASS muss korrekt konfiguriert sein (PV-System, Batterie, Solcast)
- Die Automation `EnergieHA EMHASS Optimierung triggern` muss aktiviert sein

### Wie es zusammenspielt

1. Die HA-Automation triggert EMHASS alle 4 Stunden mit aktuellen EPEX-Preisen
2. EMHASS berechnet den optimalen Batterie-Fahrplan (LP-Optimierung)
3. EMHASS publiziert Ergebnisse als HA-Sensoren (`sensor.p_batt_forecast`, `sensor.soc_batt_forecast`)
4. EnergieHA liest diese Sensoren und uebersetzt den Plan in TOU-Programme

### EMHASS-Sensoren

| Sensor | Bedeutung |
|--------|-----------|
| `sensor.p_batt_forecast` | Batterie-Fahrplan (W): positiv=Entladung, negativ=Ladung |
| `sensor.soc_batt_forecast` | SOC-Prognose (%) |
| `sensor.p_pv_forecast` | PV-Leistungsprognose (W) |
| `sensor.p_load_forecast` | Hausverbrauchsprognose (W) |
| `sensor.optim_status` | "Optimal" wenn Berechnung erfolgreich |

### Daten-Frische

EnergieHA prueft ob EMHASS-Daten maximal 6 Stunden alt sind. Bei aelteren Daten wird auf die Surplus-Strategie zurueckgefallen.

---

## Publizierte Entities

### Steuerungs-Entities

| Entity | Beschreibung |
|--------|-------------|
| `sensor.energieha_battery_mode` | Aktueller Batteriemodus: charge/discharge/idle |
| `sensor.energieha_grid_setpoint` | Geplanter Netzfluss (W): positiv=Import |

### Informations-Entities

| Entity | Beschreibung |
|--------|-------------|
| `sensor.energieha_status` | Gesamtstatus mit Strategie, Fehler, Version |
| `sensor.energieha_battery_plan` | 24h-Plan als JSON mit pro-Slot-Daten |
| `sensor.energieha_planned_soc` | SOC-Projektion (Ziel-SOC am Ende) |
| `sensor.energieha_savings` | Geschaetzte Ersparnis, Eigenverbrauch, Netzimport |

### Plan-Timeline Felder (pro Slot)

| Feld | Beschreibung |
|------|-------------|
| `t` | Uhrzeit (HH:MM) |
| `mode` | charge / discharge / idle |
| `soc` | Projizierter SOC (%) |
| `batt` | Batterie-Leistung (W): positiv=Laden |
| `gridload` | Netzladung der Batterie (W): >0 wenn aus Netz geladen |
| `pv` | PV-Prognose (W) |
| `load` | Hausverbrauch-Schaetzung (W) |
| `grid` | Netzleistung (W): positiv=Import |
| `price` | Strompreis (EUR/kWh, Brutto) |
| `cost` | Slot-Kosten (EUR, nur Netzimport) |
| `total` | Kumulierte Kosten (EUR) |

---

## Sicherheitsfunktionen

| Funktion | Beschreibung |
|----------|-------------|
| **SOC Safety Net** | Plan wird nach jeder Strategie geprueft: SOC-Verletzungen werden geclippt |
| **Config-Validierung** | Beim Start: min_soc < max_soc, grid_charge_soc plausibel |
| **Circuit-Breaker** | Nach 3 Fehlern hintereinander: Safe Mode (nur idle, keine TOU) |
| **Hysterese** | Modus wechselt nicht schneller als `mode_hold_seconds` (Default 120s) |
| **Dry-Run** | Testmodus: Entities werden publiziert aber WR nicht gesteuert |
| **EMHASS Freshness** | Fallback auf surplus wenn EMHASS-Daten aelter als 6 Stunden |
| **Entity-Validierung** | Warnung beim Start wenn konfigurierte Sensoren nicht existieren |

---

## Fehlerbehebung

### Add-on startet, aber keine Entities

1. Pruefe die Add-on Logs (Einstellungen → Add-ons → EnergieHA → Log)
2. `sensor.energieha_status` sollte nach ~30s erscheinen
3. Falls "error" im Status: Attribut `error` zeigt die Ursache

### Strategy zeigt "surplus" statt "emhass"

- EMHASS Add-on muss **gestartet** sein
- Pruefe `strategy_error` Attribut in `sensor.energieha_status`
- Haeufige Ursachen: EMHASS nicht erreichbar, Daten zu alt, Validierung fehlgeschlagen

### TOU-Programme werden nicht geschrieben

- `sungrow_tou_enabled: true` in der Config?
- `dry_run: false`?
- Sungrow-Entities muessen existieren (`select.inverter_program_1_charging` etc.)

### Batterie laedt unerwartet aus dem Netz

- Pruefe ob P2 auf "Grid" steht (sollte nur bei guenstigen Preisen passieren)
- `max_grid_charge_soc` begrenzt Netzladung (Default 80%)
- Bei PV-Laden: P2 = "Disabled" + SOC-Ziel

---

## Dashboard

Das EnergieHA Dashboard (`/energie-ha/`) hat 4 Reiter:

1. **Uebersicht**: Status, Live-Werte, PV-Prognose, Kosten, TOU, EMHASS
2. **Grafiken**: Energiefluss, EPEX-Preise, Batterie/PV/Last Forecasts (ApexCharts)
3. **Planungstabelle**: Predbat-aehnliche Tabelle mit Stundenwerten (Modus, SOC, Leistung, Kosten)
4. **Einstellungen**: WR-Modus, Batterie-Grenzen, EMHASS-Steuerung, TOU manuell
