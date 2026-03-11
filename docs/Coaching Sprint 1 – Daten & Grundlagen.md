# Coaching Sprint 1 – Daten & Grundlagen

## Leitentscheidung

Für Coaching und Offline-Analyse ist `.ibt` ab jetzt die primäre Datenquelle.

- iWAS soll beim Start nach neuen `.ibt`-Dateien suchen, sie importieren und die relevanten Daten kompakt unter `C:\iWAS\data\coaching\` sichern.
- Der bisherige Live-Recorder ist für diesen Teil des Projekts nicht mehr die führende Architektur.
- Live-Recording bleibt als Vorarbeit für späteres echtes Live Coaching erhalten, ist aber nicht mehr die Basis für den Coaching-Datenbestand.

Das Zielbild ist damit klar:

1. `.ibt` finden
2. relevante Session-Daten importieren
3. kompakt persistieren
4. Session, Runs, Pit-Phasen und Lap-Grenzen aus den importierten Daten ableiten
5. Analyse und Visualisierung auf diesen importierten Daten aufbauen

---

## Ziel des Sprints

Sprint 1 schafft die Daten- und Persistenzgrundlage für den IBT-first-Coachingpfad.

Enthalten sind in diesem Zielbild:

- Import-Basis für `.ibt`
- persistente Ablage importierter Coaching-Daten
- Source-/Meta-Nachvollziehbarkeit
- Ableitung von Session, Run, Pit und Lap aus IBT-Daten
- Browser-/Index-Grundlage auf Basis importierter Daten

Nicht enthalten:

- eigentliche Fahranalyse
- Visualisierung pro Corner
- Live Coaching

---

## Architekturüberblick

### Primäre Datenquelle

Primärquelle für Coaching ist eine importierte `.ibt`-Datei, nicht ein parallel mitlaufender Recorder.

Vorteile des Zielbilds:

- Analyse funktioniert auch dann, wenn iWAS beim Fahren nicht lief
- Lat/Lon, `YawNorth` und Streckengeometrie kommen direkt aus IBT
- weniger Dead-Reckoning- und Koordinatensystem-Probleme
- die originale `.ibt` kann später gelöscht werden, die importierten Coaching-Daten bleiben erhalten

### Zielpfad beim App-Start

Beim Start der App soll iWAS:

1. den konfigurierten iRacing-Telemetry-Ordner prüfen
2. neue `.ibt`-Dateien erkennen
3. noch nicht importierte Dateien verarbeiten
4. daraus kompakte Coaching-Artefakte unter `data/coaching/` erzeugen
5. den Coaching-Browser auf Basis dieser importierten Daten aktualisieren

### Persistenzziel

Unter `C:\iWAS\data\coaching\` sollen importierte Sessions in kompakter Form gespeichert werden, zum Beispiel mit:

- Session-Meta
- Source-Meta zur ursprünglichen `.ibt`
- kompakten Telemetrie-Parquets
- abgeleiteten Run-/Lap-/Pit-Informationen
- optionalen, wiederverwendbaren Track-Geometrien

Die konkrete Dateiaufteilung darf sich technisch weiterentwickeln. Führend ist, dass die importierten Artefakte stabil, klein und ohne erneuten Zugriff auf die ursprüngliche `.ibt` nutzbar sind.

---

## Ist-Stand im Repo

Vorhanden sind bereits mehrere Bausteine, die für Sprint 1 weiterverwendet werden können:

- Coaching-View und Coaching-Browser als UI-Grundlage
- Coaching-Storage unter `data/coaching/`
- Settings für Coaching-Storage und iRacing-Telemetry-Ordner
- Run- und Lap-Logik aus dem bisherigen Recorder-Pfad
- Audit-/Analyse-Werkzeuge für Coaching-Artefakte

Wichtig:

- Diese vorhandenen Recorder-Bausteine beschreiben den historischen Weg.
- Für die aktive Umstellung dienen sie nur noch als Referenz oder Übergangspfad.
- Sprint 1 beschreibt künftig den Datenunterbau für den `.ibt`-Import.

---

## Datenbasis aus IBT

Für die Import-Persistenz sollen mindestens die Daten gesichert werden, die für Analyse, Browser und spätere Visualisierung nötig sind.

### Session und Meta

- SessionTime
- SessionState
- SessionUniqueID
- SessionFlags
- Weekend-/SessionInfo-Metadaten
- Track-, Car-, Session- und Zeitinformationen

### Lap-, Run- und Pit-Ableitung

- `Lap`
- `LapCompleted`
- `LapDist`
- `LapDistPct`
- `OnPitRoad`
- `IsOnTrack`
- `IsOnTrackCar`
- weitere für Session-/Run-/Pit-Erkennung benötigte Statuskanäle

### Fahrdynamik und Inputs

- Speed
- Yaw / `YawNorth`
- YawRate
- Pitch / Roll
- Velocity / Accel-Kanäle
- Throttle / Brake / Clutch
- SteeringWheelAngle
- Gear / RPM

### Geometrie und Streckenbezug

- Lat / Lon
- TrackLength
- Track-/Config-Meta
- aus IBT ableitbare Track-Geometrie

Leitlinie:

- Wenn IBT die fachlich bessere Quelle liefert, wird diese bevorzugt dokumentiert.
- Dead-Reckoning ist hier kein Primärkonzept mehr.

---

## Zielbild für Session-, Run- und Lap-Ableitung

Session-Erkennung, Run-Split, Pit-Detection und Lap-Grenzen sollen im Zielbild aus den importierten IBT-Daten ableitbar sein.

### Session

- SessionType aus IBT-Meta normalisieren
- Practice / Qualify / Race sauber unterscheiden
- Session-ID und Zeitbezug persistieren

### Run

- Runs aus Pit-Exit, Session-State und Fahrphasen ableiten
- Race-spezifische Start-/Endlogik aus IBT-Signalen ableiten
- kein führender Architekturzwang mehr, dass der Run live während des Fahrens mitgeschrieben worden sein muss

### Laps

- Lap-Grenzen primär aus Lap-Zählern ableiten
- `LapDistPct`-Wrap nur als Fallback
- Pit-out, Incomplete und Offtrack als abgeleitete Zustände mitpersistieren

---

## Browser und Storage

Der Coaching-Browser soll perspektivisch importierte Sessions anzeigen, unabhängig davon, ob iWAS während der Fahrt lief.

Hierarchie weiterhin fachlich sinnvoll:

```text
Track
└── Car
    └── Event / Session
        └── Run
            └── Lap
```

Die Browser-Struktur basiert dabei auf importierten Artefakten, nicht auf einem laufenden Recorder als Voraussetzung.

---

## Settings

Für den aktiven Zielpfad relevant:

```ini
[coaching_recording]
coaching_storage_dir = C:\iWAS\data\coaching

[iracing]
telemetry_dir = %USERPROFILE%\Documents\iRacing\telemetry
```

Hinweis:

- Die bestehenden Recorder-Settings bleiben technisch bestehen.
- Für die aktive Coaching-Umstellung sind Storage-Pfad und Telemetry-Pfad die führenden Settings.

---

## Verschoben auf später: Live Coaching

Der bisherige Live-Recorder bleibt dokumentarisch bestehen, gehört aber nicht mehr zur aktiven Sprint-1-Zielarchitektur.

Vorerst verschoben:

- IRSDK-Live-Recording als primäre Coaching-Datenquelle
- Recorder-Status als zentrales Element des Coaching-Moduls
- Architekturannahme "Analyse basiert auf parallel mitgeschriebenen Live-Daten"

Später wieder relevant:

- echtes Live Coaching
- In-Session-Hinweise
- Recorder-gestützte Live-Metriken

Dann idealerweise auf derselben fachlichen Datenstruktur wie der Offline-Import, nicht als zweites inkompatibles Modell.

---

## Sprint 1 – Stories

### Story 1.1 – IBT-Discovery und Import-Queue beim App-Start

**Status:** offen

Beim Start der App werden neue `.ibt`-Dateien erkannt, dedupliziert und in eine Import-Queue überführt.

### Story 1.2 – Source-Meta und kompakte Import-Persistenz

**Status:** offen

Pro importierter `.ibt` werden kompakte Coaching-Artefakte unter `data/coaching/` abgelegt, inklusive Herkunftsverweis zur Quell-Datei.

### Story 1.3 – Session-, Run-, Pit- und Lap-Ableitung aus IBT

**Status:** offen

Die bisherige Live-Run-/Lap-Logik wird fachlich in einen IBT-Importpfad überführt, sodass die Zielableitung aus importierten Daten kommt.

### Story 1.4 – Browser-/Index-Modell für importierte Sessions

**Status:** offen

Der Coaching-Browser wird auf importierte Sessions ausgerichtet und darf nicht mehr voraussetzen, dass ein Live-Recorder die Daten zuerst erzeugt hat.

### Story 1.5 – Aufbewahrung und Quell-Lifecycle

**Status:** offen

Nach erfolgreichem Import bleibt der Coaching-Datensatz erhalten, auch wenn die ursprüngliche `.ibt` später gelöscht wurde.

---

## Definition of Done

Sprint 1 ist in diesem Zielbild abgeschlossen, wenn:

- [ ] iWAS erkennt neue `.ibt`-Dateien beim Start
- [ ] jede neue `.ibt` kann einmalig und nachvollziehbar importiert werden
- [ ] importierte Sessions liegen kompakt unter `data/coaching/`
- [ ] Session-Meta und Source-Meta sind sauber persistiert
- [ ] Run-Split, Pit-Phasen und Lap-Grenzen sind aus den IBT-Daten ableitbar
- [ ] der Coaching-Browser kann importierte Sessions ohne Live-Recorder anzeigen
- [ ] die ursprüngliche `.ibt` ist nach dem Import nicht mehr Laufzeitvoraussetzung

Nicht Teil der Definition of Done:

- Live Coaching
- LLM-Interpretation
- Mehrfachvergleich

---

## Nächste sinnvolle Folgearbeiten

### Sprint 1a – Import-Registry und Deduplizierung

- Import-Historie pro `.ibt`
- erneute Imports sicher vermeiden
- Fehlerfälle und Re-Import sauber behandeln

### Sprint 1b – Kanonisches Coaching-Importformat

- verbindliches Meta-/Parquet-Schema für importierte Sessions
- klare Trennung zwischen Rohquelle, Import-Artefakt und Analyse-Artefakt

### Sprint 1c – IBT-basierte Ableitung von Session, Run, Pit und Lap

- heuristische Regeln aus Recorder-Pfad auf IBT-Persistenz übertragen
- Resultate als Index-/Meta-Artefakte ablegen

### Später – Live Coaching

- Live-Recorder nur noch als Ergänzung für Echtzeitfunktionen
- wenn möglich auf dasselbe kanonische Coaching-Schema schreiben
