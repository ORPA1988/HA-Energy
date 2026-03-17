# Changelog

## 0.1.0

- **Auto-Erkennung**: Automatische Erkennung von HA-Entitäten (Sensoren, Switches) mit Confidence-Bewertung
- **Bedingte Felder**: Nicht relevante Konfigurationsfelder werden je nach Auswahl ausgeblendet
- **Preiskonfiguration**: Dedizierte Sektionen für aWATTar, Tibber, EPEX Spot, Sensor
- **PV-Prognose**: Forecast-Source Auswahl (Auto/Solcast/Open-Meteo) mit Solcast-Konfiguration
- **Batterie-Balancing UI**: Vollständige Konfigurationsoberfläche für Balancing-Parameter
- **Benachrichtigungen UI**: Konfiguration von Benachrichtigungszielen und Auslösern
- **go-e Cloud**: Cloud-Verbindungsfelder (Serial, Token) bei Cloud-Modus
- **Wallbox-Sichtbarkeit**: Wallbox-Konfiguration nur sichtbar wenn aktiviert
- **Versionierung**: Umstellung auf 0.x Versionierung (Pre-Release)

## 0.0.3

- **Read-Only Modus**: Sicheres Testen ohne aktive Steuerung
- **MCP-Server**: 17 Tools für Claude Code / Cursor Integration
- **Bug-Fixes**: 6 kritische Bugs behoben (Config-Crash, LP-Constraint, Cache-Timing, Multi-EV SOC)
- **Multi-EV Dashboard**: Alle Wallboxen live im Dashboard
- **Lastzerlegung**: Visualisierung im Dashboard (Grundlast vs. steuerbar)
- **Config-Validierung**: Prüfung mit Fehlern/Warnungen im Settings-Tab
- **Logging**: Rotierende Logdatei für MCP-Server und Debugging

## 0.0.2

- **EMHASS Backend**: Optionaler EMHASS-Optimizer als Drop-in Ersatz für eingebauten LP
- **Multi-EV**: Unterstützung mehrerer Wallboxen (go-e, HA Entity, OCPP)
- **Wallbox-Abstraktion**: Einheitliche Schnittstelle für verschiedene Wallbox-Typen
- **Lastzerlegung**: Grundlast-Berechnung durch Subtraktion steuerbarer Lasten

## 0.0.1

- Erstveröffentlichung
- Dreistufige Optimierung (Realtime 30s, LP stündlich, Genetisch 6h)
- Live-Dashboard mit WebSocket
- PV-Prognose via Open-Meteo
- Strompreise: ENTSO-E, Tibber, aWATTar, EPEX Spot, HA-Sensor, Festpreis
- go-e Wallbox Integration
- Batterie-Balancing
- Steuerbare Lasten (Waschmaschine, Spülmaschine, etc.)
- EPEX-Entity Direktanbindung
- Web-GUI Konfiguration mit Entity-Picker
