# iWAS - Vollstaendige Projektdokumentation (Ist-Stand Codebasis)

## 1. Zweck und Ziel

iWAS ist ein Windows-zentriertes Desktop-Tool zur Analyse und Visualisierung von iRacing-Runden.
Der Kernnutzen ist die Erzeugung eines synchronisierten Vergleichsvideos (slow vs fast) mit HUD-Overlays aus Telemetrie.

Hauptfunktionen:
- Split-Video Rendering mit zwei Quellen (links/rechts oder oben/unten)
- Telemetrie-Synchronisation ueber `LapDistPct`
- HUD-Overlay-Rendering (mehrere HUD-Typen)
- Zwei Videomodi: `full` und `cut`
- Coaching-Recording (IRSDK), Speicherung, Browser/Index
- EXE-Betrieb inkl. gebuendelter FFmpeg/FFprobe-Suche


## 2. Dokumentationsstatus und Abgleich mit bestehendem `docs/`

Die vorhandenen Dateien in `docs/` enthalten vorrangig Sprint-/Historien-Dokumentation. Diese sind wichtig als Entstehungskontext, aber nicht immer 1:1 aktueller Laufzeitvertrag.

Wesentliche Korrekturen gegenuerber aelteren Dokumenten:
- `render_split.py` liegt aktuell unter `src/features/render_split.py` (nicht mehr unter `src/render_split.py`).
- CSV/Sync/Ffmpeg/Encoder/Modelle liegen in `src/core/*` und nicht mehr als flache Struktur.
- UI ist stark modularisiert (`src/ui/*`, Controller/Preview/Services), nicht mehr nur monolithisch.
- Layout-/Vertragsmigrationen sind aktiv in `core/models.py` (legacy faellt auf neue Schluessel zurueck).
- FFmpeg wird zuerst aus gebuendelten Pfaden gesucht (EXE-Layout), dann aus PATH.

Diese Datei (`docs/iWAS.md`) ist die konsolidierte technische Referenz fuer den aktuellen Code.


## 3. Laufzeit-Entry-Points

### 3.1 `src/app_entry.py`
- Einziger Hauptentry fuer Source und EXE.
- Schaltet auf Basis der CLI:
  - Mit `--ui-json`: Render-Modus (`src/main.py`)
  - Ohne `--ui-json`: GUI-Modus (`src/ui/app.py`)
- Initialisiert im GUI-Modus den `RecorderService` und exponiert Hooks in `ui.app`.

### 3.2 `src/main.py` (Render-Lauf)
- Liest UI-Payload JSON (normalerweise `config/ui_last_run.json`).
- Migriert Layout-/Video-Vertrag bei Bedarf (`core.models`).
- Loest Videos/CSVs auf, auto-matcht CSVs bei Bedarf.
- Fuehrt CSV-Vorbereitung (Resampling + SyncMap) fuer Debug/Robustheit aus.
- Wendet HUD-/Layout-/Cut-Parameter an.
- Ruft je nach CSV-Verfuegbarkeit auf:
  - `render_split_screen_sync(...)` (mit CSV)
  - `render_split_screen(...)` (fallback ohne CSV)


## 4. Architekturueberblick

### 4.1 UI-Schicht (`src/ui/`)
- `ui/app.py`: Haupt-GUI, Theme, View-Registry, Settings, Update-Check, Rendering-Ansteuerung.
- `ui/controller.py`: Application-Controller (Useraktionen, Dateiauswahl, Render-Start, Profile, Preview-Umschaltung).
- `ui/preview/`:
  - `layout_preview.py`: Layout-Preview
  - `png_preview.py`: PNG-basierte Vorschau
  - `video_preview.py`: Video-Vorschau (cv2-optional)
- `ui/coaching_browser.py`: Coaching-Baumansicht mit Session/Run/Lap-Summary.

### 4.2 Core-Schicht (`src/core/`)
- Modelle/Vertrag: `models.py`
- Persistenz/INI/JSON: `persistence.py`
- Output-Geometrie: `output_geometry.py`
- Render-Service/UI-Subprozess: `render_service.py`
- FFmpeg-Planung: `ffmpeg_plan.py`
- Encoderwahl/Fallback: `encoders.py`
- CSV-Lader: `csv_g61.py`
- Sync/Resampling: `resample_lapdist.py`, `sync_map.py`
- Cut-Event-Erkennung: `cut_events.py`
- Diagnostik/Bundle: `diagnostics.py`
- FFmpeg Tool-Resolver: `ffmpeg_tools.py`
- IRSDK + Coaching: `core/irsdk/*`, `core/coaching/*`

### 4.3 Feature-Schicht (`src/features/`)
- `render_split.py`: zentrale Render-Orchestrierung.
- `hud_registry.py`, `hud_layout.py`: HUD-Registry und aktive Layoutableitung.
- `huds/*`: einzelne HUD-Renderer.


## 5. Datenvertraege

### 5.1 Layout-/Rendervertrag (`core/models.py`)
Wichtige Dataclasses:
- `LayoutConfig`
  - `video_layout`: `LR` oder `TB`
  - `hud_mode`: `frame` oder `free`
  - `hud_frame`: Orientierung/Anker/Rahmendicke
  - `video_transform`: Scale/Shift/Mirror/Fit-Mode
  - `hud_free`: Alpha + absolute HUD-Boxen
- `OutputFormat`: aspect/preset/quality/hud_width_px
- `Profile`, `AppModel`, `RenderPayload`

Migrationslogik:
- `migrate_layout_contract_dict(...)`
- `migrate_video_state_contract_dict(...)`
- Legacy-Werte werden robust auf aktuelle Schluessel normalisiert.

### 5.2 JSON-Dateien (config)
- `config/ui_last_run.json`: Laufzeitpayload fuer Render-Subprozess.
- `config/output_format.json`: Output-Format-Auswahl.
- `config/hud_layout.json`: HUD-Box-Layouts.
- `config/png_view.json`: PNG-Preview-Zustand.
- `config/startframes.json` / `endframes.json`: Start/End-Cuts aus UI.
- `config/user.ini`: persistierte User-Overrides.


## 6. Render- und Sync-Pipeline

### 6.1 CSV-Zuordnung
- Direkte UI-Zuordnung moeglich.
- Auto-Matching nach Dateinamen (exakt bevorzugt, dann contains/compact).
- Suchorte: projektbezogene CSV-Ordner, EXE-Portabelpfade, Video-Nachbarschaft.

### 6.2 Telemetrievorbereitung
- `core/csv_g61.py`: CSV laden (`utf-8-sig`), Pflichtspalten validieren.
- `core/resample_lapdist.py`: LapDist-Grid + lineares Resampling.
- `core/sync_map.py`: Mapping slow->fast via LapDist.

### 6.3 Renderablauf
- UI schreibt Payload nach `config/ui_last_run.json`.
- `core/render_service.py` startet Render-Subprozess.
- Progress wird zweistufig gemappt:
  - Preparing HUDs (konfigurierbarer Anteil)
  - Rendering/Finalizing
- Live-Logzeilen werden geparst (`time=...`, `out_time_ms=...`, HUD-stream Framezähler).

### 6.4 FFmpeg-Toolauflösung
`core/ffmpeg_tools.py` Reihenfolge:
1. Gebuendelt (`tools/ffmpeg/*`) - EXE Layout
2. Developer-Bundle (`third_party/ffmpeg/lgpl_shared/bin/*`)
3. System `PATH`

### 6.5 Encoder-Fallback
`core/encoders.py`:
- Erkennt verfuegbare Encoder.
- Wählt Spezifikation mit Fallback (GPU -> CPU) robust.


## 7. HUD-System

Aktive HUD-Namen (API-relevant):
- `Speed`
- `Gear & RPM`
- `Throttle / Brake`
- `Steering`
- `Delta`
- `Line Delta`
- `Under-/Oversteer`

Implementierung:
- Tabelle/HUD: `huds/speed.py`, `huds/gear_rpm.py`
- Scroll/HUD: `huds/throttle_brake.py`, `huds/steering.py`, `huds/delta.py`, `huds/line_delta.py`, `huds/under_oversteer.py`
- Gemeinsame Zeichen-/Text-/Grid-Helfer in `huds/common.py`

Rendering-Modell:
- `render_split.py` erstellt HUD-Kontext je Frame.
- Unterstützt inkrementelles HUD-Rendering und Stream-Übergabe an FFmpeg.
- HUD-Boxes kommen aus Payload (`hud_boxes`) bzw. free-frame Layout aus `LayoutConfig`.


## 8. Video-Modi

### 8.1 `full`
- Gesamte Runde wird mit Sync/Overlay gerendert.

### 8.2 `cut`
- Kurvensegmente werden aus Telemetrie erkannt (`core/cut_events.py`).
- Segment-Mapping nach Frameindizes.
- Segment-Render + Concat, inkl. definierter Schwarz/Fade-Übergänge.
- Fallbacklogik bei 0 Segmenten vorhanden.

`video_cut` Parameter (Sekunden):
- `video_before_brake`
- `video_after_full_throttle`
- `video_minimum_between_two_curves`


## 9. Coaching-Subsystem

### 9.1 Aufnahme
- `core/irsdk/irsdk_client.py`: robuste IRSDK-Leseschicht.
- `core/irsdk/recorder_service.py`: Aufnahme-Lifecycle/Threading/Run-Erkennung.
- Persistenz in Coaching-Storage inkl. Meta/Debug-Dateien.

### 9.2 Struktur und Analyse
- `core/coaching/storage.py`: Ordnernamen, Session-Marker, Finalisierung.
- `core/coaching/parquet_writer.py`: typed Parquet Writer.
- `core/coaching/lap_segmenter.py`: Lap-Segmentierung.
- `core/coaching/lap_metrics.py`: Metriken pro Lap/Run.
- `core/coaching/indexer.py`: Baumindex (Session/Run/Lap) fuer UI-Browser.

### 9.3 UI
- `ui/coaching_browser.py` zeigt aggregierte Summaries und Lap-Details.
- `ui/app.py` bietet Recording-Settings inkl. Storage/Retention/Warnungen.


## 10. Konfiguration (`config/defaults.ini` + `config/user.ini`)

### 10.1 Layering
- `defaults.ini` liefert Projektdefaults.
- `user.ini` uebersteuert selektiv Werte.
- Lese-Reihenfolge: defaults -> user.

### 10.2 Aktive Sektionen
- `[video_compare]`
- `[video_cut]`
- `[coaching_recording]`

### 10.3 Wichtige Validierungen
- Zahlreiche Werte werden geklammert (z.B. Update-Hz, Alpha, Prozentbereiche).
- Ungueltige `video_cut`-Werte:
  - In `cut` Modus: Fehler
  - In `full` Modus: Warnung + Fallback auf Defaults


## 11. Build, Runtime und Abhaengigkeiten

### 11.1 Python-Abhaengigkeiten (`requirements.txt`)
- `numpy`
- `opencv-python`
- `Pillow`
- `pyirsdk`
- `pyarrow`

### 11.2 Runtime
- Windows ist Zielplattform der verteilten EXE.
- FFmpeg/FFprobe werden bevorzugt aus Bundles geladen.

### 11.3 Packaging
- `packaging/iWAS_onefolder.spec` fuer PyInstaller one-folder Build.
- `packaging/build_onefolder.ps1` als Build-Helfer.


## 12. Logging, Debug, Diagnose

### 12.1 Logs
- Zentrale Laufzeitlogs unter `_logs/` (Dateiname je Komponente).
- Render-Service schreibt Stage-Dauern und Fortschritt.

### 12.2 Diagnostik
- `core/diagnostics.py` exportiert Diagnose-Bundle (inkl. redigierter Inhalte).
- OneDrive-Risikoerkennung fuer problematische Sync-Pfade.

### 12.3 Relevante ENV-Flags (Auszug)
- `IRVC_NO_MSGBOX`
- `IRVC_DEBUG_SWALLOWED`
- `IRVC_DEBUG`
- `IRVC_HUD_DEBUG`
- `IRVC_FFMPEG_LIVE`
- `IRVC_DEBUG_MAX_S`
- `IRVC_KEEP_CUT_SEGMENTS`
- `SYNC6_*` (Sync-Tuning)
- `IWAS_DEBUG_COACHING`


## 13. Bekannte technische Auffaelligkeiten

- `README.md` enthaelt Merge-Konfliktmarker und ist aktuell nicht die verlaessliche Referenz.
- Historische Sprint-Dokumente beschreiben Zwischenstaende; fuer Betrieb/Weiterentwicklung gilt diese konsolidierte Doku plus Code.


## 14. Entwickler-Checkliste fuer Aenderungen

1. Vertrag zuerst: `core/models.py` und Migrationspfad pruefen.
2. Persistenzpfad pruefen: `defaults.ini` + `user.ini` + JSON-State.
3. UI->Render Payload verifizieren (`core/render_service.py`, `src/main.py`).
4. Bei HUD-Aenderungen: HUD-Key-Namen stabil halten und `hud_registry`/`hud_layout` pruefen.
5. Bei Cut/Sync-Aenderungen: `core/cut_events.py`, `sync_map.py`, Debug-Artefakte validieren.
6. Bei Packaging/Runtime: FFmpeg-Aufloesung im EXE-Layout testen.


## 15. Datei-Landkarte (schnell)

- `src/app_entry.py`: Top-Level Entry (UI/Render Umschalter)
- `src/main.py`: Render entry + Payload-Verarbeitung
- `src/ui/app.py`: GUI Hauptdatei
- `src/ui/controller.py`: UI-Aktionen und Service-Wiring
- `src/core/render_service.py`: UI-seitiger Render-Subprozess
- `src/features/render_split.py`: zentrale Render-Orchestrierung
- `src/core/output_geometry.py`: Layout-Geometrie
- `src/core/ffmpeg_plan.py`: Filtergraph/FFmpeg-Plan
- `src/core/encoders.py`: Encoder-Erkennung/Fallback
- `src/core/csv_g61.py`: CSV-Laden/Parsing
- `src/core/resample_lapdist.py`: Resampling
- `src/core/sync_map.py`: LapDist-Sync-Mapping
- `src/core/cut_events.py`: Kurvenerkennung/Cut-Segmente
- `src/core/persistence.py`: INI/JSON Persistenz
- `src/core/models.py`: Datenmodelle + Migrationen
- `src/core/diagnostics.py`: Diagnose-Bundles
- `config/defaults.ini`: globale Defaults
- `config/user.ini`: User-Overrides


## 16. Schluss

Diese Datei ersetzt keine fachlichen Sprint-Notizen, aber sie ist die technische, codebasierte Referenz fuer den aktuellen Ist-Stand.
Bei Konflikten zwischen alten Dokus und Code gilt der Code.
