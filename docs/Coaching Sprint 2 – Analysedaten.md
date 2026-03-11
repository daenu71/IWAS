# Coaching Sprint 2 – Analysedaten

## Leitentscheidung

Sprint 2 beschreibt die Analysepipeline auf Basis importierter Coaching-Daten aus `.ibt`.

- Führende Quelle ist der importierte Coaching-Datensatz unter `data/coaching/`
- Der Live-Recorder ist keine fachliche Voraussetzung mehr für Analyse
- Wenn der Live-Pfad später dieselben Artefakte liefern kann, ist das ein Kompatibilitätsbonus, aber nicht die Architekturannahme dieses Sprints

---

## Ziel des Sprints

Deterministische Datenaufbereitung pro Lap als Grundlage für:

- CornerMap
- Event-Erkennung
- Feature-Berechnung
- spätere Visualisierung
- spätere LLM-Interpretation

Nicht enthalten:

- Coaching-Text
- Bewertung / Scoring
- Multi-Lap-Vergleich

---

## Eingangsmodell

Sprint 2 arbeitet fachlich auf importierten Coaching-Artefakten, nicht auf einem laufenden Stream.

Erwartete Eingänge pro Session / Run / Lap:

- kompakte Telemetrie-Parquets
- Session-Meta
- Run-/Lap-Meta
- optional gespeicherte Track-Geometrie

Das Ziel ist, dass diese Eingänge direkt aus dem `.ibt`-Import kommen oder über einen klaren Adapter daraus erzeugt werden.

---

## Ist-Stand im Repo

Für Sprint 2 sind bereits wesentliche Analysebausteine im Code vorhanden:

- `analysis_contract.py`
- `feature_schema.py`
- `resample_lapdist.py`
- `corner_map.py`
- `event_engine.py`
- `feature_engine.py`
- `analysis_cache.py`
- `lap_analyzer.py`

Wichtig für die Dokumentation:

- Diese Module sind die Basis des Analysepfads.
- Dokumentiert wird hier aber das Zielbild "Importdaten aus IBT rein, Analyseartefakte raus".
- Historische Kopplungen an den alten Recorder-Pfad sind Übergangstechnik und nicht der führende fachliche Stand.

---

## Analysepipeline

### 1. Contract und Schema

Analyse braucht einen stabilen Kanalvertrag und ein versioniertes Feature-Schema.

Ist-Stand:

- Analysevertrag und Feature-Schema sind als eigene Module / JSONs vorhanden

Zielbild:

- der `.ibt`-Import liefert oder mappt genau die Kanäle, die die Analyse braucht
- fehlende optionale Kanäle führen zu `partial`, nicht zu Architekturbruch

### 2. Deterministisches Resampling

Pro Lap wird ein gleichmäßiges `LapDistPct`-Grid erzeugt.

Leitlinie:

- deterministisch
- wiederholbar
- unabhängig davon, ob die Quelle live oder importiert war

### 3. CornerMap

CornerMap ist ein persistenter, trackbezogener Analysebaustein.

Im IBT-first-Zielbild gilt:

- CornerMap basiert auf den importierten Daten
- Track-Key und Track-Geometrie kommen aus Session-/Track-Meta des Imports
- keine führende Abhängigkeit mehr von live rekonstruierter Geometrie

### 4. Event Engine

Events werden pro Lap deterministisch aus den resampleten importierten Daten extrahiert.

### 5. Feature Engine

Features werden pro Corner aus:

- resampleter Lap
- Event-Daten
- CornerMap
- Schema-/Contract-Versionen

berechnet und gespeichert.

### 6. Analysis Cache

Der Cache orchestriert Recompute, Stale Detection und Artefaktstatus.

Für die aktive Umstellung ist wichtig:

- Der Cache soll auf importierten Artefakten arbeiten können
- Status und Invalidierung müssen unabhängig vom Recorder-Pfad funktionieren

---

## Story-Status

### Story 2.0 – Verträge und Schemata

**Ist-Stand:** Basis vorhanden

- Analysis Contract und Feature Schema existieren
- diese Bausteine passen fachlich zum IBT-first-Zielbild

### Story 2.1 – Resampling

**Ist-Stand:** Basis vorhanden

- deterministische LapDist-Resampling-Bausteine sind vorhanden

### Story 2.2 – CornerMap

**Ist-Stand:** Basis vorhanden

- CornerMap-Modul existiert
- für das Zielbild muss die CornerMap sauber aus importierten Sessions gespeist werden

### Story 2.3 – Event Engine

**Ist-Stand:** Basis vorhanden

### Story 2.4 – Feature Engine

**Ist-Stand:** Basis vorhanden

### Story 2.5 – Analysis Cache / Orchestrierung

**Ist-Stand:** Basis vorhanden

### Story 2.6 – Direkte Anbindung an den künftigen IBT-Importpfad

**Status:** offen

Diese Story ist für die aktive Umstellung zentral:

- Analyse darf nicht davon abhängen, dass alte Recorder-Artefakte zuerst vorhanden sind
- der künftige `.ibt`-Import muss entweder
  - direkt das von Sprint 2 erwartete Artefaktmodell schreiben oder
  - einen klaren, kleinen Adapter in dieses Modell bereitstellen

### Story 2.7 – Regression-Fälle auf echten importierten IBT-Sessions

**Status:** offen

Die vorhandene Pipeline soll mit echten importierten Sessions validiert werden, insbesondere für:

- Session-/Run-/Lap-Ableitung
- Coverage
- Corner-Stabilität
- Partial-/Blocked-Fälle

---

## Nicht mehr führend

Folgende Annahmen sollen Sprint 2 nicht mehr prägen:

- "Analyse startet erst, wenn der Live-Recorder eine Session aufgezeichnet hat"
- "Track-Geometrie wird primär aus Dead-Reckoning im Analysepfad gewonnen"
- "Coaching-Analyse setzt voraus, dass iWAS während des Fahrens lief"

Diese Punkte sind höchstens Übergangskompatibilität.

---

## Definition of Done

Sprint 2 ist aus Sicht der aktiven Umstellung abgeschlossen, wenn:

- [ ] die Analysepipeline direkt auf importierten Coaching-Daten aus `.ibt` laufen kann
- [ ] Contract, Schema, Resampling, CornerMap, Events, Features und Cache konsistent zusammenspielen
- [ ] Partial-/Blocked-Zustände sauber aus importierten Daten entstehen
- [ ] CornerMap und Analyseartefakte einen stabilen Track-Key aus dem Import nutzen
- [ ] die Pipeline nicht mehr unnötig den alten Live-Recorder als führende Quelle suggeriert

Ein vorhandener Recorder-Kompatibilitätspfad ist erlaubt, aber nicht Teil des fachlichen Erfolgsmaßstabs.

---

## Nächste sinnvolle Folgearbeiten

### Sprint 2a – Import-zu-Analyse-Adapter

- definiert den Übergang vom `.ibt`-Importformat in das Analyseformat
- minimiert Sonderlogik im `lap_analyzer`

### Sprint 2b – Analyse auf importierten Sessions als Batch

- neue oder geänderte Imports automatisch analysieren
- Status im Browser / Index nachziehen

### Sprint 2c – Qualitäts- und Regressionssuite mit echten IBT-Fällen

- mehrere Strecken
- mehrere Session-Typen
- Pit-out, Inlap, Offtrack, Teleport-/Abbruchfälle

### Sprint 2d – Entkopplung vom historischen Recorder-Pfad

- verbliebene Pfadannahmen bereinigen
- alte Spezialfälle nur als Kompatibilitätsschicht behalten
