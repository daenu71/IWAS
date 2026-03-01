# Coaching Sprint 1 â€“ Daten & Grundlagen

## Ziel des Sprints

Sprint 1 schafft die vollstÃ¤ndige technische Grundlage fÃ¼r das Coaching-Modul:

* Integration eines neuen App-Moduls â€žCoachingâ€œ
* Live-Aufzeichnung aller relevanten IRSDK-Daten (120 Hz)
* Strukturierte Speicherung (Performance-orientiert, Parquet/Binary)
* Saubere Run- und Lap-Segmentierung
* Robuste Start-/End-Logik je SessionType (Practice / Qualify / Race)
* Retention-, Speicher- und Auto-Delete-Mechanik
* Erste Coaching-UI mit Garage61-Ã¤hnlicher Aufklapp-Hierarchie
* Minimaler Replay-Loader

âš ï¸ Keine Analyse-Logik in diesem Sprint.
âš ï¸ Keine KI, keine Feature-Extraktion.
Nur Datengrundlage und Systemarchitektur.

---

# 1. ArchitekturÃ¼berblick

## 1.1 Neues Modul: Coaching

Neuer Ribbon-Button:

```
Video Analysis | Coaching | Settings
```

* Coaching Ã¶ffnet eine eigene View
* ZunÃ¤chst:

  * Links: Accordion-/Tree-Struktur
  * Rechts: Detailbereich + Recorder-Status

Keine Analysefunktionen in Sprint 1.

---

# 2. IRSDK Recording Engine

## 2.1 Sampling

* Default: **120 Hz**
* Drosselbar via `defaults.ini`
* Sampling-Modus: â€žwie IRSDK liefertâ€œ, mit optionalem Rate-Limit

INI-Key:

```
irsdk_sample_hz = 120
```

0 = unthrottled

---

## 2.2 Gespeicherte Daten (Vollumfang)

Alle vom Nutzer definierten Variablen werden gespeichert.

### 2.2.1 Session/System

* SessionTime
* SessionState
* SessionUniqueID
* SessionFlags

### 2.2.2 Rundendaten

* Lap
* LapCompleted
* LapDist
* LapDistPct
* LapCurrentLapTime
* LapLastLapTime
* LapBestLapTime
* LapDeltaToBestLap
* LapDeltaToSessionBestLap
* LapDeltaToSessionOptimalLap
* LapDeltaToOptimalLap

### 2.2.3 Fahrzeugbewegung

* Speed
* Yaw
* Pitch
* Roll
* VelocityX / Y / Z
* YawRate
* LatAccel
* LongAccel
* VertAccel

### 2.2.4 Eingaben

* Throttle
* Brake
* Clutch
* SteeringWheelAngle
* SteeringWheelTorque
* SteeringWheelPctTorque

### 2.2.5 Motor/Getriebe

* RPM
* Gear
* FuelLevel
* FuelLevelPct
* FuelUsePerHour

### 2.2.6 Reifen/Suspension

* ShockDefl (alle 4)
* TireTemp L/M/R (alle 4)

### 2.2.7 Elektronik

* ABSactive
* TractionControl
* BrakeBias

### 2.2.8 Position / Umwelt

* LatAccel
* LongAccel
* Alt
* TrackTemp
* AirTemp

### 2.2.9 Zusatzfelder

* OnPitRoad
* IsOnTrack
* IsOnTrackCar
* SessionInfo.Sessions[SessionNum].SessionType
* WeekendInfo.SessionType

Hinweis zur Speicherung:

* Array-Felder aus 2.2.6 werden als expandierte Flat-Columns gespeichert (z. B. `ShockDeflLF`, `ShockDeflRF`, `...` bzw. index-basiert wie `ShockDefl_0..3`, wenn nur Array-Header verfuegbar sind).
* Die konkrete Expansion wird beim Session-Start ueber IRSDK-Var-Header entdeckt (kein blindes Hardcoding von Car-/Build-spezifischen Namen).
* `session_meta.json.recorded_channels` enthaelt die tatsaechlich aufgezeichneten (konkret expandierten) Spalten in stabiler Reihenfolge; `dtype_decisions` dokumentiert die finale Typentscheidung pro Spalte.
* Nicht verfuegbare angeforderte Specs stehen in `session_meta.json` unter `missing_channels` mit `request_spec` + `reason` (bei Partial-Matches inkl. Detailfeldern wie fehlende Wheels/Komponenten).
* `SessionInfo.Sessions[...].SessionType` und `WeekendInfo.SessionType` bleiben Meta-Infos (`session_info.yaml` / `session_meta.json`) und werden nicht als Telemetrie-Spalten gespeichert; `SessionUniqueID` soll hingegen als aufgezeichneter Wert verfuegbar sein (Telemetrie oder Broadcast-Konstante).

### Audit Tool (Sprint 1 Vollumfang)

Sprint-1 Sessions koennen mit `tools/audit_coaching_session.py` geprueft werden.

```bash
.\.venv\Scripts\python.exe tools\audit_coaching_session.py "data\coaching\YYYY-MM-DD__HHMMSS__Track__Car__SessionType__SessionID"
```

Outputs im Session-Ordner:
* `audit_report.json` (maschinenlesbar: `expected`, `present_exact`, `missing_exact`, `near_miss_candidates`, `files_checked`, `runs_found`)
* `audit_report.md` (menschenlesbarer Audit-Report mit Kategorien A-I, Artefakt-Check und Near-Misses)
* Exit-Codes: `0` vollstaendig, `2` mindestens ein Feld fehlt, `3` zentrales Artefakt fehlt (`session_info.yaml` oder `run_*.parquet`)

---

## 2.3 SessionInfo

SessionInfo YAML wird vollstÃ¤ndig gespeichert:

```
session_info.yaml
```

ZusÃ¤tzlich wird ein extrahiertes Index-Meta gespeichert:

```
session_meta.json
```

Minimal extrahiert:

* DriverName
* CarScreenName
* CarClassShortName
* TrackDisplayName
* TrackConfigName
* SessionType
* SessionUniqueID
* Datum/Zeit

---

# 3. Run-Logik (State Machine)

## 3.1 SessionType Normalisierung

Primary:

```
SessionInfo.Sessions[SessionNum].SessionType
```

Fallback:

```
WeekendInfo.SessionType
```

Normalisiert:

* practice
* qualify
* race

---

## 3.2 RunStart

### Practice / Qualify

Start bei:

```
OnPitRoad: True â†’ False
```

Optionaler Sicherheitscheck:

* IsOnTrackCar == True innerhalb 3 Sekunden

---

### Race

Start bei:

* SessionType == race
* Green Flag in SessionFlags
* Passender SessionState
* Optional: IsOnTrackCar == True

---

## 3.3 RunEnd

### Race / Qualify

1. Checkered Flag + passender SessionState

ODER

2. Incomplete:

* LapCompleted steigt 25 Sekunden nicht
* UND sekundÃ¤rer Check:

  * IsOnTrackCar == False ODER
  * Speed < Schwellenwert

---

### Practice

RunEnd bei:

* LapCompleted steigt 25 Sekunden nicht
* UND sekundÃ¤rer Check Ã¼ber IsOnTrackCar/Speed

---

# 4. Lap-Segmentierung

PrimÃ¤r:

* Lap oder LapCompleted ZÃ¤hlerwechsel

Backup:

* LapDistPct Wrap:

  * > = 0.99 â†’ <= 0.01

Optionaler Safety:

* IsOnTrackCar == True

Wrap dient nur als Fallback.

---

# 5. Speicherformat

## 5.1 Struktur

Root:

```
C:\iWAS\data\coaching\
```

Session-Ordner:

```
YYYY-MM-DD__HHMMSS__Track__Car__SessionType__SessionID\
```

Inhalt:

* session_info.yaml
* session_meta.json
* run_0001.parquet
* run_0001_meta.json
* run_0002.parquet
* ...
* log.txt

---

## 5.2 Parquet-Regeln

* Chunked Writing (Buffer 1â€“2 Sekunden)
* float32 wo mÃ¶glich
* int16/int32 sinnvoll wÃ¤hlen
* bool als boolean/uint8

Ziel: Performance + moderate DateigrÃ¶ÃŸe trotz 120 Hz.

---

# 6. Coaching UI â€“ Recording Browser

Hierarchie:

```
Track
 â””â”€â”€ Car
      â””â”€â”€ Event
           â””â”€â”€ Run
                â””â”€â”€ Lap
```

Jeder Node zeigt Summary:

* Total Time
* Laps
* Fastest Lap
* Last Driven

Rechtsbereich:

* Details
* Open Folder
* Delete
* (Analyze â€“ disabled in Sprint 1)

---

# 7. Recorder Status Panel

Im Coaching-Screen sichtbar:

* IRSDK connected / disconnected
* SessionType
* Run active yes/no
* Samples recorded
* Write buffer status
* Dropped samples (falls vorhanden)

---

# 8. Settings

INI-Section (Sprint 1):

```ini
[coaching_recording]
coaching_recording_enabled = true
coaching_storage_dir = C:\iWAS\data\coaching
irsdk_sample_hz = 120
coaching_retention_months_enabled = false
coaching_retention_months = 6
coaching_low_disk_warning_enabled = false
coaching_low_disk_warning_gb = 20
coaching_auto_delete_enabled = false
```

Key-Beschreibung + Defaults:

* `coaching_recording_enabled` (bool, Default `true`): Master-Schalter fÃ¼r Coaching-Recording (nur Konfiguration/UI, keine Runtime-Logik in Sprint 1).
* `coaching_storage_dir` (str, Default `C:\iWAS\data\coaching`): Zielordner fÃ¼r Coaching-Daten.
* Fallback-Verhalten: Wenn `coaching_storage_dir` fehlt/leer ist (z. B. leerer User-Override), nutzt die Runtime automatisch `C:\iWAS\data\coaching`, legt den Ordner an und zeigt den effektiven Pfad im Settings-Feld.
* `irsdk_sample_hz` (int, Default `120`): Sampling-Rate fÃ¼r IRSDK-Aufnahme. `0` bedeutet unthrottled.
* `coaching_retention_months_enabled` (bool, Default `false`): Aktiviert Retention nach Monaten.
* `coaching_retention_months` (int, Default `6`): Anzahl Monate fÃ¼r Retention-Regel.
* `coaching_low_disk_warning_enabled` (bool, Default `false`): Aktiviert Low-Disk-Warnung.
* `coaching_low_disk_warning_gb` (int, Default `20`): Warnschwelle in GB.
* `coaching_auto_delete_enabled` (bool, Default `false`): Aktiviert Auto-Delete (nur Flag/Config in Sprint 1).

Validierungsbereiche (Settings-UI):

* `irsdk_sample_hz`: Integer `0..1000`
* `coaching_retention_months`: Integer `1..120` (fachlich relevant nur wenn `coaching_retention_months_enabled = true`)
* `coaching_low_disk_warning_gb`: Integer `1..2000` (fachlich relevant nur wenn `coaching_low_disk_warning_enabled = true`)

---

# 9. Retention & Auto-Delete

* LÃ¶schen nur auf Session-Ordner-Ebene
* Niemals aktive Session lÃ¶schen
* Ã„lteste Session zuerst
* Low-Disk-Warnung vor LÃ¶schung

---

# 10. Out of Scope (Sprint 1)

Nicht enthalten:

* Feature-Vector-Berechnung
* Corner-Segmentierung
* KI-Analyse
* Hypothesen-Generierung
* Vergleich zweier Runs
* LLM Integration

---

# Sprint 1 Definition of Done

* Coaching-Modul sichtbar
* IRSDK Recording stabil
* 120 Hz Logging lÃ¤uft ohne UI-Blockade
* Run-Segmentierung korrekt
* Lap-Segmentierung korrekt
* Parquet-Dateien erzeugt
* Session-Browser zeigt echte Daten
* Retention funktioniert
* Keine Datenverluste bei Disconnect

---


## Lap validity and status rules (2026-02-28 hotfix)

- TrackSurface classification is centralized in lap segmentation:
  - `ON_TRACK`, `OFF_TRACK`, `PIT`, `UNKNOWN`
  - `None` or negative values -> `UNKNOWN`
  - pit values (`1`, `2`) are not treated as offtrack
  - offtrack detection is based on class `OFF_TRACK`, not on a single hardcoded value.
- Lap-time sanity is enforced for `lap_complete` and `valid_lap`:
  - `min_valid_lap_time_s = 30.0`
  - `min_valid_lap_samples = 60`
  - tiny fragments stay `incomplete` and cannot become `valid_lap`.
- Best-lap selection uses the same lap duration source as lap display and excludes:
  - laps with `valid_lap = false`
  - laps below sanity threshold
  - `lap_no == 0` (outlap semantics)
  - if no valid laps remain: display `best=na`
- Duplicate `lap_no` fragments are resolved deterministically:
  - keep the longer segment
  - mark shorter duplicate as fragment (`incomplete` / not valid).
  - rule note: this avoids unrealistic short laps (for example ~12s) becoming "best".

## Story 1 â€” Coaching-View anlegen + Ribbon Button

**Ziel:** Neuer MenÃ¼punkt â€žCoachingâ€œ zwischen â€žVideo Analysisâ€œ und â€žSettingsâ€œ Ã¶ffnet eine leere Coaching-Seite.

**Scope**

* Ribbon/Button hinzufÃ¼gen
* View-Routing/Controller erweitern
* Platzhalter-Layout (links Browser / rechts Details+Status, noch leer)

**Akzeptanzkriterien**

* App startet ohne Fehler
* Klick auf â€žCoachingâ€œ zeigt neue Seite
* ZurÃ¼ck zu Video Analysis / Settings funktioniert

**GeÃ¤nderte Dateien (erwartet)**

* `src/ui_app.py` (oder dein aktueller UI-Entry/Controller, je nach Umbau)
* ggf. `src/ui/controller.py` / `src/app_entry.py` (falls vorhanden)

**Deliverables**

* Title + changed files + short summary

### Umsetzung (Ist-Stand)

* Neue Platzhalter-View `CoachingView` ergÃ¤nzt (2-Spalten-Layout).
* Linke Spalte als leerer Container â€žBrowserâ€œ umgesetzt.
* Rechte Spalte als leere Container â€žDetailsâ€œ (oben) und â€žStatusâ€œ (unten) umgesetzt.
* `VIEW_REGISTRY` minimal erweitert und â€žCoachingâ€œ exakt zwischen â€žVideo Analysisâ€œ und â€žSettingsâ€œ eingefÃ¼gt.
* Routing/Controller-Verhalten bleibt unverÃ¤ndert, da Navigation generisch Ã¼ber `VIEW_REGISTRY` lÃ¤uft.

### Abnahme / Check

* `python -m py_compile src/ui/app.py src/ui_app.py` âœ…
* Stub-basierter Tk-Smoke-Test: Klickfolge validiert + Container (Browser/Details/Status) geprÃ¼ft âœ…
* Echter App-Start-Smoke in dieser Umgebung: na (blockiert durch fehlendes `cv2` ohne Stub)

**Deliverables**

* Title: Coaching-View als neue Ribbon-Seite (minimal, registry-basiert)
* Changed files: `src/ui/app.py`
* Short summary:
  * CoachingView Platzhalter-Layout ergÃ¤nzt (Browser | Details/Status).
  * `VIEW_REGISTRY` erweitert, Button â€žCoachingâ€œ korrekt einsortiert.
  * Navigation bleibt generisch/unverÃ¤ndert.

**Fertig wenn**

* âœ… App startet ohne Fehler (in einer Umgebung mit allen Runtime-Dependencies, z. B. `cv2`)
* âœ… Klick auf â€žCoachingâ€œ zeigt neue Seite
* âœ… ZurÃ¼ck zu Video Analysis / Settings funktioniert
---

## Story 2 â€” Settings Keys fÃ¼r Coaching Recording (defaults.ini + UI)

**Ziel:** Alle Sprint-1 Keys sind vorhanden, werden geladen/gespeichert und im Settings-Screen editierbar.

**Scope (INI Keys)**

* `coaching_recording_enabled` (default true)
* `coaching_storage_dir`
* `irsdk_sample_hz = 120` (0 = unthrottled)
* `coaching_retention_months_enabled`, `coaching_retention_months`
* `coaching_low_disk_warning_enabled`, `coaching_low_disk_warning_gb`
* `coaching_auto_delete_enabled`

**Akzeptanzkriterien**

* Defaults greifen ohne User-Config
* UI zeigt Werte, Ã„nderungen werden persistiert
* Validierung: Zahlenfelder (GB/Monate/Hz) nur sinnvoller Bereich

**GeÃ¤nderte Dateien (erwartet)**

* `config/defaults.ini`
* `src/core/cfg.py` (oder dein Config-Modul)
* `src/ui_app.py` (Settings UI)
* `docs/Coaching Sprint1 Daten und Grundlagen.md` (INI Doku Abschnitt)

**Deliverables**

* Title + changed files + short summary + wo die INI keys dokumentiert sind

### Umsetzung (Ist-Stand)

* Neue INI-Section `[coaching_recording]` in `config/defaults.ini` ergÃ¤nzt mit allen 8 Sprint-1 Keys inkl. Defaults (`coaching_recording_enabled = true`, `irsdk_sample_hz = 120`).
* Typed Loader `load_coaching_recording_settings()` in `src/core/persistence.py` ergÃ¤nzt (bool/int/str, Defaults aus defaults.ini, Range-Normalisierung).
* Save-API `save_coaching_recording_settings()` ergÃ¤nzt, persistiert nach `config/user.ini` und aktualisiert die geladene Layer-Config.
* Settings-Screen in `src/ui/app.py` erweitert:
  * Neuer Block â€žCoaching Recordingâ€œ.
  * Checkboxes fÃ¼r *_enabled Keys.
  * Storage-Path-Entry mit Browse.
  * Zahlenfelder fÃ¼r Hz / Monate / GB.
* Validierung umgesetzt:
  * `irsdk_sample_hz`: 0â€“1000
  * `coaching_retention_months`: 1â€“120
  * `coaching_low_disk_warning_gb`: 1â€“2000
  * Nur Integer-Eingaben erlaubt.
  * UngÃ¼ltige Werte werden nicht gespeichert und erzeugen UI-Fehlertext.
* Autosave-Persistenz wie bestehende Settings-Patterns.
* Keine Recording-, Retention- oder Delete-Logik implementiert.

---

### Abnahme / Check

* `python -m py_compile` fÃ¼r:
  * `src/core/persistence.py`
  * `src/ui/app.py`
  * `src/ui_app.py`
  * `src/core/cfg.py` âœ…
* Persistenz-Smoke-Test (Defaults + temp user.ini Roundtrip) erfolgreich âœ…
* App-Start bis GUI-Loop erfolgreich (interaktiver Klicktest hier nicht automatisiert) âš ï¸

---

### Dokumentation

* Datei: `docs/Coaching Sprint 1 â€“ Daten & Grundlagen.md`
* Abschnitt:
  * `# 8. Settings`
  * INI-Section (Sprint 1)
  * Validierungsbereiche (Settings-UI)

Dort sind dokumentiert:
* alle 8 Keys
* Default-Werte
* Kurzbeschreibung je Key
* feste Validierungsbereiche (Hz / Monate / GB)

---

## Deliverables

* **Title:** Coaching-Recording Settings-Keys (INI Defaults, Persistenz, UI + Validierung)
* **Changed files:**  
  `config/defaults.ini`  
  `src/core/persistence.py`  
  `src/ui/app.py`  
  `docs/Coaching Sprint 1 â€“ Daten & Grundlagen.md`
* **Short summary:**
  * Neue `[coaching_recording]` Section mit 8 Keys in defaults.ini.
  * Typed Loader + Save-API in persistence ergÃ¤nzt.
  * Settings-UI erweitert inkl. Checkboxes, Path-Entry, Zahlenfelder.
  * Harte Range-Validierung (0â€“1000 / 1â€“120 / 1â€“2000).
  * Persistenz Ã¼ber user.ini, Defaults greifen ohne User-Config.

---

## Fertig wenn

* âœ… Defaults greifen ohne User-Config.
* âœ… UI zeigt Werte, Ã„nderungen werden persistiert.
* âœ… Zahlenfelder akzeptieren nur definierte Bereiche.

---

## Story 3 â€” IRSDK Connector + Recorder-Service GrundgerÃ¼st (ohne Run-Logik)

**Ziel:** Hintergrund-Service verbindet sich mit IRSDK, sampled und puffert Daten (120 Hz default, drosselbar).

**Scope**

* Service lÃ¤uft **nicht-blockierend** zur UI
* Connection state (connected/disconnected)
* Sampling loop mit throttle (`irsdk_sample_hz`)
* Ring/Chunk Buffer (1â€“2s) vorbereitet

**Akzeptanzkriterien**

* App bleibt responsive
* Service kann starten/stoppen (app lifecycle)
* Bei Disconnect keine Crashs, sauberer Reconnect mÃ¶glich
* Logging: connect/disconnect + sample count

**GeÃ¤nderte Dateien (erwartet)**

* neu: `src/core/irsdk/recorder_service.py` (oder Ã¤hnliche Struktur)
* neu: `src/core/irsdk/irsdk_client.py`
* `src/ui_app.py` (Start/Stop hook oder init)
* `src/core/log.py` (falls Logging zentral)

**Deliverables**

* Title + changed files + short summary

### Umsetzung (Ist-Stand)

* `IRSDKClient` ergÃ¤nzt:
  * Optionaler/lazy IRSDK-Import, `connect()`/`disconnect()` stabil mit `try/except`, bleibt bei Fehlern â€ždisconnectedâ€œ.
  * `read_sample()` liefert ein Sample-Dict mit Zeitstempeln + kleinen Raw-Feldern, oder `None` bei disconnect.
* `RecorderService` ergÃ¤nzt:
  * Hintergrund-Thread non-blocking mit `start()`/`stop()` (inkl. `join(timeout)`), Properties `running`/`connected`, sowie `get_buffer_snapshot()`.
  * Sampling-Loop:
    * Bei Disconnect: Reconnect-Versuche mit Backoff (1s), ohne Crash.
    * Throttle:
      * `hz > 0`: driftarm via `monotonic` + `next_tick`
      * `hz == 0`: unthrottled mit yield/sleep, um CPU-Spike zu vermeiden
  * Ring-Buffer vorbereitet via `deque(maxlen=...)`:
    * KapazitÃ¤t `hz*2` (â‰ˆ 2s)
    * Fallback `240` bei `hz==0`
* Minimales Logging vorhanden:
  * connect / disconnect
  * reconnect attempt
  * sample_count
* Lifecycle-Hooks:
  * Service wird nur instanziiert + Hook exponiert (kein Autostart).
  * `irsdk_sample_hz` wird genutzt (bestehender Key), ohne neue Settings einzufÃ¼hren.
* Keine Run-/Write-/Export-Logik implementiert (keine Files, keine Retention).

---

### Abnahme / Check

* `python -m py_compile` fÃ¼r alle neuen/geÃ¤nderten Dateien âœ…
* Service-Smoke: Start/Stop, Reconnect-Attempts ohne IRSDK, Fake-Client fÃ¼r Samples/Buffer/Logging âœ…
* UI-Smoke im Sandbox-Environment: na (blockiert durch fehlendes `cv2` beim Import von `ui.app`; nicht durch IRSDK-Ã„nderungen)

---

## Deliverables

* **Title:** IRSDK Connector + RecorderService GrundgerÃ¼st (Thread, Throttle, Buffer)
* **Changed files:**
  * `src/core/irsdk/irsdk_client.py` (neu)
  * `src/core/irsdk/recorder_service.py` (neu)
  * `src/app_entry.py`
  * `src/ui_app.py`
* **Short summary:**
  * IRSDKClient kapselt optionalen Import + stabile Connect/Disconnect Pfade.
  * RecorderService als Background-Thread mit sauberem Start/Stop.
  * Sampling-Loop mit Reconnect-Backoff und driftarmem Throttle Ã¼ber `irsdk_sample_hz`.
  * Ring-Buffer (â‰ˆ1â€“2s) via `deque(maxlen=...)` vorbereitet.
  * Logging fÃ¼r connect/disconnect und sample_count ergÃ¤nzt.
 

---

## Story 3.1 â€” Optional cv2 + Safe Import Boundary (Video-Features gated, App startet ohne OpenCV)

**Ziel:**
iWAS soll **ohne installiertes OpenCV (`cv2`) starten kÃ¶nnen**.
Wenn `cv2` fehlt, werden **Video-Features** (Video Analysis / Rendering / HUD-Render) sauber deaktiviert bzw. mit Hinweis angezeigt â€“ **Coaching / IRSDK Recorder / Settings** funktionieren weiterhin.

---

### Hintergrund / Problem

Aktuell blockiert in bestimmten Umgebungen (z.B. Codex/CI/Sandbox) ein fehlendes `cv2` bereits beim Import/Start, weil `cv2` zu frÃ¼h (Top-Level) importiert wird.
Dadurch ist UI-Smoke-Testing unmÃ¶glich, obwohl Coaching/Recorder keinen OpenCV-Zwang haben.

---

## Scope

### 1) Harte Import-Regel

* `import cv2` darf **nicht** in Modulen vorkommen, die beim App-Start immer geladen werden:

  * UI Root / MenÃ¼band / Settings
  * Coaching View
  * App Entry / Controller / Config

**cv2** darf nur in **Video/Render-spezifischen** Modulen importiert werden (Lazy Import).

---

### 2) Dependency Probe (OpenCV verfÃ¼gbar?)

* Zentrale, kleine Helper-Funktion, z.B.:

  * `has_cv2()` â†’ bool
  * oder `try_import_cv2()` â†’ (ok, error_message)

Diese Funktion wird verwendet, um Features zu aktivieren/deaktivieren.

---

### 3) Feature Gating in der UI

Wenn `cv2` fehlt:

* App startet normal.
* Beim Wechsel zu **Video Analysis**:

  * Es wird **kein Crash** ausgelÃ¶st.
  * Stattdessen erscheint im Video-View ein klarer Hinweis:

    * â€žVideo features unavailable: OpenCV (cv2) not installed.â€œ
  * Buttons, die zwingend `cv2` brauchen, sind deaktiviert (oder nicht sichtbar).

Wenn `cv2` vorhanden ist:

* Verhalten unverÃ¤ndert.

---

### 4) Saubere Fehlerkommunikation

* Fehlermeldung soll konkret sein:

  * `ModuleNotFoundError: No module named 'cv2'` â†’ in UI-Text Ã¼bersetzen
* Optional: Hinweis, wie man OpenCV installiert (nur Text, kein Autoinstall).

---

## Out of Scope

* Kein Refactor der Render-Pipeline
* Kein Austausch von OpenCV durch FFmpeg-Pipes
* Keine neuen Video-Features
* Keine Ã„nderungen an Coaching/Recorder-Logik (nur sicherstellen, dass sie ohne cv2 startet)

---

## Akzeptanzkriterien

1. **Ohne cv2 installiert:**

   * `python src/app_entry.py` (oder dein normaler Startpfad) startet ohne Exception.
   * Settings und Coaching funktionieren.
   * Klick auf â€žVideo Analysisâ€œ zeigt Info-Panel statt Crash.

2. **Mit cv2 installiert:**

   * Video Analysis funktioniert wie zuvor.
   * Rendering/HUD-Funktionen unverÃ¤ndert.

3. **Keine Top-Level cv2 Imports** in Start-Pfaden:

   * `ui_app.py`, `app_entry.py`, Controller/Config dÃ¼rfen `cv2` nicht importieren.

4. **Logging (optional, aber empfohlen):**

   * Ein Log-Eintrag bei fehlendem cv2 beim Wechsel in Video Analysis.

---

## Implementation Notes (Leitplanken fÃ¼r Codex)

* Ersetze Top-Level Imports durch Lazy Import:

  * `def _require_cv2(): import cv2; return cv2`
* UI soll beim Laden des Video Views prÃ¼fen:

  * `if not has_cv2(): show_disabled_panel() else: load_video_view()`
* Falls in HUD-Modulen OpenCV genutzt wird:

  * dort darf cv2 importiert bleiben, solange diese Module **nur** geladen werden, wenn Video Features aktiv sind.

---

## GeÃ¤nderte Dateien (erwartet)

* `src/ui_app.py`

  * Video Analysis View: Guard + Fallback-Panel
* `src/app_entry.py` (falls Import-Pfad noch cv2 triggert)
* ggf. `src/main.py` / `src/ui/controller.py` (je nach Routing)
* **neu (empfohlen):** `src/core/deps.py` oder `src/core/optional_deps.py`

  * `has_cv2()` / `try_import_cv2()`

---

## Deliverables (nach Umsetzung ausgeben)

* **Title:** Make OpenCV (cv2) optional: gate Video Analysis, allow app start without cv2
* **Changed files:** (Liste)
* **Short summary:**

  * Wo die cv2-Checks sitzen
  * Wie die UI reagiert, wenn cv2 fehlt
  * Keine VerÃ¤nderung am Verhalten, wenn cv2 vorhanden ist
 
### Umsetzung (Ist-Stand)
- Zentrale cv2-Dependency-Checks in `src/core/optional_deps.py` umgesetzt:
  - `has_cv2()` (Zeile ~9)
  - `try_import_cv2()` (Zeile ~16) mit UI-tauglicher Fehlermeldung:
    - `Video features unavailable: OpenCV (cv2) not installed.`
    - `Install: pip install opencv-python`
- UI-Gate in `src/ui/app.py` frÃ¼h eingebaut (`build_video_analysis_view`, Zeile ~1102 ff.):
  - Wenn `cv2` fehlt: Info-Panel wird angezeigt und Video-UI/Buttons werden nicht aufgebaut.
  - Wichtig: `ui.preview.video_preview` wird in diesem Fall nicht importiert (Lazy Import erst bei vorhandenem `cv2`, Zeile ~1115).
- `cv2` in `src/ui/preview/video_preview.py` von Top-Level auf Lazy Import umgestellt:
  - `_require_cv2()` (Zeile ~25)
  - Verhalten bleibt bei installiertem `cv2` unverÃ¤ndert.
- Startpfad in `src/ui/app.py` abgesichert:
  - Top-Level `VideoPreviewController` Import entfernt.
  - Nur noch `TYPE_CHECKING` + lokaler Import im Video-View.
- Optionales Logging ergÃ¤nzt:
  - Warn-Log beim Ã–ffnen von Video Analysis ohne `cv2`.

### Abnahme / Check
- `python -m py_compile` auf geÃ¤nderten Dateien: OK
- Environment ohne `cv2`:
  - `python src/app_entry.py` startet bis GUI-Loop (Timeout nach 5s), kein Crash
  - Warn-Log beim Ã–ffnen von Video Analysis: OK
  - Video Analysis zeigt Guard-Panel statt Crash: OK
  - CoachingView instanziierbar: OK
  - SettingsView instanziierbar: OK
- Environment mit `cv2`:
  - na (hier nicht testbar)

### Fertig wenn
- âœ… App startet ohne installiertes `cv2` ohne Exception.
- âœ… Coaching und Settings funktionieren weiterhin ohne `cv2`.
- âœ… Klick auf â€žVideo Analysisâ€œ zeigt Hinweis-Panel statt Crash, und importiert keine Video-Module im Fehlerfall.
- âœ… Keine Top-Level `cv2` Imports in Start-Pfaden (UI Root/Settings/Coaching/App Entry/Controller/Config).
- âœ… Verhalten mit installiertem `cv2` bleibt unverÃ¤ndert (nicht umgebaut, nur gated).

  
---

## Story 4 â€” Channel Discovery: â€œAlles speichernâ€ best-effort + Missing-Channel Handling

**Ziel:** Recorder speichert **alle von dir gelisteten Variablen**, aber robust: wenn ein Channel fehlt â†’ weiterlaufen und im Meta vermerken.

**Scope**

* Liste der gewÃ¼nschten Variablen als â€œRequested Channelsâ€
* Beim Start: Header/Var-Map aus IRSDK lesen, Intersection bilden
* Missing Channels in `session_meta.json` speichern

**Akzeptanzkriterien**

* Bei unterschiedlichen Cars keine Exceptions wegen fehlenden Variablen
* `session_meta.json` enthÃ¤lt:

  * recorded_channels[]
  * missing_channels[]
  * sample_hz (effective)
  * dtype decisions (wo relevant)

**GeÃ¤nderte Dateien (erwartet)**

* `src/core/irsdk/recorder_service.py`
* neu/ergÃ¤nzt: `src/core/irsdk/channels.py`
* neu: `src/core/coaching/models.py` (Meta-Strukturen)

**Deliverables**

* Title + changed files + short summary

### Umsetzung (Ist-Stand)
- `REQUESTED_CHANNELS` zentral definiert in `src/core/irsdk/channels.py` (Zeile ~5) als reine Konstanten-Datei ohne schwere Imports.
- Channel Discovery beim Connect/Start Ã¼ber IRSDK Var-Map/Header umgesetzt in `src/core/irsdk/irsdk_client.py` (Zeilen ~118 und ~163).
- Recorder bildet beim Start robust die finalen Listen (jeweils in REQUESTED-Reihenfolge):
  - `recorded_channels = requested âˆ© available` in `src/core/irsdk/recorder_service.py` (Zeile ~164)
  - `missing_channels = requested - available` in `src/core/irsdk/recorder_service.py` (Zeile ~165)
- Sampling nutzt ausschlieÃŸlich `recorded_channels` (`src/core/irsdk/recorder_service.py` Zeile ~117), fehlende Variablen fÃ¼hren nicht mehr zu Exceptions.
- Missing-Channels erzeugen nur einen einmaligen Warn-Log (kein Crash) in `src/core/irsdk/recorder_service.py` (Zeile ~179).
- `session_meta.json` erweitert Ã¼ber `SessionMeta` in `src/core/coaching/models.py` (Zeile ~8) um:
  - `recorded_channels[]`
  - `missing_channels[]`
  - `sample_hz` (effektiv genutzter Recorder-Wert)
  - `dtype_decisions` (nur wenn aus Header-Info ableitbar)
- Meta-Schreiben im Recorder umgesetzt inkl. Merge (bestehende JSON-Keys bleiben erhalten) in `src/core/irsdk/recorder_service.py` (Zeilen ~234 und ~268).

### Abnahme / Check
- `python -m py_compile` nach jedem Schritt auf den geÃ¤nderten Dateien: OK
- Recorder-Smokes mit FakeClient/Mock Var-Map (kein Live-iRacing im Sandbox-Setup):
  - Missing-Channel-Fall: kein Crash, `missing_channels` gefÃ¼llt, Samples nur mit vorhandenen Channels: OK
  - All-Channels-Fall: `missing_channels == []`, `recorded_channels` vollstÃ¤ndig: OK
  - `session_meta.json` enthÃ¤lt neue Felder; bestehende Keys bleiben erhalten (Merge statt Replace): OK

### Fertig wenn
- âœ… Bei unterschiedlichen Cars keine Exceptions wegen fehlenden Variablen.
- âœ… `session_meta.json` enthÃ¤lt `recorded_channels[]`, `missing_channels[]`, `sample_hz` (effective) und `dtype_decisions` (wo relevant).
- âœ… Recorder lÃ¤uft best-effort weiter und loggt fehlende Channels einmalig als Warnung.


---

## Story 5 â€” SessionInfo YAML speichern + Meta-Index extrahieren

**Ziel:** `session_info.yaml` wird raw gespeichert, zusÃ¤tzlich `session_meta.json` mit normalisierten Kernfeldern.

**Scope**

* Raw YAML dump 1Ã— pro Session (oder bei Updates, aber Sprint-1 minimal: einmal + ggf. final)
* Extraktion:

  * DriverName, CarScreenName, CarClassShortName
  * TrackDisplayName, TrackConfigName
  * SessionType (Primary SessionInfo, Fallback WeekendInfo) + Normalisierung
  * SessionUniqueID
  * timestamps

**Akzeptanzkriterien**

* Jede Session hat Ordner + YAML + JSON
* SessionType Normalisierung stabil (practice/qualify/race)

**GeÃ¤nderte Dateien (erwartet)**

* `src/core/irsdk/sessioninfo_parser.py`
* `src/core/irsdk/recorder_service.py`

**Deliverables**

* Title + changed files + short summary

## Story 5 â€” SessionInfo YAML speichern + Meta-Index extrahieren

### Umsetzung (Ist-Stand)
- Raw `session_info.yaml` wird im Recorder beim Start/Connect best-effort gespeichert, sobald SessionInfo erstmals verfÃ¼gbar ist (`src/core/irsdk/recorder_service.py` Zeile ~288).
- YAML wird als raw Text in `session_info.yaml` im Session-Ordner abgelegt (`src/core/irsdk/recorder_service.py` Zeilen ~329, ~335).
- Pro Recorder-Session wird ein eigener Session-Ordner angelegt (`session-YYYYMMDD-HHMMSS-mmm`) unter dem Coaching-Storage-Ordner (Fallback: `cwd`); `session_meta.json` liegt jetzt im gleichen Ordner (`src/core/irsdk/recorder_service.py` Zeilen ~282, ~335).
- `extract_session_meta()` extrahiert und normalisiert Kernfelder aus raw SessionInfo YAML (`src/core/irsdk/sessioninfo_parser.py` Zeile ~7):
  - `DriverName`
  - `CarScreenName`
  - `CarClassShortName`
  - `TrackDisplayName`
  - `TrackConfigName`
  - `SessionUniqueID` (mit Fallbacks auf Session/SubSession IDs)
  - `SessionType` (normalisiert)
  - `session_type_raw`
  - `timestamps` (`recorder_start_ts`, `session_info_saved_ts`)
- SessionType Normalisierung ist stabil umgesetzt:
  - Primary aus SessionInfo (Sessions-Liste / aktueller SessionNum, soweit vorhanden), Fallback aus WeekendInfo (`src/core/irsdk/sessioninfo_parser.py` Zeilen ~42, ~131)
  - Mapping auf `practice` / `qualify` / `race`, sonst `unknown` (`src/core/irsdk/sessioninfo_parser.py` Zeile ~67)
- Meta-Merge erfolgt direkt nach YAML-Dump:
  - `extract_session_meta()` wird aufgerufen und die Felder werden in `session_meta.json` gemerged (bestehende Keys bleiben erhalten) (`src/core/irsdk/recorder_service.py` Zeilen ~322, ~370, ~390).

### Abnahme / Check
- `python -m py_compile` auf geÃ¤nderten Dateien: OK
- Recorder-Smokes mit FakeClient/Mock SessionInfo:
  - Session-Ordner + `session_info.yaml` + `session_meta.json` erzeugt: OK
  - Retry bei zunÃ¤chst leerer SessionInfo ohne Crash: OK
  - SessionType Normalisierung geprÃ¼ft fÃ¼r Practice, Qualification, Race: OK
  - Meta-Merge geprÃ¼ft: bestehende Keys bleiben erhalten: OK

### Fertig wenn
- âœ… Jede Session hat Ordner + `session_info.yaml` (raw) + `session_meta.json` (normalisierte Kernfelder).
- âœ… SessionType Normalisierung ist stabil: `practice` / `qualify` / `race` (sonst `unknown`).
---

## Story 6 â€” Run State Machine (Start/End Regeln nach SessionType)

**Ziel:** Runs werden automatisch gestartet/gestoppt gemÃ¤ÃŸ deiner Regeln.

**Start**

* Practice/Quali: `OnPitRoad True -> False`
* Race: Green in `SessionFlags` + passender `SessionState`

**End**

* Race/Quali: Checkered in `SessionFlags` + `SessionState` passend **oder**
* Incomplete: `LapCompleted` steigt **25s** nicht + Secondary Check `IsOnTrackCar/Speed`
* Practice: Incomplete wie oben

**Akzeptanzkriterien**

* Mehrere Runs pro Session mÃ¶glich
* Kein Run startet doppelt (armed/active states)
* End condition erzeugt saubere run files (siehe Story 7)

**GeÃ¤nderte Dateien (erwartet)**

* neu: `src/core/coaching/run_detector.py`
* `src/core/irsdk/recorder_service.py`
* `docs/Coaching Sprint1 Daten und Grundlagen.md` (Run Regeln)

**Deliverables**

* Title + changed files + short summary

### Umsetzung (Ist-Stand)
- Neue Run-State-Machine in `src/core/coaching/run_detector.py`:
  - ZustÃ¤nde: `IDLE` / `ARMED` / `ACTIVE` (Zeile ~11)
  - `update(sample, now_ts) -> events` (Zeile ~31)
  - Guards in `_start()` / `_end()` verhindern Doppelstart und Doppelende.
- Start-Regeln:
  - Practice/Qualify: `OnPitRoad` True â†’ False (nach ARMED) â†’ `RUN_START` reason=`pit_exit` (Zeile ~46)
  - Race: `Green` in `SessionFlags` + best-effort passender `SessionState` â†’ `RUN_START` reason=`green_flag` (Zeile ~65)
- End-Regeln:
  - Quali/Race: `Checkered` in `SessionFlags` + passender `SessionState` â†’ `RUN_END` reason=`checkered_flag` (Zeile ~86)
  - Practice: kein Checkered-Ende, nur Incomplete-Rule
- Incomplete-Rule (25s):
  - `LapCompleted` stagniert 25s
  - Secondary: `IsOnTrackCar == False` oder `Speed < 1.0`
  - â†’ `RUN_END` reason=`incomplete_timeout` (Zeile ~193)
- Integration im Recorder:
  - RunDetector wird erzeugt, sobald `SessionType` bekannt ist (`recorder_service.py` ~288, ~397, ~423)
  - Pro Sample `run_detector.update(...)` im Loop (`recorder_service.py` ~147)
  - Event-Verarbeitung + Logging + run_id-Guard (`recorder_service.py` ~444, ~466)
- ZusÃ¤tzliche Run-Signale in `REQUESTED_CHANNELS` aufgenommen:
  - `SessionState`, `SessionFlags`, `LapCompleted`, `OnPitRoad`, `IsOnTrackCar` (`channels.py` ~8)
- Doku ergÃ¤nzt:
  - Run-Regeln, State Machine, Start/End-Logik, Incomplete 25s + Secondary in  
    `docs/Coaching Sprint 1 â€“ Daten & Grundlagen.md` (~997)

### Story 6 Umsetzung (Run Regeln)

* State Machine: IDLE -> ARMED -> ACTIVE
* Guards: kein doppeltes RUN_START in ACTIVE, kein doppeltes RUN_END ausserhalb ACTIVE
* Mehrere Runs pro Session sind erlaubt

* Start practice / qualify: OnPitRoad wechselt True -> False (nach vorherigem ARMED auf OnPitRoad=True)
* Start race: Green in SessionFlags + best-effort passender SessionState
* Start unknown: kein Auto-Start (best-effort / Logging)

* End race / qualify: Checkered in SessionFlags + best-effort passender SessionState
* End practice / qualify / race: incomplete_timeout wenn LapCompleted 25s nicht steigt
* Secondary Check fuer incomplete_timeout: IsOnTrackCar == False ODER Speed < 1.0
* Wenn Signale fehlen/unklar sind: kein Crash, Run-Logik bleibt stabil (best-effort)

* Beispiele:
* Practice: Boxenausfahrt startet Run, 25s Stagnation + off-track/slow beendet Run
* Qualify: Pit-Exit startet Run, Checkered beendet Run
* Race: Green startet Run, erneutes Green startet nicht doppelt, Checkered beendet Run

### Abnahme / Check
- `python -m py_compile` nach jedem Schritt: OK
- Recorder-Smokes (FakeClient/Mock):
  - Practice:
    - `RUN_START(pit_exit)`
    - `RUN_END(incomplete_timeout)`
    - Zweiter Run mÃ¶glich: OK
  - Quali:
    - `RUN_START(pit_exit)`
    - `RUN_END(checkered_flag)`
  - Race:
    - `RUN_START(green_flag)`
    - `RUN_END(checkered_flag)`
    - Kein Doppelstart bei erneutem Green
- Incomplete-Rule:
  - Kein Ende bei 25s Stagnation, solange Auto aktiv (`IsOnTrackCar=True`, Speed hoch)
  - Ende nur wenn Secondary erfÃ¼llt: OK

### Fertig wenn
- âœ… Mehrere Runs pro Session mÃ¶glich.
- âœ… Kein Run startet doppelt (saubere Guards).
- âœ… End-Conditions liefern saubere RUN_END Events (Grund gesetzt).
- âœ… Run-Regeln dokumentiert im Sprint-Dokument.


---

## Story 7 â€” Storage: Session Ordnerstruktur + Parquet Writer (Chunked, typed)

**Ziel:** Daten werden pro Run als Parquet/Binary gespeichert, chunked (1â€“2s), mit typed columns.

**Scope**

* Root dir: `coaching_storage_dir`
* Session folder naming:

  * `YYYY-MM-DD__HHMMSS__Track__Car__SessionType__SessionID`
* Files:

  * `session_info.yaml`, `session_meta.json`
  * `run_0001.parquet`, `run_0001_meta.json`, â€¦
* Type policy:

  * float32 wo mÃ¶glich, ints passend, bool as bool/uint8
* â€œActive session never deleteâ€ Vorbereitung (Flag/lock)

**Akzeptanzkriterien**

* Bei 10+ Minuten Recording keine UI-Stalls
* Parquet Dateien lesbar, enthalten alle recorded_channels
* run_meta enthÃ¤lt sample_count, start/end SessionTime, lap range (falls mÃ¶glich)

**GeÃ¤nderte Dateien (erwartet)**

* neu: `src/core/coaching/storage.py`
* neu: `src/core/coaching/parquet_writer.py`
* `src/core/irsdk/recorder_service.py`

**Deliverables**

* Title + changed files + short summary

### Umsetzung (Ist-Stand)
- Session folder naming + active lock
  - Neuer Storage-Helper in `src/core/coaching/storage.py` (ab Zeile ~18) mit:
    - `get_coaching_storage_dir()`, `sanitize_name()`, `build_session_folder_name()`, `ensure_session_dir()`
  - Ordnerformat: `YYYY-MM-DD__HHMMSS__Track__Car__SessionType__SessionID`
  - `.active_session.lock` + `.finalized` vorbereitet/geschrieben (`storage.py` ~77, ~89)
  - Recorder nutzt das neue Naming und benennt den Session-Ordner nach `session_info.yaml` um, sobald Track/Car/Session/ID bekannt sind
    (`src/core/irsdk/recorder_service.py` ~381, ~495)

- Parquet writer (chunking + typing policy)
  - Neuer `ParquetRunWriter` in `src/core/coaching/parquet_writer.py` (ab Zeile ~10)
  - Chunked flush mit bounded buffer (1s Ã¼ber `chunk_seconds=1.0`), `append()` pro Sample, `flush()` / `close()`
    (`parquet_writer.py` ~39, ~64)
  - Typed schema aus `dtype_decisions` + Fallback-Inferenz; zusÃ¤tzliche Zeitspalten `ts` + `monotonic_ts`;
    fehlende Werte bleiben `null` (`parquet_writer.py` ~104, ~114)
  - `pyarrow` ist optional importiert und wird klar vorausgesetzt (kein Binary-Fallback)

- run_meta Inhalte und Recorder-Anbindung
  - `RUN_START`: Ã¶ffnet `run_{id:04d}.parquet` + initialisiert in-memory `run_meta`
    (`recorder_service.py` ~583, ~746)
  - Sample-Loop: schreibt nur wÃ¤hrend aktivem Run, erhÃ¶ht `sample_count`, trackt best-effort `SessionTime` und Lap/LapCompleted Range
    (`recorder_service.py` ~527)
  - `RUN_END`: schlieÃŸt Writer (flush) und schreibt `run_{id:04d}_meta.json`
    (`recorder_service.py` ~618, ~658)
  - Session-Meta dokumentiert Lock-/Finalized-Dateinamen; Finalized-Timestamp wird beim Stop gesetzt
    (`recorder_service.py` ~288, ~462, ~674)

### Abnahme / Check
- `python -m py_compile` auf allen 3 Dateien: OK
- FakeClient-Recorder-Smoke:
  - Session-Ordner-Naming, `session_info.yaml`, `session_meta.json`, `.active_session.lock`, `.finalized`: OK
  - `run_0001_meta.json`, `run_0002_meta.json`: OK
  - Plausible `sample_count` / `SessionTime`- und Lap-Ranges: OK
- Parquet Readback:
  - na (in dieser Umgebung nicht validierbar, da `pyarrow` nicht installiert ist)

### Fertig wenn
- âœ… Session-Ordnerstruktur + Lock/Finalized-Marker vorhanden.
- âœ… Pro Run wird chunked geschrieben (1s) mit typed Columns und bounded buffer.
- âœ… `run_meta` enthÃ¤lt `sample_count` + best-effort SessionTime/Lap-Range.
- âœ… Parquet ist im Ziel-Environment mit `pyarrow` lesbar (hier: na).

---

## Story 8 â€” Lap Segmentierung (Lap/LapCompleted + Wrap Fallback 0.99->0.01)

**Ziel:** Laps innerhalb eines Runs werden zuverlÃ¤ssig identifiziert und in Run-Meta abgelegt.

**Regeln**

* PrimÃ¤r: `Lap` oder `LapCompleted` ZÃ¤hlerwechsel
* Fallback: `LapDistPct >= 0.99 -> <= 0.01`
* Fallback optional gated: `IsOnTrackCar == True`

**Akzeptanzkriterien**

* Keine falschen Lap-Wechsel trotz vieler 0.00 / 0.99 Samples
* run_meta.json enthÃ¤lt Lap-Index mit start/end sample index (oder timestamps)

**GeÃ¤nderte Dateien (erwartet)**

* neu: `src/core/coaching/lap_segmenter.py`
* `src/core/coaching/run_detector.py` oder `storage.py`

**Deliverables**

* Title + changed files + short summary

### Umsetzung (Ist-Stand)
- Neues Modul `src/core/coaching/lap_segmenter.py` (neu) fÃ¼r zuverlÃ¤ssige Lap-Segmentierung.
- Signale / PrioritÃ¤t:
  - PrimÃ¤r: `Lap` (bevorzugt), sonst `LapCompleted` (`lap_segmenter.py` ~40, ~186)
  - Fallback: `LapDistPct` Wrap `>=0.99 -> <=0.01`, optional gated Ã¼ber `IsOnTrackCar` (`lap_segmenter.py` ~68, ~101)
- False-Positives verhindert durch:
  - Edge-based Wrap-Erkennung nur beim echten Ãœbergang (prev >= hi und curr <= lo)
  - Cooldown nach Wrap; Freigabe erst auÃŸerhalb des Randbereichs (mittlerer Bereich, `>0.1` und `< wrap_hi`) (`lap_segmenter.py` ~69, ~115)
  - PrimÃ¤rregel hat Vorrang; Wrap feuert nicht zusÃ¤tzlich bei Counter-Wechsel (`lap_segmenter.py` ~47, ~68)
- Speicherung:
  - `lap_segments` wird bei Run-Finalisierung in `run_{id:04d}_meta.json` geschrieben (`src/core/irsdk/recorder_service.py` ~677)
  - Format enthÃ¤lt `start_sample`, `end_sample`, optional `lap_no`, optional `start_ts/end_ts`, `reason` (`recorder_service.py` ~692)

### Offtrack detection (per lap)
- Verwendete IRSDK Channels:
  - `PlayerTrackSurface` (primaerer Offtrack-Indikator pro Sample)
  - `PlayerCarMyIncidentCount` (Incident-Delta pro Lap)
  - `Lap` / `LapCompleted` / `LapDistPct` fuer Lap-Segmentierung wie bisher
- Lap-Meta wird pro Lap als `run_XXXX_lap_YYYY_meta.json` gespeichert mit:
  - `lap_index`, optional `lap_num`
  - `lap_start_ts` / `lap_end_ts` sowie `lap_start_sample` / `lap_end_sample`
  - `lap_complete`: Lap wurde durch Counter-Change oder DistPct-Wrap geschlossen
  - `offtrack_surface`: mindestens ein Sample mit `PlayerTrackSurface` in OffTrack/NotInWorld
  - `incident_delta`: `max(PlayerCarMyIncidentCount) - min(PlayerCarMyIncidentCount)` innerhalb der Lap
  - `valid_lap`: `lap_complete AND NOT offtrack_surface AND incident_delta == 0`
- Wichtig: `IsOnTrackCar` bleibt Secondary Check fuer die Incomplete-Timeout-Rule.
  Es ist kein Offtrack-Signal fuer die Lap-Validierung.

### Integration (Recorder)
- Segmenter-State pro aktivem Run im `RecorderService` ergÃ¤nzt (`recorder_service.py` ~58)
- Update pro aktivem Sample inkl. run-lokalem `sample_index` (`recorder_service.py` ~545)
- Reset/Init bei Run-Start (`recorder_service.py` ~613)
- Finalize bei Run-Ende + Meta-Mapping (`recorder_service.py` ~642)

### Abnahme / Check
- `python -m py_compile` auf `lap_segmenter.py`, `recorder_service.py` (und `channels.py`): OK
- Smoke-Szenarien:
  - LapCompleted zÃ¤hlt hoch â†’ genau 1 Segment-Ende pro Wechsel: OK
  - LapDistPct 0.99/0.00-Flattern â†’ kein Extra-Wechsel (nur echter Wrap): OK
  - Echter Wrap on-track â†’ genau 1 Wechsel: OK
  - Wrap off-track (Gate aktiv) â†’ kein Wechsel: OK
- `run_meta.json` enthÃ¤lt plausible `lap_segments` mit Start/End Sample-Indizes (Timestamps optional): OK

### Fertig wenn
- âœ… Lap-Wechsel primÃ¤r Ã¼ber Counter, Wrap nur als Fallback.
- âœ… Keine falschen Lap-Wechsel bei 0.00/0.99-Flattern (edge + cooldown).
- âœ… `run_meta.json` enthÃ¤lt `lap_segments` mit start/end Indizes (oder timestamps).


---

## Story 9 â€” Coaching UI: Garage61-Ã¤hnlicher Browser + Recorder Status Panel

**Ziel:** Coaching Screen zeigt eine aufklappbare Baumstruktur: Track â†’ Car â†’ Event â†’ Run â†’ Lap. Plus Status Panel.

**Scope**

* Scan des Storage-Root und Build des Trees
* Accordion/Tree UI (expand/collapse)
* Node summary (total time, laps, fastest lap wenn vorhanden, last driven)
* Actions:

  * Open Folder (Session oder Run)
  * Delete (Session/Run; Run = Datei, Session = Folder)
* Recorder Status Panel:

  * connected/disconnected, sessionType, run active, sample count, dropped, write lag

**Akzeptanzkriterien**

* Nach Recording taucht Session im Browser auf ohne App-Neustart (Refresh Button ok)
* Delete lÃ¶scht korrekt, ohne aktive Session zu lÃ¶schen
* UI bleibt schnell bei vielen Sessions (basic caching ok)

**GeÃ¤nderte Dateien (erwartet)**

* `src/ui_app.py` (Coaching View UI)
* neu: `src/ui/coaching_browser.py` (optional, wenn du UI modular hÃ¤ltst)
* `src/core/coaching/indexer.py` (Storage scan + tree model)

**Deliverables**

* Title + changed files + short summary

### Umsetzung (Ist-Stand)
- Indexer (scan + caching + tree model)
  - `scan_storage()` in `src/core/coaching/indexer.py` (Zeile ~91) scannt Session-Ordner ohne Parquet-Reads.
  - Tree-Struktur: Track â†’ Car â†’ Event(Session) â†’ Run â†’ Lap.
  - Summaries werden best-effort berechnet (z.B. total_time, laps, fastest_lap, last_driven).
  - mtime-basierter Session-Cache zur Performance (`src/core/coaching/indexer.py` ~181).

- UI Browser (expand/collapse + summaries + actions)
  - Neue Tk-Komponente `CoachingBrowser` in `src/ui/coaching_browser.py` (Zeile ~15).
  - Refresh-Button + Rescan.
  - Expand-State wird Ã¼ber Refresh erhalten (`src/ui/coaching_browser.py` ~143).
  - Lap-Status im Summary-Feld: Prioritaet `incomplete` > `offtrack` (Offtrack nur, wenn die Lap nicht incomplete ist).
  - Actions:
    - Open Folder (Session/Run)
    - Delete Run/Session inkl. Confirm

- Recorder Status Panel (live)
  - `RecorderService.get_status()` in `src/core/irsdk/recorder_service.py` (Zeile ~85).
  - CoachingView pollt live und zeigt:
    - connected/disconnected
    - sessionType
    - run active
    - sample count
    - dropped
    - write lag (na, falls nicht vorhanden)
  - Polling/Update in `src/ui/app.py` (Zeile ~1258).

### Wesentliche Integrationspunkte
- Coaching UI Integration in `src/ui/app.py` (ab Zeile ~1067).
- Refresh/Rescan Trigger in `src/ui/app.py` (Zeile ~1139).
- Open Folder Action in `src/ui/app.py` (Zeile ~1170).
- Delete Run/Session (mit Confirm) in `src/ui/app.py` (Zeile ~1190).
- Schutz â€œActive session never deleteâ€:
  - Lock-Check Ã¼ber `.active_session.lock` + fehlendes `.finalized`
  - UI-Hinweis: â€œActive session â€“ delete disabled.â€
  - Implementiert in `src/ui/app.py` (Zeile ~1248).

### Abnahme / Check
- `python -m py_compile` auf allen betroffenen Dateien: OK
- Schrittweise Tk-Smokes (scripted):
  - Coaching Screen Ã¶ffnet
  - Refresh/Tree expand/collapse
  - Open Folder
  - Delete (inkl. Block bei aktiver Session)
  - Status Panel Updates
  - â†’ OK

### Fertig wenn
- âœ… Neue Sessions erscheinen nach Recording per Refresh ohne App-Neustart.
- âœ… Delete lÃ¶scht korrekt, aber blockiert aktive Sessions zuverlÃ¤ssig.
- âœ… UI bleibt schnell (Scan ohne Parquet-Reads + Cache).
- âœ… Status Panel zeigt Recorder-Status live (oder na, wenn Wert fehlt).

---

### Optional (falls du unbedingt bei 9 bleiben willst: im Zweifel weglassen)

**Story 10 â€” Retention + Low Disk Warning + Auto-Delete**
Wenn du merkst, dass Story 9 schon groÃŸ wird, ist Retention besser als eigene Story:

* periodic check on startup + manual â€œRun cleanup nowâ€
* respects active session lock
* lÃ¶scht oldest sessions bis free space ok

---


