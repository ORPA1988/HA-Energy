# Changelog

## 1.0.2

- **Read-Only Modus**: Sicheres Testen ohne aktive Steuerung
- **MCP-Server**: 17 Tools für Claude Code / Cursor Integration
- **Bug-Fixes**: 6 kritische Bugs behoben (Config-Crash, LP-Constraint, Cache-Timing, Multi-EV SOC)
- **Multi-EV Dashboard**: Alle Wallboxen live im Dashboard
- **Lastzerlegung**: Visualisierung im Dashboard (Grundlast vs. steuerbar)
- **Config-Validierung**: Prüfung mit Fehlern/Warnungen im Settings-Tab
- **Logging**: Rotierende Logdatei für MCP-Server und Debugging

## 1.0.1

- **EMHASS Backend**: Optionaler EMHASS-Optimizer als Drop-in Ersatz für eingebauten LP
- **Multi-EV**: Unterstützung mehrerer Wallboxen (go-e, HA Entity, OCPP)
- **Wallbox-Abstraktion**: Einheitliche Schnittstelle für verschiedene Wallbox-Typen
- **Lastzerlegung**: Grundlast-Berechnung durch Subtraktion steuerbarer Lasten

## 1.0.0

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
