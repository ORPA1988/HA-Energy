# HA Energy Optimizer

> **Intelligentes Home-Energy-Management-System zur Optimierung des Stromverbrauchs**

[![Version](https://img.shields.io/badge/Version-0.2.2-blue)](https://github.com/ORPA1988/HA-Energy)
[![Plattform](https://img.shields.io/badge/Plattform-Home%20Assistant-41BDF5)](https://www.home-assistant.io/)
[![Architektur](https://img.shields.io/badge/Arch-amd64%20%7C%20aarch64%20%7C%20armv7%20%7C%20armhf-green)](#installation)
[![Lizenz](https://img.shields.io/badge/Lizenz-MIT-yellow)](LICENSE)

---

> Intelligentes Home-Energy-Management-System (HEMS) als Home Assistant Add-on.
> Kombiniert die besten Konzepte aus EVCC, EOS und EMHASS in einer einheitlichen Plattform mit dreistufiger Optimierung und Live-Dashboard.

**Version 0.2.2** · [Changelog](#changelog) · [Installation](#installation) · [Konfiguration](#konfiguration)

---

## Inhaltsverzeichnis

- [Features](#features)
- [Systemarchitektur](#systemarchitektur)
- [Voraussetzungen](#voraussetzungen)
- [Installation](#installation)
- [Konfiguration](#konfiguration)
  - [PV-Anlage](#pv-anlage)
  - [Hausbatterie](#hausbatterie)
  - [Batterie-Balancing](#batterie-balancing)
  - [Netz](#netz)
  - [Strompreise](#strompreise)
  - [go-e Wallbox](#go-e-wallbox)
  - [E-Auto / EV-Laden](#e-auto--ev-laden)
  - [Ladefenster](#ladefenster)
  - [Steuerbare Verbraucher](#steuerbare-verbraucher)
  - [Betriebsmodus](#betriebsmodus)
  - [Optimierung](#optimierung)
  - [Benachrichtigungen](#benachrichtigungen)
- [Dashboard](#dashboard)
- [Optimierungsstrategie](#optimierungsstrategie)
- [Strompreisquellen](#strompreisquellen)
- [Hardware-Integrationen](#hardware-integrationen)
- [API-Endpunkte](#api-endpunkte)
- [Read-Only-Modus](#read-only-modus)
- [MCP-Server (KI-Integration)](#mcp-server-ki-integration)
- [Fehlerbehebung](#fehlerbehebung)
- [Performance-Tipps (Raspberry Pi)](#performance-tipps-raspberry-pi)
- [Entwicklung und Mitarbeit](#entwicklung-und-mitarbeit)
- [Changelog](#changelog)
- [Lizenz](#lizenz)

---

## Features

- **PV-Eigenverbrauchsmaximierung** – Intelligente Steuerung von Lasten und Batterie, um möglichst viel Solarstrom selbst zu nutzen
- **Batterie-Management** – Optimale Lade-/Entladeplanung mit SOC-Überwachung und automatischem Balancing
- **EV-Ladeoptimierung** – EVCC-ähnliche Echtzeit-Stromregelung für Wallboxen mit Smart-, PV- und Schnelllade-Modus
- **Dreistufige Optimierung** – Realtime (30 s), stündlich (LP-Solver) und 48-h-Planung (genetischer Algorithmus)
- **Dynamische Strompreise** – ENTSO-E, Tibber, aWATTar, EPEX Spot, HA-Sensor oder Festpreis
- **PV-Prognose** – Via Open-Meteo (kostenlos) oder Solcast (genauer)
- **Batterie-Balancing** – Automatische oder geplante Volllade-Zyklen zur Kalibrierung
- **Multi-Wallbox-Support** – go-e (lokal/Cloud), HA-Entity, OCPP
- **Live-Dashboard** – WebSocket-basiert mit Chart.js-Visualisierung
- **Read-Only-Testmodus** – Sicheres Ausprobieren ohne aktive Gerätesteuerung
- **MCP-Server** – 17 Tools für KI-gestützte Konfiguration (Claude Code, Cursor etc.)
- **Steuerbare Verbraucher** – Waschmaschine, Spülmaschine u.a. in günstige Zeitfenster verschieben

---

## Systemarchitektur

```
┌─────────────────────────────────────────────────────┐
│                  HA Energy Optimizer                 │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐ │
│  │  Daten   │  │ Optimizer│  │     Geräte        │ │
│  │          │  │          │  │                   │ │
│  │collector │  │ realtime │  │ goe.py            │ │
│  │prices    │  │ linear   │  │ wallbox.py        │ │
│  │forecast  │  │ genetic  │  │ battery_balancer  │ │
│  │load_dec. │  │ ev_strat.│  │                   │ │
│  │          │  │ coord.   │  │                   │ │
│  └────┬─────┘  └────┬─────┘  └────────┬──────────┘ │
│       │              │                 │            │
│  ┌────┴──────────────┴─────────────────┴──────────┐ │
│  │              ha_client.py (REST API)            │ │
│  └────────────────────┬───────────────────────────┘ │
│                       │                             │
│  ┌────────────────────┴───────────────────────────┐ │
│  │         scheduler.py + main.py                 │ │
│  └────────────────────┬───────────────────────────┘ │
│                       │                             │
│  ┌──────────┐  ┌──────┴─────┐  ┌────────────────┐  │
│  │Dashboard │  │  REST API  │  │  MCP-Server     │  │
│  │(Web-GUI) │  │  :8080     │  │  (KI-Tools)     │  │
│  └──────────┘  └────────────┘  └────────────────┘  │
└─────────────────────────────────────────────────────┘
           │
    ┌──────┴──────┐
    │Home Assistant│
    │   REST API   │
    └─────────────┘
```

### Modul-Übersicht

| Modul | Datei | Beschreibung |
|-------|-------|-------------|
| **Daten-Collector** | `data/collector.py` | Sammelt HA-Sensorwerte alle 30 Sekunden |
| **Strompreise** | `data/prices.py` | 48-h-Preisprognose aus 7 verschiedenen Quellen |
| **PV-Prognose** | `data/forecast.py` | PV-Erzeugungsprognose via Open-Meteo oder Solcast |
| **Lastzerlegung** | `data/load_decomposition.py` | Trennt Grundlast von steuerbaren Verbrauchern |
| **Realtime-Controller** | `optimizer/realtime.py` | Wallbox-Stromregelung alle 30 Sekunden |
| **LP-Solver** | `optimizer/linear.py` | Stündliche Kostenminimierung (24 h) |
| **Genetischer Planer** | `optimizer/genetic.py` | 48-h-Strategieplanung |
| **EV-Strategie** | `optimizer/ev_strategy.py` | Lademodus-Logik für Elektrofahrzeuge |
| **Coordinator** | `optimizer/coordinator.py` | Orchestriert alle Optimierungsebenen |
| **EMHASS-Backend** | `optimizer/emhass_backend.py` | Optionaler EMHASS-Optimizer als Drop-in |
| **go-e Integration** | `devices/goe.py` | go-e Wallbox (lokal + Cloud API) |
| **Wallbox-Abstraktion** | `devices/wallbox.py` | Einheitliche Schnittstelle für alle Wallbox-Typen |
| **Batterie-Balancer** | `devices/battery_balancer.py` | Volllade-Zyklen zur Batterie-Kalibrierung |
| **HA-Client** | `app/ha_client.py` | Home Assistant REST API Client |
| **MCP-Server** | `app/mcp_server.py` | KI-Integrations-Schnittstelle (17 Tools) |
| **Scheduler** | `app/scheduler.py` | Zeitplanung aller Optimierungs-Zyklen |
| **Dashboard** | `app/static/index.html` | Live-Web-Dashboard mit Chart.js |

---

## Voraussetzungen

### Hardware

- **Minimum:** Raspberry Pi 4 mit 4 GB RAM
- **Empfohlen:** Raspberry Pi 4 mit 8 GB RAM oder Intel/AMD x64-System
- **Architekturen:** amd64, aarch64, armv7, armhf

### Software

- Home Assistant OS oder Supervised (Version 2024.1 oder neuer)
- Home Assistant Supervisor
- Konfigurierte HA-Entitäten für:
  - PV-Erzeugung (Sensor, z. B. `sensor.inverter_pv_power`)
  - Batterie-SOC und -Leistung
  - Netz-Leistung (Import/Export)
  - Optional: EV-Batteriestand, Wallbox-Entitäten

### Getestete Plattformen

- Raspberry Pi 4 (4 GB / 8 GB) mit Home Assistant OS
- Intel NUC / x86-Mini-PC mit Home Assistant Supervised
- Deye/Sunsynk Hybrid-Wechselrichter
- go-eCharger HOME+ (lokal und Cloud)

---

## Installation

### Methode 1: Über den Add-on-Store (empfohlen)

1. Öffne Home Assistant → **Einstellungen** → **Add-ons** → **Add-on-Store**
2. Klicke auf das **⋮-Menü** (oben rechts) → **Repositories**
3. Füge folgende URL hinzu:
   ```
   https://github.com/ORPA1988/HA-Energy
   ```
4. Klicke auf **Hinzufügen** und schließe den Dialog
5. Suche nach **HA Energy Optimizer** und klicke auf **Installieren**
6. Nach der Installation: Tab **Konfiguration** öffnen und Werte anpassen
7. Add-on starten

### Methode 2: Manuelle Installation

1. Repository klonen:
   ```bash
   cd /addons
   git clone https://github.com/ORPA1988/HA-Energy.git
   ```
2. Home Assistant → **Einstellungen** → **Add-ons** → **Add-on-Store** → Aktualisieren
3. **HA Energy Optimizer** erscheint unter „Lokale Add-ons"

### Sichere Standardwerte

Das Add-on startet mit sicheren Defaults:
- `read_only: true` – Keine aktive Gerätesteuerung
- `operation_mode: stopped` – Optimierung läuft nicht automatisch

So kannst du alles in Ruhe konfigurieren und testen, bevor du das System scharf schaltest.

---

## Konfiguration

Die Konfiguration erfolgt über den Tab **Konfiguration** im Add-on oder als YAML. Nachfolgend alle Sektionen mit Erklärungen.

### PV-Anlage

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `pv_power_sensor` | string | `""` | HA-Entität für aktuelle PV-Leistung (W) |
| `pv_forecast_kwp` | float | `10.0` | Installierte PV-Leistung in kWp |
| `pv_orientation` | int | `180` | Azimut in Grad (180 = Süd) |
| `pv_tilt` | int | `30` | Neigungswinkel in Grad |
| `pv_latitude` | float | `48.21` | Breitengrad des Standorts |
| `pv_longitude` | float | `16.37` | Längengrad des Standorts |
| `pv_efficiency` | float | `0.18` | Modulwirkungsgrad (0.15–0.22 typisch) |
| `pv_forecast_source` | string | `"auto"` | Prognosequelle: `auto`, `solcast`, `openmeteo` |
| `solcast_entity` | string | `""` | HA-Entität für Solcast-Prognose |
| `solcast_estimate_type` | string | `"pv_estimate"` | Solcast-Schätztyp |

**Beispiel:**
```yaml
pv_power_sensor: "sensor.inverter_pv_power"
pv_forecast_kwp: 10.0
pv_orientation: 180
pv_tilt: 30
pv_latitude: 48.229195
pv_longitude: 13.827813
pv_efficiency: 0.18
pv_forecast_source: "solcast"
solcast_entity: "sensor.solcast_pv_forecast_prognose_aktuelle_stunde"
```

### Hausbatterie

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `battery_soc_sensor` | string | `""` | SOC-Sensor (Ladestand in %) |
| `battery_power_sensor` | string | `""` | Batterie-Leistungssensor (W) |
| `battery_capacity_kwh` | float | `10.0` | Speicherkapazität in kWh |
| `battery_charge_switch` | string | `""` | Switch zum Aktivieren der Netzladung |
| `battery_discharge_switch` | string | `""` | Switch zum Aktivieren der Entladung |
| `battery_max_charge_w` | int | `5000` | Maximale Ladeleistung in W |
| `battery_max_discharge_w` | int | `5000` | Maximale Entladeleistung in W |
| `battery_min_soc` | int | `10` | Minimaler SOC in % (Tiefentladeschutz) |
| `battery_reserve_soc` | int | `15` | Reserve-SOC in % für Notfälle |
| `battery_efficiency` | float | `0.95` | Lade-/Entladewirkungsgrad |

**Beispiel (Deye/Sunsynk 25.5 kWh):**
```yaml
battery_soc_sensor: "sensor.inverter_battery"
battery_power_sensor: "sensor.inverter_battery_power"
battery_capacity_kwh: 25.5
battery_charge_switch: "switch.inverter_battery_grid_charging"
battery_discharge_switch: "switch.inverter"
battery_max_charge_w: 6000
battery_max_discharge_w: 6000
battery_min_soc: 10
battery_reserve_soc: 15
battery_efficiency: 0.95
```

### Batterie-Balancing

Regelmäßige Volllade-Zyklen kalibrieren den SOC-Sensor und verlängern die Batterielebensdauer.

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `battery_balancing_enabled` | bool | `false` | Balancing aktivieren |
| `battery_balancing_mode` | string | `"auto"` | `auto` oder `scheduled` |
| `battery_balancing_frequency` | string | `"monthly"` | `weekly`, `monthly`, `custom` |
| `battery_balancing_custom_days` | int | `30` | Intervall in Tagen (bei `custom`) |
| `battery_balancing_target_soc` | int | `100` | Ziel-SOC für Balancing |
| `battery_balancing_hold_duration_h` | int | `2` | Haltezeit bei 100 % in Stunden |
| `battery_balancing_preferred_time` | string | `"10:00"` | Bevorzugte Startzeit |
| `battery_balancing_auto_trigger_soc_deviation` | int | `5` | SOC-Abweichung für Auto-Trigger (%) |
| `battery_balancing_use_solar_only` | bool | `true` | Nur mit Solarstrom balancen |

**Beispiel:**
```yaml
battery_balancing_enabled: true
battery_balancing_mode: "auto"
battery_balancing_frequency: "monthly"
battery_balancing_target_soc: 100
battery_balancing_hold_duration_h: 2
battery_balancing_preferred_time: "10:00"
battery_balancing_use_solar_only: true
```

### Netz

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `grid_power_sensor` | string | `""` | Netzleistungssensor (W, positiv = Import) |
| `grid_max_import_w` | int | `0` | Max. Netzbezug in W (0 = unbegrenzt) |
| `total_power_sensor` | string | `""` | Optional: Gesamtverbrauchssensor |

### Strompreise

Das System unterstützt 7 verschiedene Preisquellen. Die Konfiguration hängt von der gewählten Quelle ab.

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `price_source` | string | `"fixed"` | Preisquelle (siehe unten) |
| `fixed_price_ct_kwh` | float | `25.0` | Festpreis in ct/kWh (Fallback) |

**Preisquellen:**

| Quelle | `price_source`-Wert | Zusätzliche Felder | Authentifizierung |
|--------|---------------------|-------------------|-------------------|
| ENTSO-E | `"entso-e"` | `entso_e_token`, `entso_e_area` | API-Token nötig |
| Tibber | `"tibber"` | `tibber_token` | API-Token nötig |
| aWATTar | `"awattar"` | `awattar_country` (`AT`/`DE`) | Keine (frei) |
| EPEX Spot (SMARD) | `"epex_spot"` | `epex_spot_area` | Keine (frei) |
| EPEX HA-Entity | `"epex_entity"` | `epex_import_entity`, `epex_unit` | HA-Integration |
| HA-Sensor | `"sensor"` | `price_sensor_entity` | HA-Integration |
| Festpreis | `"fixed"` | `fixed_price_ct_kwh` | Keine |

#### Preisberechnung (Netto/Brutto)

Für österreichische und deutsche Nutzer gibt es eine integrierte Preisberechnung:

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `price_input_is_netto` | bool | `true` | Sind die Eingabepreise netto? |
| `price_vat_percent` | float | `20.0` | Mehrwertsteuersatz in % |
| `price_grid_fee_source` | string | `"fixed"` | `fixed` oder `entity` |
| `price_grid_fee_fixed_ct_kwh` | float | `7.6` | Netzentgelt in ct/kWh |
| `price_grid_fee_entity` | string | `""` | HA-Entität für dynamisches Netzentgelt |
| `price_supplier_markup_ct_kwh` | float | `1.2` | Aufschlag des Versorgers in ct/kWh |
| `price_other_taxes_ct_kwh` | float | `0.0` | Sonstige Abgaben in ct/kWh |
| `price_feed_in_ct_kwh` | float | `8.0` | Einspeisevergütung in ct/kWh |

**Formel:**
```
Netto     = Marktpreis (ct/kWh)
Brutto    = Netto × (1 + MwSt/100)
Gesamt    = Brutto + Netzentgelt + Versorger-Aufschlag + sonstige Abgaben
```

**Beispiel (Österreich, EPEX Spot):**
```yaml
price_source: "epex_entity"
epex_import_entity: "sensor.epex_spot_data_market_price"
epex_unit: "EUR/kWh"
price_input_is_netto: true
price_vat_percent: 20.0
price_grid_fee_fixed_ct_kwh: 7.6
price_supplier_markup_ct_kwh: 1.2
price_feed_in_ct_kwh: 8.0
```

### go-e Wallbox

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `goe_enabled` | bool | `false` | go-e Integration aktivieren |
| `goe_connection_type` | string | `"local"` | `local` oder `cloud` |
| `goe_local_ip` | string | `""` | IP-Adresse der Wallbox (bei `local`) |
| `goe_cloud_serial` | string | `""` | Seriennummer (bei `cloud`) |
| `goe_cloud_token` | string | `""` | API-Token (bei `cloud`) |
| `goe_max_current_a` | int | `16` | Max. Ladestrom in Ampere |
| `goe_phases` | int | `3` | Anzahl der Phasen (1 oder 3) |

**Beispiel (lokal):**
```yaml
goe_enabled: true
goe_connection_type: "local"
goe_local_ip: "192.168.0.91"
goe_max_current_a: 16
goe_phases: 1
```

### E-Auto / EV-Laden

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `ev_soc_sensor` | string | `""` | SOC-Sensor des E-Autos (%) |
| `ev_battery_capacity_kwh` | float | `60.0` | Akkukapazität des E-Autos in kWh |
| `ev_charge_mode` | string | `"smart"` | Lademodus: `smart`, `pv_only`, `fast`, `off` |
| `ev_min_charge_current_a` | int | `6` | Min. Ladestrom in Ampere |
| `ev_max_charge_current_a` | int | `16` | Max. Ladestrom in Ampere |
| `ev_allow_battery_to_charge_ev` | bool | `true` | Hausbatterie darf EV laden |
| `ev_allow_grid_to_charge_ev` | bool | `true` | Netzstrom darf EV laden |
| `ev_combined_charge_threshold_ct` | float | `15.0` | Preisgrenze für Netz+PV-Laden (ct/kWh) |
| `ev_surplus_start_threshold_w` | int | `1400` | PV-Überschuss zum Starten (W) |
| `ev_surplus_stop_threshold_w` | int | `1000` | PV-Überschuss zum Stoppen (W) |

**Lademodi erklärt:**

| Modus | Verhalten |
|-------|-----------|
| `smart` | Kombiniert PV-Überschuss + günstige Netzpreise, um rechtzeitig voll zu werden |
| `pv_only` | Lädt ausschließlich mit PV-Überschuss |
| `fast` | Lädt sofort mit maximalem Strom |
| `off` | Laden deaktiviert |

### Ladefenster

Definiere Zeitfenster, in denen das E-Auto geladen werden soll:

```yaml
ev_charging_windows:
  - name: "Nacht"
    available_from: "22:00"
    available_until: "07:00"
    target_soc_percent: 80
    must_finish_by: "07:00"
    priority: "cost"          # "cost" oder "speed"
```

Mehrere Fenster sind möglich, z. B. ein Nacht-Fenster und ein Mittags-PV-Fenster.

### Steuerbare Verbraucher

Verschiebe energieintensive Geräte in günstige Zeitfenster:

```yaml
deferrable_loads:
  - name: "Waschmaschine"
    power_w: 2000
    duration_h: 2
    earliest_start: "06:00"
    latest_end: "22:00"
    max_cost_ct_kwh: 20.0
  - name: "Spülmaschine"
    power_w: 1800
    duration_h: 1.5
    earliest_start: "12:00"
    latest_end: "06:00"
    max_cost_ct_kwh: 18.0
```

### Betriebsmodus

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `read_only` | bool | `true` | `true` = nur lesen, keine Steuerung |
| `operation_mode` | string | `"stopped"` | `stopped`, `monitoring`, `optimizing` |

**Modi erklärt:**

| Modus | Verhalten |
|-------|-----------|
| `stopped` | Alles aus, kein Monitoring |
| `monitoring` | Daten werden gesammelt und angezeigt, keine Steuerung |
| `optimizing` | Volle Optimierung mit aktiver Gerätesteuerung |

### Optimierung

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `optimizer_backend` | string | `"builtin"` | `builtin` oder `emhass` |
| `optimization_goal` | string | `"cost"` | `cost` (Kosten) oder `self_consumption` (Eigenverbrauch) |
| `optimization_interval_minutes` | int | `60` | LP-Solver-Intervall in Minuten |
| `long_term_plan_interval_hours` | int | `6` | Genetischer Planer Intervall in Stunden |
| `peak_shaving_limit_w` | int | `0` | Lastspitzenbegrenzung in W (0 = deaktiviert) |

### Benachrichtigungen

| Parameter | Typ | Default | Beschreibung |
|-----------|-----|---------|-------------|
| `notify_target` | string | `"notify.mobile_app"` | HA-Benachrichtigungsziel |
| `notify_on_balancing` | bool | `true` | Benachrichtigung bei Batterie-Balancing |
| `notify_on_cheap_window` | bool | `true` | Benachrichtigung bei günstigen Preisfenstern |
| `notify_on_ev_charged` | bool | `true` | Benachrichtigung wenn E-Auto fertig geladen |

---

## Dashboard

Das Add-on bietet ein Live-Dashboard unter **Port 8080**, erreichbar über die Sidebar in Home Assistant.

Das Dashboard zeigt:
- Aktuelle PV-Erzeugung, Batterie-SOC und Netzleistung in Echtzeit
- 48-h-Strompreisverlauf mit farbcodierten günstigen/teuren Zeitfenstern
- PV-Prognose für die nächsten 48 Stunden
- Wallbox-Status und Ladestrom aller angeschlossenen EV-Ladepunkte
- Optimierungsentscheidungen und geplante Aktionen
- Lastzerlegung: Grundlast vs. steuerbare Verbraucher

Die Daten werden per WebSocket in Echtzeit aktualisiert (kein manuelles Neuladen nötig).

---

## Optimierungsstrategie

Das System nutzt drei Optimierungsebenen, die zusammenarbeiten:

### Ebene 1: Realtime-Controller (alle 30 Sekunden)

- Regelt den Wallbox-Ladestrom basierend auf aktuellem PV-Überschuss
- Reagiert auf schnelle Änderungen (Wolken, Verbraucher schalten sich ein/aus)
- Hält die Netz-Einspeisung und den Netzbezug minimal

### Ebene 2: LP-Solver (stündlich)

- Lineare Programmierung für die nächsten 24 Stunden
- Minimiert Stromkosten unter Berücksichtigung aller Constraints
- Entscheidet: Batterie laden/entladen, EV laden, Verbraucher starten/verschieben
- Berücksichtigt Strompreise, PV-Prognose und Verbrauchsmuster

### Ebene 3: Genetischer Planer (alle 6 Stunden)

- 48-h-Strategieplanung mit genetischem Algorithmus
- Findet optimale Zeitpunkte für Batterie-Zyklen und EV-Laden
- Berücksichtigt Unsicherheiten in PV-Prognose und Preisen
- Gibt strategische Vorgaben an LP-Solver und Realtime-Controller

### Coordinator

Der Coordinator (`optimizer/coordinator.py`) orchestriert alle drei Ebenen und löst Konflikte zwischen den Empfehlungen auf.

---

## Strompreisquellen

### ENTSO-E Transparency Platform
- Europaweite Day-Ahead-Preise
- Benötigt kostenlosen API-Token von [transparency.entsoe.eu](https://transparency.entsoe.eu/)
- Preise in EUR/MWh, werden automatisch in ct/kWh umgerechnet

### Tibber
- Benötigt Tibber-Account und API-Token
- GraphQL-API liefert Preise in EUR/kWh
- Unterstützt heute + morgen Preise

### aWATTar
- Kostenlos, keine Registrierung nötig
- Verfügbar für Österreich und Deutschland
- Preise in EUR/MWh

### EPEX Spot (über SMARD.de)
- Kostenlos, keine Registrierung nötig
- Unterstützt DE-LU, AT, CH
- Viertelstündliche Auflösung, wird zu Stundenwerten aggregiert

### EPEX HA-Entity
- Liest Preise direkt aus einer HA-Integration (z. B. EPEX Spot Integration, Nordpool)
- Unterstützt verschiedene Attribut-Formate: `data`, `today`/`tomorrow`, `raw_today`/`raw_tomorrow`
- Automatische Einheitenkonvertierung (EUR/MWh, EUR/kWh, ct/kWh)

### HA-Sensor
- Beliebiger HA-Sensor als Preisquelle
- Kein Forecast, aktueller Preis wird für 48 h angenommen

### Festpreis
- Statischer Preis als Fallback
- Wird auch bei Fehlern der anderen Quellen automatisch verwendet

---

## Hardware-Integrationen

### go-e Wallbox

Unterstützt den go-eCharger HOME+ über zwei Verbindungsmodi:

- **Lokal:** Direkte HTTP-Kommunikation im LAN (schneller, zuverlässiger)
- **Cloud:** Über die go-e Cloud-API (Serial + Token nötig)

Funktionen: Ladestrom setzen, Laden starten/stoppen, Status abfragen, Phasenumschaltung.

### HA-Entity-Wallbox

Jede Wallbox, die über Home Assistant steuerbar ist, kann eingebunden werden. Konfiguriere die entsprechenden HA-Entitäten für Stromstärke, Laden ein/aus etc.

### OCPP-Wallbox

Wallboxen mit OCPP-Protokoll können über die HA-OCPP-Integration angebunden werden.

### Wechselrichter / Batterie

Das System steuert die Hausbatterie über HA-Switches:
- `battery_charge_switch` – Netzladung aktivieren/deaktivieren
- `battery_discharge_switch` – Entladung aktivieren/deaktivieren

Getestet mit Deye/Sunsynk Hybrid-Wechselrichtern, funktioniert aber mit jedem System, das über HA-Entitäten steuerbar ist.

---

## API-Endpunkte

Das Add-on stellt eine REST API auf Port 8080 bereit:

| Endpunkt | Methode | Beschreibung |
|----------|---------|-------------|
| `/api/status` | GET | Aktueller Systemstatus |
| `/api/prices` | GET | 48-h-Strompreise |
| `/api/forecast` | GET | PV-Prognose |
| `/api/battery` | GET | Batterie-Status |
| `/api/ev/mode` | POST | EV-Lademodus setzen |
| `/api/config` | GET/POST | Konfiguration lesen/schreiben |
| `/api/optimizer` | GET | Optimierungsstatus und -plan |

---

## Read-Only-Modus

Mit `read_only: true` läuft das gesamte System, ohne aktiv Geräte zu steuern:

- Alle Daten werden gesammelt und angezeigt
- Alle Optimierungsberechnungen laufen durch
- Dashboard zeigt, was das System tun *würde*
- Kein Switch, kein Strom, kein Gerät wird tatsächlich gesteuert

So kannst du das System tagelang beobachten und prüfen, ob die Entscheidungen sinnvoll sind, bevor du `read_only: false` setzt.

---

## MCP-Server (KI-Integration)

Das Add-on enthält einen MCP-Server (Model Context Protocol) mit 17 Tools, der KI-Assistenten wie Claude Code oder Cursor direkten Zugriff auf das System gibt.

Funktionen über MCP:
- Konfiguration lesen und ändern
- Systemstatus und Sensorwerte abfragen
- Strompreise und PV-Prognose abrufen
- Optimierungsentscheidungen nachvollziehen
- Fehlerdiagnose und Log-Analyse

Der MCP-Server wird automatisch mit dem Add-on gestartet.

---

## Fehlerbehebung

### Add-on startet nicht
- Prüfe die Logs: **Einstellungen** → **Add-ons** → **HA Energy Optimizer** → Tab **Protokoll**
- Stelle sicher, dass Home Assistant Version ≥ 2024.1 ist
- Prüfe, ob die konfigurierten Entitäten existieren

### Keine Strompreise
- Überprüfe die `price_source`-Konfiguration
- Bei ENTSO-E/Tibber: Token gültig?
- Bei EPEX-Entity: Existiert die Entität? Prüfe mit **Entwicklerwerkzeuge** → **Zustände**
- Das System fällt automatisch auf den Festpreis zurück

### Wallbox reagiert nicht
- Prüfe `goe_enabled: true`
- Bei lokaler Verbindung: Ist die IP erreichbar? (`ping 192.168.0.91`)
- Prüfe, ob `read_only: false` gesetzt ist
- Prüfe, ob `operation_mode: "optimizing"` gesetzt ist

### Dashboard zeigt keine Daten
- Warte 1–2 Minuten nach dem Start (Daten-Collector braucht erste Werte)
- Prüfe, ob die Sensor-Entitäten korrekte Werte liefern
- Browser-Konsole auf Fehler prüfen (F12)

### Batterie-Balancing startet nicht
- Prüfe, ob `battery_balancing_enabled: true` gesetzt ist
- Bei `use_solar_only: true`: Genug PV-Leistung vorhanden?
- Prüfe den Balancing-Zeitplan im Dashboard

---

## Performance-Tipps (Raspberry Pi)

- `optimization_interval_minutes: 60` (nicht niedriger als 30 setzen)
- `long_term_plan_interval_hours: 6` (Standard beibehalten)
- Genetischen Algorithmus nicht gleichzeitig mit anderen rechenintensiven Add-ons laufen lassen
- Bei Speicherproblemen: Swap-Partition auf USB-SSD auslagern

---

## Entwicklung und Mitarbeit

### Projektstruktur

```
HA-Energy/
├── ha-energy-optimizer/
│   ├── app/
│   │   ├── data/
│   │   │   ├── __init__.py
│   │   │   ├── collector.py
│   │   │   ├── forecast.py
│   │   │   ├── load_decomposition.py
│   │   │   └── prices.py
│   │   ├── devices/
│   │   │   ├── __init__.py
│   │   │   ├── battery_balancer.py
│   │   │   ├── goe.py
│   │   │   └── wallbox.py
│   │   ├── optimizer/
│   │   │   ├── __init__.py
│   │   │   ├── coordinator.py
│   │   │   ├── emhass_backend.py
│   │   │   ├── ev_strategy.py
│   │   │   ├── genetic.py
│   │   │   ├── linear.py
│   │   │   └── realtime.py
│   │   ├── static/
│   │   │   └── index.html
│   │   ├── config.py
│   │   ├── ha_client.py
│   │   ├── main.py
│   │   ├── mcp_server.py
│   │   ├── models.py
│   │   ├── requirements.txt
│   │   └── scheduler.py
│   ├── rootfs/
│   │   └── etc/services.d/energy-optimizer/
│   ├── translations/
│   ├── .dockerignore
│   ├── build.yaml
│   ├── CHANGELOG.md
│   ├── config.yaml
│   ├── Dockerfile
│   ├── icon.png
│   └── logo.png
├── repository.yaml
└── README.md
```

### Mitmachen

1. Repository forken
2. Feature-Branch erstellen: `git checkout -b feature/mein-feature`
3. Änderungen committen: `git commit -m "Add: Beschreibung"`
4. Branch pushen: `git push origin feature/mein-feature`
5. Pull Request erstellen

---

## Changelog

### 0.2.0

- **Versionsbereinigung:** Konsistente Version 0.2.0 in allen Dateien
- **Bug-Fix Batterie-Balancing:** Hold-Timer wurde ab Ladebeginn statt ab Hold-Beginn gemessen – Halten konnte vorzeitig enden
- **Bug-Fix EV-SOC-Warnung:** Warnung über fehlenden EV-SOC-Sensor erschien alle 30 s – jetzt nur einmalig bis Sensor wieder verfügbar
- **Bug-Fix Config-Update:** App-State wurde nach API-Config-Update nicht aktualisiert – Änderungen erst nach Neustart wirksam
- **Bug-Fix EV-Modus-Validierung:** `/api/ev/mode` akzeptierte ungültige Moduswerte ohne Fehlermeldung
- **Bug-Fix Open-Meteo Timeout:** Timeout von 5 s auf 15 s erhöht – verhindert Fehlschläge auf Raspberry Pi mit langsamer Verbindung
- **Code-Bereinigung:** Unbenutzte Variablen in ENTSO-E-Preisparser entfernt

### 0.1.0

- Auto-Erkennung von HA-Entitäten mit Confidence-Bewertung
- Bedingte Felder in der Konfiguration
- Dedizierte Sektionen für alle Preisquellen
- PV-Prognose-Source-Auswahl (Auto/Solcast/Open-Meteo)
- Vollständige Batterie-Balancing-UI
- Benachrichtigungs-Konfiguration
- go-e Cloud-Verbindungsfelder
- Wallbox-Sichtbarkeit abhängig von Aktivierung

### 0.0.3

- Read-Only-Modus für sicheres Testen
- MCP-Server mit 17 Tools
- 6 kritische Bug-Fixes
- Multi-EV Dashboard
- Lastzerlegung im Dashboard
- Config-Validierung mit Fehlern/Warnungen
- Rotierende Logdatei

### 0.0.2

- EMHASS-Backend als optionaler Drop-in-Optimizer
- Multi-EV-Support (go-e, HA Entity, OCPP)
- Wallbox-Abstraktion
- Lastzerlegung

### 0.0.1

- Erstveröffentlichung
- Dreistufige Optimierung (Realtime, LP, Genetisch)
- Live-Dashboard mit WebSocket
- PV-Prognose via Open-Meteo
- 7 Strompreisquellen
- go-e Wallbox Integration
- Batterie-Balancing
- Steuerbare Verbraucher
- Web-GUI Konfiguration

---

## Lizenz

Dieses Projekt ist unter der MIT-Lizenz veröffentlicht. Details siehe [LICENSE](LICENSE).

---

> **Hinweis:** Dieses Projekt befindet sich in aktiver Entwicklung. Feedback und Beiträge sind willkommen!
