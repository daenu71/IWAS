# Coaching Sprint 4 – iRacing Telemetry Daten für Track Map

## Status

**Sprint 4 ist abgeschlossen.**

Dieser Sprint deckt den Track-Map-spezifischen Teil der IBT-Umstellung ab:

- Telemetry-Ordner konfigurierbar machen
- `.ibt` für eine Strecke finden und daraus persistente Track-Geometrie erzeugen
- diese Geometrie im Analysepfad nachziehen
- gespeicherte Geometrie in der TrackMap priorisieren

Wichtig:

- Sprint 4 bedeutet **nicht**, dass die komplette Coaching-Pipeline schon auf `.ibt` umgestellt ist
- abgeschlossen ist der Teilpfad für **Track-Geometrie und TrackMap-Quelle**

---

## Einordnung in die Gesamtarchitektur

Sprint 4 ist ein abgeschlossener Baustein innerhalb der größeren IBT-first-Umstellung.

Er liefert bereits einen zentralen Vorteil des neuen Zielbilds:

- Track-Geometrie kommt aus `.ibt`
- TrackMap muss nicht primär auf Dead-Reckoning beruhen
- die Geometrie wird persistent gespeichert und wiederverwendet

Damit ist Sprint 4 die fertige Grundlage für eine robustere Visualisierung, aber noch nicht der vollständige IBT-Import aller Coaching-Daten.

---

## Erledigte Stories

### Story 4.1 – Settings: iRacing Telemetry Ordner konfigurieren

**Status:** abgeschlossen

Erledigt:

- konfigurierbarer iRacing-Telemetry-Ordner in den Settings
- Default auf den üblichen iRacing-Telemetry-Pfad
- persistente Speicherung
- zentraler Zugriff aus der Runtime

Architektureinordnung:

- schafft die Basis für spätere `.ibt`-Discovery
- ist Voraussetzung für automatische und manuelle IBT-Importpfade

### Story 4.2 – Track-Geometrie aus `.ibt` erzeugen und speichern

**Status:** abgeschlossen

Erledigt:

- Scan nach passenden `.ibt`-Dateien im konfigurierten Telemetry-Ordner
- robuste Track-Zuordnung über Track-Meta
- Extraktion und Persistenz unter `C:\iWAS\data\coaching\track_geometries\<track_key>\track_road_geometry.json`
- klare Quellenkennzeichnung über `source_type`
- vorhandene echte IBT-Geometrie wird nicht blind überschrieben
- vorhandene Fallback-Geometrie kann sauber ersetzt werden

Architektureinordnung:

- liefert wiederverwendbare Track-Geometrie als persistentes Artefakt
- trennt echte IBT-Geometrie klar von Fallback-Geometrie

### Story 4.3 – Nach Analyse prüfen, ob echte `.ibt`-Geometrie nachgezogen werden kann

**Status:** abgeschlossen

Erledigt:

- Hook im Analysepfad
- bei erfolgreicher Lap-Analyse wird geprüft, ob für die Strecke bereits eine echte Geometrie vorliegt
- wenn nicht, wird ein Importversuch aus dem Telemetry-Ordner gestartet
- falls keine passende `.ibt` existiert, bleibt das fehlerfrei und wird nur geloggt

Architektureinordnung:

- ist ein Übergangshook
- sorgt dafür, dass Track-Geometrie bereits heute in den Analysefluss nachrückt
- ersetzt noch nicht das spätere Zielbild "IBT-Import beim App-Start für komplette Sessions"

### Story 4.4 – Trackmap-Priorität: gespeicherte Geometrie vor Fallback

**Status:** abgeschlossen

Erledigt:

- gespeicherte IBT-Geometrie wird für die TrackMap priorisiert
- gespeicherte Fallback-Geometrie ist zweite Stufe
- Dead-Reckoning bleibt nur der letzte Fallback
- Logs weisen die konkrete Quelle aus

Architektureinordnung:

- macht die Visualisierung fachlich IBT-first-kompatibel
- reduziert die Abhängigkeit von fehleranfälliger Rekonstruktion

---

## Was Sprint 4 konkret fertiggestellt hat

Nach Sprint 4 ist für die TrackMap-Kette erledigt:

1. Telemetry-Verzeichnis ist konfigurierbar
2. passende `.ibt`-Dateien können gefunden werden
3. Track-Geometrie kann daraus persistent erzeugt werden
4. Analyse kann diese Geometrie nachziehen
5. Visualisierung priorisiert die gespeicherte Geometrie sauber vor Fallbacks

Das ist abgeschlossen und muss in der Doku nicht mehr als offen geführt werden.

---

## Was Sprint 4 bewusst noch nicht erledigt

Offen bleibt weiterhin:

- vollständiger Import neuer `.ibt`-Dateien beim App-Start
- Persistenz kompletter Coaching-Sessions aus `.ibt`
- Session-Erkennung, Run-Split, Pit-Detection und Lap-Grenzen direkt aus den importierten IBT-Daten
- vollständige Analysepipeline direkt auf importierten Sessions
- Umstellung des Coaching-Browsers auf einen IBT-importierten Primärbestand

Diese Punkte gehören in die nächsten Sprints der Gesamtumstellung.

---

## Definition of Done

Sprint 4 ist abgeschlossen, weil folgende Punkte erfüllt sind:

- [x] Telemetry-Ordner ist konfigurierbar
- [x] Track-Geometrie kann offline aus `.ibt` importiert werden
- [x] Quelle der gespeicherten Geometrie ist eindeutig markiert
- [x] vorhandene echte Geometrie wird nicht still überschrieben
- [x] Analysepfad kann fehlende Geometrie nachziehen
- [x] TrackMap priorisiert gespeicherte Geometrie vor Fallback
- [x] Dead-Reckoning ist für die TrackMap nicht mehr die bevorzugte Quelle

---

## Nächste sinnvolle Folge-Sprints

### Sprint 5 – IBT-Discovery und Vollimport beim App-Start

- neue `.ibt`-Dateien automatisch erkennen
- Import nicht nur für Track-Geometrie, sondern für komplette Coaching-Sessions
- Import-Historie und Deduplizierung

### Sprint 6 – Kanonisches Coaching-Importformat

- kompakte Persistenz pro importierter Session
- Meta, Source-Meta, Run-/Lap-Index und Analyseeingänge aus einem einheitlichen Importmodell

### Sprint 7 – Session-, Run-, Pit- und Lap-Ableitung aus IBT

- Zielableitung vollständig aus importierten Daten
- Recorder-Regeln nur noch als Referenz oder späterer Live-Pfad

### Sprint 8 – Analyse vollständig auf IBT-Importpfad

- Sprint-2-Pipeline direkt auf importierten Sessions betreiben
- Browser-, Status- und Cache-Logik auf den Importpfad ausrichten

### Später – Live Coaching

- Live-Recorder wieder aktiv relevant
- aber nur für Echtzeitfunktionen, nicht mehr als führende Offline-Datenquelle
