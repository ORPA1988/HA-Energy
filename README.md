# HA Energy Optimizer

> **Intelligentes Home-Energy-Management-System zur Optimierung des Stromverbrauchs**

[![Version](https://img.shields.io/badge/Version-0.1.1-blue)](https://github.com/ORPA1988/HA-Energy)
[![Plattform](https://img.shields.io/badge/Plattform-Home%20Assistant-41BDF5)](https://www.home-assistant.io/)
[![Architektur](https://img.shields.io/badge/Arch-amd64%20%7C%20aarch64%20%7C%20armv7%20%7C%20armhf-green)](#installation)
[![Lizenz](https://img.shields.io/badge/Lizenz-MIT-yellow)](LICENSE)

---

## Schnellstart

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FORPA1988%2FHA-Energy)

**Oder manuell:**
1. Home Assistant -> **Einstellungen** -> **Add-ons** -> **Add-on Store**
2. Oben rechts: **&#8942;** -> **Repositories** -> URL einfuegen: `https://github.com/ORPA1988/HA-Energy`
3. Seite neu laden -> **HA Energy Optimizer** installieren -> **Starten**
4. Das Dashboard oeffnet sich automatisch im HA-Seitenmenü

> **Tipp:** Beim ersten Start ist `read_only: true` und `operation_mode: stopped` voreingestellt — das Setup kann gefahrlos getestet werden!

---

## Was ist HA Energy Optimizer?

Ein vollstaendiges **Home-Energy-Management-System (HEMS)** als Home Assistant Add-on. Es vereint die besten Konzepte aus [EVCC](https://evcc.io/), [EOS](https://github.com/josepowera/eos) und [EMHASS](https://github.com/davidusb-geek/emhass) in einer einzigen intelligenten Plattform — mit dreistufiger Optimierung, Live-Dashboard und umfassenden Hardware-Integrationen.

---

## Inhaltsverzeichnis

1. [Funktionsuebersicht](#funktionsuebersicht)
2. [Systemarchitektur](#systemarchitektur)
3. [Voraussetzungen](#voraussetzungen)
4. [Installation](#installation)
5. [Konfiguration](#konfiguration)
   - [PV-Anlage](#pv-anlage)
   - [Hausbatterie](#hausbatterie)
   - [Batterie-Balancing](#batterie-balancing)
   - [Stromnetz](#stromnetz)
   - [Strompreise](#strompreise)
   - [go-e Wallbox](#go-e-wallbox)
   - [Elektrofahrzeug (EV)](#elektrofahrzeug-ev)
   - [Steuerbare Lasten](#steuerbare-lasten)
   - [Betriebsmodus](#betriebsmodus)
   - [Optimierung](#optimierung)
   - [Benachrichtigungen](#benachrichtigungen)
6. [Dashboard](#dashboard)
7. [Optimierungsstrategien](#optimierungsstrategien)
8. [Strompreisquellen](#strompreisquellen)
9. [Hardware-Integrationen](#hardware-integrationen)
10. [API-Endpunkte](#api-endpunkte)
11. [Read-Only Modus (Test-Modus)](#read-only-modus-test-modus)
12. [MCP-Server (KI-Integration)](#mcp-server-ki-integration)
13. [Weitere Installationsmethoden](#weitere-installationsmethoden)
14. [Fehlerbehebung](#fehlerbehebung)
15. [Performance-Empfehlungen fuer Raspberry Pi 4](#performance-empfehlungen-fuer-raspberry-pi-4)
16. [Entwicklung & Beitrag](#entwicklung--beitrag)
17. [Changelog](#changelog)

---

## Funktionsuebersicht

| Funktion | Beschreibung |
|---|---|
| **PV-Eigenverbrauch** | Maximiert den Eigenverbrauch der Solaranlage durch intelligente Last- und Batteriesteuerung |
| **Batteriesteuerung** | Optimales Laden/Entladen der Hausbatterie basierend auf Preisen, Prognosen und Verbrauch |
| **EV-Ladeoptimierung** | EVCC-artige Echtzeit-Regelung der Wallbox — solar, guenstig oder schnell |
| **Dreistufige Optimierung** | Realtime (30 s), Stundenbasis (LP-Solver) und 48-h-Planung (genetischer Algorithmus) |
| **Dynamische Strompreise** | ENTSO-E, Tibber, aWATTar, EPEX Spot, EPEX-Entity, HA-Sensor oder Festpreis |
| **PV-Prognose** | 48-h-Ertragsprognose via Open-Meteo (kostenlos) oder Solcast |
| **Batterie-Balancing** | Automatische oder geplante Volllade-Zyklen zur Zellausgleichung |
| **Multi-Wallbox** | Unterstuetzung mehrerer Wallboxen (go-e lokal/Cloud, HA-Entity, OCPP) |
| **EMHASS-Backend** | Optionaler EMHASS-LP-Solver als Alternative zum eingebauten Optimizer |
| **Live-Dashboard** | Echtzeit-Visualisierung aller Energiefluesse, Preise und Zeitplaene via WebSocket |
| **Lastzerlegung** | Visualisierung der Grundlast vs. steuerbare Lasten im Dashboard |
| **Benachrichtigungen** | Push-Nachrichten bei guenstigen Ladezeiten, Balancing-Ereignissen und vollem EV |
| **HA-Integration** | Native Integration mit Ingress-Panel, Supervisor-API und allen HA-Entitaeten |
| **Read-Only Modus** | Sicheres Testen des Setups ohne aktive Schalt- und Regelvorgaenge |
| **MCP-Server** | KI-gestuetzte Konfiguration und Analyse via Claude Code, Cursor oder andere MCP-Clients |

---

## Systemarchitektur

```
+----------------------------------------------------------------+
|                    HA Energy Optimizer                          |
|                                                                |
|  +--------------+  +--------------+  +----------------------+  |
|  | Datenbeschaf-|  | Optimierungs-|  |    Hardware-          |  |
|  |   fung       |  |   Engines    |  |    Integrationen      |  |
|  |              |  |              |  |                       |  |
|  | - collector  |  | - realtime   |  | - go-e Wallbox        |  |
|  |   (HA Sensor-|  |   (30s, EVCC)|  |   (lokal + Cloud)     |  |
|  |    Abfrage)  |  | - linear     |  | - HA-Entity Wallbox   |  |
|  | - prices     |  |   (stuendl.  |  | - OCPP Wallbox        |  |
|  |   (7 Quellen)|  |    LP-Solver)|  | - battery_balancer    |  |
|  | - forecast   |  | - genetic    |  |   (LiFePO4/Blei)     |  |
|  |   (Open-Meteo|  |   (48h Plan) |  +----------------------+  |
|  |    + Solcast) |  | - emhass     |                            |
|  +--------------+  |   (optional) |  +----------------------+  |
|                    | - coordinator|  |   Web-Dashboard        |  |
|  +--------------+  +--------------+  |   (Chart.js +          |  |
|  |   Home Assistant REST API     |   |    WebSocket)          |  |
|  |   (Supervisor Token Auth)     |   +----------------------+  |
|  +-------------------------------+                             |
+----------------------------------------------------------------+
```

### Komponenten im Ueberblick

| Modul | Datei | Funktion |
|---|---|---|
| **Datenerfassung** | `data/collector.py` | Liest HA-Sensoren alle 30 s und berechnet abgeleitete Groessen |
| **Preisabfrage** | `data/prices.py` | Holt 48-h-Preisvorhersage aus konfigurierbarer Quelle |
| **PV-Prognose** | `data/forecast.py` | Berechnet stuendliche Erzeugung via Open-Meteo oder Solcast |
| **Lastzerlegung** | `data/load_decomposition.py` | Berechnet Grundlast vs. steuerbare Lasten |
| **Echtzeit-Regler** | `optimizer/realtime.py` | Stellt Ladestrom der Wallbox sekundengenau nach |
| **LP-Optimierer** | `optimizer/linear.py` | Minimiert Energiekosten fuer die naechsten 24 h |
| **EMHASS-Backend** | `optimizer/emhass_backend.py` | Optionaler EMHASS-LP-Solver |
| **Genetischer Planer** | `optimizer/genetic.py` | Erstellt strategischen 48-h-Plan via genetischem Algorithmus |
| **EV-Strategie** | `optimizer/ev_strategy.py` | Ermittelt optimale Ladefenster fuer das EV |
| **Koordinator** | `optimizer/coordinator.py` | Fusioniert alle drei Optimierer zu finalen Steuerbefehlen |
| **HA-Client** | `app/ha_client.py` | Async-HTTP-Client fuer Home Assistant REST API |
| **go-e Integration** | `devices/goe.py` | Lokale und Cloud-API der go-e Wallbox |
| **Wallbox-Abstraktion** | `devices/wallbox.py` | Einheitliche Schnittstelle fuer verschiedene Wallbox-Typen |
| **Batterie-Balancing** | `devices/battery_balancer.py` | Steuerung der Volllade-Zyklen |
| **MCP-Server** | `app/mcp_server.py` | KI-Integration (Claude Code, Cursor) |
| **Web-Dashboard** | `app/static/index.html` | Echtzeit-Dashboard mit Chart.js und WebSocket |

---

## Voraussetzungen

### Hardware
- **Raspberry Pi 4** mit mindestens 4 GB RAM (8 GB empfohlen)
- Alternativ: Intel/AMD x64 System oder andere ARM-Plattformen (armv7, armhf)
- **SD-Karte/SSD:** Mindestens 32 GB fuer Home Assistant OS + Add-ons

### Software
- **Home Assistant OS** oder **Home Assistant Supervised** (mindestens Version 2024.1)
- Home Assistant **Supervisor** (fuer Add-on-Unterstuetzung)
- Konfigurierte HA-Entitaeten fuer:
  - PV-Erzeugung (Sensor in Watt)
  - Hausbatterie (SOC-Sensor in %, Leistungssensor in W)
  - Netzbezug/-einspeisung (Sensor in W, positiv = Bezug)
  - (Optional) EV-Batteriestand (SOC-Sensor in %)
- Internetverbindung fuer externe Preisquellen und PV-Prognose

### Getestete Plattformen
- Raspberry Pi 4 (8 GB) — aarch64/armv7
- Intel NUC — amd64
- Generic x86_64 PC
- Home Assistant Yellow

---

## Installation

### 1. Repository als Add-on-Quelle hinzufuegen

1. In Home Assistant: **Einstellungen -> Add-ons -> Add-on Store**
2. Oben rechts auf die **drei Punkte** klicken -> **Repositories**
3. URL eingeben:
   ```
   https://github.com/ORPA1988/HA-Energy
   ```
4. **Hinzufuegen** klicken und die Seite neu laden

### 2. Add-on installieren

1. Im Add-on Store nach **"HA Energy Optimizer"** suchen
2. **Installieren** klicken (Download-Dauer je nach Architektur 2-10 Minuten)
3. Nach der Installation: **Konfiguration** oeffnen und Einstellungen anpassen (siehe [Konfiguration](#konfiguration))
4. **Starten** klicken

**Raspberry Pi 4 Hinweise:**
- Installation dauert auf RPi4 ca. 5-8 Minuten (SciPy/NumPy werden kompiliert)
- Unterstuetzte Architekturen: `aarch64` (64-bit) oder `armv7` (32-bit)
- Empfohlen: Home Assistant OS auf 64-bit fuer beste Performance

### 3. Dashboard aufrufen

Nach dem Start ist das Dashboard verfuegbar unter:
- **Home Assistant Ingress:** Seitenleiste -> **Energy Optimizer**
- **Direktaufruf:** `http://<HA-IP>:8080`

---

## Konfiguration

Die Konfiguration erfolgt ueber die Add-on-Oberflaeche in Home Assistant (mit Entity-Picker und bedingten Feldern). Alle Optionen koennen auch direkt in der YAML-Konfigurationsdatei bearbeitet werden.

### PV-Anlage

| Option | Standard | Beschreibung |
|---|---|---|
| `pv_power_sensor` | `sensor.solar_power` | HA-Entitaet fuer aktuelle PV-Leistung (W) |
| `pv_forecast_kwp` | `10.0` | Installierte PV-Spitzenleistung in kWp |
| `pv_orientation` | `180` | Ausrichtung in Grad (0=N, 90=O, 180=S, 270=W) |
| `pv_tilt` | `30` | Neigungswinkel der Module in Grad |
| `pv_latitude` | `48.0` | Breitengrad des Standorts |
| `pv_longitude` | `11.0` | Laengengrad des Standorts |
| `pv_efficiency` | `0.18` | Wirkungsgrad der Module (0.0-1.0) |
| `pv_forecast_source` | `auto` | Prognosequelle: `auto`, `solcast`, `open_meteo` |
| `solcast_entity` | `""` | HA-Entitaet fuer Solcast-Prognose (wenn `solcast` gewaehlt) |
| `solcast_estimate_type` | `pv_estimate` | Solcast-Schaetztyp: `pv_estimate`, `pv_estimate10`, `pv_estimate90` |

### Hausbatterie

| Option | Standard | Beschreibung |
|---|---|---|
| `battery_soc_sensor` | `sensor.battery_soc` | HA-Entitaet fuer Batterieladezustand (%) |
| `battery_power_sensor` | `sensor.battery_power` | HA-Entitaet fuer Batterieleistung (W, positiv = Laden) |
| `battery_capacity_kwh` | `10.0` | Nutzbare Kapazitaet der Batterie in kWh |
| `battery_charge_switch` | `switch.battery_charge` | HA-Schalter zum Aktivieren des Ladens |
| `battery_discharge_switch` | `switch.battery_discharge` | HA-Schalter zum Aktivieren des Entladens |
| `battery_max_charge_w` | `3000` | Maximale Ladeleistung in Watt |
| `battery_max_discharge_w` | `3000` | Maximale Entladeleistung in Watt |
| `battery_min_soc` | `10` | Minimaler SOC — Entladen wird darunter gestoppt (%) |
| `battery_reserve_soc` | `20` | Reserve-SOC fuer Notfall (%) |
| `battery_efficiency` | `0.95` | Round-trip-Wirkungsgrad der Batterie (0.0-1.0) |

### Batterie-Balancing

Regelmaessige Volllade-Zyklen gleichen die Zellspannungen aus und erhoehen die Lebensdauer von LiFePO4- und Bleibatterien.

| Option | Standard | Beschreibung |
|---|---|---|
| `battery_balancing_enabled` | `true` | Batterie-Balancing aktivieren/deaktivieren |
| `battery_balancing_mode` | `auto` | Modus: `auto` (bei Abweichung), `scheduled` (Zeitplan), `manual` |
| `battery_balancing_frequency` | `monthly` | Haeufigkeit bei `scheduled`: `daily`, `weekly`, `monthly`, `custom` |
| `battery_balancing_custom_days` | `30` | Anzahl Tage bei `custom`-Haeufigkeit |
| `battery_balancing_target_soc` | `100` | Ziel-SOC fuer den Balancing-Zyklus (%) |
| `battery_balancing_hold_duration_h` | `2` | Haltezeit bei Ziel-SOC in Stunden |
| `battery_balancing_preferred_time` | `10:00` | Bevorzugte Startzeit (HH:MM) |
| `battery_balancing_auto_trigger_soc_deviation` | `5` | SOC-Abweichung in % fuer Auto-Ausloesung |
| `battery_balancing_use_solar_only` | `true` | Nur Solarenergie fuer Balancing verwenden |

### Stromnetz

| Option | Standard | Beschreibung |
|---|---|---|
| `grid_power_sensor` | `sensor.grid_power` | HA-Entitaet fuer Netzleistung (W, positiv = Bezug) |
| `grid_max_import_w` | `0` | Maximaler Netzbezug in W (0 = unbegrenzt) |
| `total_power_sensor` | `""` | HA-Entitaet fuer Gesamt-Hausverbrauch (W, optional) |

### Strompreise

#### Preisquelle

| Option | Standard | Beschreibung |
|---|---|---|
| `price_source` | `entso-e` | Quelle: `entso-e`, `awattar`, `tibber`, `epex_spot`, `epex_entity`, `sensor`, `fixed` |
| `entso_e_token` | `""` | API-Token fuer ENTSO-E Transparency Platform |
| `entso_e_area` | `10YDE-EON------1` | Marktgebiet (z. B. DE: `10YDE-EON------1`, AT: `10YAT-APG------L`) |
| `tibber_token` | `""` | API-Token fuer Tibber |
| `awattar_country` | `AT` | Land fuer aWATTar: `AT` oder `DE` |
| `epex_spot_area` | `DE-LU` | Marktgebiet fuer EPEX SPOT |
| `epex_import_entity` | `""` | HA-Entitaet fuer EPEX-Import-Preis (bei `epex_entity`) |
| `epex_export_entity` | `""` | HA-Entitaet fuer EPEX-Export-Preis (bei `epex_entity`) |
| `epex_unit` | `ct/kWh` | Einheit der EPEX-Entitaet: `ct/kWh`, `EUR/MWh`, `EUR/kWh` |
| `price_sensor_entity` | `""` | HA-Entitaet fuer Strompreis bei `sensor`-Quelle |
| `fixed_price_ct_kwh` | `25.0` | Festpreis in ct/kWh bei `fixed`-Quelle |

#### Preisberechnung

| Option | Standard | Beschreibung |
|---|---|---|
| `price_input_is_netto` | `true` | `true` wenn der API-Preis Netto (ohne MwSt.) ist |
| `price_vat_percent` | `19.0` | Mehrwertsteuersatz in % |
| `price_grid_fee_source` | `fixed` | Netzentgeltquelle: `fixed` oder `entity` |
| `price_grid_fee_fixed_ct_kwh` | `7.5` | Netzentgelt in ct/kWh (bei `fixed`) |
| `price_grid_fee_entity` | `""` | HA-Entitaet fuer dynamisches Netzentgelt (bei `entity`) |
| `price_supplier_markup_ct_kwh` | `2.0` | Versorgeraufschlag in ct/kWh |
| `price_other_taxes_ct_kwh` | `0.0` | Weitere Abgaben/Steuern in ct/kWh |
| `price_feed_in_ct_kwh` | `8.0` | Einspeiseverguetung in ct/kWh |

**Preisformel:**
```
Gesamtpreis = (API-Preis x (1 + MwSt/100)) + Netzentgelt + Versorgeraufschlag + Sonstige Abgaben
```

### go-e Wallbox

| Option | Standard | Beschreibung |
|---|---|---|
| `goe_enabled` | `false` | go-e Wallbox-Integration aktivieren |
| `goe_connection_type` | `local` | Verbindungstyp: `local` (HTTP API v2) oder `cloud` |
| `goe_local_ip` | `""` | IP-Adresse der Wallbox im lokalen Netz |
| `goe_cloud_serial` | `""` | Seriennummer der Wallbox fuer Cloud-API |
| `goe_cloud_token` | `""` | API-Token fuer Cloud-Zugang |
| `goe_max_current_a` | `16` | Maximaler Ladestrom in Ampere (6-32 A) |
| `goe_phases` | `1` | Anzahl der Phasen (1 oder 3) |

### Elektrofahrzeug (EV)

| Option | Standard | Beschreibung |
|---|---|---|
| `ev_soc_sensor` | `sensor.ev_battery_soc` | HA-Entitaet fuer EV-Batterieladezustand (%) |
| `ev_battery_capacity_kwh` | `60.0` | Batteriekapazitaet des EV in kWh |
| `ev_charge_mode` | `smart` | Lademodus: `solar`, `min_solar`, `fast`, `smart`, `off` |
| `ev_min_charge_current_a` | `6` | Mindest-Ladestrom in A |
| `ev_max_charge_current_a` | `16` | Maximaler Ladestrom in A |
| `ev_allow_battery_to_charge_ev` | `true` | Hausbatterie darf EV laden |
| `ev_allow_grid_to_charge_ev` | `true` | Netz darf EV laden |
| `ev_combined_charge_threshold_ct` | `15.0` | Preisschwelle in ct/kWh fuer kombinierten Solar+Netz-Lademodus |
| `ev_surplus_start_threshold_w` | `1400` | PV-Ueberschuss in W ab dem Solar-Laden startet |
| `ev_surplus_stop_threshold_w` | `1000` | PV-Ueberschuss in W unter dem Solar-Laden stoppt |

**Lademodi:**

| Modus | Beschreibung |
|---|---|
| `solar` | Nur Ueberschusssolar — Laden nur wenn genug PV-Ueberschuss vorhanden |
| `min_solar` | Mindestladung + Solar — laedt immer mit Minimum, Ueberschuss wird addiert |
| `fast` | Schnelladen — laedt sofort mit maximaler Leistung |
| `smart` | Intelligentes Laden — nutzt Preisoptimierung und Ladefenster |
| `off` | Laden deaktiviert |

#### EV-Ladefenster

Ladefenster definieren, wann und wie das EV geladen werden soll:

```yaml
ev_charging_windows:
  - name: "Nacht"
    available_from: "22:00"
    available_until: "07:00"
    target_soc_percent: 80
    must_finish_by: "07:00"
    priority: "cost"           # cost | solar | balanced
```

Mehrere Ladefenster sind moeglich (z. B. Nacht fuer guenstige Zeiten, Mittag fuer Solarueberschuss).

### Steuerbare Lasten

Haushaltsgeraete koennen zeitlich verschoben werden, um guenstige Strom- oder Solarzeiten zu nutzen:

```yaml
deferrable_loads:
  - name: "Waschmaschine"
    switch: "switch.washing_machine"
    power_w: 2000
    duration_h: 2.0
    latest_end_h: 8
    earliest_start_h: 22
    min_soc_battery: 20
    price_limit_ct_kwh: 20.0
  - name: "Dishwasher"
    switch: "switch.dishwasher"
    power_w: 1800
    duration_h: 1.5
    latest_end_h: 8
    earliest_start_h: 22
    min_soc_battery: 20
    price_limit_ct_kwh: 20.0
```

| Parameter | Beschreibung |
|---|---|
| `name` | Bezeichnung der Last |
| `switch` | HA-Schalter-Entitaet |
| `power_w` | Durchschnittliche Leistungsaufnahme in W |
| `duration_h` | Benoetigte Laufzeit in Stunden |
| `latest_end_h` | Spaeteste Endzeit (Stunde des Tages, 0-23) |
| `earliest_start_h` | Frueheste Startzeit (Stunde des Tages, 0-23) |
| `min_soc_battery` | Mindest-Batterie-SOC, damit die Last gestartet wird (%) |
| `price_limit_ct_kwh` | Last wird nur gestartet wenn Preis unter diesem Wert liegt |

### Betriebsmodus

| Option | Standard | Beschreibung |
|---|---|---|
| `read_only` | `true` | Read-Only Modus — keine aktiven Schaltbefehle |
| `operation_mode` | `stopped` | Betriebsmodus: `stopped` (keine Optimierung) oder `running` (Vollbetrieb) |

### Optimierung

| Option | Standard | Beschreibung |
|---|---|---|
| `optimizer_backend` | `builtin` | Optimizer-Backend: `builtin` (SciPy LP) oder `emhass` (EMHASS) |
| `optimization_goal` | `cost` | Optimierungsziel: `cost`, `self_consumption`, `balanced` |
| `optimization_interval_minutes` | `60` | Intervall des LP-Optimierers in Minuten |
| `long_term_plan_interval_hours` | `6` | Intervall des genetischen Planers in Stunden |
| `peak_shaving_limit_w` | `0` | Spitzenlastbegrenzung in W (0 = deaktiviert) |

**Optimierungsziele:**

| Ziel | Beschreibung |
|---|---|
| `cost` | Minimiert Stromkosten — laedt guenstig, entlaedt teuer |
| `self_consumption` | Maximiert Eigenverbrauch — Solarstrom wird priorisiert |
| `balanced` | Ausgewogen zwischen Kosten und Eigenverbrauch |

### Benachrichtigungen

| Option | Standard | Beschreibung |
|---|---|---|
| `notify_target` | `notify.mobile_app` | HA-Benachrichtigungs-Dienst |
| `notify_on_balancing` | `true` | Benachrichtigung bei Batterie-Balancing |
| `notify_on_cheap_window` | `true` | Benachrichtigung bei guenstigen Ladefenstern |
| `notify_on_ev_charged` | `true` | Benachrichtigung wenn EV vollgeladen |

---

## Dashboard

Das integrierte Web-Dashboard ist ueber den HA-Ingress erreichbar und zeigt in Echtzeit:

### Statusleiste (oben)
- **PV-Leistung** — aktuelle Solarproduktion in W/kW
- **Batterie-SOC** — Ladezustand der Hausbatterie in %
- **Netzleistung** — aktueller Netzbezug (+) oder Einspeisung (-) in W
- **EV-SOC** — Ladezustand des Elektrofahrzeugs in %
- **Strompreis** — aktueller Preis in ct/kWh

### Charts
- **Energiefluss** — Zeitverlauf aller Energiestroeme (PV, Batterie, Netz, EV)
- **Batteriestatus** — SOC-Verlauf und Lade-/Entladezyklen
- **Preisprognose** — 48-h-Strompreisvorhersage mit guenstigsten Fenstern
- **24-h-Zeitplan** — geplante Schaltzeiten fuer Batterie, EV und Lasten
- **Lastzerlegung** — Grundlast vs. steuerbare Lasten

### Systemstatus
- Aktives Optimierungsziel und Betriebsmodus
- Letzter Lauf der Optimierer (Echtzeit, LP, genetisch)
- Verbindungsstatus zur Wallbox
- Config-Validierung mit Fehlern/Warnungen

---

## Optimierungsstrategien

### 1. Echtzeit-Regler (alle 30 Sekunden)

Steuert den Ladestrom der Wallbox direkt basierend auf dem aktuellen Solarueberschuss:

```
Solarueberschuss = PV-Leistung - Hausverbrauch - Batterieladen
Ladestrom = Ueberschuss / (Spannung x Phasen)
```

- Passt den Ladestrom stufenlos zwischen `ev_min_charge_current_a` und `ev_max_charge_current_a` an
- Beruecksichtigt Batterie-Reserve und Netzlimit
- Reagiert in Sekunden auf Wolken oder Lastwechsel
- Hysterese ueber `ev_surplus_start_threshold_w` und `ev_surplus_stop_threshold_w`

### 2. Linearer Optimierer — LP-Solver (stuendlich)

Loest ein lineares Programm fuer die naechsten 24 Stunden:

**Entscheidungsvariablen (pro Stunde):**
- Batterie-Ladeleistung / -Entladeleistung
- EV-Ladeleistung
- Schaltbefehle fuer steuerbare Lasten
- Netzbezug / -einspeisung

**Zielfunktion:**
```
Minimiere: Summe(Netzbezug_h x Preis_h) - Summe(Einspeisung_h x Einspeiseverguetung)
```

**Nebenbedingungen:**
- Energiebilanz pro Stunde (Erzeugung = Verbrauch + Speicherung)
- Batterie-SOC-Grenzen (min/max)
- EV-Ziel-SOC bis Abfahrtszeit
- Laufzeiten der steuerbaren Lasten
- Netzbezugslimit

**Backend:** Wahlweise `builtin` (SciPy linprog/HiGHS) oder `emhass` (EMHASS-Solver).

### 3. Genetischer Planer (alle 6 Stunden)

Erstellt einen strategischen 48-h-Plan mit einem evolutionaeren Algorithmus:

| Parameter | Wert |
|---|---|
| Populationsgroesse | 50 Chromosomen |
| Generationen | 100 |
| Selektion | Turnier-Selektion |
| Gene pro Stunde | Batterie-Modus, EV-Laden, Lastanteil |

Das Ergebnis ist ein `LongTermPlan` mit empfohlenen Reserve-SOC-Werten, der den LP-Solver und den Echtzeit-Regler mit strategischer Weitsicht versorgt.

### 4. Koordinator

Fusioniert alle drei Ebenen zu finalen Steuerbefehlen mit folgender Prioritaetsreihenfolge:

```
Echtzeit-Regler > LP-Zeitplan > Genetischer Plan
```

---

## Strompreisquellen

| Quelle | Beschreibung | API-Key erforderlich |
|---|---|---|
| **ENTSO-E** | Europaeische Transparenzplattform, Day-Ahead-Preise | Ja (kostenlos) |
| **Tibber** | Tibber-Kunden-API, Echtzeit-Boersenpreise | Ja (Tibber-Konto) |
| **aWATTar** | Oesterreich und Deutschland, stuendliche EPEX-Preise | Nein |
| **EPEX SPOT** | Europaeische Stromboerse, Day-Ahead | Nein |
| **EPEX-Entity** | Direkte HA-Entitaet fuer EPEX-Import/Export-Preise | Nein |
| **HA-Sensor** | Beliebige HA-Entitaet als Preisquelle | Nein |
| **Festpreis** | Statischer Preis in ct/kWh | Nein |

### ENTSO-E API-Key beantragen

1. Registrierung auf [transparency.entsoe.eu](https://transparency.entsoe.eu)
2. Nach Login: **Mein Konto -> Web API Security Token**
3. Token in die Add-on-Konfiguration unter `entso_e_token` eintragen

### Marktgebiete (ENTSO-E / EPEX)

| Land | ENTSO-E Code |
|---|---|
| Deutschland | `10YDE-EON------1` |
| Oesterreich | `10YAT-APG------L` |
| Schweiz | `10YCH-SWISSGRIDZ` |
| Frankreich | `10YFR-RTE------C` |

---

## Hardware-Integrationen

### Wallboxen

Das System unterstuetzt mehrere Wallbox-Typen ueber eine einheitliche Abstraktion:

**go-e Charger** (HOME, HW-11, HW-22):
- **Lokale HTTP API v2** (empfohlen, Latenz < 100 ms)
- **Cloud API** (Fallback, hoehere Latenz)

**HA-Entity Wallbox:**
- Steuerung ueber beliebige HA-Schalter und -Sensoren

**OCPP Wallbox:**
- Steuerung ueber OCPP-Protokoll

**Gesteuerte Parameter:**
- Laden aktivieren/deaktivieren
- Ladestrom (6-32 A)
- Phasenumschaltung (1-phasig/3-phasig)

**Gelesene Werte:**
- Fahrzeugstatus (nicht angeschlossen, wartend, laedt, vollgeladen)
- Aktuelle Ladeenergie der Session in kWh
- Temperatur und Phasenstroeme

### Batterie-Balancing

Unterstuetzte Batterie-Chemien:
- **LiFePO4** (Lithium-Eisenphosphat) — weit verbreitet in Heimspeichern
- **Bleiakku** (AGM, Gel, nass)

---

## API-Endpunkte

Das Add-on stellt eine FastAPI-Applikation bereit. Die Swagger-Dokumentation ist unter `http://<HA-IP>:8080/docs` erreichbar.

| Endpunkt | Methode | Beschreibung |
|---|---|---|
| `/` | GET | Web-Dashboard |
| `/api/state` | GET | Aktueller Energiezustand (JSON) |
| `/api/schedule` | GET | Aktueller 24-h-Zeitplan (JSON) |
| `/api/plan` | GET | Aktueller 48-h-Langzeitplan (JSON) |
| `/api/prices` | GET | Aktuelle Strompreise (JSON) |
| `/api/forecast` | GET | PV-Prognose (JSON) |
| `/api/mode` | GET/POST | Betriebsmodus lesen/setzen |
| `/ws` | WebSocket | Echtzeit-Updates fuer das Dashboard |

---

## Read-Only Modus (Test-Modus)

Der Read-Only Modus erlaubt es, das gesamte System zu testen, ohne dass aktive Steuerungsvorgaenge ausgefuehrt werden.

### Was passiert im Read-Only Modus?

| Funktion | Read-Only | Aktiv |
|---|---|---|
| Sensoren lesen (PV, Batterie, Netz) | Ja | Ja |
| Strompreise abrufen | Ja | Ja |
| PV-Prognose berechnen | Ja | Ja |
| LP-Optimierung berechnen | Ja | Ja |
| Genetischer Algorithmus | Ja | Ja |
| Dashboard / WebSocket | Ja | Ja |
| EV-Ladesteuerung (Wallbox) | Nein | Ja |
| Steuerbare Lasten schalten | Nein | Ja |
| Batterie-Balancing starten | Nein | Ja |
| HA-Entitaeten schreiben | Nein | Ja |

### Aktivierung

**Option 1: Dashboard** — Klicke auf den Modus-Indikator in der Statusleiste (AKTIV/READ-ONLY)

**Option 2: API**
```bash
# Aktivieren
curl -X POST http://<IP>:8080/api/mode -H "Content-Type: application/json" -d '{"read_only": true}'

# Status pruefen
curl http://<IP>:8080/api/mode
```

**Option 3: Konfiguration**
```yaml
read_only: true
operation_mode: stopped
```

### Empfohlene Ersteinrichtung

1. Bei Erstinstallation: `read_only: true` und `operation_mode: stopped` (Standardwerte)
2. Alle Sensoren in den Einstellungen konfigurieren
3. Konfiguration pruefen — alle Fehler/Warnungen beheben
4. Dashboard beobachten: PV, Batterie, Preise sollten korrekte Werte zeigen
5. LP-Schedule und EV-Strategie pruefen
6. Wenn alles korrekt: `operation_mode: running` setzen, dann `read_only: false`

---

## MCP-Server (KI-Integration)

Der integrierte MCP-Server erlaubt es, das Energy-Management-System direkt aus Claude Code, Cursor oder anderen MCP-kompatiblen KI-Tools zu steuern und zu analysieren.

### Verfuegbare Tools

| Tool | Beschreibung |
|---|---|
| `get_state` | Aktueller Energiesystem-Status (PV, Batterie, Netz, EV, Preise) |
| `get_schedule` | 24h LP-Optimierungsplan |
| `get_plan` | 48h Genetischer Algorithmus Plan |
| `get_prices` | 48h Strompreise aller Quellen |
| `get_config` | Aktuelle Konfiguration |
| `update_config` | Konfiguration live aendern |
| `validate_config` | Konfiguration pruefen (Fehler/Warnungen) |
| `get_logs` | Anwendungslogs lesen und filtern |
| `get_ha_logs` | Home Assistant Systemlogs |
| `get_history` | Historische Energiedaten (30s-Snapshots) |
| `get_ev_strategy` | EV-Ladestrategie-Bewertung |
| `trigger_optimization` | Sofortige Neuoptimierung |
| `set_ev_mode` | EV-Lademodus setzen (solar/smart/fast/off) |
| `set_read_only` | Read-Only Modus ein/ausschalten |
| `get_ha_entity` | Einzelne HA-Entitaet lesen |
| `list_ha_entities` | HA-Entitaeten nach Domain auflisten |
| `get_load_decomposition` | Lastzerlegung (Grundlast vs. steuerbar) |

### Einrichtung in Claude Code

In `~/.claude/settings.json` (oder Projekt-Settings):

```json
{
  "mcpServers": {
    "ha-energy": {
      "command": "python3",
      "args": ["/path/to/ha-energy-optimizer/app/mcp_server.py", "--url", "http://<HA-IP>:8080"],
      "env": {}
    }
  }
}
```

### Einrichtung in Cursor

In `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "ha-energy": {
      "command": "python3",
      "args": ["/path/to/ha-energy-optimizer/app/mcp_server.py", "--url", "http://<HA-IP>:8080"]
    }
  }
}
```

### Verwendungsbeispiele

- *"Zeige mir den aktuellen PV-Ertrag und Batteriestand"*
- *"Wie sieht der Optimierungsplan fuer heute aus?"*
- *"Stelle den EV-Lademodus auf Solar-only"*
- *"Validiere die aktuelle Konfiguration"*
- *"Zeige mir die letzten Fehler im Log"*

---

## Weitere Installationsmethoden

### Lokale Installation (fuer Entwickler)

```bash
git clone https://github.com/ORPA1988/HA-Energy.git
cp -r HA-Energy/ha-energy-optimizer /addons/ha-energy-optimizer
# In HA: Einstellungen -> Add-ons -> Lokale Add-ons -> Neu laden
```

### Docker (ohne HA Add-on)

```bash
cd HA-Energy/ha-energy-optimizer
docker build -t ha-energy-optimizer .
docker run -d \
  --name energy-optimizer \
  -p 8080:8080 \
  -e HA_URL=http://<HA-IP>:8123 \
  -e SUPERVISOR_TOKEN=<long-lived-access-token> \
  -v /opt/energy-data:/data \
  ha-energy-optimizer
```

> **Hinweis**: Ohne HA Supervisor muss ein [Long-Lived Access Token](https://www.home-assistant.io/docs/authentication/#your-account-profile) als `SUPERVISOR_TOKEN` uebergeben werden.

### Docker Compose

```yaml
version: '3.8'
services:
  energy-optimizer:
    build: ./ha-energy-optimizer
    ports:
      - "8080:8080"
    environment:
      - HA_URL=http://homeassistant:8123
      - SUPERVISOR_TOKEN=${HA_TOKEN}
    volumes:
      - energy-data:/data
    restart: unless-stopped

volumes:
  energy-data:
```

### Netzwerk & Ports

| Port | Protokoll | Beschreibung |
|---|---|---|
| 8080 | HTTP | Web-Dashboard + REST-API |
| 8080 | WebSocket | Live-Updates (`/ws`) |

### Unterstuetzte Architekturen

| Architektur | Status | Empfohlen fuer |
|---|---|---|
| amd64 | Getestet | NUC / Server |
| aarch64 | Getestet | Raspberry Pi 4/5 |
| armv7 | Getestet | Aeltere RPi |
| armhf | Eingeschraenkt | Wenig RAM |

---

## Fehlerbehebung

### Add-on startet nicht

1. **Logs pruefen:** Einstellungen -> Add-ons -> HA Energy Optimizer -> **Log**
2. **Konfiguration validieren:** Alle Pflichtfelder (Sensoren, Preisquelle) muessen ausgefuellt sein
3. **HA-Entitaeten pruefen:** Die angegebenen Sensor-Entitaeten muessen in HA existieren

### PV-Prognose liefert keine Daten

- Koordinaten (`pv_latitude`, `pv_longitude`) muessen korrekt sein
- Bei `pv_forecast_source: solcast`: Solcast-Entitaet pruefen
- Internetverbindung pruefen (Open-Meteo API muss erreichbar sein)

### Wallbox wird nicht gesteuert

1. `goe_enabled: true` setzen
2. IP-Adresse der Wallbox pruefen (`goe_local_ip`)
3. Wallbox-API v2 aktivieren (in go-e App)
4. Firewall zwischen HA und Wallbox pruefen

### Strompreise werden nicht geladen

- Bei ENTSO-E: API-Token korrekt? Marktgebiet stimmt?
- Bei Tibber: Token gueltig? Rate-Limits beachten
- Bei EPEX-Entity: Sensor-Entitaet und Einheit pruefen
- Fallback: `price_source: fixed` mit `fixed_price_ct_kwh` setzen

### Optimierung laeuft langsam

- `optimization_interval_minutes` erhoehen (z. B. auf 120)
- `long_term_plan_interval_hours` erhoehen (z. B. auf 12)
- Genetischer Algorithmus ist fuer RPi4 optimiert (50 Population, 100 Generationen ~ 15-20 s)

### Hohe Speichernutzung

- Normaler Speicherbedarf: ~150-250 MB
- WebSocket-Clients auf 100 begrenzt
- Historie auf 24 h begrenzt (2880 Eintraege)

---

## Performance-Empfehlungen fuer Raspberry Pi 4

### Optimale Konfiguration (getestet auf RPi4 mit 8 GB RAM)

| Parameter | Empfohlener Wert | Begruendung |
|---|---|---|
| `optimization_interval_minutes` | 60 | Stuendliche LP-Optimierung ausreichend |
| `long_term_plan_interval_hours` | 6 | Genetischer Algorithmus braucht ~15-20 s CPU-Zeit |
| Realtime Loop | 30 s (fest) | EVCC-Style, optimal fuer Solar-Ueberschussregelung |
| Price Refresh | 60 min (fest) | Day-Ahead-Preise aendern sich nur einmal taeglich |
| WebSocket Clients | Max 100 | Verhindert Memory Leak bei vielen Dashboards |

### Ressourcenverbrauch

- **CPU:** ~5-10 % idle, ~30-50 % waehrend LP/Genetic-Optimierung
- **RAM:** ~150-250 MB (inkl. NumPy/SciPy Arrays)
- **Netzwerk:** ~500-1000 API-Calls/h zu Home Assistant (mit Rate Limiting)

---

## Entwicklung & Beitrag

### Lokale Entwicklungsumgebung

```bash
git clone https://github.com/ORPA1988/HA-Energy.git
cd HA-Energy/ha-energy-optimizer

pip install -r app/requirements.txt

HA_TOKEN=<token> HA_BASE_URL=http://<ha-ip>:8123 python3 app/main.py
```

### Docker-Build

```bash
cd ha-energy-optimizer
docker build --build-arg BUILD_ARCH=amd64 -t ha-energy-optimizer:dev .
docker run -p 8080:8080 \
  -e SUPERVISOR_TOKEN=<token> \
  -e HA_BASE_URL=http://<ha-ip>:8123 \
  ha-energy-optimizer:dev
```

### Projektstruktur

```
ha-energy-optimizer/
├── Dockerfile              # Multi-stage Container-Build
├── config.yaml             # HA Add-on Manifest & Konfigurationsschema
├── build.yaml              # Multi-Architektur Build-Konfiguration
├── CHANGELOG.md            # Versionshistorie
└── app/
    ├── main.py             # FastAPI Applikation & WebSocket
    ├── config.py           # Konfigurationsmanagement
    ├── models.py           # Pydantic Datenmodelle
    ├── ha_client.py        # Home Assistant REST API Client
    ├── scheduler.py        # APScheduler Job-Verwaltung
    ├── mcp_server.py       # MCP-Server (Claude Code / Cursor)
    ├── requirements.txt    # Python-Abhaengigkeiten
    ├── static/
    │   └── index.html      # Web-Dashboard (Chart.js + WebSocket)
    ├── optimizer/
    │   ├── realtime.py     # 30s EV-Steuerung (EVCC-Stil)
    │   ├── linear.py       # 24h Kostenoptimierung (scipy.linprog)
    │   ├── genetic.py      # 48h Energieplanung (genetischer Algorithmus)
    │   ├── ev_strategy.py  # EV-Ladestrategie
    │   ├── coordinator.py  # Optimizer-Koordination
    │   └── emhass_backend.py  # Optionaler EMHASS-LP-Solver
    ├── data/
    │   ├── collector.py    # HA-Sensor-Erfassung
    │   ├── prices.py       # Strompreisabfrage
    │   ├── forecast.py     # PV-Ertragsprognose (Open-Meteo + Solcast)
    │   └── load_decomposition.py  # Lastzerlegung
    ├── devices/
    │   ├── goe.py          # go-e Wallbox Integration
    │   ├── wallbox.py      # Abstrakte Wallbox-Schnittstelle
    │   └── battery_balancer.py  # Batterie-Zellenausgleich
    └── translations/
        ├── en.yaml         # Englische Uebersetzung
        └── de.yaml         # Deutsche Uebersetzung
```

### Technologie-Stack

| Bereich | Technologie |
|---|---|
| **Backend** | Python 3.11, FastAPI, APScheduler |
| **Optimierung** | SciPy (linprog/HiGHS), NumPy, optional EMHASS |
| **Datenvalidierung** | Pydantic v2 |
| **HTTP-Client** | HTTPX (async), AIOHTTP |
| **Frontend** | HTML/CSS/JS, Chart.js, WebSocket |
| **Container** | Docker, Alpine Linux 3.18 |
| **HA-Integration** | Supervisor API, Ingress, Add-on Schema |

### Fehler melden / Feature-Requests

Issues direkt auf GitHub erstellen:
[github.com/ORPA1988/HA-Energy/issues](https://github.com/ORPA1988/HA-Energy/issues)

---

## Changelog

Siehe [CHANGELOG.md](ha-energy-optimizer/CHANGELOG.md) fuer die vollstaendige Versionshistorie.

### Aktuelle Version: 0.1.0

- **Auto-Erkennung**: Automatische Erkennung von HA-Entitaeten (Sensoren, Switches) mit Confidence-Bewertung
- **Bedingte Felder**: Nicht relevante Konfigurationsfelder werden je nach Auswahl ausgeblendet
- **PV-Prognose**: Forecast-Source Auswahl (Auto/Solcast/Open-Meteo) mit Solcast-Konfiguration
- **Batterie-Balancing UI**: Vollstaendige Konfigurationsoberflaeche fuer Balancing-Parameter
- **Benachrichtigungen UI**: Konfiguration von Benachrichtigungszielen und Ausloesern
- **go-e Cloud**: Cloud-Verbindungsfelder (Serial, Token) bei Cloud-Modus

### Fruehere Versionen

- **0.0.3**: Read-Only Modus, MCP-Server mit 17 Tools, Bug-Fixes, Multi-EV Dashboard, Lastzerlegung
- **0.0.2**: EMHASS Backend, Multi-EV, Wallbox-Abstraktion, Lastzerlegung
- **0.0.1**: Erstveroeffentlichung mit dreistufiger Optimierung, Live-Dashboard, Multi-Source Strompreise

---

*HA Energy Optimizer kombiniert Konzepte aus [EVCC](https://evcc.io/), [EOS](https://github.com/josepowera/eos) und [EMHASS](https://github.com/davidusb-geek/emhass).*
