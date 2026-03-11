# Coaching Sprint 3 – Visualisierung (Single Lap)

## Leitentscheidung

Sprint 3 visualisiert eine einzelne analysierte Lap im IBT-first-Zielbild.

- Führende Datenbasis ist eine importierte und analysierte Coaching-Session aus `.ibt`
- TrackMap und Fahrlinie sollen fachlich auf IBT-Geometrie, Lat/Lon und gespeicherter Track-Geometrie aufbauen
- Dead-Reckoning bleibt nur Fallback, nicht Primärlösung

Wichtig für den Status dieses Dokuments:

- **Nur Story 3.3a ist abgeschlossen**
- **Alle anderen Sprint-3-Stories sind offen**
- Bereits vorhandene Prototypen oder Teilimplementierungen im Repo zählen hier nicht automatisch als erledigt

---

## Ziel

Analysierte Lap-Daten grafisch darstellen.

Nicht enthalten:

- Multi-Lap-Vergleich
- Live Coaching
- LLM-Bewertung

---

## Architekturrahmen

### LapViewModel

Die Visualisierung soll über ein zentrales `LapViewModel` laufen.

Zielbild:

- eine Lap laden
- Analyseartefakte und Meta bereitstellen
- gespeicherte Track-Geometrie einbinden
- Visualisierungs-UI von Dateistrukturdetails entkoppeln

### TrackMap und Geometrie

Führend ist künftig:

1. gespeicherte IBT-basierte Track-Geometrie
2. gespeicherte Fallback-Geometrie
3. nur wenn beides fehlt: Dead-Reckoning-Fallback

Damit wird die Visualisierung fachlich auf die robustere Quelle ausgerichtet und nicht auf eine fehleranfällige Rekonstruktion als Primärannahme festgeschrieben.

### Corner-Zoom, Trace und Scorecard

Diese UI-Bausteine sind weiterhin fachlich sinnvoll, gelten in Sprint 3 aber erst als erledigt, wenn sie sauber auf dem IBT-first-Datenpfad abgenommen sind.

---

## Story-Status

| Story | Thema | Status | Einordnung |
|---|---|---|---|
| 3.0 | `LapViewModel` als Datenzugriffsschicht | offen | Zielbild bleibt gültig, aber noch nicht als abgeschlossen dokumentiert |
| 3.1 | TrackMap Canvas | offen | soll IBT-Geometrie priorisieren, Dead-Reckoning nur als Fallback |
| 3.2 | `CoachingDetailView` | offen | vorhandene UI-Arbeiten gelten hier nicht als Sprint-Abnahme |
| 3.3 | Corner-Zoom mit Event-Markern | offen | fachlich weiter sinnvoll, noch nicht erledigt |
| 3.3a | IBT-Extraktor für `TrackRoadGeometry` | **abgeschlossen** | einzig abgeschlossene Sprint-3-Story |
| 3.4 | Telemetrie-Trace | offen | noch nicht als erledigt markieren |
| 3.5 | Corner Scorecard | offen | noch nicht als erledigt markieren |
| 3.6 | Feature-Heatmap | offen | noch nicht als erledigt markieren |
| 3.7 | Analyse-Trigger / Öffnen der Detail-View | offen | noch nicht als erledigt markieren |

---

## Stories

### Story 3.0 – LapViewModel: Datenzugriffs-Layer

**Status:** offen

Das `LapViewModel` bleibt die vorgesehene Datenzugriffsschicht für die Single-Lap-Visualisierung.

Für das Zielbild wichtig:

- lädt Analyseartefakte einer importierten Session
- kennt Source-Meta und Track-Meta
- bindet gespeicherte Track-Geometrie null-safe ein

### Story 3.1 – TrackMap Canvas

**Status:** offen

Die TrackMap soll fachlich zum IBT-first-Pfad passen.

Leitlinie:

- primär gespeicherte IBT-Track-Geometrie oder direkt aus importierten IBT-Daten ableitbare Geometrie nutzen
- keine Primärfestlegung auf Dead-Reckoning
- Fallback nur für fehlende Geometrie

### Story 3.2 – CoachingDetailView

**Status:** offen

Die Detail-Ansicht bleibt als Ziel-Layout richtig, ist in diesem Sprintdokument aber nicht als abgeschlossen zu führen.

### Story 3.3 – Corner-Zoom Canvas mit Event-Markern

**Status:** offen

Corner-Zoom bleibt fachlich Bestandteil von Sprint 3, ist aber noch offen.

### Story 3.3a – IBT-Extraktor: TrackRoadGeometry aus IBT-Dateien

**Status:** abgeschlossen

Abgeschlossen ist der Baustein, der Track-Geometrie aus `.ibt` extrahiert und persistent speichert.

Erledigt im größeren Architekturkontext:

- `.ibt` als Quelle für robuste Streckengeometrie
- persistente Ablage unter `data/coaching/track_geometries/`
- Kennzeichnung der Quelle über `source_type`
- Grundlage dafür, dass spätere TrackMap-Ansichten nicht primär auf Dead-Reckoning beruhen müssen

Diese Story ist damit sowohl für Sprint 3 als auch als Vorarbeit für Sprint 4 relevant.

### Story 3.4 – Telemetrie-Trace

**Status:** offen

### Story 3.5 – Corner Scorecard

**Status:** offen

### Story 3.6 – Feature-Heatmap auf TrackMap

**Status:** offen

### Story 3.7 – Analyse-Trigger: Klick auf Lap öffnet Detail-View

**Status:** offen

---

## Definition of Done

Der Sprint ist erst abgeschlossen, wenn die Single-Lap-Visualisierung sauber auf dem IBT-first-Datenpfad funktioniert.

Aktueller Stand der DoD-Checkliste:

- [ ] `LapViewModel.load()` ist für importierte Analyseartefakte fachlich abgeschlossen
- [ ] TrackMap rendert stabil für importierte Sessions
- [ ] gespeicherte IBT-Track-Geometrie wird priorisiert genutzt
- [ ] Dead-Reckoning ist nur Fallback
- [ ] Corner-Zoom ist fachlich fertig und abgenommen
- [x] IBT-Extraktor für `TrackRoadGeometry` ist vorhanden
- [ ] Telemetrie-Trace ist fertig
- [ ] Scorecard ist fertig
- [ ] Heatmap ist fertig
- [ ] Öffnen der Detail-View aus dem Browser ist fachlich abgeschlossen

---

## Nächste sinnvolle Reihenfolge

### 1. Story 3.1 fertigziehen

Zuerst die TrackMap fachlich sauber auf gespeicherte IBT-Geometrie ausrichten.

### 2. Story 3.0 und 3.7 konsolidieren

Danach den Ladepfad von Browser zu `LapViewModel` und Detail-View stabilisieren.

### 3. Story 3.3 abschließen

Erst danach lohnt es sich, den Corner-Zoom als belastbaren UI-Baustein abzunehmen.

### 4. Story 3.4 und 3.5

Trace und Scorecard auf dem stabilen Single-Lap-Datenmodell aufsetzen.

### 5. Story 3.6 zuletzt

Heatmap ist ein Zusatzlayer und sollte erst kommen, wenn TrackMap, Corner-Zoom und Feature-Anbindung sauber stehen.

---

## Folge-Sprints nach Sprint 3

### Sprint 5 – Vollständiger IBT-Import beim App-Start

- neue `.ibt`-Dateien automatisch erkennen
- komplette Coaching-Sessions importieren
- Browser und Index aktualisieren

### Sprint 6 – Session-, Run-, Pit- und Lap-Ableitung aus Importdaten

- Zielableitung vollständig aus importierten IBT-Daten
- kein fachlicher Vorrang des alten Recorder-Pfads mehr

### Sprint 7 – Visualisierung vollständig auf Importdaten

- alle UI-Pfade laden aus importierten Coaching-Artefakten
- kein impliziter Rückgriff auf historische Recorder-Annahmen

### Später – Live Coaching

- Recorder wieder aktiv relevant
- dann aber als eigener Echtzeitpfad, nicht als Primärquelle für Offline-Coaching
