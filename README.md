# HA Energy Optimizer

> **Energiesteuerung mit Dashboard zur Optimierung des Stromverbrauches**

[![Version](https://img.shields.io/badge/Version-1.0.1-blue)](https://github.com/ORPA1988/HA-Energy)
[![Plattform](https://img.shields.io/badge/Plattform-Home%20Assistant-41BDF5)](https://www.home-assistant.io/)
[![Architektur](https://img.shields.io/badge/Arch-amd64%20%7C%20aarch64%20%7C%20armv7%20%7C%20armhf-green)](#installation)
[![Lizenz](https://img.shields.io/badge/Lizenz-MIT-yellow)](LICENSE)

Ein vollständiges **Home-Energy-Management-System (HEMS)** als Home Assistant Add-on. Es vereint die besten Konzepte aus [EVCC](https://evcc.io/), [EOS](https://github.com/josepowera/eos) und [EMHASS](https://github.com/davidusb-geek/emhass) in einer einzigen intelligenten Plattform – mit dreistufiger Optimierung, Live-Dashboard und umfassenden Hardware-Integrationen.

---

## Inhaltsverzeichnis

1. [Funktionsübersicht](#funktionsübersicht)
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
   - [Optimierung](#optimierung)
   - [Benachrichtigungen](#benachrichtigungen)
6. [Dashboard](#dashboard)
7. [Optimierungsstrategien](#optimierungsstrategien)
8. [Strompreisquellen](#strompreisquellen)
9. [Hardware-Integrationen](#hardware-integrationen)
10. [API-Endpunkte](#api-endpunkte)
11. [Fehlerbehebung](#fehlerbehebung)
12. [Performance-Empfehlungen für Raspberry Pi 4](#performance-empfehlungen-für-raspberry-pi-4)
13. [Changelog](#changelog)
14. [Entwicklung & Beitrag](#entwicklung--beitrag)

---

## Funktionsübersicht

| Funktion | Beschreibung |
|---|---|
| ☀️ **PV-Eigenverbrauch** | Maximiert den Eigenverbrauch der Solaranlage durch intelligente Last- und Batteriesteuerung |
| 🔋 **Batteriesteuerung** | Optimales Laden/Entladen der Hausbatterie basierend auf Preisen, Prognosen und Verbrauch |
| 🚗 **EV-Ladeoptimierung** | EVCC-artige Echtzeit-Regelung der Wallbox – solar, günstig oder schnell |
| 📊 **Dreistufige Optimierung** | Realtime (30 s), Stundenbasis (LP-Solver) und 48-h-Planung (genetischer Algorithmus) |
| 💰 **Dynamische Strompreise** | Unterstützt ENTSO-E, Tibber, aWATTar, EPEX Spot, HA-Sensor oder Festpreis |
| 🔮 **PV-Prognose** | Kostenlose 48-h-Ertragsprognose via Open-Meteo (kein API-Key erforderlich) |
| ⚖️ **Batterie-Balancing** | Automatische oder geplante Volllade-Zyklen zur Zellausgleichung |
| 📱 **Live-Dashboard** | Echtzeit-Visualisierung aller Energieflüsse, Preise und Zeitpläne via WebSocket |
| 🔔 **Benachrichtigungen** | Push-Nachrichten bei günstigen Ladezeiten, Balancing-Ereignissen und vollem EV |
| 🏠 **HA-Integration** | Native Integration mit Ingress-Panel, Supervisor-API und allen HA-Entitäten |

---

## Systemarchitektur

```
┌─────────────────────────────────────────────────────────────────┐
│                    HA Energy Optimizer                          │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │  Datenbeschaf-│  │  Optimierungs│  │    Hardware-         │  │
│  │    fung       │  │    Engines   │  │    Integrationen     │  │
│  │               │  │              │  │                      │  │
│  │ • collector   │  │ • realtime   │  │ • go-e Wallbox       │  │
│  │   (HA Sensor- │  │   (30s, EVCC)│  │   (lokal + Cloud)    │  │
│  │    Abfrage)   │  │ • linear     │  │ • battery_balancer   │  │
│  │ • prices      │  │   (stündl.   │  │   (LiFePO4/Blei)     │  │
│  │   (5 Quellen) │  │    LP-Solver)│  │                      │  │
│  │ • forecast    │  │ • genetic    │  └──────────────────────┘  │
│  │   (Open-      │  │   (48h Plan) │                            │
│  │    Meteo)     │  │ • coordinator│  ┌──────────────────────┐  │
│  └──────────────┘  └──────────────┘  │   Web-Dashboard       │  │
│                                       │   (Chart.js +         │  │
│  ┌──────────────────────────────┐     │    WebSocket)         │  │
│  │    Home Assistant REST API   │     └──────────────────────┘  │
│  │    (Supervisor Token Auth)   │                                │
│  └──────────────────────────────┘                               │
└─────────────────────────────────────────────────────────────────┘
```

### Komponenten im Überblick

| Modul | Datei | Funktion |
|---|---|---|
| **Datenerfassung** | `data/collector.py` | Liest HA-Sensoren alle 30 s und berechnet abgeleitete Größen |
| **Preisabfrage** | `data/prices.py` | Holt 48-h-Preisvorhersage aus konfigurierbarer Quelle |
| **PV-Prognose** | `data/forecast.py` | Berechnet stündliche Erzeugung via Open-Meteo-API |
| **Echtzeit-Regler** | `optimizer/realtime.py` | Stellt Ladestrom der Wallbox sekundengenau nach |
| **LP-Optimierer** | `optimizer/linear.py` | Minimiert Energiekosten für die nächsten 24 h |
| **Genetischer Planer** | `optimizer/genetic.py` | Erstellt strategischen 48-h-Plan via genetischem Algorithmus |
| **EV-Strategie** | `optimizer/ev_strategy.py` | Ermittelt optimale Ladefenster für das EV |
| **Koordinator** | `optimizer/coordinator.py` | Fusioniert alle drei Optimierer zu finalen Steuerbefehlen |
| **HA-Client** | `app/ha_client.py` | Async-HTTP-Client für Home Assistant REST API |
| **go-e Integration** | `devices/goe.py` | Lokale und Cloud-API der go-e Wallbox |
| **Batterie-Balancing** | `devices/battery_balancer.py` | Steuerung der Volllade-Zyklen |
| **Web-Dashboard** | `app/static/index.html` | Echtzeit-Dashboard mit Chart.js und WebSocket |

---

## Voraussetzungen

### Hardware
- **Raspberry Pi 4** mit mindestens 4GB RAM (8GB empfohlen für optimale Performance)
- Alternativ: Intel/AMD x64 System, oder andere ARM-Plattformen (armv7, armhf)
- **SD-Karte/SSD:** Mindestens 32GB für Home Assistant OS + Add-ons

### Software
- **Home Assistant OS** oder **Home Assistant Supervised** (mindestens Version 2024.1)
- Home Assistant **Supervisor** (für Add-on-Unterstützung)
- Konfigurierte HA-Entitäten für:
  - PV-Erzeugung (Sensor in Watt)
  - Hausbatterie (SOC-Sensor in %, Leistungssensor in W)
  - Netzbezug/-einspeisung (Sensor in W, positiv = Bezug)
  - (Optional) EV-Batteriestand (SOC-Sensor in %)
- Internetverbindung für externe Preisquellen und PV-Prognose

### Getestete Plattformen
- ✅ Raspberry Pi 4 (8GB) - aarch64/armv7
- ✅ Intel NUC - amd64
- ✅ Generic x86_64 PC
- ✅ Home Assistant Yellow

---

## Installation

### 1. Repository als Add-on-Quelle hinzufügen


1. In Home Assistant: **Einstellungen → Add-ons → Add-on Store**
2. Oben rechts auf die **drei Punkte** klicken → **Repositories**
3. URL eingeben:
   ```
   https://github.com/ORPA1988/HA-Energy
   ```
4. **Hinzufügen** klicken und die Seite neu laden


### 2. Add-on installieren

1. Im Add-on Store nach **"HA Energy Optimizer"** suchen
2. **Installieren** klicken (Download-Dauer je nach Architektur 2–10 Minuten)
3. Nach der Installation: **Konfiguration** öffnen und Einstellungen anpassen (siehe [Konfiguration](#konfiguration))
4. **Starten** klicken

**Raspberry Pi 4 Hinweise:**
- Installation dauert auf RPi4 ca. 5-8 Minuten (SciPy/NumPy werden kompiliert)
- Unterstützte Architekturen: `aarch64` (64-bit) oder `armv7` (32-bit)
- Empfohlen: Home Assistant OS auf 64-bit für beste Performance
- RAM-Bedarf: ~150-250 MB (von 8 GB verfügbar)

### 3. Dashboard aufrufen

Nach dem Start ist das Dashboard verfügbar unter:
- **Home Assistant Ingress:** Seitenleiste → ⚡ **Energy Optimizer**
- **Direktaufruf:** `http://<HA-IP>:8080`

---

## Konfiguration

Die Konfiguration erfolgt über die Add-on-Oberfläche in Home Assistant. Alle Optionen können auch direkt in der YAML-Konfigurationsdatei des Add-ons bearbeitet werden.

### PV-Anlage

| Option | Standard | Beschreibung |
|---|---|---|
| `pv_power_sensor` | `sensor.solar_power` | HA-Entität für aktuelle PV-Leistung (W) |
| `pv_forecast_kwp` | `10.0` | Installierte PV-Spitzenleistung in kWp |
| `pv_orientation` | `180` | Ausrichtung in Grad (0=N, 90=O, 180=S, 270=W) |
| `pv_tilt` | `30` | Neigungswinkel der Module in Grad |
| `pv_latitude` | `48.0` | Breitengrad des Standorts |
| `pv_longitude` | `11.0` | Längengrad des Standorts |
| `pv_efficiency` | `0.18` | Wirkungsgrad der Module (0.0–1.0) |

### Hausbatterie

| Option | Standard | Beschreibung |
|---|---|---|
| `battery_soc_sensor` | `sensor.battery_soc` | HA-Entität für Batterieladezustand (%) |
| `battery_power_sensor` | `sensor.battery_power` | HA-Entität für Batterieleistung (W, positiv = Laden) |
| `battery_capacity_kwh` | `10.0` | Nutzbare Kapazität der Batterie in kWh |
| `battery_charge_switch` | `switch.battery_charge` | HA-Schalter zum Aktivieren des Ladens |
| `battery_discharge_switch` | `switch.battery_discharge` | HA-Schalter zum Aktivieren des Entladens |
| `battery_max_charge_w` | `3000` | Maximale Ladeleistung in Watt |
| `battery_max_discharge_w` | `3000` | Maximale Entladeleistung in Watt |
| `battery_min_soc` | `10` | Minimaler SOC – Entladen wird darunter gestoppt (%) |
| `battery_reserve_soc` | `20` | Reserve-SOC für Notfall (nicht für EV/Lasten genutzt) (%) |
| `battery_efficiency` | `0.95` | Round-trip-Wirkungsgrad der Batterie (0.0–1.0) |

### Batterie-Balancing

Regelmäßige Volllade-Zyklen gleichen die Zellspannungen aus und erhöhen die Lebensdauer von LiFePO4- und Bleibatterien.

| Option | Standard | Beschreibung |
|---|---|---|
| `battery_balancing_enabled` | `true` | Batterie-Balancing aktivieren/deaktivieren |
| `battery_balancing_mode` | `auto` | Modus: `auto` (bei Abweichung), `scheduled` (Zeitplan), `manual` |
| `battery_balancing_frequency` | `monthly` | Häufigkeit bei `scheduled`: `daily`, `weekly`, `monthly`, `custom` |
| `battery_balancing_custom_days` | `30` | Anzahl Tage bei `custom`-Häufigkeit |
| `battery_balancing_target_soc` | `100` | Ziel-SOC für den Balancing-Zyklus (%) |
| `battery_balancing_hold_duration_h` | `2` | Haltezeit bei Ziel-SOC in Stunden |
| `battery_balancing_preferred_time` | `10:00` | Bevorzugte Startzeit (HH:MM) |
| `battery_balancing_auto_trigger_soc_deviation` | `5` | SOC-Abweichung in % für Auto-Auslösung |
| `battery_balancing_use_solar_only` | `true` | Nur Solarenergie für Balancing verwenden |

### Stromnetz

| Option | Standard | Beschreibung |
|---|---|---|
| `grid_power_sensor` | `sensor.grid_power` | HA-Entität für Netzleistung (W, positiv = Bezug) |
| `grid_max_import_w` | `0` | Maximaler Netzbezug in W (0 = unbegrenzt) |

### Strompreise

#### Preisquelle

| Option | Standard | Beschreibung |
|---|---|---|
| `price_source` | `entso-e` | Quelle: `entso-e`, `awattar`, `tibber`, `epex_spot`, `sensor`, `fixed` |
| `entso_e_token` | `""` | API-Token für ENTSO-E Transparency Platform |
| `entso_e_area` | `10YDE-EON------1` | Marktgebiet (z. B. `10YDE-EON------1` für DE, `10YAT-APG------L` für AT) |
| `tibber_token` | `""` | API-Token für Tibber |
| `awattar_country` | `AT` | Land für aWATTar: `AT` (Österreich) oder `DE` (Deutschland) |
| `epex_spot_area` | `DE-LU` | Marktgebiet für EPEX SPOT |
| `price_sensor_entity` | `""` | HA-Entität für Strompreis bei `sensor`-Quelle (ct/kWh) |
| `fixed_price_ct_kwh` | `25.0` | Festpreis in ct/kWh bei `fixed`-Quelle |

#### Preisberechnung

| Option | Standard | Beschreibung |
|---|---|---|
| `price_input_is_netto` | `true` | `true` wenn der API-Preis Netto (ohne MwSt.) ist |
| `price_vat_percent` | `19.0` | Mehrwertsteuersatz in % |
| `price_grid_fee_source` | `fixed` | Netzentgeltquelle: `fixed` oder `entity` (HA-Entität) |
| `price_grid_fee_fixed_ct_kwh` | `7.5` | Netzentgelt in ct/kWh (bei `fixed`) |
| `price_grid_fee_entity` | `""` | HA-Entität für dynamisches Netzentgelt (bei `entity`) |
| `price_supplier_markup_ct_kwh` | `2.0` | Versorgeraufschlag in ct/kWh |
| `price_other_taxes_ct_kwh` | `0.0` | Weitere Abgaben/Steuern in ct/kWh |
| `price_feed_in_ct_kwh` | `8.0` | Einspeisevergütung in ct/kWh |

**Preisformel:**
```
Gesamtpreis = (API-Preis × (1 + MwSt/100)) + Netzentgelt + Versorgeraufschlag + Sonstige Abgaben
```

### go-e Wallbox

| Option | Standard | Beschreibung |
|---|---|---|
| `goe_enabled` | `false` | go-e Wallbox-Integration aktivieren |
| `goe_connection_type` | `local` | Verbindungstyp: `local` (HTTP API v2) oder `cloud` |
| `goe_local_ip` | `""` | IP-Adresse der Wallbox im lokalen Netz |
| `goe_cloud_serial` | `""` | Seriennummer der Wallbox für Cloud-API |
| `goe_cloud_token` | `""` | API-Token für Cloud-Zugang |
| `goe_max_current_a` | `16` | Maximaler Ladestrom in Ampere (6–32 A) |
| `goe_phases` | `1` | Anzahl der Phasen (1 oder 3) |

### Elektrofahrzeug (EV)

| Option | Standard | Beschreibung |
|---|---|---|
| `ev_soc_sensor` | `sensor.ev_battery_soc` | HA-Entität für EV-Batterieladezustand (%) |
| `ev_battery_capacity_kwh` | `60.0` | Batteriekapazität des EV in kWh |
| `ev_charge_mode` | `smart` | Lademodus: `solar`, `min_solar`, `fast`, `smart`, `off` |
| `ev_min_charge_current_a` | `6` | Mindest-Ladestrom in A (EVSE-Minimum, typisch 6 A) |
| `ev_max_charge_current_a` | `16` | Maximaler Ladestrom in A |
| `ev_allow_battery_to_charge_ev` | `true` | Hausbatterie darf EV laden |
| `ev_allow_grid_to_charge_ev` | `true` | Netz darf EV laden |
| `ev_combined_charge_threshold_ct` | `15.0` | Preisschwelle in ct/kWh für kombinierten Solar+Netz-Lademodus |

**Lademodi:**

| Modus | Beschreibung |
|---|---|
| `solar` | Nur Überschusssolar – Laden nur wenn genug PV-Überschuss vorhanden |
| `min_solar` | Mindestladung + Solar – lädt immer mit Minimum, Überschuss wird addiert |
| `fast` | Schnelladen – lädt sofort mit maximaler Leistung unabhängig vom Preis |
| `smart` | Intelligentes Laden – nutzt Preisoptimierung und Ladefenster |
| `off` | Laden deaktiviert |

#### EV-Ladefenster

Ladefenster definieren, wann und wie das EV geladen werden soll:

```yaml
ev_charging_windows:
  - name: "Nacht"
    available_from: "22:00"    # Fenster öffnet sich
    available_until: "07:00"   # Fenster schließt sich
    target_soc_percent: 80     # Gewünschter Ladestand bei Abfahrt
    must_finish_by: "07:00"    # Spätestmögliche Fertigstellung
    priority: "cost"           # Priorität: cost | solar | balanced
```

Mehrere Ladefenster sind möglich (z. B. Nacht für günstige Zeiten, Mittag für Solarüberschuss).

### Steuerbare Lasten

Haushaltsgeräte können zeitlich verschoben werden, um günstige Strom- oder Solarzeiten zu nutzen:

```yaml
deferrable_loads:
  - name: "Waschmaschine"
    switch: "switch.washing_machine"  # HA-Schalter des Geräts
    power_w: 2000                     # Durchschnittsleistung in Watt
    duration_h: 2.0                   # Betriebsdauer in Stunden
    latest_end_h: 8                   # Späteste Fertigstellung (Uhrzeit)
    earliest_start_h: 22              # Frühester Start (Uhrzeit)
    min_soc_battery: 20               # Mindest-Batterie-SOC für Start (%)
    price_limit_ct_kwh: 20.0          # Preislimit in ct/kWh
```

| Parameter | Beschreibung |
|---|---|
| `name` | Bezeichnung der Last |
| `switch` | HA-Schalter-Entität |
| `power_w` | Durchschnittliche Leistungsaufnahme in W |
| `duration_h` | Benötigte Laufzeit in Stunden |
| `latest_end_h` | Späteste Endzeit (Stunde des Tages, 0–23) |
| `earliest_start_h` | Früheste Startzeit (Stunde des Tages, 0–23) |
| `min_soc_battery` | Mindest-Batterie-SOC, damit die Last gestartet wird (%) |
| `price_limit_ct_kwh` | Last wird nur gestartet wenn Preis unter diesem Wert liegt |

### Optimierung

| Option | Standard | Beschreibung |
|---|---|---|
| `optimization_goal` | `cost` | Optimierungsziel: `cost`, `self_consumption`, `balanced` |
| `optimization_interval_minutes` | `60` | Intervall des LP-Optimierers in Minuten |
| `long_term_plan_interval_hours` | `6` | Intervall des genetischen Planers in Stunden |
| `peak_shaving_limit_w` | `0` | Spitzenlastbegrenzung in W (0 = deaktiviert) |

**Optimierungsziele:**

| Ziel | Beschreibung |
|---|---|
| `cost` | Minimiert Stromkosten – lädt günstig, entlädt teuer |
| `self_consumption` | Maximiert Eigenverbrauch – Solarstrom wird priorisiert |
| `balanced` | Ausgewogen zwischen Kosten und Eigenverbrauch |

### Benachrichtigungen

| Option | Standard | Beschreibung |
|---|---|---|
| `notify_target` | `notify.mobile_app` | HA-Benachrichtigungs-Dienst |
| `notify_on_balancing` | `true` | Benachrichtigung bei Batterie-Balancing |
| `notify_on_cheap_window` | `true` | Benachrichtigung bei günstigen Ladefenstern |
| `notify_on_ev_charged` | `true` | Benachrichtigung wenn EV vollgeladen |

---

## Dashboard

Das integrierte Web-Dashboard ist über den HA-Ingress erreichbar und zeigt in Echtzeit:

### Statusleiste (oben)
- **PV-Leistung** – aktuelle Solarproduktion in W/kW
- **Batterie-SOC** – Ladezustand der Hausbatterie in %
- **Netzleistung** – aktueller Netzbezug (+) oder Einspeisung (−) in W
- **EV-SOC** – Ladezustand des Elektrofahrzeugs in %
- **Strompreis** – aktueller Preis in ct/kWh

### Charts
- **Energiefluss** – Zeitverlauf aller Energieströme (PV, Batterie, Netz, EV)
- **Batteriestatus** – SOC-Verlauf und Lade-/Entladezyklen
- **Preisprognose** – 48-h-Strompreisvorhersage mit günstigsten Fenstern
- **24-h-Zeitplan** – geplante Schaltzeiten für Batterie, EV und Lasten

### Systemstatus
- Aktives Optimierungsziel
- Letzter Lauf der Optimierer (Echtzeit, LP, genetisch)
- Verbindungsstatus zur Wallbox

---

## Optimierungsstrategien

### 1. Echtzeit-Regler (alle 30 Sekunden)

Steuert den Ladestrom der Wallbox direkt basierend auf dem aktuellen Solarüberschuss:

```
Solarüberschuss = PV-Leistung − Hausverbrauch − Batterieladen
Ladestrom = Überschuss ÷ (Spannung × Phasen)
```

- Passt den Ladestrom der go-e Wallbox stufenlos zwischen `ev_min_charge_current_a` und `ev_max_charge_current_a` an
- Berücksichtigt Batterie-Reserve und Netzlimit
- Reagiert in Sekunden auf Wolken oder Lastwechsel

### 2. Linearer Optimierer – LP-Solver (stündlich)

Löst ein lineares Programm für die nächsten 24 Stunden:

**Entscheidungsvariablen (pro Stunde):**
- Batterie-Ladeleistung / -Entladeleistung
- EV-Ladeleistung
- Schaltbefehle für steuerbare Lasten
- Netzbezug / -einspeisung

**Zielfunktion:**
```
Minimiere: Σ (Netzbezug_h × Preis_h) − Σ (Einspeisung_h × Einspeisevergütung)
```

**Nebenbedingungen:**
- Energiebilanz pro Stunde (Erzeugung = Verbrauch + Speicherung)
- Batterie-SOC-Grenzen (min/max)
- EV-Ziel-SOC bis Abfahrtszeit
- Laufzeiten der steuerbaren Lasten
- Netzbezugslimit

### 3. Genetischer Planer (alle 6 Stunden)

Erstellt einen strategischen 48-h-Plan mit einem evolutionären Algorithmus:

| Parameter | Wert |
|---|---|
| Populationsgröße | 50 Chromosomen |
| Generationen | 100 |
| Selektion | Turnier-Selektion |
| Gene pro Stunde | Batterie-Modus, EV-Laden, Lastanteil |

Das Ergebnis ist ein `LongTermPlan` mit empfohlenen Reserve-SOC-Werten, der den LP-Solver und den Echtzeit-Regler mit strategischer Weitsicht versorgt.

### 4. Koordinator

Fusioniert alle drei Ebenen zu finalen Steuerbefehlen mit folgender Prioritätsreihenfolge:

```
Echtzeit-Regler > LP-Zeitplan > Genetischer Plan
```

---

## Strompreisquellen

| Quelle | Beschreibung | API-Key erforderlich |
|---|---|---|
| **ENTSO-E** | Europäische Transparenzplattform, Day-Ahead-Preise | ✅ Kostenlos registrieren |
| **Tibber** | Tibber-Kunden-API, Echtzeit-Börsenpreise | ✅ Tibber-Konto erforderlich |
| **aWATTar** | Österreich und Deutschland, stündliche EPEX-Preise | ❌ |
| **EPEX SPOT** | Europäische Strombörse, Day-Ahead | ❌ |
| **HA-Sensor** | Beliebige HA-Entität als Preisquelle | ❌ |
| **Festpreis** | Statischer Preis in ct/kWh | ❌ |

### ENTSO-E API-Key beantragen

1. Registrierung auf [transparency.entsoe.eu](https://transparency.entsoe.eu)
2. Nach Login: **Mein Konto → Web API Security Token**
3. Token in die Add-on-Konfiguration unter `entso_e_token` eintragen

### Marktgebiete (ENTSO-E / EPEX)

| Land | ENTSO-E Code |
|---|---|
| Deutschland | `10YDE-EON------1` |
| Österreich | `10YAT-APG------L` |
| Schweiz | `10YCH-SWISSGRIDZ` |
| Frankreich | `10YFR-RTE------C` |

---

## Hardware-Integrationen

### go-e Charger

Unterstützt alle go-e Charger Modelle (HOME, HW-11, HW-22) über:

**Lokale HTTP API v2** (empfohlen):
- Direkte Verbindung im lokalen Netzwerk
- Keine Cloud-Abhängigkeit
- Latenz < 100 ms

**Cloud API:**
- Verbindung über go-e Server
- Nützlich wenn lokale API nicht erreichbar
- Etwas höhere Latenz

**Gesteuerte Parameter:**
- Laden aktivieren/deaktivieren
- Ladestrom (6–32 A)
- Phasenumschaltung (1-phasig/3-phasig)

**Gelesene Werte:**
- Fahrzeugstatus (nicht angeschlossen, wartend, lädt, vollgeladen)
- Aktuelle Ladeenergie der Session in kWh
- Temperatur
- Phasenströme

### Batterie-Balancing

Unterstützte Batterie-Chemien:
- **LiFePO4** (Lithium-Eisenphosphat) – weit verbreitet in Heimspeichern
- **Bleiakku** (AGM, Gel, nass)

---

## API-Endpunkte

Das Add-on stellt eine FastAPI-Applikation bereit. Die automatisch generierte **Swagger-Dokumentation** ist unter `http://<HA-IP>:8080/docs` erreichbar.

| Endpunkt | Methode | Beschreibung |
|---|---|---|
| `/` | GET | Web-Dashboard |
| `/api/state` | GET | Aktueller Energiezustand (JSON) |
| `/api/schedule` | GET | Aktueller 24-h-Zeitplan (JSON) |
| `/api/plan` | GET | Aktueller 48-h-Langzeitplan (JSON) |
| `/api/prices` | GET | Aktuelle Strompreise (JSON) |
| `/api/forecast` | GET | PV-Prognose (JSON) |
| `/ws` | WebSocket | Echtzeit-Updates für das Dashboard |

---

## Fehlerbehebung

### Add-on startet nicht

1. **Logs prüfen:** Einstellungen → Add-ons → HA Energy Optimizer → **Log**
2. **Konfiguration validieren:** Alle Pflichtfelder (Sensoren, Preisquelle) müssen ausgefüllt sein
3. **HA-Entitäten prüfen:** Die angegebenen Sensor-Entitäten müssen in HA existieren

### PV-Prognose liefert keine Daten

- Koordinaten (`pv_latitude`, `pv_longitude`) müssen korrekt sein
- Internetverbindung prüfen (Open-Meteo API muss erreichbar sein)
- Logs auf HTTP-Fehler prüfen

### Wallbox wird nicht gesteuert

1. `goe_enabled: true` setzen
2. IP-Adresse der Wallbox im lokalen Netz prüfen (`goe_local_ip`)
3. Wallbox-API v2 aktivieren (in go-e App unter Einstellungen)
4. Firewall zwischen HA und Wallbox prüfen

### Strompreise werden nicht geladen

- Bei ENTSO-E: API-Token korrekt? Marktgebiet stimmt?
- Bei Tibber: Token gültig? Rate-Limits beachten
- Fallback: `price_source: fixed` mit `fixed_price_ct_kwh` setzen

### Optimierung läuft langsam

- `optimization_interval_minutes` erhöhen (z. B. auf 120)
- `long_term_plan_interval_hours` erhöhen (z. B. auf 12)
- Genetischer Algorithmus bereits für RPi4 optimiert (50 Population, 100 Generationen ≈ 15-20s)
- LP-Solver verwendet HiGHS Methode (schneller und speichereffizienter als Simplex)

### Hohe Speichernutzung

- Add-on ist für Raspberry Pi 4 mit 8GB RAM optimiert
- Normaler Speicherbedarf: ~150-250 MB
- WebSocket-Clients auf 100 begrenzt (verhindert Memory Leak)
- Historie auf 24h begrenzt (2880 Einträge à 30s)

### Rate Limit Warnungen im Log

- HA API ist auf 100 Requests/Minute begrenzt (schützt Home Assistant)
- Normal bei vielen konfigurierten Sensoren und Lasten
- Falls häufig auftretend: Anzahl der Sensoren reduzieren oder Intervalle erhöhen

---

## Performance-Empfehlungen für Raspberry Pi 4

### ✅ Optimale Konfiguration (getestet auf RPi4 mit 8GB RAM)

| Parameter | Empfohlener Wert | Begründung |
|---|---|---|
| `optimization_interval_minutes` | 60 | Stündliche LP-Optimierung ausreichend |
| `long_term_plan_interval_hours` | 6 | Genetischer Algorithmus braucht ~15-20s CPU-Zeit |
| Realtime Loop | 30s (fest) | EVCC-Style, optimal für Solar-Überschussregelung |
| Price Refresh | 60 min (fest) | Day-Ahead-Preise ändern sich nur einmal täglich |
| WebSocket Clients | Max 100 | Verhindert Memory Leak bei vielen Dashboards |

### ⚡ Ressourcenverbrauch

- **CPU:** ~5-10% idle, ~30-50% während LP/Genetic-Optimierung
- **RAM:** ~150-250 MB (inkl. NumPy/SciPy Arrays)
- **Netzwerk:** ~500-1000 API-Calls/h zu Home Assistant (mit Rate Limiting)

### 🛡️ Sicherheit

- **Version 1.0.1+**: Sicherheitslücke in aiohttp behoben (CVE zip bomb vulnerability)
- **Rate Limiting**: Schutz vor HA-API-Überlastung (100 req/min)
- **Input Validation**: Konfiguration wird beim Laden validiert
- **Error Recovery**: Alle Scheduler-Jobs haben Exception-Handling

---

## Changelog

### Version 1.0.1 (März 2026)

#### 🔒 Sicherheit
- **aiohttp aktualisiert**: Version 3.9.5 → 3.13.3+ (behebt CVE zip bomb vulnerability)
- **Rate Limiting**: HA API auf 100 Requests/Minute begrenzt (schützt vor Überlastung)

#### 🐛 Fehlerbehebungen
- **House Load Prediction**: TODO entfernt, intelligente Lastprofilberechnung aus 24h Historie implementiert
- **Memory Leak**: WebSocket-Client-Liste auf 100 Clients begrenzt
- **Input Validation**: Linear Optimizer warnt bei unvollständigen Prognosen
- **Config Fehlerbehandlung**: Try-Catch für EV Windows und Deferrable Loads
- **Timeout Optimierung**: Open-Meteo API-Timeout von 20s auf 5s reduziert

#### ✨ Verbesserungen
- **Logging erweitert**: Optimierungsentscheidungen detaillierter protokolliert
- **Genetic Algorithm**: Konvergenz-Monitoring alle 20 Generationen
- **EV SOC Warnung**: Meldet fehlende Sensoren wenn Laden konfiguriert
- **Multi-Window Info**: Dokumentiert dass nur erstes EV-Fenster verwendet wird
- **RPi4 Optimierung**: Parameter und Code für Raspberry Pi 4 (8GB) optimiert

#### 📚 Dokumentation
- **Performance-Guide**: Ressourcenverbrauch und Empfehlungen für RPi4
- **Troubleshooting erweitert**: Rate Limiting, Memory, Performance-Probleme
- **Security Section**: Übersicht implementierter Sicherheitsmaßnahmen

### Version 1.0.0 (Initial Release)
- Drei-stufige Optimierung (Realtime + LP + Genetisch)
- Multi-Source Strompreise (ENTSO-E, Tibber, aWATTar, EPEX, HA-Sensor, Festpreis)
- go-e Wallbox Integration (lokal + Cloud)
- Battery Balancing (LiFePO4/Bleiakku)
- WebSocket Dashboard mit Chart.js
- Multi-Architektur Support (aarch64, amd64, armv7, armhf)

---

## Entwicklung & Beitrag

### Lokale Entwicklungsumgebung

```bash
# Repository klonen
git clone https://github.com/ORPA1988/HA-Energy.git
cd HA-Energy/ha-energy-optimizer

# Python-Abhängigkeiten installieren
pip install -r app/requirements.txt

# Entwicklungsserver starten
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
└── app/
    ├── main.py             # FastAPI Applikation & WebSocket
    ├── config.py           # Konfigurationsmanagement
    ├── models.py           # Pydantic Datenmodelle
    ├── ha_client.py        # Home Assistant REST API Client
    ├── scheduler.py        # APScheduler Job-Verwaltung
    ├── requirements.txt    # Python-Abhängigkeiten
    ├── static/
    │   └── index.html      # Web-Dashboard (Chart.js + WebSocket)
    ├── optimizer/
    │   ├── realtime.py     # 30s EV-Steuerung (EVCC-Stil)
    │   ├── linear.py       # 24h Kostenoptimierung (scipy.linprog)
    │   ├── genetic.py      # 48h Energieplanung (genetischer Algorithmus)
    │   ├── ev_strategy.py  # EV-Ladestrategie
    │   └── coordinator.py  # Optimizer-Koordination
    ├── data/
    │   ├── collector.py    # HA-Sensor-Erfassung
    │   ├── prices.py       # Strompreisabfrage
    │   └── forecast.py     # PV-Ertragsprognose
    └── devices/
        ├── goe.py          # go-e Wallbox Integration
        └── battery_balancer.py  # Batterie-Zellenausgleich
```

### Technologie-Stack

| Bereich | Technologie |
|---|---|
| **Backend** | Python 3.11, FastAPI, APScheduler |
| **Optimierung** | SciPy (linprog/HiGHS), NumPy |
| **Datenvalidierung** | Pydantic v2 |
| **HTTP-Client** | HTTPX (async), AIOHTTP |
| **Frontend** | HTML/CSS/JS, Chart.js, WebSocket |
| **Container** | Docker, Alpine Linux 3.18 |
| **HA-Integration** | Supervisor API, Ingress, Add-on Schema |

### Branch Protection & Automatisierung

Für automatisierte Commits (z.B. durch GitHub Copilot/Claude) kann es notwendig sein, die Branch Protection auf dem `main` Branch temporär zu entfernen. Eine detaillierte Anleitung findest du in:
➡️ [BRANCH_PROTECTION_REMOVAL.md](BRANCH_PROTECTION_REMOVAL.md)

### Fehler melden / Feature-Requests

Bitte Issues direkt auf GitHub erstellen:
➡️ [github.com/ORPA1988/HA-Energy/issues](https://github.com/ORPA1988/HA-Energy/issues)

---

*HA Energy Optimizer kombiniert Konzepte aus [EVCC](https://evcc.io/), [EOS](https://github.com/josepowera/eos) und [EMHASS](https://github.com/davidusb-geek/emhass).*
