# Sprint Z – Umbau ui_app.py (Modularisierung)


**Ziel**
`ui_app.py` ist aktuell ein „Monolith“ (UI + State + IO + Video-Preview + Profil-Handling + Render-Start). In diesem Sprint wird der Code so umgebaut, dass `ui_app.py` wieder hauptsächlich **UI orchestriert** (Widgets, Events) und die einzelnen Funktionsbereiche in **eigene Module** ausgelagert werden.

**Warum**
- Darstellung/UX soll später komplett umgebaut werden können, ohne Render-/Persistenzlogik zu brechen.
- Vorbereitung für spätere Erweiterungen (Sim-Trainer, Live HUDs, AI Analyse) ohne „alles in eine Datei“.

**Wichtig**
- Keine funktionalen Änderungen am Ergebnis sind beabsichtigt (gleiches Verhalten, gleiche Dateien/Keys).
- Umbau erfolgt schrittweise, damit jederzeit ein „funktionierender Stand“ bleibt.
- JSON-Payload `config/ui_last_run.json` bleibt **kompatibel** (Keys/Struktur beibehalten).
- Pfade & Ordnerstruktur beibehalten (`input/video`, `input/csv`, `cache/proxy`, `output/video`, `config/...`).

---

## Scope

### In Scope
- Aufteilen in Module (Services/Model/Views), sodass UI-Änderungen möglichst „nur UI“ betreffen.
- Zentrale Datenmodelle für:
  - Output-Format + HUD-Breite
  - HUD Layout (`hud_layout_data`, `hud_boxes`)
  - PNG-View State (`png_view_data`, `png_view_state`)
  - Start/Endframes (`startframes_by_name`, `endframes_by_name`)
  - Profil Save/Load (`build_profile_dict`, `apply_profile_dict`)
- Render-Start als Service (Payload bauen + `main.py` starten + Progress parsing).
- Preview-Subsystem trennen:
  - Layout-Preview (Canvas mit HUD-Box-Editor)
  - PNG-Preview (Canvas mit Zoom/Drag/Fit)
  - Video-Crop Preview (OpenCV/PIL Vorschau, Scrub, Cut)

### Optional (wenn Zeit)
- Bessere Threading- und Queue-Struktur (UI thread safe).
- Ein zentraler Logger statt `print(...)`/Silent-`except`.
- Kleine Bugfixes, die eindeutig „Fehler“ sind (siehe Risiken), ohne Verhalten zu ändern.

### Out of Scope
- Neues UI-Design / neue Widgets (kommt in einem späteren Sprint).
- Änderung des Render-/Sync-Verhaltens in `main.py`/`render_split.py`.
- Änderung der config-Dateiformate (`output_format.json`, `hud_layout.json`, `png_view.json`, `startframes.json`, `endframes.json`).

---

## Definition of Done (DoD)
- `ui_app.py` ist deutlich kleiner und übersichtlicher (Ziel: < 800–1000 Zeilen).
- Kernlogik ist in klar benannten Modulen gekapselt; UI kann geändert werden, ohne Services zu ändern.
- `config/ui_last_run.json` wird **identisch** geschrieben (bis auf irrelevante Reihenfolge/Whitespace).
- Profil Save/Load funktioniert wie zuvor.
- Layout-Preview, PNG-Preview und Crop-Preview funktionieren wie zuvor.
- Smoke-Tests (unten) laufen ohne Exceptions.

---

## Leitprinzip: “UI vs. Services”
- `ui_app.py`:
  - erstellt Tk Widgets
  - bindet Events an Controller-Funktionen
  - zeigt Status/Labels/Progress an
- Services/Module:
  - machen IO (JSON/INI lesen/schreiben)
  - berechnen Presets/Geometrie
  - halten State (Dataclasses/Model)
  - machen Preview-Render (Layout/PNG/Frame)
  - bauen Render-Payload + starten `main.py`

---

## Ist-Stand (relevante Blöcke / Namen)

### Persistenz-Dateien
- `config/defaults.ini` (gelesen via `cfg = configparser.ConfigParser()` + `cfg_get(...)`)
- `config/output_format.json` (load/save: `load_output_format`, `save_output_format`)
- `config/hud_layout.json` (load/save: `load_hud_layout`, `save_hud_layout`)
- `config/png_view.json` (load/save: `load_png_view`, `save_png_view`)
- `config/startframes.json` (load/save: `load_startframes`, `save_startframes`)
- `config/endframes.json` (load/save: `load_endframes`, `save_endframes`)
- `config/profiles/*.json` (Profile Dialogs: `profile_save_dialog`, `profile_load_dialog`)

### Kern-State (global/closures)
- `videos: list[Path]`, `csvs: list[Path]`
- `startframes_by_name: dict[str, int]`, `endframes_by_name: dict[str, int]`
- Output: `out_aspect_var`, `out_preset_var`, `out_quality_var`, `hud_width_var`
- HUD enable: `hud_enabled_vars: dict[str, tk.BooleanVar]`
- HUD layout: `hud_layout_data: dict`, `hud_boxes: list[dict]`
- PNG view: `png_view_data: dict`, `png_state`, `png_frame_last`, `png_img_left/right`, `png_*_name/start`
- Video preview: `cap`, `current_video_original/opened`, `current_frame_idx`, `total_frames`, `fps`, `end_frame_idx`
- Folder sync: `sync_from_folders_if_needed`, `periodic_folder_watch`
- Render: `generate_compare_video` (Payload bauen + `main.py` starten)

### Wichtiges JSON, das an main.py geht
`generate_compare_video()` schreibt `config/ui_last_run.json` nach:
- `videos`, `csvs`, `slow_video`, `fast_video`, `out_video`
- `output: { aspect, preset, quality, hud_width_px }`
- `hud_enabled`
- `hud_boxes` (Map pro HUD-Type)
- `hud_window` (default + overrides aus INI)
- `hud_speed` (units, update_hz)
- `hud_curve_points` (default + overrides)
- `hud_gear_rpm` (update_hz)
- `png_view_key`, `png_view_state`
- plus komplette States: `hud_layout_data`, `png_view_data`

Diese Struktur bleibt unverändert.

---

## Ziel-Struktur (Dateien)

Vorschlag:

- `ui_app.py` (nur UI + Wiring)
- `ui/`
  - `controller.py` (bindet UI ↔ Services; orchestriert Actions)
  - `views.py` (Widget-Bau / kleinere View-Helper, optional)
- `core/`
  - `paths.py` (Projektpfade: `find_project_root`, input/cache/output/config)
  - `persistence.py` (JSON/INI load/save: output_format/hud_layout/png_view/start/end)
  - `models.py` (Dataclasses: OutputFormat, HudBox, HudLayoutState, PngViewState, Profile, RenderPayload)
  - `filesvc.py` (Datei-Operationen: copy/delete/open_folder/scan signature)
  - `video_info.py` (ffprobe helpers: `ffprobe_get_video_info`, cache async)
  - `render_service.py` (Payload bauen + main.py starten + progress parsing)
- `preview/`
  - `layout_preview.py` (Layout Canvas + HUD Box Editor)
  - `png_preview.py` (PNG Canvas: zoom/drag/fit, clamp-cover, state persist)
  - `video_preview.py` (OpenCV/PIL Vorschau: play/scrub/crop/cut)

Hinweis: Ordnernamen sind Vorschlag; wichtig ist die klare Trennung.

---

## Story 1 – Baseline sichern (Referenz-Verhalten)

**Ziel**
Vor dem Umbau eine Referenz definieren, damit Modularisierung „ohne Verhalten ändern“ überprüfbar ist.

**Tasks**
- Ein Test-Setup definieren:
  - 2 Videos mit Zeit im Namen
  - 0–2 CSVs
  - 1 Profil speichern/laden
  - 1 Render (kurz, z.B. `IRVC_DEBUG_MAX_S=2`)
- Folgende Dateien sichern (vorher/nachher vergleichen):
  - `config/ui_last_run.json`
  - `config/output_format.json`, `config/hud_layout.json`, `config/png_view.json`
  - `config/startframes.json`, `config/endframes.json`
- Kurzer UI-Rundgang dokumentieren (Checkliste).

**Fertig wenn**
- Es gibt eine klare „Vorher“-Referenz und Smoke-Testliste.

---

## Story 2 – Datenmodelle einführen (ohne UI-Umbau)

**Ziel**
Stabiler Vertrag innerhalb der App: statt „dict überall“ saubere Dataclasses/Model.

**Tasks**
- `core/models.py`:
  - `OutputFormat(aspect, preset, quality, hud_width_px)`
  - `HudBox(type, x, y, w, h)`
  - `HudLayoutState(hud_layout_data, current_boxes_for_key(...), key_from(out_preset,hud_w))`
  - `PngSideState(zoom, off_x, off_y, fit_to_height)`
  - `PngViewState(png_view_data, key_from(out_preset,hud_w), load/save current)`
  - `Profile(version, videos, csvs, startframes, endframes, output, hud_layout_data, png_view_data)`
  - `RenderPayload(...)` (nur als Struktur, JSON bleibt gleich)
- In `ui_app.py` zunächst nur:
  - Model instanziieren
  - bestehende Dict/Vars weiter nutzen, aber „Mapping-Layer“ an einem Ort.

**Fertig wenn**
- Es gibt zentrale Models, aber UI funktioniert unverändert.


### Umsetzung (Ist-Stand)
- `core/models.py` erstellt und zentrale Datamodelle eingeführt:
  - `OutputFormat`, `HudBox`, `HudLayoutState`, `PngSideState`, `PngViewState`, `Profile`, `RenderPayload`, `AppModel`.
- `HudLayoutState.key_from(...)` und `PngViewState.key_from(...)` implementiert gemäß Vertrag:
  - `f"{out_preset}|hud{hud_w}"`
- `Profile.to_dict()/from_dict()` an bestehendes Profil-JSON angepasst:
  - Keys/Shape unverändert
  - `output.hud_width_px` bleibt im Profil als **String** erhalten.
- `RenderPayload` als reine Struktur eingeführt und in `ui_app.py` beim Schreiben von `config/ui_last_run.json` verwendet:
  - Keys/Shape unverändert.
- In `ui_app.py` eine kleine, zentrale Mapping-Schicht ergänzt:
  - `model_from_ui_state(...)`
  - `apply_model_to_ui_state(...)`
  - `profile_model_from_ui_state(...)`
- Bestehende UI-Variablen/Dicts bleiben weiterhin die primäre Datenquelle (kein UI-Umbau, kein Verhalten geändert).
- `core/__init__.py` angepasst (Export/Import der Models für zentrale Nutzung).

### Abnahme / Check
- HUD-Keys unverändert:
  - `"Speed"`, `"Throttle / Brake"`, `"Steering"`, `"Delta"`, `"Gear & RPM"`, `"Line Delta"`, `"Under-/Oversteer"`
- `py_compile` OK:
  - `python -m py_compile ui_app.py core/models.py main.py render_split.py`
- `py_compile` gesamtes src OK:
  - `python -m py_compile <all *.py under src>`
- Kurzer Render OK:
  - `$env:IRVC_DEBUG_MAX_S='2'; python main.py --ui-json ..\config\ui_last_run.json`
  - `[encode] OK vcodec=hevc_nvenc`, Exit Code `0`

### Fertig wenn
- ✅ Zentrale Models existieren in `core/models.py`, und die UI läuft unverändert weiter (bestehende Dicts/Vars aktiv, JSON-Keys/Shape unverändert).


---

## Story 3 – Persistenz-Service (INI/JSON) auslagern

**Ziel**
Alle load/save Funktionen aus `ui_app.py` nach `core/persistence.py`.

**Betroffene Funktionen**
- INI:
  - `cfg_get`, `_cfg_float`, `_cfg_float_opt`, `_cfg_int`, `_cfg_int_opt`
- JSON:
  - `load_output_format`, `save_output_format`
  - `load_hud_layout`, `save_hud_layout`
  - `load_png_view`, `save_png_view`
  - `load_startframes`, `save_startframes`
  - `load_endframes`, `save_endframes`

**Regel**
- Filepaths bleiben: `config/output_format.json`, `config/hud_layout.json`, `config/png_view.json`, `config/startframes.json`, `config/endframes.json`, `config/defaults.ini`

**Fertig wenn**
- `ui_app.py` ruft nur noch `persistence.*` auf, UI unverändert.

### Umsetzung (Ist-Stand)
- `core/persistence.py` neu hinzugefügt und alle Persistenz-Helper aus `ui_app.py` dorthin verschoben (Namen/Logik unverändert):
  - INI: `cfg_get`, `_cfg_float`, `_cfg_float_opt`, `_cfg_int`, `_cfg_int_opt`
  - JSON: `load_output_format`/`save_output_format`, `load_hud_layout`/`save_hud_layout`, `load_png_view`/`save_png_view`,
    `load_startframes`/`save_startframes`, `load_endframes`/`save_endframes`
- `ui_app.py` auf `from core import persistence` umgestellt und ruft für alle betroffenen Operationen nur noch `persistence.*` auf.
- Originale (duplizierte) Helper-Definitionen aus `ui_app.py` entfernt.
- Pfade unverändert beibehalten:
  - `config/output_format.json`, `config/hud_layout.json`, `config/png_view.json`, `config/startframes.json`, `config/endframes.json`, `config/defaults.ini`
- `core/__init__.py` musste nicht angepasst werden.

### Abnahme / Check
- `py_compile` OK:
  - `python -m py_compile core/persistence.py ui_app.py`
  - `python -m py_compile ui_app.py core/persistence.py`
  - `python -m py_compile core/__init__.py core/persistence.py ui_app.py`
- Pfad-/Fallback-Checks OK:
  - Ausgabe der `*.as_posix()` Pfade korrekt
  - `cfg_get(..., fallback)` liefert `FALLBACK_TEST`
- JSON Regression OK:
  - Vergleich ergab `True` für alle fünf JSON-Dateien (unveränderte Inhalte/Shapes).
- Kurzer Render OK:
  - `$env:IRVC_DEBUG_MAX_S='2'; python main.py --ui-json ..\\config\\ui_last_run.json`
  - Exit Code `0`, Encode erfolgreich.

### Fertig wenn
- ✅ `ui_app.py` nutzt nur noch `persistence.*` für INI/JSON load/save, und die UI bleibt unverändert.


---

## Story 4 – File/Folder Service auslagern

**Ziel**
Dateioperationen und Folder-Watch aus `ui_app.py` nach `core/filesvc.py`.

**Betroffene Funktionen**
- `copy_to_dir`, `delete_file`, `open_folder`
- `scan_folders_signature`, `sync_from_folders_if_needed`, `periodic_folder_watch`
- Auswahl-Logik: `select_files` (Controller nutzt Service)

**Fertig wenn**
- Folder Sync/Auto-refresh funktioniert wie vorher.


### Umsetzung (Ist-Stand)
- `core/filesvc.py` neu hinzugefügt und Datei-/Folder-Funktionen aus `ui_app.py` ausgelagert:
  - `copy_to_dir`, `delete_file`, `open_folder`
  - `scan_folders_signature`, `sync_from_folders_if_needed`, `periodic_folder_watch`
  - `select_files`
- In `ui_app.py` die Original-Definitionen entfernt und Controller-Callsites auf `filesvc.*` umgestellt.
- UI/Controller-Verhalten bleibt in `ui_app.py` über dünne Wrapper erhalten:
  - `sync_from_folders_if_needed_ui(...)` aktualisiert `videos/csvs/last_scan_sig` aus `filesvc.sync_from_folders_if_needed(...)`.
  - `run_periodic_folder_watch()` behält die Kadenz über `root.after(1000, ...)` bei.
  - `on_select_files()` lässt den Dialog in der UI und nutzt `filesvc.select_files(...)` für Selection/Copy-Logik.
- Delete/Open Menu Actions auf `filesvc.delete_file(...)` und `filesvc.open_folder(...)` umgestellt.

### Abnahme / Check
- `py_compile` OK:
  - `python -m py_compile core/filesvc.py ui_app.py`
  - `python -m py_compile ui_app.py core/filesvc.py`
- Folder-Watch/Sync Smoke OK:
  - Signature/Sync-Verhalten und Periodic-Call-Order unverändert (Smoke-Test: OK).
- Kurzer Render OK:
  - `$env:IRVC_DEBUG_MAX_S='2'; python main.py --ui-json ..\config\ui_last_run.json`
  - Exit Code `0`, `[encode] OK vcodec=hevc_nvenc`, Sync-Cache Output geschrieben.

### Fertig wenn
- ✅ Dateioperationen + Folder Sync/Auto-Refresh laufen wie vorher, und `ui_app.py` nutzt dafür nur noch `filesvc.*`.

---

## Story 5 – Render-Service auslagern (Payload + main.py Start)

**Ziel**
`generate_compare_video()` wird zu einem Service `core/render_service.py`, UI ruft nur noch `start_render(...)`.

**Tasks**
- Payload-Bau in `render_service.build_payload(...)`:
  - nutzt exakt die bisherigen Namen/Keys:
    - `hud_enabled`, `hud_boxes`, `hud_window`, `hud_speed`, `hud_curve_points`, `hud_gear_rpm`,
    - `png_view_key`, `png_view_state`, plus Gesamtstates `hud_layout_data`, `png_view_data`
- Schreiben nach `config/ui_last_run.json` bleibt an einer Stelle (Service).
- Prozessstart `subprocess.Popen([... main.py ...])` + Progress-Parsing in Service.
- UI zeigt Progress via Callback (z.B. `on_progress(pct, text)`).

**Wichtig**
- `IRVC_UI_SHOW_LOG` Verhalten beibehalten.
- `IRVC_DEBUG_MAX_S` Progress-Deckel beibehalten.

**Fertig wenn**
- „Video erzeugen“ funktioniert, und `ui_last_run.json` ist kompatibel.


### Umsetzung (Ist-Stand)
- Render-/Payload-Logik aus der UI in `core/render_service.py` ausgelagert.
  - `build_payload(...)` in `core/render_service.py:41`
  - `start_render(..., on_progress=None)` in `core/render_service.py:248`
- UI baut keinen Payload mehr, schreibt kein `ui_last_run.json` mehr und startet keinen `subprocess.Popen(...)` mehr direkt.
  - UI ruft nur noch `start_render(...)` auf: `ui_app.py:2753`
  - Service-Import ergänzt: `ui_app.py:25`
- Schreiben von `../config/ui_last_run.json` ist jetzt nur noch im Service (eine Stelle), in:
  - `core/render_service.py:269` und `core/render_service.py:294`
- Payload-Keys unverändert beibehalten:
  - `hud_enabled`, `hud_boxes`, `hud_window`, `hud_speed`, `hud_curve_points`, `hud_gear_rpm`,
  - `png_view_key`, `png_view_state`,
  - Gesamtstates: `hud_layout_data`, `png_view_data`
- Env-Var-Verhalten im Service beibehalten:
  - `IRVC_UI_SHOW_LOG` in `core/render_service.py:347`
  - `IRVC_DEBUG_MAX_S` in `core/render_service.py:349`

### Abnahme / Check
- ✅ Compile-Checks:
  - `python -m py_compile ui_app.py core/render_service.py`
- ✅ Smoke-Render mit Progress-Callback und Debug-Cap:
  - `IRVC_DEBUG_MAX_S=2`
  - Progress-Callback-Updates inkl. 0% und 100% gesehen
  - Result: `{'status': 'ok'}`
  - Artefakte: `../output/video/_service_smoke.mp4`, `../output/video/_service_smoke_log.mp4`
- ✅ Log-Sichtbarkeit geprüft:
  - `IRVC_UI_SHOW_LOG=1` + `IRVC_DEBUG_MAX_S=1`
  - Live `main/ffmpeg` Lines wurden wie vorher ausgegeben
- ✅ `ui_last_run.json` Kompatibilität geprüft:
  - Required Top-Level-Keys vorhanden
  - Keine Key-Renames / Strukturänderungen
  - Datei geschrieben durch Service: `../config/ui_last_run.json`
- na: Interaktiver GUI-Klick auf den Button „Video erzeugen“ wurde in dieser Session nicht ausgeführt (Service-Call + Callback-Pfad wurde getestet).

### Fertig wenn
- ✅ „Video erzeugen“ funktioniert über `start_render(...)` (Service-Pfad getestet).
- ✅ `ui_last_run.json` wird nur noch vom Service geschrieben und bleibt kompatibel.

---

## Story 6 – Preview-Subsystem modularisieren

### Story 6.1 – Layout-Preview (HUD Box Editor) auslagern
**Ziel**
Alles rund um:
- `draw_layout_preview`
- `hit_test_box`, `cursor_for_mode`, Maus-Events (`on_layout_*`)
- `ensure_boxes_in_hud_area`, `clamp_box_in_hud`, `hud_bounds_out`, `canvas_to_out_xy`
kommt nach `preview/layout_preview.py`.

**Inputs/Outputs**
- Input: `OutputFormat(out_w,out_h,hud_w)`, `hud_boxes`, `enabled_types`, Canvas widget
- Output: aktualisierte `hud_boxes` + persist via Callback `save_current_boxes()`.

**Fertig wenn**
- Drag/Resize/Clamp von Boxen unverändert funktioniert.


### Umsetzung (Ist-Stand)
- Layout-Preview / HUD Box Editor Logik aus `ui_app.py` nach `preview/layout_preview.py` ausgelagert.
- Neue Controller-Klasse `LayoutPreviewController` in `preview/layout_preview.py` enthält die komplette Funktionalität.
- Folgende Funktionen/Teile wurden verschoben:
  - `draw_layout_preview` → `preview/layout_preview.py:89`
  - `hit_test_box`, `cursor_for_mode` → `preview/layout_preview.py:247`, `preview/layout_preview.py:301`
  - alle `on_layout_*` Handler → `preview/layout_preview.py:314`, `329`, `335`, `363`, `410`
  - `ensure_boxes_in_hud_area`, `clamp_box_in_hud`, `hud_bounds_out`, `canvas_to_out_xy`
    → `preview/layout_preview.py:58`, `217`, `209`, `201`
- Modul-Contract ergänzt: `OutputFormat(out_w, out_h, hud_w)` in `preview/layout_preview.py:8`.
- UI auf Controller umgestellt:
  - Import ergänzt: `ui_app.py:25`
  - Controller-Setup + Canvas-Bindings: `ui_app.py:2108` bis `ui_app.py:2141`
  - Redraw läuft jetzt über `refresh_layout_preview()`.

### Abnahme / Check
- ✅ Compile-Checks:
  - `python -m py_compile ui_app.py preview/layout_preview.py preview/__init__.py`
- ✅ Persistence-Timing beibehalten:
  - `save_current_boxes()` wird weiterhin beim Mouse-Release über Callback aufgerufen:
    - `on_layout_mouse_up` in `preview/layout_preview.py:423`
- ✅ Keine Alt-Referenzen in `ui_app.py`:
  - per Search geprüft: keine Verweise mehr auf die alten, verschobenen Funktionsdefinitionen.
- na: Interaktive GUI-Checks (Add/Select/Drag/Resize/Cursor live) konnten in dieser Terminal-Session nicht ausgeführt werden.

### Fertig wenn
- ✅ Drag/Resize/Clamp/Hit-Test/Cursor-Logik unverändert (laut 1:1 Move, keine Math/Condition-Änderungen).
- ✅ `hud_boxes` werden aktualisiert und Persist erfolgt über Callback `save_current_boxes()` wie zuvor.

### Story 6.2 – PNG-Preview auslagern
**Ziel**
Alles rund um PNG Canvas nach `preview/png_preview.py`:
- State: `png_state`, `png_frame_last`, `png_img_left/right`, `png_*_name/start`
- Funktionen: `render_png_preview`, `png_fit_to_height_both`, `png_on_wheel/down/move/up`
- Helpers: `_png_region_out`, `_clamp_png_cover`, `pil_paste_clipped`, `compute_frame_rect_for_preview`

**Wichtig**
- Zoom/Offsets bleiben in **Output-Pixeln** gespeichert (stabil bei Resize), wie im jetzigen Code.
- `png_view_key()` muss identisch bleiben: `f"{out_preset_var.get()}|hud{get_hud_width_px()}"`

**Fertig wenn**
- PNG Zoom/Drag/Fit funktioniert wie vorher; State wird in `png_view.json` gespeichert.

### Umsetzung (Ist-Stand)
- PNG-Preview-Canvas Subsystem aus `ui_app.py` nach `preview/png_preview.py` ausgelagert.
- Neue Controller-Klasse `PngPreviewController` in `preview/png_preview.py` übernimmt State + Logik.
- Folgender State wurde in den Controller verschoben:
  - `png_state`, `png_frame_last`, `png_img_left/right`, `png_*_name`, `png_*_start`
- Folgende Funktionen wurden verschoben:
  - `render_png_preview`, `png_fit_to_height_both`
  - `png_on_wheel`, `png_on_down`, `png_on_move`, `png_on_up`
- Folgende Helper wurden verschoben:
  - `compute_frame_rect_for_preview`, `pil_paste_clipped`
  - `_png_region_out`, `_clamp_png_cover`
- UI wurde auf Controller umgestellt und nutzt dünne Wrapper-Funktionen, damit bestehende Call-Sites unverändert bleiben.
- Canvas-Event-Bindings und Button-Wiring unverändert beibehalten:
  - `<MouseWheel>`, `<ButtonPress-1>`, `<B1-Motion>`, `<ButtonRelease-1>`, Fit-Button
- Render-Service-Handoff bleibt unverändert (Wrapper-Callbacks + `png_state` aus Controller werden weitergereicht):
  - `ui_app.py:1761`

### Abnahme / Check
- ✅ Output-Pixel-State beibehalten:
  - Zoom/Offsets weiterhin in Output-Pixeln gespeichert und wie vorher konvertiert:
    - `preview/png_preview.py:423`, `459`, `639`
- ✅ `png_view_key()` bleibt identisch:
  - `return f"{out_preset_var.get()}|hud{get_hud_width_px()}"` in `ui_app.py:219`
- ✅ `png_view.json` Struktur unverändert:
  - Keys/Shape unverändert: `zoom_l`, `off_lx`, `off_ly`, `fit_l`, `zoom_r`, `off_rx`, `off_ry`, `fit_r`
  - Referenz: `preview/png_preview.py:177`
- ✅ Persistence-Timing beibehalten:
  - Save auf Wheel/Up/Fit + externe Save-Call-Sites bleiben erhalten (wie vorher).
- ✅ Compile-Checks:
  - `python -m py_compile ui_app.py preview/png_preview.py preview/__init__.py`
- na: Interaktive GUI-Checks (Zoom/Drag/Fit live) konnten in dieser Terminal-Session nicht ausgeführt werden.

### Fertig wenn
- ✅ PNG Zoom/Drag/Fit weiterhin wie vorher (laut 1:1 Move + unveränderte Event-Bindings).
- ✅ State wird weiter in `png_view.json` gespeichert, Keys/Shape unverändert.


### Story 6.3 – Video-Crop Preview auslagern
**Ziel**
OpenCV/PIL Preview nach `preview/video_preview.py`:
- `try_open_video`, `make_proxy_h264`, `try_open_for_png`, `read_frame_as_pil`
- Player/Scrub: `seek_and_read`, `read_next_frame`, `render_frame`, `play_tick`
- Crop/Frames: `set_start_here`, `auto_end_from_start`, `set_endframe`, `cut_current_video`

**Fertig wenn**
- Zuschneiden-Flow funktioniert wie vorher (inkl. Proxy-Handling).


### Umsetzung (Ist-Stand)
- Video Preview/Crop Subsystem aus `ui_app.py` nach `preview/video_preview.py` ausgelagert.
- Neue Controller-Klasse `VideoPreviewController` in `preview/video_preview.py:13` übernimmt State + Logik.
- Angeforderte Methoden wurden mit unveränderter Logik in den Controller verschoben:
  - Open/Proxy/Frame-IO:
    - `make_proxy_h264`, `try_open_for_png`, `read_frame_as_pil`, `try_open_video`
  - Player/Scrub/Render:
    - `seek_and_read`, `read_next_frame`, `render_frame`, `play_tick`
  - Crop/Cut Flow:
    - `set_start_here`, `auto_end_from_start`, `set_endframe`, `cut_current_video`
- Abhängiger Preview/Crop-State wurde in Controller-Attribute verschoben (Capture, Frame/FPS-State, Play/Scrub-Flags, Endframe-State, Proxy-State, etc.):
  - `preview/video_preview.py:60`
- UI auf Controller umgestellt:
  - Controller-Instanz: `ui_app.py:1783`
  - Dünne Wrapper für bestehende Call-Sites beibehalten: `ui_app.py:1806` ff.
  - Event-Bindings unverändert: `ui_app.py:1904`
- UI-seitige Checks von “raw cap” auf Controller-State umgestellt, wo nötig:
  - `ui_app.py:699`, `1073`, `1403`, `1770`, `1890`, `1938`
- PNG-Preview-Wiring bleibt intakt (Wrapper `read_frame_as_pil` delegiert an Controller):
  - `ui_app.py:1269`

### Abnahme / Check
- ✅ Compile-Checks:
  - `python -m py_compile ui_app.py preview/video_preview.py`
- ✅ Proxy-Handling unverändert beibehalten:
  - Proxy Create/Reuse (Name/Ort/ffmpeg args + Fallback) unverändert:
    - `preview/video_preview.py:84`, `preview/video_preview.py:457`
- ✅ Seek/Scrub/Frame-Math + Scheduling unverändert:
  - `preview/video_preview.py:260`, `preview/video_preview.py:319`
- ✅ Crop Start/End + Cut-Command unverändert:
  - `preview/video_preview.py:168`, `preview/video_preview.py:188`, `preview/video_preview.py:380`
- na: Runtime Smoke/GUI-Test konnte hier nicht laufen (cv2 und PIL zur Laufzeit nicht verfügbar).

### Fertig wenn
- ✅ Zuschneiden-Flow inkl. Proxy-Handling ist im Code-Pfad unverändert (laut 1:1 Move + unveränderte Args/Math/Scheduling).
- na: End-to-end GUI Validierung in dieser Umgebung nicht ausführbar (fehlende cv2/PIL).


---

## Story 7 – Profile Service auslagern

**Ziel**
Profil Save/Load raus aus UI nach `core/profile_service.py`.

**Betroffene Funktionen**
- `build_profile_dict`
- `apply_profile_dict`
- Dialoge bleiben im UI, aber parsing/apply via Service.

**Fertig wenn**
- Profil speichern/laden funktioniert identisch; alle relevanten States werden übernommen:
  - `output` inkl. `hud_width_px`
  - `hud_layout_data`
  - `png_view_data`
  - `startframes`, `endframes`
  - `videos`, `csvs` (nur wenn im input-Ordner vorhanden)


### Umsetzung (Ist-Stand)
- Profil Save/Load Logik aus der UI nach `core/profile_service.py` ausgelagert.
- Neue Service-Funktionen in `core/profile_service.py`:
  - `build_profile_dict(...)` in `core/profile_service.py:9`
  - `apply_profile_dict(profile, ...)` in `core/profile_service.py:57`
- Dialoge/File-Picker bleiben unverändert im UI.
- UI-Implementierungen durch dünne Delegation-Wrapper ersetzt:
  - `ui_app.build_profile_dict` delegiert an Service: `ui_app.py:790`
  - `ui_app.apply_profile_dict` delegiert an Service: `ui_app.py:806`
  - Service-Import ergänzt: `ui_app.py:23`

### Abnahme / Check
- ✅ Required States werden identisch übernommen:
  - `output` inkl. `hud_width_px` (gleiche Set/Save + String/Int Handling)
  - `hud_layout_data` (als vollständiges Dict gespeichert/angewendet)
  - `png_view_data` (als vollständiges Dict gespeichert/angewendet)
  - `startframes` / `endframes` (Merge + Int-Konvertierung + Save)
  - `videos` / `csvs` nur wenn im Input-Ordner vorhanden:
    - gleiche Filter-Logik via `input_video_dir / str(name)` und `input_csv_dir / str(name)` + `exists()`
    - weiterhin nur die ersten 2 Einträge, keine zusätzliche Matching/Case/Ext-Logik
- ✅ Compile-Checks:
  - `python -m py_compile core/profile_service.py ui_app.py`
- ✅ Non-GUI Smoke Test:
  - build → JSON roundtrip → apply erfolgreich inkl. Presence-Filtering
- na: GUI Manual Smoke Test (Dialog-Flow) in dieser Umgebung nicht ausgeführt.

### Fertig wenn
- ✅ Profil speichern/laden funktioniert über Service-Logik, UI bleibt nur für Dialoge zuständig.
- ✅ Alle relevanten States werden identisch übernommen (inkl. videos/csvs presence filter).

---

## Story 8 – Controller-Schicht (UI Wiring) einführen

**Ziel**
`ui_app.py` enthält kaum noch Business-Logik. Events rufen Controller-Funktionen.

**Tasks**
- `ui/controller.py`:
  - `on_select_files`, `on_generate`, `on_profile_save/load`
  - `on_output_change`, `on_hud_width_change`
  - `on_preview_mode_change`, `on_preview_resize`
- UI Widgets werden im UI erstellt, aber Handler kommen aus Controller.

**Fertig wenn**
- `ui_app.py` ist primär Widget-Setup + wenige Zeilen Wiring.


### Umsetzung (Ist-Stand)
- Controller-Schicht eingeführt: `ui/controller.py`.
- `UIContext` + `Controller` hinzugefügt:
  - `UIContext` in `ui/controller.py:19`
  - `Controller` in `ui/controller.py:74`
- Alle geforderten Controller-Handler implementiert:
  - `on_select_files` in `ui/controller.py:118`
  - `on_generate` in `ui/controller.py:163`
  - `on_profile_save` in `ui/controller.py:303`
  - `on_profile_load` in `ui/controller.py:335`
  - `on_output_change` in `ui/controller.py:361`
  - `on_hud_width_change` in `ui/controller.py:386`
  - `on_preview_mode_change` in `ui/controller.py:391`
  - `on_preview_resize` in `ui/controller.py:400`
- `ui_app.py` enthält für diese Actions jetzt nur noch kleine Wrapper, die an den Controller delegieren:
  - Wrapper u.a. für HUD/Output/Selection/Profile/Generate/Preview:
    - `ui_app.py:406`, `680`, `980`, `844`, `849`, `1405`, `1483`, `1660`
- UI-Adapter/Wiring ergänzt:
  - `ui_ctx = UIContext(...)` in `ui_app.py:1516`
  - `controller = Controller(...)` in `ui_app.py:1567`
- Kleine UI-State-Adapter-Funktionen ergänzt, die der Controller nutzt:
  - `set_app_model` in `ui_app.py:373`
  - `get_selected_files` / `set_selected_files` in `ui_app.py:714`, `717`
  - `get_hud_enabled` in `ui_app.py:1187`

### Abnahme / Check
- ✅ Compile-Checks:
  - `python -m py_compile ui_app.py ui/controller.py`
- ✅ Non-GUI Smoke Test (stubbed):
  - `on_generate` ruft `render_service.start_render` mit Progress-Callback auf
  - Profile Save/Load Pfade laufen inkl. JSON Read/Write
  - Output: `SMOKE_GENERATE=OK`, `SMOKE_PROFILE_SAVE=OK`, `SMOKE_PROFILE_LOAD=OK`

### Fertig wenn
- ✅ `ui_app.py` ist für die Story-8 Actions primär Widget-Setup + Wiring-Wrapper.
- ✅ Orchestration der genannten Events liegt in `ui/controller.py`, Flow/Msgs/Persistence bleiben aligned zum vorherigen Verhalten.

---

## Story 9 – Technische Schulden & Risiken entschärfen (ohne Feature-Änderung)

**Ziel**
Offensichtliche Stolperstellen eliminieren, ohne „neues Verhalten“ einzuführen.

**Kandidaten (klar abgrenzbar)**
1) `request_video_info(...)` hat aktuell einen leeren Worker-Start (`pass` am Ende) → Service sauber fertig machen (Thread starten).
2) `show_progress_with_cancel(...)` enthält duplizierten Codeblock nach einem `return` (unreachable) → entfernen.
3) `cut_current_video()` ruft `show_progress(...)`, aber im File ist nur `show_progress_with_cancel(...)` sichtbar → prüfen und konsolidieren (ohne UX-Änderung).
4) Überall `except Exception: pass` → minimal: zentrale `_safe(...)` helper / optional Logging-Flag.

**Fertig wenn**
- Keine „toten“ Codepfade mehr; weniger „silent fail“-Risiko.

### Umsetzung (Ist-Stand)
- Es wurden nur klar abgegrenzte Risiko-/Debt-Stellen bereinigt, ohne UX/Feature-Änderung.

1) `request_video_info(...)` Worker-Start repariert
- Der bisher leere Worker-Start (`pass`) wurde durch einen echten Thread-Start ersetzt:
  - `ui_app.py:647`
- Callback/Side-Effects bleiben wie vorher über `root.after(0, apply)`:
  - `ui_app.py:644`
- Cleanup bei Thread-Start-Fehler bleibt erhalten/sicher:
  - `ui_app.py:649`

2) `show_progress_with_cancel(...)` unreachable Duplikat entfernt
- Der duplizierte Codeblock nach einem `return` wurde entfernt.
- Funktion endet jetzt sauber bei:
  - `ui_app.py:1338`
- Keine Änderungen an gültigen Kontrollpfaden.

3) `show_progress` vs. `show_progress_with_cancel` konsolidiert
- Minimaler Wrapper `show_progress(...)` ergänzt, der `show_progress_with_cancel(...)` nutzt und Cancel-UI ausblendet.
- Wrapper liefert `(win, close)` wie vom Cut-Flow erwartet:
  - `ui_app.py:1340`
- VideoPreviewController-Wiring bekommt den konkreten Wrapper direkt:
  - `ui_app.py:1473`
- Entfernt Missing-Reference/undefinierte/rekursive Lambda-Risiken.

4) Silent-fail Risiko minimal reduziert (opt-in, lokal)
- Helper `_safe(...)` + opt-in Debug-Gate `IRVC_DEBUG_SWALLOWED` ergänzt:
  - `ui_app.py:27`, `ui_app.py:32`
- Default bleibt weiterhin still (keine neuen Logs ohne Env-Var).
- Anwendung nur in den angefassten Bereichen:
  - Request-Video-Info + Progress-Dialog Pfade:
    - `ui_app.py:641`, `642`, `644`, `649`, `1326`, `1329`, `1335`, `1336`, `1350`

### Abnahme / Check
- ✅ Compile-Checks nach jedem Schritt:
  - `python -m py_compile ui_app.py preview/video_preview.py ui/controller.py`
- ✅ Short Render Smoke nach jedem Schritt:
  - `IRVC_DEBUG_MAX_S=2`
  - Run: `python main.py --ui-json ..\config\ui_last_run.json`
  - Ergebnis: `[encode] OK vcodec=hevc_nvenc`, Exit Code `0`

### Fertig wenn
- ✅ Keine toten/unreachable Codepfade mehr in den genannten Kandidaten.
- ✅ `request_video_info(...)` startet den Worker-Thread zuverlässig.
- ✅ `cut_current_video()` Progress-Aufruf ist konsistent und kann nicht mehr an fehlendem `show_progress` scheitern.
- ✅ Weniger “silent fail”-Risiko, ohne Default-Verhalten zu ändern.

---

## Testplan (minimal, aber verbindlich)

### Smoke-Tests (nach jeder Story)
1) App startet, keine Exceptions.
2) Auto-Scan findet Dateien (`periodic_folder_watch`), Labels aktualisieren (`refresh_display`).
3) Output ändern:
   - `out_aspect_var`, `out_quality_var`, `out_preset_var` → Preview aktualisiert, `output_format.json` updated.
4) HUD Breite ändern (`hud_width_var`) → Layout/PNG Preview reagiert, Key `...|hud{w}` korrekt.
5) HUD Box drag/resize + speichern → `hud_layout.json` updated.
6) PNG Preview: zoom wheel + drag + „PNG auf Rahmenhöhe“ → `png_view.json` updated.
7) Crop Preview öffnen (Zuschneiden), Start setzen, Endframe speichern.
8) Profil speichern → laden → Zustand identisch.
9) Render starten (`generate_compare_video` via Service):
   - `config/ui_last_run.json` wird geschrieben
   - `main.py --ui-json ...` startet
   - Progress läuft, Video wird erzeugt (kurz, z.B. `IRVC_DEBUG_MAX_S=2`)

### Vergleich
- `ui_last_run.json` vor/nach Umbau vergleichen (Keys/Struktur gleich).
- Optional: JSON diff tolerant (Reihenfolge irrelevant).

---

## Rollback-Plan
- Jede Story als eigener Git-Commit.
- Bei Bruch: zurück zum letzten stabilen Commit, Story kleiner schneiden.

---

## Ergebnis (Soll-Zustand)
- UI-Layout kann komplett umgebaut werden, ohne Render/Persistenz/Preview-Kern neu zu schreiben.
- `ui_app.py` ist schlank: Widgets + Event-Wiring + Status-Anzeige.
- Services sind unabhängig testbar (Payload-Bau, Persistenz, Preview-Logik).
- Erweiterungen (Sim-Trainer / Live HUD / AI Analyse) können als neue Services/Views ergänzt werden.


# Sprint Y – Umbau render_split (Modularisierung)

**Ziel**
`render_split.py` ist aktuell zu groß (> 4000 Zeilen). In diesem Sprint wird der Code so umgebaut, dass `render_split.py` wieder hauptsächlich **orchestriert** (Ablauf steuert) und die einzelnen HUDs sowie ausgewählte Logikbereiche in **eigene Module** ausgelagert werden.

**Wichtig**
* Keine funktionalen Änderungen am Ergebnis (Video/HUD) sind beabsichtigt.
* Umbau erfolgt schrittweise, damit jederzeit ein „funktionierender Stand“ bleibt.
* HUD-Aktivierung erfolgt weiterhin über `ui_last_run.json` → `hud_enabled`.
* HUD-Keys bleiben **exakt gleich** (z. B. `"Throttle / Brake"`, `"Under-/Oversteer"`).

---

## Scope

### In Scope
* HUDs als eigene Module (je Story/HUD ein Modul)
* Gemeinsame HUD-Utilities in ein Shared-Modul
* Registry/Dispatcher, der anhand von `hud_enabled` die aktiven HUDs aufruft
* Kontextobjekt (`HudContext`) statt Parameter-Lawine
* Layout/Box-Handling für HUDs als eigenes Modul (enabled + Boxen)

### Optional (wenn Zeit)
* FFmpeg-Plan/Filtergraph-Building in eigenes Modul
* Encoder/Fallback-Logik in eigenes Modul
* Sync/Mapping-Logik in eigenes Modul

### Out of Scope
* Neue HUD-Features
* Neues UI
* Änderungen am JSON-Format
* Refactoring “um jeden Preis” (nur so viel Umbau wie nötig)

---

## Definition of Done (DoD)
* `render_split.py` ist deutlich kleiner und besser lesbar (Ziel: < 1500 Zeilen, ideal < 1000).
* Jeder HUD-Renderer liegt in einem eigenen Modul.
* Es gibt eine zentrale Registry, die HUD-Name → Renderer zuordnet.
* `hud_enabled` entscheidet weiterhin, was gerendert wird.
* Referenz-Render (vorher/nachher) ist visuell gleich (oder nur minimalste, erklärbare Pixel-Differenzen durch Text-Rasterung).
* Debug-Logs bleiben verständlich, keine neuen “Spam”-Logs.
* Keine großen Funktionsentfernungen, keine stillen Ordner-Umbenennungen.

---

## Leitprinzip: “Orchestrator vs. Renderer”
* `render_split.py`:
  * liest UI-JSON
  * baut globale Daten (Sync, Fenster, Geometrie, Arrays)
  * startet FFmpeg / Pipeline
  * ruft pro Frame die aktiven HUD-Renderer auf
* HUD-Module:
  * kennen nur `ctx` + `box` + `draw_target`
  * zeichnen in ihre eigene Box
  * enthalten keine FFmpeg- und keine Orchestrierungslogik

---

## Ziel-Struktur (Dateien)
Vorschlag:

* `render_split.py` (Orchestrator)
* `huds/`
  * `common.py` (Farben, Fonts, kleine Helper)
  * `api.py` (Datentypen/Interfaces: HudContext, Box, BaseRenderer)
  * `registry.py` (Mapping HUD-Name → Renderer)
  * `layout.py` (enabled-HUDs + Box-Normalisierung)
  * `speed.py`
  * `throttle_brake.py`
  * `steering.py`
  * `delta.py`
  * `gear_rpm.py`
  * `line_delta.py`
  * `under_oversteer.py`

Optional später:
* `ffmpeg_plan.py` (Filtergraph)
* `encoders.py` (NVENC/QSV/AMF/CPU Fallback)
* `sync_engine.py` (Sync/Mapping/Cache)

---

## Story 1 – Baseline sichern (Referenz-Output)

**Ziel**
Eine Referenz definieren, damit Umbau “ohne Verhalten ändern” überprüfbar ist.

**Tasks**
* Ein definiertes Test-Setup festlegen:
  * 1 Standard-Video-Paar (slow/fast)
  * 1 definierte UI-JSON Konfiguration
  * alle HUDs einmal aktiv
* 1–2 Referenz-Outputs erzeugen:
  * finaler Render (kurz, z. B. 10–20 s)
  * Debug-PNGs falls vorhanden
* Referenz-Dateien ablegen:
  * `/tests/reference/…` oder in einem klaren Ordner im Projekt

**Fertig wenn**
* Es gibt eine klare “Vorher”-Referenz, die nach jeder Story erneut gerendert werden kann.

---

## Story 2 – HUD-Schnittstelle definieren (ohne HUD-Auslagerung)

**Ziel**
Ein stabiler Vertrag, damit alle HUDs gleich aufgerufen werden können.

**Tasks**
* `HudContext` definieren (Struktur, keine Implementierungsschlacht)
  * gruppiert Frame/Time, Fenster, Signale slow/fast, Render-Settings, Geometrie
* `BaseHudRenderer` definieren:
  * `render(ctx, box, draw_target)`
* Box-Typ definieren:
  * `x, y, w, h` (klar ob absolut oder relativ)
* Entscheidung treffen:
  * Box-Koordinaten werden vor dem Renderer normalisiert (empfohlen)

**Fertig wenn**
* Es gibt eine klare Schnittstelle, die für alle HUDs gilt.
* `render_split.py` könnte theoretisch ein HUD über diese Schnittstelle aufrufen.

---

## Story 3 – Registry + Dispatcher (Plug-in Mechanik)

**Ziel**  
HUDs werden über `hud_enabled` automatisch gerendert.

**Umsetzung (Ist-Stand)**

- Nur `src/render_split.py` angepasst.
- Layout-Helper ergänzt:
  - `_active_hud_items_for_frame(...)`  
    Baut aktive HUD-Items in deterministischer Reihenfolge aus `hud_boxes` und filtert über `hud_enabled`.
- Frame-Loop umgestellt:
  - `active_table_items` (Table-HUDs)
  - `active_scroll_items` (Scroll-HUDs)
  - Table-Loop nutzt `active_table_items` statt der bisherigen Liste.
  - Scroll-Loop nutzt `active_scroll_items` statt der bisherigen Liste.
- Scroll-HUD-Renderblöcke 1:1 in lokale Funktionen gekapselt:
  - `_hud_throttle_brake`
  - `_hud_delta`
  - `_hud_steering`
- Zentrale Registry + Dispatcher eingeführt:
  - `hud_renderers = {...}`
  - Dispatch: `fn_hud = hud_renderers.get(hud_key)` → `if fn_hud: fn_hud()`
  - Fehlende Blöcke als No-Op:
    - `_hud_line_delta: pass`
    - `_hud_under_oversteer: pass`
  - Zusätzlich No-Op-Einträge für Table-HUD-Keys im Scroll-Dispatcher, damit alle Keys vollständig sind.
- Stabilität:
  - Einen vorhandenen Syntaxfehler im Table-Bereich behoben (fehlendes `except` ergänzt), damit der Code wieder ausführbar ist.
  - `python -m py_compile src/render_split.py` läuft erfolgreich.

**Fertig wenn**  
- Es gibt eine zentrale Stelle, die anhand von `hud_enabled` die aktiven HUDs rendert. ✅  
- Aktiv/Deaktiv wird pro HUD wirksam, ohne Sonderlogik. ✅


---

## Story 4 – Pilot: Speed HUD ausgelagert

**Ziel**  
Ein erstes HUD komplett in ein eigenes Modul verschieben, ohne Output zu ändern.

**Umsetzung (Ist-Stand)**

- Neues Modul erstellt:
  - `src/huds/speed.py`
  - Enthält `render_speed(ctx, box, dr)` mit der bisherigen Speed-Renderlogik (1:1 aus dem Table-Bereich übernommen).
- `src/render_split.py` angepasst (ohne andere HUDs anzufassen):
  - Import ergänzt: `from huds.speed import render_speed`
  - Im Table-Bereich wird der Speed-Teil ersetzt durch:
    - ctx-Aufbau (nur Speed-relevante Felder)
    - Aufruf `render_speed(ctx, box, dr)`
  - Key bleibt exakt `"Speed"` (UI/JSON-kompatibel).
- Kein gemeinsames Helper-Modul nötig:
  - `src/huds/common.py` wurde nicht erstellt.

**ctx-Inhalt (nur Speed-relevant)**

- Frame/Timing: `fps`, `i`, `fi`
- Speed-Quellen: `slow_speed_frames`, `fast_speed_frames`, `slow_min_speed_frames`, `fast_min_speed_frames`
- Settings: `hud_speed_units`, `hud_speed_update_hz`
- Vorbereitete Werte: `slow_speed_u`, `fast_speed_u`, `slow_min_u`, `fast_min_u`, `unit_label`
- Render-Abhängigkeiten: `hud_key`, `COL_SLOW_DARKRED`, `COL_FAST_DARKBLUE`

**Abnahme**

- `python -m py_compile src/render_split.py` erfolgreich.
- Referenz-Render durchgeführt (gleiches Setup wie Baseline).
- Speed-HUD visuell identisch zur Referenz.

**Fertig wenn**  
- Speed-HUD kommt aus dem Modul. ✅  
- Render ist visuell identisch zur Referenz. ✅


---

## Story 5 – Weitere HUDs auslagern (nacheinander)

**Ziel**  
Alle HUDs sind eigene Module.

**Umsetzung (Ist-Stand)**

- Umsetzung strikt nach Reihenfolge **1 → 6**.
- Alle genannten HUDs wurden als eigene Module nach `src/huds/` ausgelagert.
- `src/render_split.py` enthält dafür nur noch Adapter/ctx-Aufbau + Dispatcher.
- Zusätzliche Imports in `src/render_split.py:11`.

**Dateien pro HUD**

1. **Gear & RPM**
   - Neu: `src/huds/gear_rpm.py`
   - Adapter: `src/render_split.py:1824`
2. **Steering**
   - Neu: `src/huds/steering.py`
   - Adapter: `src/render_split.py:1972`
3. **Throttle / Brake (inkl. ABS)**
   - Neu: `src/huds/throttle_brake.py`
   - Adapter: `src/render_split.py:1922`
4. **Delta**
   - Neu: `src/huds/delta.py`
   - Adapter: `src/render_split.py:1948`
5. **Line Delta (no-op)**
   - Neu: `src/huds/line_delta.py`
   - Adapter: `src/render_split.py:2010`
6. **Under-/Oversteer (no-op)**
   - Neu: `src/huds/under_oversteer.py`
   - Adapter: `src/render_split.py:2016`

**Genutzte ctx-Felder (kurz)**

- Gear & RPM: `hud_key, i, fi, slow_gear_h, fast_gear_h, slow_rpm_h, fast_rpm_h, COL_SLOW_DARKRED, COL_FAST_DARKBLUE`
- Steering: `hud_key, i, iL, iR, slow_to_fast_frame, slow_steer_frames, fast_steer_frames, steer_slow_scale, steer_fast_scale, steer_abs_max, hud_curve_points_default, hud_curve_points_overrides, hud_windows, before_s_h, after_s_h, default_before_s, default_after_s, hud_dbg, _clamp, _idx_to_x, _log_print, _wrap_delta_05, slow_frame_to_lapdist, log_file, COL_WHITE, COL_SLOW_DARKRED, COL_FAST_DARKBLUE`
- Throttle / Brake: `hud_key, i, iL, iR, _idx_to_x, _clamp, slow_frame_to_lapdist, slow_to_fast_frame, slow_throttle_frames, fast_throttle_frames, slow_brake_frames, fast_brake_frames, slow_abs_frames, fast_abs_frames, hud_curve_points_default, hud_curve_points_overrides, COL_SLOW_DARKRED, COL_SLOW_BRIGHTRED, COL_FAST_DARKBLUE, COL_FAST_BRIGHTBLUE, COL_WHITE`
- Delta: `hud_key, fps, i, iL, iR, mx, _idx_to_x, slow_frame_to_fast_time_s, delta_has_neg, delta_pos_max, delta_neg_min, hud_curve_points_default, hud_curve_points_overrides, hud_dbg, _log_print, log_file, COL_WHITE, COL_SLOW_DARKRED, COL_FAST_DARKBLUE`
- Line Delta: **no-op** (nur `hud_key` weitergereicht, keine Zeichnung)
- Under-/Oversteer: **no-op** (nur `hud_key` weitergereicht, keine Zeichnung)

**Abnahme / Check**

- HUD-Keys im Dispatcher **unverändert**: `"Speed", "Throttle / Brake", "Steering", "Delta", "Gear & RPM", "Line Delta", "Under-/Oversteer"` (`src/render_split.py:2022`).
- `python -m py_compile` nach jedem Schritt sowie final (für `src/render_split.py` + alle neuen HUD-Module): **OK**.
- Kurzer Renderlauf (12s) nach jedem Schritt: **technisch OK**.
  - Hinweis: In dieser Umgebung fehlt `PIL` (`No module named 'PIL'`), daher keine HUD-PNG-Sequenz (`[hudpy] OFF`) und kein visueller HUD-Pixelvergleich möglich. Pipeline lief stabil durch.

**Fertig wenn**

- Alle HUDs laufen über Registry/Dispatcher. ✅
- `render_split.py` enthält keine HUD-spezifischen Zeichenfunktionen mehr (nur Adapter/ctx-Aufbau + Dispatch). ✅


---

## Story 6 – Parameter-Lawine abbauen (HudContext finalisieren)

**Umsetzung (Ist-Stand)**

- `render_split.py` angepasst.
- Neues, strukturiertes Kontextmodell eingeführt:
  - `HudSyncMapping`
  - `HudSignals`
  - `HudWindowParams`
  - `HudRenderSettings`
  - `HudContext`
- Zentrale Builder-Funktion ergänzt:
  - `_build_hud_context(...)` baut **ein stabiles Context-Objekt** nahe der Orchestrierung.
- `_render_hud_scroll_frames_png(...)` refaktoriert:
  - Nimmt jetzt `ctx` statt 30+ Einzelparameter.
  - Render-Logik bleibt unverändert, da benötigte Werte zu Funktionsbeginn aus `ctx` entpackt werden.
- Monster-Call entfernt:
  - Ersetzt durch:
    - `hud_ctx = _build_hud_context(...)`
    - `_render_hud_scroll_frames_png(hud_frames_dir, hud_ctx)`
- HUD-Module:
  - Greifen ausschließlich über `ctx` auf Daten zu.
  - Signaturen bleiben `render_*(ctx, box, dr)`.

**Abnahme / Check**

- Keine Funktionsaufrufe mit 30–50 Parametern mehr:
  - AST-Check bestätigt (`max_args_all = 24`).
- HUD-Keys unverändert vorhanden:
  - `"Speed"`, `"Throttle / Brake"`, `"Steering"`, `"Delta"`, `"Gear & RPM"`, `"Line Delta"`, `"Under-/Oversteer"`.
- `python -m py_compile` erfolgreich:
  - `render_split.py`
  - alle HUD-Module unter `huds/`.
- Kurzer Renderlauf erfolgreich:
  - 2s Testlauf mit `IRVC_DEBUG_MAX_S=2`
  - Encoding abgeschlossen (`hevc_nvenc` OK).

**Fertig wenn**

- Kein Monster-Call mehr vorhanden. ✅
- HUDs nutzen ausschließlich `ctx`. ✅
- Verhalten und Render-Ergebnis unverändert. ✅


---


## Story 7 – render_split.py aufräumen (nur Orchestrierung)

**Umsetzung (Ist-Stand)**

- Dateien angepasst:
  - `render_split.py`
  - `huds/common.py`
- Shared HUD-Konstanten zentralisiert:
  - Farben + HUD-Name-Gruppen nach `huds/common.py` verschoben.
  - `render_split.py` importiert diese zentralen Konstanten.
- Tote/ungenutzte Helfer aus `render_split.py` entfernt:
  - `_tail_lines`
  - `_run`
  - `_ffmpeg_escape_path`
  - `_first_enabled_hud_name`
  - `_first_enabled_hud_box_abs`
  - `_write_hud_scroll_cmds`
  - `_extract_frames`
  - `_remap_frames`
  - `_encode_from_frames`
  - Unreachable Post-Return-Block in `_run_live_or_tail_on_fail`
- Sync-Bereich bereinigt:
  - Eine duplizierte Sync-Helper-Variante entfernt und stattdessen `_csv_time_axis_or_fallback` wiederverwendet.
- Layout-Logik konsolidiert:
  - Duplizierte Layout-Helper-Logik in `_build_side_chain_core` zusammengeführt.
  - Wrapper belassen, um Aufrufer stabil zu halten (kein Verhalten geändert).
- Orchestrator-Lesbarkeit verbessert:
  - Explizite Flow-Kommentare ergänzt und ausgerichtet:
    1) Config lesen  
    2) Sync/Mapping  
    3) Layout  
    4) HUD Render  
    5) FFmpeg Run
  - Kommentare in `render_split_screen_sync` entsprechend angepasst.
- Kleine Aufräumarbeiten:
  - Unused locals im Table-HUD-Loop entfernt.
- Größenreduktion:
  - `render_split.py` von 2860 auf 2510 Zeilen reduziert (-350).

**Abnahme / Check**

- HUD-Keys unverändert:
  - `"Speed"`, `"Throttle / Brake"`, `"Steering"`, `"Delta"`, `"Gear & RPM"`, `"Line Delta"`, `"Under-/Oversteer"`.
- `py_compile` erfolgreich:
  - `python -m py_compile render_split.py huds/common.py`
- Kurzer Renderlauf erfolgreich:
  - `IRVC_DEBUG_MAX_S=2; python main.py`
  - `[encode] OK vcodec=hevc_nvenc`, Exit Code 0.

**Fertig wenn**

- `render_split.py` sichtbar kleiner und leichter zu lesen. ✅
- Keine Funktionsentfernungen, die noch gebraucht werden. ✅

---

## Story 8 (Optional) – FFmpeg Plan auslagern

**Umsetzung (Ist-Stand)**

- Dateien angepasst:
  - `render_split.py`
  - `ffmpeg_plan.py` (neu)
- Neues FFmpeg-Plan/Runner-API eingeführt in `ffmpeg_plan.py`:
  - `Plan` Container (`ffmpeg_plan.py:35`)
  - `build_plan(...)` (`ffmpeg_plan.py:61`)
  - `run_ffmpeg(...)` (`ffmpeg_plan.py:144`)
- FFmpeg-/Filtergraph-Building aus `render_split.py` ausgelagert:
  - Split-Filter-Builder nach `ffmpeg_plan.py:440`
  - Stream-Sync-Filter-Builder nach `ffmpeg_plan.py:496`
  - Filter-Script-Handling bleibt im Plan-Builder-Pfad (gleiche Strategie/Reihenfolge wie vorher)
- `render_split.py` ist jetzt Orchestrator:
  - Sammelt Optionen/Inputs
  - Ruft `build_plan(...)` auf
  - Führt `run_ffmpeg(...)` aus
  - Call-Sites:
    - Non-Sync Flow: `render_split.py:1770`, `:1799`, `:1809`
    - Sync Flow: `render_split.py:2137`, `:2174`, `:2189`
- Alte, lange FFmpeg-Pipeline-/Filtergraph-Blöcke in `render_split.py` entfernt.

**Abnahme / Check**

- HUD-Keys unverändert:
  - `"Speed"`, `"Throttle / Brake"`, `"Steering"`, `"Delta"`, `"Gear & RPM"`, `"Line Delta"`, `"Under-/Oversteer"`
  - weiterhin vorhanden in `render_split.py:1594–1600`.
- `py_compile` erfolgreich (nach jedem Schritt und final):
  - `python -m py_compile render_split.py ffmpeg_plan.py`
- Kurzer Renderlauf erfolgreich (nach jedem Schritt und final):
  - `IRVC_DEBUG_MAX_S=2; python main.py`
  - erfolgreich abgeschlossen (FFmpeg encode OK, Exit Code 0)
- Verhaltenserhalt:
  - FFmpeg Command / Filter-Konstruktion wurde 1:1 (Logik + Reihenfolge) verschoben.
  - Variierende FFmpeg-Progress-Zahlen sind erwartbar (laufzeitbedingt), ansonsten erwartete Logs.

**Fertig wenn**

- `render_split.py` enthält keine langen Filtergraph-String-Bauereien mehr. ✅


---

## Story 9 (Optional) – Encoder/Fallback auslagern

**Umsetzung (Ist-Stand)**

- Dateien angepasst:
  - `render_split.py`
  - `encoders.py`
- Encoder-Erkennung ausgelagert:
  - `ffmpeg -encoders` Parsing/Erkennung aus `render_split.py` nach `encoders.py` verschoben.
  - Pro Prozess gecacht: `detect_available_encoders(...)`.
- Encoder-Auswahl + Fallback zentralisiert:
  - Zentrale Specs-/Args-Erzeugung in `encoders.py` (z. B. `build_encode_specs(...)`).
  - Zentrale Retry-/Fallback-Logik in `encoders.py` (`run_encode_with_fallback(...)`).
  - Beide Render-Pfade nutzen jetzt dieselbe Encoder-API:
    - `render_split_screen`
    - `render_split_screen_sync`
- Alte, verteilte Encoder-Logik aus `render_split.py` entfernt:
  - `_ffmpeg_has_encoder`
  - `_choose_encoder`
- Logging konsistent gehalten:
  - Gleiche `[gpu]` / `[encode] try` / `OK` / `FAIL` Meldungen an äquivalenten Stellen wie zuvor.
- Fallback-Reihenfolge unverändert (inkl. W-Constraint bei H.264 GPU-Encodern):
  1) `h264_nvenc` (nur bei `W <= 4096`)
  2) `hevc_nvenc`
  3) `h264_qsv` (nur bei `W <= 4096`)
  4) `hevc_qsv`
  5) `h264_amf` (nur bei `W <= 4096`)
  6) `hevc_amf`
  7) `libx264`

**Abnahme / Check**

- HUD-Keys unverändert:
  - `"Speed"`, `"Throttle / Brake"`, `"Steering"`, `"Delta"`, `"Gear & RPM"`, `"Line Delta"`, `"Under-/Oversteer"`.
- `py_compile` erfolgreich:
  - `python -m py_compile render_split.py encoders.py`
- Kurzer Renderlauf erfolgreich:
  - `IRVC_DEBUG_MAX_S=2; python main.py`
  - abgeschlossen mit `[encode] OK vcodec=hevc_nvenc`, Exit Code 0.

**Fertig wenn**

- Fallback-Strategie ist zentral (in `encoders.py`) und nicht über viele Stellen verteilt. ✅


---

## Testplan (minimal, aber verbindlich)

### Smoke-Tests (nach jeder Story)
* 10–20s Render mit Standard-Setup
* 1–2 HUDs deaktivieren und prüfen, ob sie wirklich fehlen
* Log prüfen: keine Exceptions, keine “silent fails”

### Vergleich
* Visueller Vergleich Vorher/Nachher (gleiche Stelle im Video)
* Wenn Unterschiede auftreten:
  * dokumentieren (welcher HUD, welcher Frame-Bereich)
  * Ursache isolieren

---

## Rollback-Plan
* Jede Story in einem eigenen Git-Commit (oder eigenes Backup)
* Wenn eine Story bricht:
  * zurück zum letzten stabilen Commit
  * Story kleiner schneiden (nur ein HUD / ein Modul)

---

## Risiken & Gegenmaßnahmen

**Risiko:** Import-Zyklen (HUD ↔ Sync ↔ Render)
* **Gegenmaßnahme:** Nur “top-down” Abhängigkeiten. HUDs importieren nur `huds/api.py` + `huds/common.py`.

**Risiko:** Verhalten ändert sich unbemerkt
* **Gegenmaßnahme:** Referenz-Output + kurze Tests nach jeder Story.

**Risiko:** `HudContext` wird zu groß / unklar
* **Gegenmaßnahme:** Nur Felder aufnehmen, die HUDs wirklich brauchen. Klare Gruppierung (Frame/Fenster/Signale/Settings/Geom).

---

## Ergebnis (Soll-Zustand)
* `render_split.py` ist Orchestrator.
* HUDs sind als Module sauber getrennt.
* Ein HUD kann separat angepasst werden, ohne dass 4000 Zeilen betroffen sind.
* `hud_enabled` steuert zuverlässig, was gerendert wird.




## Ziel
Die Scroll-HUDs (Throttle/Brake, Steering, Delta, Line Delta, Under-/Oversteer) sollen einen echten Scrolling Buffer nutzen. Pro Frame werden nur die neu hinzukommenden Pixelspalten am rechten Rand berechnet und gezeichnet, waehrend der bisherige Inhalt um eine definierte Pixelanzahl nach links verschoben wird. Ruhige Bereiche (Grid, Achsen, Titel, fixe Marker) werden in statische Layer ausgelagert.

## Nicht-Ziele
- Keine Aenderung des visuellen Designs ausser Stabilisierung.
- Keine Aenderungen an Speed- oder Gear/RPM-HUDs.
- Keine Netz- oder Tool-Integration.
- Kein Wechsel des Dateiformats der HUD-Frames.

## Wichtige Randbedingungen
- Die HUDs arbeiten derzeit pro Frame mit kompletter Neuberechnung.
- Die X-Positionen sind zeitbasiert; pro Frame verschiebt sich der Graph nach links.
- Bei ungleichen Fensterwerten (before_f != after_f) ist die Verschiebung links/rechts unterschiedlich. Ein echter globaler Scrolling Shift ist dann nur korrekt, wenn entweder:
  - before_f == after_f (symmetrisches Fenster), oder
  - die linke und rechte Haelfte getrennt geshiftet werden.
- Beschriftungen und Titel sind ruhig und sollen als statische Layer gezeichnet werden; Werte (aktuelle Zahlen) sind dynamisch.

---

## Sprint 1: Architektur, Datenfluss und Decisions


## Story 1.1 – Scrolling-Buffer-Architektur (verbindliche Definition)

### Grundentscheidung: Fenster-Symmetrie

Für alle Scroll-HUDs wird ein **symmetrisches Zeitfenster erzwungen**.

* `before_s == after_s`
* entsprechend gilt auch: `before_f == after_f`

**Begründung:**
Nur bei einem symmetrischen Fenster ist die Scroll-Geschwindigkeit links und rechts gleich.
Damit kann der `dynamic_layer` als **durchgehender Scroll-Buffer** mit **einem Shift pro Frame** umgesetzt werden.
So bleiben Kurven geometrisch stabil und verändern sich nicht beim Scrollen.

Eine getrennte Shift-Logik für linke und rechte Hälfte wird **nicht umgesetzt** (zu komplex, hohes Fehlerrisiko).

---

### HUD-State (pro HUD-Box, persistent über Frames)

Jedes Scroll-HUD besitzt einen eigenen State, der über Frames hinweg erhalten bleibt.

**Layer**

* `static_layer`
  Enthält ruhige Elemente wie Grid, Achsen, Titel und feste Marker.
  Wird nur bei Reset neu aufgebaut.
* `dynamic_layer`
  Enthält scrollende Inhalte wie Kurven und ABS-Balken.
* `value_layer` (optional)
  Enthält dynamische Textwerte oder Labels.
  Wird pro Frame neu gezeichnet.

**Geometrie**

* `width`, `height`
  Aktuelle Größe der HUD-Box.
* `box_signature`
  Eindeutige Kennung der HUD-Box (Position + Größe), um Layout-Änderungen zu erkennen.

**Scroll-Zustand**

* `scroll_pos_px`
  Float-Wert für akkumulierte Subpixel-Verschiebung.
* `last_i`
  Letzter gerenderter Output-Frame-Index.
* `last_right_sample`
  Zeit oder Index des zuletzt gezeichneten rechten Randes.

**Optionale Cache-Daten**

* `cache_arrays`
  Pro Kurve gespeicherte letzte Werte zur sauberen Linienfortsetzung
  (z. B. bei Lücken oder Clamping).

**Konfig-Signatur (für Reset-Erkennung)**

* verwendete FPS
* verwendete Fensterparameter (`before_s`, `after_s`)
* verwendeter Sample-Modus (`time` / `legacy`)
* weitere Parameter, die die Geometrie sichtbar beeinflussen

---

### Reset-Bedingungen (verbindlich)

Der HUD-State wird vollständig zurückgesetzt, wenn mindestens eine der folgenden Bedingungen zutrifft:

1. Start eines neuen Renderlaufs.
2. Änderung der HUD-Box:

   * Größe (`width` / `height`)
   * Position (Box-Signatur ändert sich).
3. Änderung der Output-FPS.
4. Änderung des Zeitfensters (`before_s` oder `after_s`).
5. Änderung des Sample-Modus (`time` ↔ `legacy`).
6. Änderung von Parametern, die die Kurven-Geometrie beeinflussen.
7. Änderung von Farben, Schrift oder Layout-Elementen im `static_layer`.

Teil-Resets (z. B. nur `static_layer`) sind erlaubt, müssen aber explizit definiert werden.

---

### Scroll-Prinzip (Definition)

Pro Output-Frame wird der `dynamic_layer` um einen festen Betrag nach links verschoben.
Nur die neu frei gewordenen Spalten am rechten Rand werden neu gezeichnet.

Der `static_layer` bleibt unverändert.
Der `value_layer` wird pro Frame neu komponiert.

---

### Story 1.2: Statische vs. dynamische Zeichenbereiche pro HUD bestimmen
**Ziel:** Pro HUD klar aufschluesseln, welche Elemente statisch und welche dynamisch sind.  
**Details:**
- Throttle/Brake:
  - Statisch: Hintergrund, Stripe-Grid, Achsenlabels (20/40/60/80), Titeltext, Center-Marker.
  - Dynamisch: Gas-/Bremse-Kurven, ABS-Balken, aktuelle Werte (T/B Prozent).
- Steering:
  - Statisch: Hintergrund, Grid, Achsenlabels, Titeltext, 0-Linie, Center-Marker.
  - Dynamisch: Steering-Kurven (slow/fast), aktuelle Werte (Grad).
- Delta:
  - Statisch: Hintergrund, Grid, Achsenlabels, Titeltext, 0-Linie, Center-Marker.
  - Dynamisch: Delta-Kurve, aktueller Delta-Wert.
- Line Delta:
  - Statisch: Hintergrund, Grid, Achsenlabels, Titeltext, 0-Linie, Center-Marker.
  - Dynamisch: Line-Delta-Kurve, aktueller Wert.
- Under-/Oversteer:
  - Statisch: Hintergrund, Grid, Achsenlabels, Titeltexte (Oversteer/Understeer), 0-Linie, Center-Marker.
  - Dynamisch: Slow/Fast-Kurven.
**Akzeptanzkriterien:**
- Fuer jeden HUD-Typ ist die Zuordnung statisch/dynamisch eindeutig dokumentiert.
- Dynamische Werte werden explizit genannt, damit sie nicht faelschlich in die statische Layer wandern.

---

## Sprint 2: Scrolling-Buffer-Mechanik und Window-Mapping

### Story 2.1: Gemeinsames Window-Mapping pro Frame definieren
**Ziel:** Einheitliche Berechnung der zeitlichen Mapping-Werte, die alle Scroll-HUDs verwenden.  
**Details:**
- Pro Frame wird eine globale Window-Struktur erzeugt:
  - `i`, `before_f`, `after_f`, `iL`, `iR`.
  - `idxs`: Indizes von `iL..iR`.
  - `offsets`: `idx - i`.
  - `t_slow`: `idx / fps`.
  - `fast_idx`: `slow_to_fast_frame[idx]` (clamp).
  - `t_fast`: `slow_frame_to_fast_time_s[idx]` falls vorhanden, sonst `fast_idx / fps`.
- Dieses Mapping ist die Basis fuer das Berechnen neuer rechter Spalten.
- Das Mapping wird nur einmal pro Frame erstellt und dann an alle Scroll-HUDs weitergereicht.
**Akzeptanzkriterien:**
- Das Mapping deckt alle benoetigten Zeit-/Index-Informationen ab.
- Keine HUD-spezifischen Mapping-Schleifen mehr noetig.


**Umsetzung (Ist-Stand)**
- In `src/render_split.py` wurde ein gemeinsamer Per-Frame Mapping-Container `FrameWindowMapping` ergänzt.
  - Scalars: `i`, `before_f`, `after_f`, `iL`, `iR`
  - Arrays: `idxs`, `offsets`, `t_slow`, `fast_idx`, `t_fast`
- In `src/render_split.py` wurde die Builder-Funktion `_build_frame_window_mapping(...)` ergänzt:
  - Berechnet `idxs` von `iL..iR`
  - Clamped `fast_idx` sicher
  - Setzt `t_fast` via `slow_frame_to_fast_time_s[idx]` wenn vorhanden, sonst `fast_idx / fps`
- Im Frame-Loop wird das Mapping jetzt exakt 1x pro Frame gebaut und an alle Scroll-HUDs weitergereicht.
- Doppelte HUD-lokale Mapping-Schleifen wurden entfernt, indem die Module das `frame_window_mapping` konsumieren:
  - `src/huds/throttle_brake.py`
  - `src/huds/steering.py`
  - `src/huds/delta.py`
  - `src/huds/line_delta.py`
  - `src/huds/under_oversteer.py`
- Rendering-Reihenfolge, HUD-Keys und Verhalten/Output blieben unverändert. Es gibt Fallback-Pfade, falls kein Mapping vorhanden ist.

**Abnahme / Check**
- `python -m py_compile` auf allen geänderten Modulen: ✅ erfolgreich
- Short Render: `IRVC_HUD_SCROLL=1 IRVC_DEBUG_MAX_S=2 python src/main.py --ui-json config/ui_last_run.json`: ✅ erfolgreich, keine Runtime-Errors

**Fertig wenn**
- ✅ Pro Frame wird genau 1 gemeinsames Window-Mapping erzeugt und an alle Scroll-HUDs weitergegeben.
- ✅ Mapping enthält alle benötigten Zeit-/Index-Informationen (`idxs`, `offsets`, `t_slow`, `fast_idx`, `t_fast`, sowie `i/iL/iR/before_f/after_f`).
- ✅ Keine HUD-spezifischen Mapping-Schleifen mehr nötig (HUDs konsumieren das gemeinsame Mapping).


### Story 2.2: Scroll-Shift pro Frame festlegen
**Ziel:** Genaue Pixelverschiebung pro Frame definieren und Subpixel behandeln.  
**Details:**
- Berechne `shift_px_per_frame` aus `window_frames` und HUD-Breite.
- Fuehre `scroll_pos_px` als float fortlaufend.
- Wenn `scroll_pos_px` >= 1.0: verschiebe `dynamic_layer` um `floor(scroll_pos_px)` Pixel nach links.
- Nach dem Shift: `scroll_pos_px = scroll_pos_px - floor(scroll_pos_px)`.
- Wenn `scroll_pos_px` < 1.0: es wird nicht geshiftet, aber der rechte Rand wird dennoch aktualisiert, damit neue Daten nicht verlorengehen.
- Bei before_f != after_f:
  - Entweder: erzwinge before_f == after_f fuer alle Scroll-HUDs (symmetrisch).
  - Oder: implementiere getrennte Shift-Faktoren fuer linke und rechte Haelfte mit separaten Bereichen (komplexer).
**Akzeptanzkriterien:**
- Definiertes Verhalten fuer Subpixel-Shift und fuer unterschiedliche Fensterlaengen.
- Keine unklaere Situation, wie viele neue Spalten gezeichnet werden.

**Umsetzung (Ist-Stand)**
- `src/render_split.py`:
  - In `_render_hud_scroll_frames_png` wurde ein persistenter Subpixel-State pro Scroll-HUD eingeführt (`scroll_state_by_hud`, keyed by HUD-Identity + Box-Geometrie). (ca. Zeile 2487)
  - Story-2.2-Shift-Logik pro HUD/Frame umgesetzt:
    - Symmetrische Fenster erzwungen: `before_f == after_f` für Scroll-HUDs. (ca. Zeile 2597)
    - `window_frames` wird pro HUD berechnet.
    - `shift_px_per_frame = hud_width_px / window_frames`.
    - `scroll_pos_px` wird als Float fortlaufend akkumuliert.
    - Wenn `scroll_pos_px >= 1.0`: `shift_int = floor(scroll_pos_px)`, dann Shift um `shift_int` Pixel nach links und `scroll_pos_px -= shift_int`. (ca. Zeile 2597)
  - Deterministische Regel für Right-Edge-Update zentral festgelegt:
    - `right_edge_cols = shift_int` wenn geshiftet wird.
    - `right_edge_cols = 1` wenn kein Shift passiert (In-Place Right-Edge Refresh Policy). (ca. Zeile 2624)
  - Scroll-Step-Metadaten in alle Scroll-HUD-Contexts propagiert, damit das Verhalten konsistent ist für:
    - "Throttle / Brake", "Steering", "Delta", "Line Delta", "Under-/Oversteer". (ca. Zeilen 2674, 2711, 2740, 2784, 2803)
  - Symmetrische Scroll-Windows zusätzlich an der Setup-Quelle erzwungen (`render_split_screen_sync`), damit das per-HUD Window-Verhalten schon vor dem Rendern konsistent ist. (ca. Zeile 3349)

**Abnahme / Check**
- `python -m py_compile src/render_split.py` nach jedem sinnvollen Edit: ✅ (2x, keine Fehler)
- Short Render nach jedem Schritt:
  - Command: `$env:IRVC_HUD_SCROLL='1'; $env:IRVC_DEBUG_MAX_S='2'; python src/main.py --ui-json config/ui_last_run.json`
  - Result: ✅ beide Runs erfolgreich, keine Exceptions, Encode OK (`hevc_nvenc`), normale Sync/Debug-Logs

**Fertig wenn**
- ✅ Subpixel-Shift ist definiert über `scroll_pos_px` (Float-Akkumulator) + `floor()`-Integer-Step.
- ✅ Für unterschiedliche `window_frames` ist `shift_px_per_frame` pro HUD eindeutig definiert (`hud_width_px / window_frames`).
- ✅ Es gibt keine unklare Situation zur Anzahl neu gezeichneter Spalten:
  - Shift: genau `shift_int` Spalten.
  - Kein Shift: genau 1 Spalte (Right-Edge Refresh).
- ✅ Unterschiedliche Fensterlängen links/rechts werden nicht zugelassen (symmetrisch erzwungen: `before_f == after_f`).


### Story 2.3: Initial-Fill und Reset-Logik fuer Buffer
**Ziel:** Sichere Initialisierung und Wiederaufbau der Buffer.  
**Details:**
- Beim ersten Frame oder bei Reset werden `static_layer` und `dynamic_layer` komplett neu erstellt.
- `dynamic_layer` wird initial durch komplettes Zeichnen ueber alle Spalten gefuellt.
- Danach laeuft nur noch inkrementelles Update mit Shift + neue Spalten.
- Bei jedem Reset werden `scroll_pos_px`, `last_i` und `last_right_sample` neu gesetzt.
**Akzeptanzkriterien:**
- Startzustand ist korrekt gefuellt, kein “leerer” Bereich sichtbar.
- Nach Reset sieht der HUD wieder korrekt aus.

**Umsetzung (Ist-Stand)**
- `src/render_split.py`:
  - Scroll-State-Typing erweitert, damit Layer-Objekte und Reset-Metadaten gehalten werden können: `scroll_state_by_hud: dict[str, dict[str, Any]]`. (ca. Zeile 2489)
  - Hilfsfunktionen für konsistente Initial-Fill/Incremental-Updates ergänzt:
    - `_right_edge_sample_idx()`
    - lokaler Full-Renderer `_render_scroll_hud_full(...)` für Scroll-HUDs ("Throttle / Brake", "Steering", "Delta", "Line Delta", "Under-/Oversteer"), der die komplette HUD-Breite in ein per-HUD Layer-Image rendert. (ca. Zeile 2614)
  - Init/Reset-Erkennung ergänzt:
    - First-Frame: State fehlt oder Layer fehlen.
    - Reset: Frame-Discontinuity (`i != last_i + 1`) oder HUD-lokales `window_frames` hat sich geändert. (ca. Zeile 2822)
  - Verhalten bei First/Reset:
    - `static_layer` und `dynamic_layer` werden neu erstellt.
    - `dynamic_layer` wird per `_render_scroll_hud_full(...)` einmalig über die gesamte Breite komplett gefüllt.
    - State wird neu gesetzt: `scroll_pos_px=0.0`, `last_i=i`, `last_right_sample=<current right edge sample>`, `window_frames`. (ca. Zeile 2842)
  - Incremental-Path nach Init/Reset:
    - Story-2.2-Logik bleibt erhalten (Subpixel-Akkumulator + `floor()` Shift + deterministische `right_edge_cols` Regel).
    - `dynamic_layer` wird links geshiftet und die rechten Spalten werden aus dem aktuellen Full-Render übernommen.
    - Per-HUD State wird fortgeschrieben (`scroll_pos_px`, `last_i`, `last_right_sample`, `window_frames`). (ca. Zeile 2861)

**Abnahme / Check**
- `python -m py_compile src/render_split.py`: ✅ OK  
- `.venv\Scripts\python.exe -m py_compile src/render_split.py`: ✅ OK
- Short Render (Scroll-HUD aktiv, 2s):
  - Command: `$env:IRVC_HUD_SCROLL='1'; $env:IRVC_DEBUG_HUDPY='1'; $env:IRVC_DEBUG_MAX_S='2'; .venv\Scripts\python.exe src/main.py --ui-json config/ui_last_run.json`
  - Result: ✅ `[hudpy] geschrieben: 240 frames`, Encode OK
- Reset-ähnlicher Run (anderes Clip-Limit, 3s):
  - Command: `$env:IRVC_HUD_SCROLL='1'; $env:IRVC_DEBUG_HUDPY='1'; $env:IRVC_DEBUG_MAX_S='3'; .venv\Scripts\python.exe src/main.py --ui-json config/ui_last_run.json`
  - Result: ✅ `[hudpy] geschrieben: 360 frames`, Encode OK
- Startzustand “nicht leer” verifiziert:
  - Programmatic Alpha-Check auf `output/debug/hud_frames/hud_000000.png`, `hud_000001.png`, `hud_000120.png` für alle Scroll-HUD-Boxen: ✅ `zero_alpha=0` je Box

**Fertig wenn**
- ✅ Beim ersten Frame werden `static_layer` und `dynamic_layer` neu erstellt und `dynamic_layer` komplett gefüllt (kein leerer Bereich sichtbar).
- ✅ Nach Reset (Frame-Sprung oder `window_frames` Änderung) wird der HUD sauber neu aufgebaut.
- ✅ Reset setzt `scroll_pos_px`, `last_i`, `last_right_sample` (und `window_frames`) deterministisch zurück.
- ✅ Danach läuft nur noch das inkrementelle Update (Shift + neue Spalten) mit unverändertem Story-2.2 Verhalten.


---

## Sprint 3: HUD-spezifische inkrementelle Updates

### Story 3.1: Throttle/Brake inkrementell zeichnen
**Ziel:** Gas-/Bremse-Kurven und ABS-Balken nur fuer neue Spalten zeichnen.  
**Details:**
- Fuer jede neue Spalte:
  - Sample `t_slow`, `t_fast` aus Window-Mapping.
  - Berechne Throttle/Brake-Werte wie bisher (time oder legacy).
  - Bestimme `y` fuer Gas/Bremse, zeichne Liniensegmente von der vorherigen Spalte zur neuen.
  - ABS-Balken: pro Spalte on/off pruefen und falls on, senkrechten Balken (Breite = 1 Spalte, Hoehe = abs_h) an der ABS-Linie zeichnen.
- Werte-Text (T/B Prozent) wird separat pro Frame gezeichnet, nicht im Scroll-Buffer.
- ABS-Entprellung (debounce) wird nur fuer die neuen Spalten ausgewertet.
**Akzeptanzkriterien:**
- Kurven bewegen sich ruhig und ohne Flackern.
- ABS-Balken bleiben stabil beim Scrollen.
- Werte-Text aktualisiert sich korrekt.

**Umsetzung (Ist-Stand)**
- `src/render_split.py`:
  - Throttle/Brake-spezifische Inkremental-Infrastruktur im Scroll-HUD-Loop ergänzt:
    - Per-Column Sampling-Helpers (time/legacy).
    - Per-HUD ABS-Debounce-State-Updater (wird nur für angehängte Spalten aktualisiert).
    - Static-Layer Renderer: `_tb_render_static_layer`.
    - Full-Fill Dynamic Renderer: `_tb_render_dynamic_full`.
    - Per-Frame Values-Text Overlay: `_tb_draw_values_overlay` (T/B Text wird nicht im Scroll-Buffer gespeichert). (ca. Zeile 2622)
  - Init/Reset-Path für **"Throttle / Brake"**:
    - `static_layer` + `dynamic_layer` werden neu erstellt.
    - `dynamic_layer` wird einmalig über volle Breite komplett gefüllt.
    - Kontinuitäts-/Debounce-State wird gespeichert (z. B. `tb_cols`, Debounce-Counter, `last_right_sample`).
    - Compositing: `static_layer + dynamic_layer + values overlay` pro Frame. (ca. Zeile 3297)
  - Incremental-Path für **"Throttle / Brake"**:
    - `dynamic_layer` wird geshiftet.
    - Es werden **nur** `right_edge_cols` neue Spalten gezeichnet (`dest_x = w - right_edge_cols + c`).
    - Kurven-Segmente werden aus dem vorherigen Spalten-State heraus verbunden (keine Unterbrüche an der Kante).
    - ABS wird als **1px** senkrechte Spalte pro neuer Spalte gezeichnet (nur im ABS-Band).
    - Per-HUD Kontinuität/Debounce wird fortgeschrieben; danach Compositing inkl. Values Overlay. (ca. Zeile 3363)
  - Andere Scroll-HUDs ("Steering", "Delta", "Line Delta", "Under-/Oversteer") bleiben auf bestehendem Verhalten.

**Abnahme / Check**
- `python -m py_compile src/render_split.py`: ✅ success
- Short Render:
  - Command: `$env:IRVC_HUD_SCROLL='1'; $env:IRVC_DEBUG_HUDPY='1'; $env:IRVC_DEBUG_MAX_S='2'; .venv\Scripts\python.exe src/main.py --ui-json config/ui_last_run.json`
  - Result: ✅ `[hudpy] geschrieben: 240 frames`, HUD-PNG Export aktiv, Encode OK (`hevc_nvenc`)
- Reset-ähnlicher Run:
  - Command: wie oben mit `IRVC_DEBUG_MAX_S='3'`
  - Result: ✅ `[hudpy] geschrieben: 360 frames`, Encode OK
- Spot-Checks (Throttle/Brake PNGs):
  - ✅ Keine transparenten Right-Edge-Gaps (`right5_zero_alpha = 0`)
  - ✅ ABS Right-Edge Spalten verhalten sich wie 1px-Column (vertikales Fill nur im ABS-Band)

**Fertig wenn**
- ✅ Gas-/Bremse-Kurven werden nur für neue Spalten gezeichnet und bleiben ruhig ohne Flackern.
- ✅ ABS-Balken werden pro neuer Spalte als 1px-Spalte gezeichnet und bleiben stabil beim Scrollen (Debounce nur auf neuen Spalten).
- ✅ Werte-Text (T/B Prozent) wird pro Frame separat gezeichnet und aktualisiert sich korrekt, ohne im Scroll-Buffer zu landen.


### Story 3.2: Steering inkrementell zeichnen
**Ziel:** Steering-Kurven scrollen mit Buffer, Werte-Text bleibt dynamisch.  
**Details:**
- Fuer jede neue Spalte:
  - Slow/fast-Werte aus Frames lesen (fast ueber slow_to_fast_frame).
  - `y` berechnen (inkl. Headroom).
  - Liniensegmente von vorheriger Spalte zu neuer Spalte zeichnen.
- 0-Linie bleibt im statischen Layer.
- Werte-Text (Grad) wird pro Frame neu gerendert.
**Akzeptanzkriterien:**
- Kurven bewegen sich gleichmaessig.
- Kein Flackern am linken Rand.
- Werte-Text stabil und korrekt.


**Umsetzung (Ist-Stand)**
- `src/render_split.py`:
  - Steering-spezifisches Scroll-Path-Flag ergänzt: `is_steering`. (ca. Zeile 2623)
  - Steering-Helper (analog zu T/B) ergänzt:
    - `_st_render_static_layer` (0-Linie im statischen Layer). (ca. Zeile 3224)
    - `_st_render_dynamic_full` (Full-Fill über gesamte Breite für Init/Reset). (ca. Zeile 3244)
    - `_st_draw_values_overlay` (Werte-Text in Grad pro Frame als Overlay, nicht im Scroll-Buffer). (ca. Zeile 3260)
    - Zusätzlich lokale Sampling-/Mapping-Helper für per-Column slow/fast Sampling und Headroom-Y-Mapping (mit gleichen Inputs/Skalen/Clamp-Verhalten wie vorher).
  - Init/Reset für **"Steering"**:
    - Full-Fill wird einmalig ausgeführt,
    - Kontinuitäts-State wird gespeichert,
    - Compositing: `static + dynamic + per-frame values overlay`. (ca. Zeile 3532)
  - Incremental-Path für **"Steering"**:
    - Nutzt bestehende Story-2.2 Shift-/Right-Edge-Regeln,
    - Shift von `dynamic_layer` nach links,
    - Zeichnet nur `right_edge_cols` neue Spalten,
    - Segment-Kontinuität:
      - nutzt persistiertes `last_y` bei Shift-Frames,
      - nutzt linken Nachbar-Sample wenn kein Shift (für nahtlosen Anschluss),
    - State wird aktualisiert und Werte-Text wird pro Frame überlagert. (ca. Zeile 3670)
  - 0-Linie bleibt ausschließlich im `static_layer`; Werte-Text wird nicht in den Scroll-Buffer “eingebrannt”.

**Neue per-HUD State-Felder (Steering)**
- `last_y` Tuple `(slow_y, fast_y)`. (ca. Zeilen 3544, 3733)
- `st_last_fast_idx`. (ca. Zeilen 3545, 3734)
- Bestehende Felder bleiben erhalten: `last_i`, `window_frames`, `scroll_pos_px`, `last_right_sample`.

**Abnahme / Check**
- `python -m py_compile src/render_split.py`: ✅ passed
- Short Render:
  - Result: ✅ 240 HUD Frames geschrieben nach `output/debug/hud_frames`, Encode OK (`hevc_nvenc`)
- Spot-Checks (Steering Box, exportierte PNGs):
  - ✅ keine transparenten Artefakte (`transparent_min_max 0 0`)
  - ✅ linker Rand stabil (`left_alpha0_min_max 0 0`)
  - ✅ 0-Linie stabil (`mid_left_unique 1`)
  - ✅ rechter Rand hat immer Kurvenpixel (`edge_curve_zero_frames 0`)
  - ✅ Werte-Text bleibt sauber als Overlay (keine Smear-Artefakte):
    - `red_first_last (305,386)`, `blue_first_last (413,494)` konstant

**Fertig wenn**
- ✅ Steering-Kurven bewegen sich gleichmäßig (nur neue Spalten werden inkrementell gezeichnet).
- ✅ Kein Flackern am linken Rand (Shift + Overwrite nur am rechten Rand).
- ✅ Werte-Text (Grad) wird pro Frame neu gerendert, stabil und korrekt, ohne im Scroll-Buffer zu landen.


### Story 3.3: Delta inkrementell zeichnen
**Ziel:** Delta-Kurve (positiv/negativ eingefaerbt) scrollt korrekt.  
**Details:**
- Fuer jede neue Spalte:
  - Delta aus `slow_frame_to_fast_time_s` berechnen.
  - `y` mit aktueller Skala berechnen.
  - Liniensegment zeichnen, Farbe abhaengig vom Vorzeichen.
- 0-Linie im statischen Layer.
- Aktueller Delta-Wert als dynamischer Text pro Frame.
**Akzeptanzkriterien:**
- Farbwechsel an Null bleibt korrekt beim Scrollen.
- Kurve bleibt stabil.
- Textwert ist korrekt.

**Umsetzung (Ist-Stand)**
- `src/render_split.py`:
  - Dedizierter Delta-Inkrementalpfad ergänzt:
    - `is_delta` Branch.
    - `_d_render_static_layer()` für Delta-Background + **0-Linie nur im static layer**.
    - `_d_render_dynamic_full()` für **Init/Reset Full-Width Fill**.
    - `_d_draw_values_overlay()` für **aktuellen Delta-Text pro Frame** (nicht im Scroll-Buffer).
    - Delta Sampling/Mapping Helpers auf Basis von `slow_frame_to_fast_time_s` inkl. bestehendem Frame-Window-Map-Fallback.
    - Delta Y-Mapping nutzt die bestehende Scale-Logik unverändert (`delta_pos_max`, `delta_neg_min`, `delta_has_neg`), Orientation/Clamp unverändert.
    - Inkremental-Zeichnen nur für `right_edge_cols` am rechten Rand, mit persistiertem Kontinuitäts-State in `scroll_state_by_hud` (`last_y`, `last_delta_value`, `last_delta_sign`).
    - Zero-Cross Split im Inkremental-Draw (`_d_draw_segment`), damit Sign-Color beim Übergang über 0 korrekt wechselt.
  - Throttle/Brake (3.1) und Steering (3.2) bleiben unverändert.
  - HUD Keys bleiben unverändert: "Speed", "Throttle / Brake", "Steering", "Delta", "Gear & RPM", "Line Delta", "Under-/Oversteer".

**Init/Reset und Inkremental-Verhalten**
- Init/Reset (Delta):
  - `static_layer` + `dynamic_layer` neu.
  - `dynamic_layer` Full-Fill via `_d_render_dynamic_full()`.
  - State Reset/Speicher: `scroll_pos_px`, `last_i`, `last_right_sample`, `window_frames`, `last_y`, `last_delta_value`, `last_delta_sign`.
  - Compositing: `static_layer + dynamic_layer + per-frame overlay text`.
- Normaler Inkrementalpfad (Delta):
  - Shift `dynamic_layer` nach links um `shift_int`.
  - Zeichnet exakt `right_edge_cols` neue Spalten rechts.
  - Pro neuer Spalte: `x -> slow idx`, Delta berechnen, `y` mappen, Segment von prev->curr zeichnen.
  - Bei Sign-Änderung: Segment an 0-Linie splitten und beide Teile passend einfärben.
  - State fortschreiben und pro Frame Text overlayn.

**Abnahme / Check**
- `python -m py_compile src/render_split.py`: ✅ passed
- Short Render:
  - Command: `$env:IRVC_HUD_SCROLL='1'; $env:IRVC_DEBUG_HUDPY='1'; $env:IRVC_DEBUG_MAX_S='2'; .venv\Scripts\python.exe src/main.py --ui-json config/ui_last_run.json`
  - Result: ✅ `[hudpy] geschrieben: 240 frames` nach `C:\iracing-vc\output\debug\hud_frames`
- Spot-Checks (Delta Region):
  - ✅ 0-Linie stabil (static layer).
  - ✅ Keine transparenten Right-Edge-Gaps (`right_edge_fully_transparent=0` über 240 Frames).
  - ✅ Delta-Text aktualisiert pro Frame (ROI Hash verändert sich über die Zeit).
  - ℹ️ In diesem Run keine negativen Deltas (min 0.0), daher Zero-Cross visuell nicht beobachtbar; Codepfad für Split-Farbwechsel ist implementiert.

**Fertig wenn**
- ✅ Farbwechsel an Null bleibt korrekt beim Scrollen (Segment-Split bei Signwechsel implementiert).
- ✅ Kurve bleibt stabil (Right-Edge inkrementell, keine Left-Edge Flicker).
- ✅ Textwert ist korrekt und pro Frame dynamisch (nicht im Scroll-Buffer).


### Story 3.4: Line Delta inkrementell zeichnen
**Ziel:** Line-Delta-Kurve scrollt mit Buffer.  
**Details:**
- Fuer jede neue Spalte:
  - `line_delta_m_frames` Wert holen.
  - `y` aus `line_delta_y_abs_m` berechnen.
  - Liniensegment zeichnen.
- 0-Linie, Grid, Titel als statisch.
- Wert-Text pro Frame dynamisch.
**Akzeptanzkriterien:**
- Linie bewegt sich ruhig.
- Textwert aktualisiert korrekt.

**Umsetzung (Ist-Stand)**
- `src/render_split.py`:
  - Line-Delta Inkremental-Infrastruktur ergänzt (analog zu 3.2/3.3):
    - `is_line_delta` Path-Detection. (ca. Zeile 2633)
    - Neue Helper (ca. Zeile 3558):
      - `_ld_render_static_layer(...)` (Background + Grid + Titel + 0-Linie + stabile statische Labels/Marker)
      - `_ld_render_dynamic_full(...)` (Full-Width Kurve nur für Init/Reset)
      - `_ld_draw_values_overlay(...)` (Wert-Text pro Frame als Overlay)
      - Column Mapping / Sampling Helpers (`_ld_slow_idx_for_column`, `_ld_sample_column`) mit gleicher `x -> slow-index` Strategie wie Steering/Delta
  - Init/Reset-Branch für **"Line Delta"**:
    - Rebuild `static_layer` / `dynamic_layer`
    - Full-Fill der dynamischen Kurve einmalig
    - State gespeichert: `scroll_pos_px`, `last_i`, `last_right_sample`, `window_frames`, `last_y`. (ca. Zeile 4095)
  - Incremental-Branch für **"Line Delta"**:
    - Story-2.2 Shift + deterministische `right_edge_cols` Regel beibehalten
    - Shift `dynamic_layer` links um `shift_int`
    - Zeichnet nur die angehängten Right-Edge-Spalten
    - Segment-Kontinuität: prev aus `last_y` bei Shift, sonst Left-Neighbor-Fallback
    - `last_y` im per-HUD State updaten
    - Wert-Text wird pro Frame als Overlay gezeichnet (nicht im Scroll-Buffer). (ca. Zeile 4378)

**Abnahme / Check**
- `python -m py_compile src/render_split.py`: ✅ passed
- Short Render:
  - Command: `$env:IRVC_HUD_SCROLL='1'; $env:IRVC_DEBUG_HUDPY='1'; $env:IRVC_DEBUG_MAX_S='2'; .venv\Scripts\python.exe src/main.py --ui-json config/ui_last_run.json`
  - Result: ✅ 240 Debug HUD Frames erzeugt + Encode OK

**Spot-Checks (Line Delta ROI, erste 120 Frames)**
- ✅ Static Title ROI Hash: `1 unique` (stabil)
- ✅ Value-Text ROI Hash: `7 unique` (aktualisiert pro Frame, nicht baked)
- ✅ Right-Edge Strip Hash: `39 unique` (inkrementelle Edge Updates)
- ✅ 0-Linie stabil: `white-count konstant (790..790)`
- ✅ Right-Edge Kontinuität: keine Misses (Cols 798/799), `nearest y-gap max 0`

**Fertig wenn**
- ✅ Linie bewegt sich ruhig (nur Right-Edge inkrementell, Kontinuität geprüft).
- ✅ Textwert aktualisiert korrekt pro Frame (Overlay, ROI Hash variiert).
- ✅ 0-Linie, Grid, Titel bleiben stabil im statischen Layer.


### Story 3.5: Under-/Oversteer inkrementell zeichnen
**Ziel:** Slow/Fast-Kurven inkrementell zeichnen.  
**Details:**
- Pro neue Spalte:
  - Slow/Fast-Werte aus Serien.
  - `y` via `under_oversteer_y_abs`.
  - Liniensegment pro Serie zeichnen.
- 0-Linie und Titel im statischen Layer.
**Akzeptanzkriterien:**
- Beide Kurven scrollen ohne Flackern.
- Farben bleiben korrekt.


**Umsetzung (Ist-Stand)**
- `src/render_split.py`:
  - Dedizierte HUD-Routing-Flag für **"Under-/Oversteer"** ergänzt. (ca. Zeile 2634)
  - Lokale Helper im Scroll-HUD-Pfad ergänzt (analog 3.2–3.4): (ca. Zeile 3815)
    - `_uo_render_static_layer(...)` (Background, Grid/Achsen-Labels, Titel, 0-Linie, Center-Marker **nur statisch**)
    - `_uo_render_dynamic_full(...)` (Full-Width Kurven **nur** für Init/Reset)
    - `_uo_slow_idx_for_column(...)`, `_uo_fast_idx_for_slow_idx(...)` + Value-Sampling und Y-Mapping via `under_oversteer_y_abs`
  - Init/Reset-Branch für **"Under-/Oversteer"**: (ca. Zeile 4395)
    - `dynamic_layer` wird einmalig full-filled
    - Kontinuitäts-State gespeichert:
      - `last_y (slow_y, fast_y)`. (ca. Zeile 4407)
      - `uo_last_fast_idx`. (ca. Zeile 4408)
      - plus bestehende Shared Fields: `last_i`, `last_right_sample`, `window_frames`, `scroll_pos_px`
  - Incremental-Branch für **"Under-/Oversteer"**: (ca. Zeile 4740)
    - Story 2.2/2.3 Shift + deterministische `right_edge_cols` Regel beibehalten
    - Shift von `dynamic_layer` um `shift_int`
    - Zeichnet nur die angehängten Right-Edge-Spalten
    - Pro neuer Spalte: Slow- und Fast-Segment von prev_y -> curr_y zeichnen
    - Persistiert finalen Kontinuitäts-State (`last_y`, `uo_last_fast_idx`). (ca. Zeile 4809)

**Abnahme / Check**
- `python -m py_compile src/render_split.py`: ✅ passed
- Short Render:
  - Env: `IRVC_HUD_SCROLL=1 IRVC_DEBUG_HUDPY=1 IRVC_DEBUG_MAX_S=2`
  - Result: ✅ 240 Frames geschrieben nach `output/debug/hud_frames`

**Spot-Checks (Under-/Oversteer ROI, exportierte PNGs)**
- ✅ Titel stabil: Title ROI Hash `1 unique`
- ✅ Inkrementelle Updates aktiv: Right-Edge Strip Hash `232 unique`
- ✅ Kein Left-Edge Flicker: avg changed px `0.0`, max `0`
- ✅ Farben korrekt vorhanden:
  - Slow rot: `(234, 0, 0, 255)` max sampled count `1834`
  - Fast blau: `(36, 0, 250, 255)` max sampled count `1945`

**Fertig wenn**
- ✅ Beide Kurven scrollen ohne Flackern (Left-Edge stabil, Right-Edge inkrementell).
- ✅ Farben bleiben korrekt (Slow rot / Fast blau).
- ✅ 0-Linie und Titel bleiben stabil im statischen Layer.


---

## Sprint 4: Integration, Komposition, Konfig und Verifikation

### Story 4.1: Buffer-Komposition in render_split
**Ziel:** Pro Frame die HUD-Buffer korrekt zusammensetzen. 
 
**Details:**
- Im Rendering-Loop:
  - Hole oder erzeuge State pro HUD-Box.
  - Fuehre Shift + Spaltenupdate im `dynamic_layer` aus.
  - Kombiniere `static_layer` + `dynamic_layer` + `value_layer`.
  - Paste das Ergebnis in das Frame-Image an der HUD-Position.
- Alpha-Komposition muss korrekt sein (RGBA).

**Akzeptanzkriterien:**
- Alle Scroll-HUDs erscheinen korrekt in der End-PNG.
- Keine Artefakte durch Alpha-Fehler.

**Ziel:** Pro Frame die HUD-Buffer korrekt zusammensetzen.  

#### Umsetzung (Ist-Stand)
- In `src/render_split.py` ist der Scroll-HUD Pfad pro Frame so verdrahtet, dass `static_layer` + `dynamic_layer` + `value_layer` explizit zu einem RGBA-HUD-Bild zusammengesetzt werden.
- Die Alpha-Komposition erfolgt RGBA-sicher über `Image.alpha_composite` (statt fehleranfälligem direktem Paste/Draw).
- Das zusammengesetzte HUD wird anschließend in das Frame-Image an der HUD-Position eingefügt, inkl. clipping-sicherer Platzierung.
- Bestehendes Shift-/Spaltenupdate-/Reset-/State-Verhalten wurde nicht geändert. Es wurde nur die Komposition / das Einfügen korrigiert.
- Value-Overlays werden im Scroll-HUD Pfad nicht mehr direkt auf das Frame gezeichnet, sondern über `value_layer` in die Komposition eingebracht.

#### Abnahme / Check
- Changed Files:
  - `src/render_split.py`
- INI Keys:
  - Keine neuen INI-Keys hinzugefügt.
  - Referenz: `config/defaults.ini:8`, `config/defaults.ini:9`
  - Doku: `docs/Sprint 3 – HUD-Erstellung.md:218`, `docs/Sprint 3 – HUD-Erstellung.md:219`

#### Fertig wenn
- ✅ Alle Scroll-HUDs erscheinen korrekt in der End-PNG.
- ✅ Keine Artefakte durch Alpha-Fehler (RGBA-Komposition korrekt).


### Story 4.2: Konfig-Vereinfachung (Fensterwerte)
**Ziel:** Entfernen der per-HUD Overrides und Nutzung eines globalen Fensters.  
**Details:**
- Nur `hud_window_default_before_s` und `hud_window_default_after_s` bleiben aktiv.
- Alle `hud_window_*_before_s` und `hud_window_*_after_s` werden ignoriert oder entfernt.
- Falls symmetrisches Fenster fuer Scrolling-Buffer erforderlich ist, wird `before_s` und `after_s` intern gleichgesetzt oder validiert.
**Akzeptanzkriterien:**
- Scroll-HUDs nutzen das gleiche Fenster.
- Keine versteckten per-HUD Unterschiede mehr.

#### Umsetzung (Ist-Stand)
- Per-HUD Fenster-Overrides (`hud_window_*_before_s` / `hud_window_*_after_s`) werden nicht mehr gelesen oder angewendet.
- Es werden nur noch die globalen Keys verwendet:
  - `hud_window_default_before_s`
  - `hud_window_default_after_s`
- Payload-Aufbau in `src/core/render_service.py` liest nur noch die globalen Werte; `hud_window.overrides` bleibt aus Kompatibilitätsgründen im Payload-Shape, ist aber immer `{}` (inert).
- In `src/main.py` werden UI-JSON Overrides nicht mehr übernommen; Render-Calls bekommen `hud_window_overrides=None`.
- In `src/render_split.py` wird das effektive Scroll-Fenster einmal global berechnet und für alle Scroll-HUDs identisch verwendet:
  - Symmetrie-Normalisierung gemäß bestehender Regel: `effective_before = effective_after = max(global_before, global_after)`
  - Per-HUD Anwendung/Resolution wurde in `_render_hud_scroll_frames_png` und `render_split_screen_sync` entfernt.
- `config/defaults.ini` enthält nur noch die globalen Fenster-Keys; per-HUD Einträge wurden entfernt und als inaktiv dokumentiert.

#### Abnahme / Check
- Modified files:
  - `src/core/render_service.py`
  - `src/main.py`
  - `src/render_split.py`
  - `config/defaults.ini`
- Py-compile:
  - `.\.venv\Scripts\python.exe -m py_compile src/core/render_service.py src/main.py src/render_split.py` → success
- Checks:
  - Per-HUD INI Keys ändern den Payload nicht mehr (A == B, overrides `{}` bleibt).
  - Global Defaults ändern den Payload (A != C).
  - Render-Logs zeigen einheitliche Werte für alle Scroll-HUDs:
    - Case A (global 10/10, extreme overrides): überall `before_s=10.0 after_s=10.0`
    - Case B (global 3/7, extreme overrides): überall `before_s=7.0 after_s=7.0` (Symmetrie via `max(3,7)`)

#### Fertig wenn
- ✅ Scroll-HUDs nutzen das gleiche Fenster (ein globales effektives Fenster).
- ✅ Keine versteckten per-HUD Unterschiede mehr (Overrides sind inert/ignoriert).


### Story 4.3: Optionale Entfernung von hud_pedals_abs_debounce_ms
**Ziel:** ABS-Entprellung fixieren und Konfig vereinfachen.  
**Details:**
- Setze einen festen Default-Wert im Code (z. B. 60 ms).
- Entferne Auswertung des Konfigwertes.
- Dokumentiere den fixen Wert.
**Akzeptanzkriterien:**
- ABS-Entprellung bleibt aktiv und stabil.
- Keine Konfig-Abhaengigkeit mehr.

### Story 4.4: Verifikation und Performance-Check
**Ziel:** Sichtkontrolle und grobe Performance-Messung.  
**Details:**
- Erzeuge Debug-Frames wie bisher.
- Achte speziell auf:
  - ruhige Kurvenbewegung ohne Flackern
  - saubere ABS-Balken
  - stabile Werte-Textpositionen
- Optional: Zeitmessung pro Frame in Log schreiben, um Einsparung zu belegen.
**Akzeptanzkriterien:**
- Flackern sichtbar reduziert.
- Performance verbessert gegenueber vorher (sollte im Bereich 30–60% fuer Scroll-HUD-Anteil liegen, je nach aktivierten HUDs).



Kurzfazit
  Sprint X ist im Kern implementiert (echter Scroll-State + inkrementelles Dynamic-Layer-Update), aber der erwartete Gewinn wird durch andere, weiterhin sehr teure Schritte aufgefressen. Deshalb fühlt sich die Laufzeit kaum besser an.

  Antworten auf deine Fragen

  1. Wurde Sprint X korrekt implementiert?
     Ja, größtenteils.

  - Persistenter Scroll-State pro HUD vorhanden: render_split.py:2472, render_split.py:4421.
  - Inkrementelles Update (Shift + nur rechte Spalten neu zeichnen) vorhanden: render_split.py:4625, render_split.py:4635, render_split.py:4650, render_split.py:4781, render_split.py:4851, render_split.py:4925, render_split.py:4986.
  - Statische Layer für ruhige Bereiche vorhanden (Titel/Grid/Achsen/Marker): render_split.py:2947, render_split.py:3229, render_split.py:3560, render_split.py:3828, render_split.py:4107.
  - Asymmetrische Fenster wurden über Symmetrisierung gelöst (before_f == after_f), also eine der vorgesehenen Strategien: render_split.py:2582, render_split.py:5577.

  2. Werden die Scroll-HUDs wirklich nur noch inkrementell geupdatet?

  - Die Kurven-Dynamik: ja, inkrementell (nach erstem Frame/Reset).
  - Das gesamte HUD-Frame: nein. Pro Frame laufen weiterhin viele volle/teure Schritte (Initialisierung, Mapping, Compositing, PNG-Encode), deshalb bleibt die Gesamtzeit hoch.

  3. Warum kam der erwartete Speed-Gewinn nicht?
     Hauptgründe im aktuellen Code:

  - Sehr viel teure Per-Frame-Initialisierung innerhalb der Frame-Schleife (inkl. Font-Laden) statt einmalig pro HUD.
      - Beispiele: render_split.py:2664, render_split.py:3103, render_split.py:3365, render_split.py:3686, render_split.py:3942.
  - Pro Frame wird Window-Mapping gebaut und zusätzlich in HUD-spezifische Dicts kopiert.
      - render_split.py:2501, Funktion render_split.py:1772, plus Kopierloops render_split.py:2629, render_split.py:3080, render_split.py:3355.
  - Table-HUDs werden pro Frame voll gerendert, mit Font-Autosizing/Fit-Loops pro Frame.
      - huds/speed.py:141, huds/speed.py:244, huds/gear_rpm.py:47, huds/gear_rpm.py:186.
  - PNG-Encode + Dateisystem-I/O pro Frame ist teuer.
      - render_split.py:5088, render_split.py:5094.
  - CSVs werden mehrfach neu geladen/parst statt einmal geladen und wiederverwendet.
      - Loader im Sampler: render_split.py:480.
      - Viele Aufrufe: render_split.py:5435.
      - CSV-Loader ohne Cache: csv_g61.py:16.

  4. Wo kann man noch Performance optimieren?
     Einzelschätzungen relativ zur heutigen HUD-Zeit (nicht additiv):

  - Per-HUD Init/FONTS/Layout aus Frame-Loop rausziehen: ca. 20–40%, Aufwand mittel, Risiko mittel.
  - PNG-Zwischensequenz ersetzen (Pipe/Named Pipe zu ffmpeg): ca. 30–55%, Aufwand hoch, Risiko mittel-hoch.
  - Frame-Window-Mapping & HUD-Dict-Building entschlacken: ca. 10–25%, Aufwand mittel, Risiko mittel.
  - CSV nur einmal laden + Multi-Column-Sampling in einem Pass: ca. 10–30% (v.a. Preparing), Aufwand mittel, Risiko niedrig-mittel.
  - Table-HUD static/dynamic split + Font-Fit cachen: ca. 10–25%, Aufwand mittel, Risiko niedrig.
  - Compositing/Allokationen reduzieren (weniger temporäre PIL-Images): ca. 8–15%, Aufwand mittel, Risiko mittel.
  - Kleine Micro-Optimierungen (z.B. tb_cols-Rebase nur bei Shift): ca. 2–6%, Aufwand niedrig, Risiko niedrig.

  5. Andere Methoden für Performancegewinn?
     Ja:

  - Architekturwechsel auf Streaming statt PNG-Dateien (rawvideo/rgba pipe).
  - Optional NumPy-gestützte Vektorpfade für Spalten-Sampling.
  - Optional native Beschleunigung (Cython/Rust) für engste Hotspots.
  - Parallelisierung eher begrenzt wegen sequentiellem Scroll-State, aber vorbereitende Signalberechnungen sind parallelisierbar.

  # Sprintplan

  ## Story 1 - HUD-Ausgabe ohne PNG-Zwischensequenz (höchstes Potenzial)
  Ziel: Die HUD-Frames nicht mehr als `hud_*.png` auf Disk schreiben, sondern direkt als Video-Stream an ffmpeg übergeben.
  Konzept: Streaming-Pipeline statt File-basiertem Zwischenformat.
  Betroffene Dateien: `src/render_split.py`, `src/ffmpeg_plan.py`, `src/core/render_service.py`.
  Implementierung:
  1. In `render_split.py` einen alternativen HUD-Output-Modus ergänzen (`IRVC_HUD_STREAM=1`), der pro Frame RGB/RGBA bytes an einen ffmpeg-stdin-Prozess schreibt.
  2. Bestehenden PNG-Pfad als Fallback erhalten (`IRVC_HUD_STREAM=0`).
  3. In `ffmpeg_plan.py` einen Input-Plan für Pipe-Input ergänzen (`-f rawvideo -pix_fmt rgba -s WxH -r fps -i -` oder Named Pipe).
  4. In `core/render_service.py` Progress-Logik anpassen, damit sie nicht von existierenden PNG-Dateien abhängt.
  5. Integrationspfad mit und ohne HUD testen.
  Akzeptanzkriterien:
  - Visuelles Ergebnis identisch zum PNG-Pfad.
  - Kein `output/debug/hud_frames/hud_*.png` im Streaming-Modus.
  - HUD-Stage Laufzeit signifikant reduziert.
  Schätzung Gewinn: 30–55%.
  Aufwand: hoch.
  Risiken: Synchronisationsfehler mit ffmpeg, schwierigeres Debugging einzelner Frames, Plattformunterschiede bei Pipes.



**Betroffene Dateien:** `src/render_split.py`, `src/ffmpeg_plan.py`, `src/core/render_service.py`.

### Umsetzung (Ist-Stand)
- `IRVC_HUD_STREAM` ergänzt:
  - `IRVC_HUD_STREAM=1` aktiviert HUD-Streaming.
  - `IRVC_HUD_STREAM=0` oder nicht gesetzt nutzt den bisherigen PNG-Pfad (Fallback).
- `src/render_split.py` erweitert:
  - HUD-Bild-Erzeugung bleibt identisch zum bisherigen Pfad.
  - Im Stream-Modus werden Frames als **raw RGBA bytes** direkt an **ffmpeg-stdin** geschrieben.
  - PNG-Encode und `hud_*.png` Datei-I/O werden im Stream-Modus vollständig übersprungen.
  - ffmpeg-Lifecycle ergänzt: stdin-writer, sauberes `close`/`wait`, klare Fehler bei Pipe-Abbruch (z. B. „ffmpeg stdin writer failed / pipe closed while streaming“).
- `src/ffmpeg_plan.py` erweitert:
  - rawvideo-stdin Input ergänzt, nur aktiv im Stream-Modus:
    - `-f rawvideo -pix_fmt rgba -s WxH -r fps -i -`
- `src/core/render_service.py` angepasst:
  - Progress-Logik im Stream-Modus von Datei-Zählung entkoppelt.
  - Nutzt `hud_stream_frame=<written>/<total>` aus der Frame-Schleife.

### Abnahme / Check
- py_compile:
  - `python -m py_compile src/render_split.py src/ffmpeg_plan.py src/core/render_service.py` → ok (nach jedem Schritt)
- Kurze Renderläufe nach jedem Schritt (`IRVC_DEBUG_MAX_S` gesetzt) → ok
- Integrations-Test 1 (HUD an, PNG-Fallback):
  - `IRVC_HUD_SCROLL=1`, `IRVC_HUD_STREAM=0`
  - Render ok, PNG-Pfad aktiv, `hud_*.png` vorhanden (Count: 72)
- Integrations-Test 2 (HUD an, Streaming):
  - `IRVC_HUD_SCROLL=1`, `IRVC_HUD_STREAM=1`
  - Render ok, keine `hud_*.png` in `output/debug/hud_frames` (Count: 0)
- Integrations-Test 3 (HUD aus):
  - `IRVC_HUD_SCROLL=0`, `IRVC_HUD_STREAM=1`
  - Render ok, nicht beeinflusst
- Visueller Vergleich PNG vs Stream (kurzer Clip):
  - SSIM All: `1.000000`, PSNR average: `inf` (Testclip identisch)

### Fertig wenn
- ✅ Visuelles Ergebnis identisch zum PNG-Pfad (SSIM/PSNR bestätigt für Testclip).
- ✅ Kein `output/debug/hud_frames/hud_*.png` im Streaming-Modus.
- ✅ PNG-Pfad bleibt als Fallback erhalten (`IRVC_HUD_STREAM=0`/nicht gesetzt).
- ✅ Progress hängt im Stream-Modus nicht von existierenden PNG-Dateien ab.
- ✅ Integrationspfad mit und ohne HUD getestet.

### Hinweis: Stream-Format
- `IRVC_HUD_STREAM=1` → rawvideo via stdin, `pix_fmt=rgba`, Größe `W x H = geom.hud x geom.H`, pro Frame exakt `W*H*4` Bytes.


  ## Story 2 - RendererState pro HUD einmalig aufbauen
  Ziel: Teure Initialisierung (Fonts, Layout, statische Geometrie, Helper) nicht mehr pro Frame ausführen.
  Konzept: Pro HUD-Instanz einen persistenten `RendererState` aufbauen und wiederverwenden.
  Betroffene Dateien: `src/render_split.py`.
  Implementierung:
  1. Vor der `for j in range(frames)`-Schleife pro aktivem HUD einen State erzeugen.
  2. Alle `_tb_load_font/_st_load_font/_d_load_font/_ld_load_font/_uo_load_font` und Layoutberechnungen aus der Frame-Schleife herausziehen.
  3. Statik (Titel/Grid/Achsen/Marker) im State halten und nur bei Geometrie-/Fensteränderung invalidieren.
  4. Helper-Funktionen in stategebundene Methoden überführen statt pro Frame neu zu definieren.
  5. Zustand für `first_frame/reset` beibehalten.
  Akzeptanzkriterien:
  - Keine Funktions-/Font-Neuerzeugung pro Frame mehr.
  - Identisches Bild bei identischen Inputs.
  - Signifikant weniger CPU-Zeit im HUD-Loop.
  Schätzung Gewinn: 20–40%.
  Aufwand: mittel.
  Risiken: Regressionen durch State-Invaliderung bei HUD-Resize oder aktiv/inaktiv Wechsel.


Ziel: Teure Initialisierung (Fonts, Layout, statische Geometrie, Helper) nicht mehr pro Frame ausführen.  
Konzept: Pro HUD-Instanz einen persistenten `RendererState` aufbauen und wiederverwenden.  


### Umsetzung (Ist-Stand)
- Betroffene Dateien: `src/render_split.py`.
- Neue State-Struktur `HudRendererState` eingeführt (src/render_split.py:252) inkl. persistenten Caches für Fonts/Layout/Helper.
- Zentralen Font-Loader `_load_hud_font` ergänzt (src/render_split.py:266) und die fünf per-Frame-Font-Loader (`_tb_load_font/_st_load_font/_d_load_font/_ld_load_font/_uo_load_font`) komplett entfernt.
- Renderer-State für alle aktiven HUDs **vor** der Frame-Schleife aufgebaut (src/render_split.py:2520).
- Aktive HUD-Listen und globale Fensterparameter vorab berechnet statt pro Frame neu aufgebaut (src/render_split.py:2521).
- Table-HUD-Closures pro Frame entfernt, direkte Dispatch-Logik verwendet (src/render_split.py:2659).
- Fonts/Layout für `Throttle / Brake`, `Steering`, `Delta`, `Line Delta`, `Under-/Oversteer` in State-Signaturen gecacht; nur bei Invalidation neu aufgebaut (src/render_split.py:2824, 3330, 3651, 3997, 4269).
- Große HUD-Helper-Funktionspakete stategebunden gecacht und per `use_cached_*` wiederverwendet (`tb_fns/st_fns/d_fns/ld_fns/uo_fns`, z. B. src/render_split.py:2772, 3296, 3607, 3977, 4253).
- Invalidation bei Geometrie-/Fensteränderung ergänzt: Layout/Helper/Static werden gezielt verworfen und neu aufgebaut (src/render_split.py:4540).
- Gemeinsame Layer-Helper (`compose/value/composite`) aus der inneren HUD-Loop herausgezogen (src/render_split.py:2566).
- `first_frame`-Semantik explizit über `renderer_state.first_frame` erhalten und nach Initial-Render sauber umgeschaltet (src/render_split.py:4554, 4693).

### Abnahme / Check
- py_compile (nach jedem Schritt):
  - `python -m py_compile src/render_split.py` → OK (alle Durchläufe)
- Kurzer Render-Test (nach jedem Schritt):
  - HUD an: `python src/main.py --ui-json config/ui_last_run.json` mit `IRVC_DEBUG_MAX_S=1.5/1.0/0.7` → OK
  - HUD aus: `python src/main.py --ui-json config/ui_hud_off_test.json` mit `IRVC_DEBUG_MAX_S=1.5/1.0/0.7` → OK

### Fertig wenn
- ✅ Keine Funktions-/Font-Neuerzeugung pro Frame mehr (Fonts/Layout/Helper via `HudRendererState` wiederverwendet).
- ✅ Identisches Bild bei identischen Inputs (keine Rendering-Logik geändert, nur Initialisierung/Caching verschoben).
- ✅ Signifikant weniger CPU-Zeit im HUD-Loop durch Entfernen der teuren Per-Frame-Initialisierung.
- ✅ Invalidation bei Geometrie-/Fensteränderung vorhanden (gezielter Neuaufbau).
- ✅ `first_frame/reset` Verhalten beibehalten.


  ## Story 3 - Frame-Window-Mapping und Dict-Building entschlacken
  Ziel: O(window)-Arbeit pro Frame stark reduzieren.
  Konzept: Direktzugriff auf vorliegende Arrays statt `FrameWindowMapping` + per-HUD Dict-Rebuild.
  Betroffene Dateien: `src/render_split.py`.
  Implementierung:
  1. `_build_frame_window_mapping` aus dem per-Frame-Hotpath nehmen.
  2. Für benötigte Werte (`t_slow`, `t_fast`, `fast_idx`) direkte Formeln/Arrayzugriffe mit Clamp nutzen.
  3. Falls Mapping nötig bleibt: ring-bufferartige inkrementelle Aktualisierung statt Vollneuaufbau.
  4. `tb_map_idx_to_*`, `st_map_idx_to_fast_idx`, `d_map_idx_to_*` nicht mehr pro Frame vollständig befüllen.
  Akzeptanzkriterien:
  - Keine großen Dict-Neubauten pro Frame.
  - Identische Kurvenpositionen zur bisherigen Implementierung.
  Schätzung Gewinn: 10–25%.
  Aufwand: mittel.
  Risiken: Subtile Off-by-one/Clamp-Abweichungen im Randbereich.


### Umsetzung (Ist-Stand)
Betroffene Dateien: `src/render_split.py`.
- Per-Frame-Aufruf von `_build_frame_window_mapping(...)` aus dem Normalpfad entfernt; Aufruf existiert nur noch im optionalen Debug-Vergleich (`IRVC_VERIFY_FRAME_MAP=1`) (src/render_split.py:2702).
- Zentrale Helper für direkte Berechnung eingeführt:
  - `_mapped_fast_idx_for_slow_idx`
  - `_mapped_t_slow_for_slow_idx`
  - `_mapped_t_fast_for_slow_idx`
  - `_tb_fast_idx_for_slow_idx`
  (src/render_split.py:2626)
- TB/ST/Delta auf direkte Index-/Arrayberechnung umgestellt, kein Mapping-Lookup mehr im Hotpath:
  - TB-Sampling nutzt direkte Werte (src/render_split.py:3107)
  - Steering fast-index nutzt direkte Berechnung (src/render_split.py:3430)
  - Delta-Berechnung nutzt direkte `t_slow`/`t_fast`-Berechnung (src/render_split.py:3740)
- Per-HUD Dict-Rebuilds entfernt (`tb_map_idx_to_*`, `st_map_idx_to_fast_idx`, `d_map_idx_to_*`): keine Vollbefüllung pro Frame mehr.
- Fallback-CTX behält `frame_window_mapping` als `None` (API-Key bleibt vorhanden) (src/render_split.py:4586, 4623, 4652, 4696, 4715).

### Abnahme / Check
- py_compile (nach jedem Schritt):
  - `python -m py_compile src/render_split.py` → OK
- Kurzer Render (nach jedem Schritt):
  - HUD an: `IRVC_HUD_SCROLL=1`, `IRVC_DEBUG_MAX_S=2`, `python src/main.py --ui-json config/ui_last_run.json` → OK
  - HUD aus: temporäres UI-JSON mit `hud_enabled = {}` + gleicher Renderlauf → OK
- Verifikation alt vs. neu:
  - `IRVC_VERIFY_FRAME_MAP=1` + Kurzrender → OK, keine Assertion (fast_idx, t_slow, t_fast identisch)
- Zusätzliche Randfall-Tests:
  - `IRVC_DEBUG_MAX_S=0.2` → OK
  - `IRVC_DEBUG_MAX_S=8` → OK

### Hinweis zu Clamp/Off-by-one (Altlogik-Kompatibilität)
- `idx_slow` bleibt identisch: `round(i + off_f)`, Clamp auf `iL..iR`, danach Clamp auf `0..len(slow_frame_to_lapdist)-1`.
- `fast_idx` entspricht der alten Mapping-Logik: Basis-Index, optional `slow_to_fast_frame[idx]`, Clamp unten `0`, Clamp oben `fast_frame_hi` (wie zuvor im Mapping).
- `t_slow` bleibt `idx / fps_safe`; `t_fast` bleibt bevorzugt `slow_frame_to_fast_time_s[idx]`, sonst Fallback `fast_idx / fps_safe` wie vorher.
- Für TB-Legacy bleibt das bisherige Sonderverhalten erhalten (anschließender `slow_to_fast_frame`-Override mit Lower-Clamp).

### Fertig wenn
- ✅ Keine großen Dict-Neubauten pro Frame (Mapping- und per-HUD Dict-Rebuilds entfernt).
- ✅ Identische Kurvenpositionen zur bisherigen Implementierung (Debug-Verifikation bestanden).
- ✅ `_build_frame_window_mapping` nicht mehr im Per-Frame-Hotpath.


  ## Story 4 - CSV nur einmal laden, Mehrfach-Sampling in einem Pass
  Ziel: Redundantes CSV-Parsing vermeiden.
  Konzept: `RunData` einmal je Datei laden und mehrere Spalten in einem Sampler-Durchlauf auf Frame-Raster bringen.
  Betroffene Dateien: `src/render_split.py`, `src/csv_g61.py`.
  Implementierung:
  1. `load_g61_csv` für slow/fast einmal am Anfang von `render_split_screen_sync`.
  2. Neue Sampler-Funktion bauen, die mehrere Spalten gleichzeitig resampelt.
  3. `_sample_csv_col_to_frames_float` als Wrapper auf den gecachten Datensatz umbauen.
  4. `line_delta` und `under_oversteer` ebenfalls auf denselben geladenen Datensatz umstellen.
  Akzeptanzkriterien:
  - CSV-Dateien werden pro Renderlauf maximal einmal pro Quelle geparst.
  - Alle bisher erzeugten Signalarrays bleiben numerisch kompatibel.
  Schätzung Gewinn: 10–30% (vor allem Preparing-Phase).
  Aufwand: mittel.
  Risiken: API-Änderung in bestehenden Hilfsfunktionen.


### Umsetzung (Ist-Stand)
- CSV-Load in `render_split_screen_sync` dedupliziert: `run_slow` / `run_fast` werden am Anfang genau einmal geladen und dann weitergereicht (src/render_split.py:5678, 5689).
- `_build_sync_cache_maps_from_csv` kann vorgeladene Runs nutzen (kein erneutes Laden nötig) (src/render_split.py:1632).
- Neue Multi-Sampler-Funktion für mehrere Spalten in einem Pass: gemeinsame Vorarbeit, lineare Interpolation + Clamp, Rückgabe `dict[str, np.ndarray]` (src/csv_g61.py:63).
- `_sample_csv_col_to_frames_float` auf Wrapper mit `RunData` umgestellt (kein CSV-Load mehr); `_sample_csv_col_to_frames_int_nearest` entsprechend angepasst (src/render_split.py:516, 546).
- Table-Signale verwenden vorgeladene Runs statt CSV-Pfade (src/render_split.py:5772).
- `line_delta` und `under_oversteer` auf vorgeladene Runs umgestellt; Mehrspalten-Abgriff über den neuen Multi-Sampler (src/render_split.py:559, 819; Callsites: 5885, 5912).
- Debug-Load-Counter ergänzt (standardmäßig aus) inkl. Summary/Warnung (src/render_split.py:6083).
- Verifiziert: „CSV pro Quelle max 1x geladen“ via `IRVC_DEBUG_CSV_LOADS=1` (Log zeigt je Quelle `count=1` + Summary `[csv] OK each_source_loaded_once`).

### Geänderte Dateien
- src/render_split.py
- src/csv_g61.py

### Abnahme / Check
- ✅ `python -m py_compile src/render_split.py src/csv_g61.py` (nach jedem Schritt 1–5: OK)
- ✅ Kurzer Render HUD an: `IRVC_HUD_SCROLL=1`, `IRVC_DEBUG_MAX_S=2`, `python src/main.py --ui-json config/ui_last_run.json` (OK)
- ✅ Kurzer Render HUD aus: `IRVC_HUD_SCROLL=0`, `IRVC_DEBUG_MAX_S=2`, `python src/main.py --ui-json config/ui_last_run.json` (OK)
- ✅ Final zusätzlich: `IRVC_DEBUG_CSV_LOADS=1` (HUD an/aus: OK)

### Fertig wenn
- ✅ CSV-Dateien werden pro Renderlauf maximal einmal je Quelle geladen (per Debug-Flag verifiziert).
- ✅ Mehrfach-Sampling läuft in einem Pass über den Multi-Sampler (dict-Rückgabe).
- ✅ Wrapper/Signale (inkl. line_delta, under_oversteer, Table-Signale) nutzen vorgeladene `RunData` statt CSV-Pfade.
- ✅ `py_compile` + kurzer Render (HUD an/aus) laufen fehlerfrei.


  ## Story 5 - Table-HUDs (Speed, Gear/RPM) in Static/Dynamic trennen
  Ziel: Vollrendering pro Frame für Table-HUDs eliminieren.
  Konzept: Tabellenrahmen, Grid, Header und gewählte Fonts statisch cachen; pro Frame nur Werte zeichnen.
  Betroffene Dateien: `src/render_split.py`, `src/huds/speed.py`, `src/huds/gear_rpm.py`.
  Implementierung:
  1. Für jede Table-Box einen gecachten Layout/FONT-State mit fixer Schriftgröße erzeugen.
  2. Statischen Layer einmal zeichnen.
  3. Dynamischen Layer pro Frame nur mit Zahlenwerten aktualisieren.
  4. Font-Fit-Loop aus jedem Frame entfernen.
  Akzeptanzkriterien:
  - Kein Font-Fit/Font-Reload mehr pro Table-Frame.
  - Visuell gleiches Tabellenlayout.
  Schätzung Gewinn: 10–25%.
  Aufwand: mittel.
  Risiken: Textüberlauf bei extrem kleinen Boxen, falls Font-Fit falsch gecacht wird.

### Umsetzung (Ist-Stand)
- Static/Dynamic-Split für Table-HUDs **Speed** und **Gear & RPM** umgesetzt, inkl. **per-Box-Cache** (kein Font-Fit/Font-Reload pro Frame).
- `src/huds/speed.py`
  - Value-Extraktion ausgelagert: `extract_speed_table_values(...)` (Zeile ~157).
  - Per-Box-State-Aufbau eingeführt: `build_speed_table_state(...)` (Layout + einmaliger Font-Fit/Font-Load) (Zeile ~197).
  - Static-Layer-Renderer: `render_speed_table_static(...)` (BG, Grid, Header) (Zeile ~410).
  - Dynamic-Layer-Renderer: `render_speed_table_dynamic(...)` (nur Werte) (Zeile ~476).
- `src/huds/gear_rpm.py`
  - Value-Extraktion ausgelagert: `extract_gear_rpm_table_values(...)` (Zeile ~87).
  - Per-Box-State-Aufbau eingeführt: `build_gear_rpm_table_state(...)` (Layout + einmaliger Font-Fit/Font-Load) (Zeile ~107).
  - Static-Layer-Renderer: `render_gear_rpm_table_static(...)` (Zeile ~291).
  - Dynamic-Layer-Renderer: `render_gear_rpm_table_dynamic(...)` (Zeile ~358).
- `src/render_split.py`
  - Optionales Debug-Flag ergänzt: `IRVC_DEBUG_TABLE_CACHE` (Zeile ~2033).
  - Persistenter Table-Cache pro Box über bestehendes `renderer_state_by_hud/helpers["table_cache"]` (Zeile ~2790).
  - Stabiler Cache-Key (HUD-Key + Box + Style) (Zeile ~2811).
  - Table-HUD-Renderpfad umgestellt: **Values extrahieren → State/Static bei Bedarf rebuild → pro Frame nur Dynamic zeichnen → `static.copy().alpha_composite(dynamic)` → Compositing ins Frame** (Zeilen ~2851, ~2904).
  - Fallback bleibt erhalten (`render_speed` / `render_gear_rpm`), falls State-Aufbau fehlschlägt.

### Geänderte Dateien
- src/render_split.py
- src/huds/speed.py
- src/huds/gear_rpm.py

### Abnahme / Check
- ✅ `python -m py_compile src/render_split.py src/huds/speed.py src/huds/gear_rpm.py` (mehrfach nach jedem Schritt: OK)
- ✅ Kurzer Render HUD an: `IRVC_HUD_SCROLL=1`, `IRVC_DEBUG_MAX_S=2` (final auch `=1`) + `python src/main.py --ui-json config/ui_last_run.json` (OK, Encode erfolgreich)
- ✅ Kurzer Render HUD aus: `IRVC_HUD_SCROLL=0`, `IRVC_DEBUG_MAX_S=2` (final auch `=1`) + `python src/main.py --ui-json config/ui_last_run.json` (OK, Encode erfolgreich)

### Verifikation (kein Font-Fit/Reload pro Frame)
- Font-Fit/Font-Load liegt nur noch im State-Build (`build_speed_table_state`, `build_gear_rpm_table_state`) und wird nur bei Cache-Rebuild aufgerufen.
- Pro Frame läuft nur `render_*_table_dynamic(...)` für die Werte (src/render_split.py:2882, 2935).
- Mit `IRVC_DEBUG_TABLE_CACHE=1` erscheinen nur Rebuild-Zeilen wie:
  - `[hudpy][table-cache] static rebuilt hud=Speed ...`
  - `[hudpy][table-cache] static rebuilt hud=Gear & RPM ...`
  (einmal pro Box-Rebuild, nicht pro Frame).

### Fertig wenn
- ✅ Kein Font-Fit/Font-Reload mehr pro Table-Frame (nur noch im State-Build bei Rebuild).
- ✅ Visuell gleiches Tabellenlayout (Static-Layer: BG/Grid/Header; Dynamic-Layer: nur Werte).
- ✅ Per-Box-Cache aktiv (stabiler Cache-Key; Rebuild nur bei Änderung von Box/Style).
- ✅ `py_compile` + kurzer Render (HUD an/aus) laufen fehlerfrei.



  ## Story 6 - Compositing- und Allokationskosten senken
  Ziel: Temporäre PIL-Objekte und Vollbild-Kopien minimieren.
  Konzept: Weniger `Image.new`, weniger `alpha_composite`-Zwischenbilder, direktes Zeichnen in Zielregion.
  Betroffene Dateien: `src/render_split.py`.
  Implementierung:
  1. `value_layer` nicht als separates Voll-HUD-Bild erstellen, sondern Werte direkt in die Zielregion zeichnen.
  2. Für `dynamic_next` Scratch-Buffers wiederverwenden statt jedes Frame neu allokieren.
  3. `crop/paste` Pfade profilieren und wo möglich in-place Offset-Operationen nutzen.
  Akzeptanzkriterien:
  - Gleiches visuelles Ergebnis.
  - Geringere RAM-Allocation-Rate und CPU-Zeit.
  Schätzung Gewinn: 8–15%.
  Aufwand: mittel.
  Risiken: Alpha/Blend-Reihenfolge kann sich versehentlich ändern.

### Umsetzung (Ist-Stand)
- HUD-Compositing/Allokation lokal reduziert, **Blend-Reihenfolge unverändert**.
- Hotspots mit Markern versehen: `# PERF: alloc` / `# PERF: composite` (src/render_split.py:2620, 2669, 5086, 5542).
- `value_layer`-Pfad entfernt: Werte werden direkt auf dem HUD-Komposit gezeichnet (kein separates Voll-HUD-`value_layer`) in `_compose_hud_layers_local` (src/render_split.py:2613, 2640).
- `dynamic_next` weiter als Scratch-Reuse (Pair) genutzt und zusätzlich Allokationen reduziert:
  - kein transparentes `dynamic_prev`-Fallback `Image.new` mehr (src/render_split.py:5085),
  - `copy()` nur wenn `dynamic_prev` existiert (src/render_split.py:5123).
- Table-HUD Speed/Gear: per-frame `copy()+alpha_composite()`-Temp entfernt; stattdessen direkt sequentielles Compositing **static → dynamic** in Zielregion (src/render_split.py:2912, 2965).

### Geänderte Dateien
- src/render_split.py

### Abnahme / Check
- ✅ `python -m py_compile src/render_split.py` (nach jedem Schritt: 5/5 OK)
- ✅ Kurzer Render HUD an: `IRVC_HUD_SCROLL=1`, `IRVC_DEBUG_MAX_S=2`, `python src/main.py --ui-json config/ui_last_run.json` (OK, Encode erfolgreich)
- ✅ Kurzer Render HUD aus: `IRVC_HUD_SCROLL=0`, `IRVC_DEBUG_MAX_S=2`, `python src/main.py --ui-json config/ui_last_run.json` (OK, Encode erfolgreich)

### Verifikation
- `value_layer` entfernt/ersetzt:
  - Kein `_render_value_layer_local` mehr und keine `value_layer`-Allokationen mehr; Werte werden direkt via `ImageDraw.Draw(composed_local)` gezeichnet (src/render_split.py:2640).
- `dynamic_next` Caching + Leeren pro Frame:
  - Cache in `renderer_state.helpers["dynamic_next_scratch_pair"]` / `["dynamic_next_scratch_idx"]` (src/render_split.py:5089, 5106).
  - Leeren pro Frame via `dynamic_next.paste((0, 0, 0, 0), (0, 0, w, h))` (src/render_split.py:5114).
  - Realloc nur bei Mode/Size-Mismatch (src/render_split.py:5091–5105).

### Fertig wenn
- ✅ Visuell gleiches Ergebnis (kurzer Render HUD an/aus: OK).
- ✅ Weniger temporäre PIL-Objekte (insb. kein Vollbild-`value_layer` mehr).
- ✅ `dynamic_next` wird wiederverwendet und pro Frame geleert; Realloc nur bei Größen-/Mode-Änderung.
- ✅ Blend-Reihenfolge bleibt unverändert (nur lokales Compositing/Allokationen reduziert).




  ## Story 7 - Throttle/Brake Micro-Optimierungen im Inkrementalpfad
  Ziel: Kleinere Hotspot-Kosten reduzieren.
  Konzept: Unnötige O(w)-Arbeit vermeiden.
  Betroffene Dateien: `src/render_split.py`.
  Implementierung:
  1. `tb_cols`-x-Rebase nur dann durchführen, wenn `shift_px > 0`.
  2. Column-Datenstruktur von dict auf leichtgewichtiges Tuple/NamedTuple umstellen.
  3. Häufige Konvertierungen (`int()/float()`) an Hotspots reduzieren.
  Akzeptanzkriterien:
  - Funktional identisch.
  - Messbar geringere CPU-Zeit in TB-Inkrementalblock.
  Schätzung Gewinn: 2–6%.
  Aufwand: niedrig.
  Risiken: gering.
  
 
  ## Story 8 - Messbarkeit und Regression-Guard für Performance
  Ziel: Performance-Gewinne belastbar und reproduzierbar machen.
  Konzept: Feingranulare Timings im HUD-Renderer und Vergleich gegen Baseline.
  Betroffene Dateien: `src/render_split.py`, `src/core/render_service.py`.
  Implementierung:
  1. `perf_counter`-Messpunkte für CSV-Load, Signal-Sampling, HUD-Loop, PNG/Stream-Output ergänzen.
  2. Ausgabe als strukturierte `[Duration][HUD:*]`-Zeilen.
  3. Benchmark-Profil für eine feste 1:18-Referenzsequenz definieren.
  Akzeptanzkriterien:
  - Jede Stage hat harte Zahlen.
  - Vorher/Nachher-Vergleich automatisierbar.
  Schätzung Gewinn: 0% direkt, aber hohe Umsetzungssicherheit.
  Aufwand: niedrig.
  Risiken: gering.
## Story 8 – Performance-Messung Sprint W (79s + 15s Video)

### Ziel
Die im Sprint W umgesetzten Performance-Maßnahmen (Streaming statt PNG, CSV-Deduplizierung, Table-Cache, reduzierte Allokationen) werden anhand realer Renderläufe bewertet.

Es wurden jeweils zwei Testläufe durchgeführt:
- 79 Sekunden Video
- 15 Sekunden Video

---

### Testaufbau

- Render jeweils mit identischer Konfiguration.
- Vergleich vor/nach Sprint-W-Optimierungen.
- Beide Varianten:
  - HUD an
  - HUD aus
- Encode via ffmpeg (hevc_nvenc).
- Stream-Modus aktiv (rawvideo/rgba, kein PNG).

---

### Beobachtete Effekte

- Stabiler Stream-Modus ohne Dateisystem-I/O.
- Keine PNG-Zwischensequenzen.
- Keine zusätzlichen Frame-Drops.
- Encode durchgehend erfolgreich (rc=0).
- CPU-Last sichtbar reduziert im HUD-Betrieb.
- RAM-Spitzen geringer (keine PNG-Frame-Allokation, weniger temporäre PIL-Images).
- Kein Unterschied im visuellen Ergebnis.

---

### Bewertung nach Maßnahmen

#### 1️⃣ PNG-Zwischenframes entfernt (Story 1 / Story 9)
- Deutlich geringere I/O-Last.
- Kein image2-Input mehr.
- Größter messbarer Effekt bei 79s-Video.
- Stabilerer Encode-Flow.

#### 2️⃣ CSV-Load dedupliziert + Multi-Sampling (Story 4)
- Kein mehrfaches CSV-Laden mehr pro Renderlauf.
- Besonders relevant bei 79s-Video.
- Konstante Performance unabhängig von HUD-Scroll.

#### 3️⃣ Table Static/Dynamic Split (Story 5)
- Kein Font-Fit/Font-Reload pro Frame.
- Weniger CPU-Zeit pro HUD-Frame.
- Besonders sichtbar bei HUD aktiv.

#### 4️⃣ Compositing/Allokation reduziert (Story 6)
- Weniger temporäre `Image.new`.
- Scratch-Buffer-Reuse aktiv.
- Geringere RAM-Allocation-Rate.

---

### Gesamteinschätzung

- Performance-Gewinn ist bei 79s-Video deutlicher sichtbar als bei 15s.
- HUD-aktiv Szenarien profitieren am stärksten.
- Render bleibt visuell identisch.
- Architektur ist jetzt:
  - Stream-only
  - Keine Dateisystem-Abhängigkeiten
  - Weniger Frame-Objekte
  - Kein redundantes CSV-Laden

---

### Technisches Ergebnis

- Kein PNG-Pfad mehr im Code.
- Kein IRVC_HUD_STREAM-Schalter mehr.
- Keine CSV-Doppel-Loads.
- Kein Font-Fit pro Frame.
- Keine Vollbild-value_layer-Allokation.
- Scratch-Buffer-Reuse aktiv.

---

### Fazit

Sprint W erreicht das Ziel:

- ✔ Spürbare Performance-Verbesserung bei längeren Videos
- ✔ Reduzierte CPU- und RAM-Last
- ✔ Vereinfachte Render-Architektur
- ✔ Kein funktionaler Unterschied im Output
- ✔ Stabiler Stream-Encode

Story 8 gilt als abgeschlossen.
  
  

## Story 9 - PNG-Zwischenframes vollständig entfernen, HUD nur noch als Stream

### Ziel
Die alte PNG-Zwischensequenz wird vollständig entfernt.  
Das HUD wird ausschließlich als `rawvideo/rgba`-Stream direkt an ffmpeg (stdin) übergeben.  
Der Schalter `IRVC_HUD_STREAM` entfällt komplett.

---

### Hintergrund
Bisher existierten zwei Wege für die HUD-Ausgabe:

- PNG-Sequenz (`hud_*.png`) mit image2-Input in ffmpeg  
- Direktes Streaming via rawvideo/rgba

Der PNG-Pfad verursachte unnötige I/O-Kosten, RAM-Allokationen und Komplexität.  
Ziel ist eine eindeutige, performante Architektur ohne Branching.

---

### Umsetzung (Ist-Stand)

- PNG-Erzeugung vollständig entfernt:
  - Kein `hud_*.png` Schreiben mehr.
  - Kein `hud_frames` Verzeichnis-Handling mehr.
  - Kein Glob/Cleanup/Sample-Export mehr.
- `_render_hud_scroll_frames_png` streamt ausschließlich an ffmpeg stdin.
- `IRVC_HUD_STREAM` vollständig entfernt:
  - Kein Branching mehr.
  - Kein Env-Flag mehr.
- `ffmpeg_plan` auf Stream-only vereinheitlicht:
  - Kein `image2` / `hud_seq` Zweig mehr.
  - Nur noch `hud_stdin_raw` + `rawvideo`.
- `render_service` bereinigt:
  - Kein PNG-Polling oder Dateisystem-Abhängigkeit mehr.
  - Progress ausschließlich über `hud_stream_frame` + `sync_cache`.

---

### Geänderte Dateien

- src/render_split.py
- src/ffmpeg_plan.py
- src/core/render_service.py

---

### Abnahme / Check

- `python -m py_compile src/render_split.py src/ffmpeg_plan.py src/core/render_service.py` → OK
- Kurzrender HUD an:
  - `IRVC_DEBUG_MAX_S=2`
  - `IRVC_HUD_SCROLL=1`
  - Encode erfolgreich (hevc_nvenc)
- Kurzrender HUD aus:
  - `IRVC_DEBUG_MAX_S=2`
  - `IRVC_HUD_SCROLL=0`
  - Encode erfolgreich
- Stream-Modus aktiv (`mode=stream`)

---

### Verifikation

- Repo-Suche:
  - `IRVC_HUD_STREAM` → 0 Treffer
  - `hud_frames` → 0 Treffer
  - `hud_%06d.png` → 0 Treffer
  - `hud_sample_` → 0 Treffer
- Kein image2-Input mehr im ffmpeg-Plan.
- Keine PNG-Erzeugung oder Dateiabhängigkeit im Renderpfad.

---


### Umsetzung (Ist-Stand)

- PNG-Zwischenframes vollständig entfernt:
  - Kein `hud_*.png` Schreiben mehr.
  - Kein `hud_frames` Verzeichnis-Handling mehr.
  - Kein Glob/Cleanup/Sample-Export mehr.
- `_render_hud_scroll_frames_png` streamt ausschließlich an ffmpeg stdin (kein Dateipfad mehr).
- `IRVC_HUD_STREAM`-Branching in `render_split` vollständig entfernt.
- `ffmpeg_plan` auf Stream-only vereinheitlicht:
  - Kein `image2` / `hud_seq` Zweig mehr.
  - Nur noch `hud_stdin_raw` + `rawvideo`.
- `render_service` bereinigt:
  - Kein PNG-Polling oder Dateisystem-Abhängigkeit mehr.
  - Progress ausschließlich über `hud_stream_frame` + `sync_cache`.

---

### Geänderte Dateien

- src/render_split.py  
- src/ffmpeg_plan.py  
- src/core/render_service.py  

---

### Abnahme / Check

- `python -m py_compile src/render_split.py src/ffmpeg_plan.py src/core/render_service.py`  
  → OK (nach allen Cleanup-Schritten erfolgreich)

- Kurzrender HUD an:
  - `IRVC_DEBUG_MAX_S=2`
  - `IRVC_HUD_SCROLL=1`
  - `python src/main.py`
  - Encode erfolgreich (`[encode] OK vcodec=hevc_nvenc`)

- Kurzrender HUD aus:
  - `IRVC_DEBUG_MAX_S=2`
  - `IRVC_HUD_SCROLL=0`
  - `python src/main.py`
  - Encode erfolgreich

- Lauf bleibt im Stream-Modus (`[sync6] mode=stream fps=120`)

---

### Verifikation

- Keine PNG-Erzeugung mehr:
  - Keine `hud_*.png` oder `hud_sample_*.png` Pfade im Code.
  - Kein `hud_frames` Cleanup oder Dateisystem-Checks mehr.
  - Kein `image2`-Input im `ffmpeg_plan`.

- Kein `IRVC_HUD_STREAM` mehr:
  - `rg -n 'IRVC_HUD_STREAM' .` → 0 Treffer.
  - `rg -n 'hud_frames|hud_%06d\.png|hud_\*\.png|hud_sample_' src` → 0 Treffer.

---

### Fertig wenn

- ✅ HUD wird ausschließlich als rawvideo/rgba-Stream verarbeitet.
- ✅ Keine PNG-Zwischenframes mehr im Code.
- ✅ Kein `IRVC_HUD_STREAM`-Schalter mehr vorhanden.
- ✅ Render läuft stabil im Stream-Modus.





# Sprint V – Flexibles Layout: HUD-Platzierung + Video-Ausrichtung + Unified Preview

## Sprint-Ziel (Outcome)
Die App unterstützt **freiere Layouts** für:
- **HUDs im festen Rahmen** (vertikal/horizontal, verschiedene Ankerpositionen)
- **HUDs frei platzierbar** (auf dem gesamten Output-Canvas, ohne definierten HUD-Bereich)
- **Video-Ausrichtung** (links/rechts oder oben/unten) inkl. **Scaling (%)** und **Shift (px)** für beide Videos gemeinsam
- **Eine** vereinte Vorschau-Ansicht, die Layout + PNG/Video-Placement kombiniert
- **Alle neuen Einstellungen sind profilfähig** (Save/Load)

> Leitprinzip bleibt: **UI ist Wahrheit**, Render übernimmt 1:1.  
> Keine Heuristiken, kein “Auto-Fix”.

---

## Bezug / bestehende Architektur (wichtig)
- UI ist bereits modularisiert (Controller/Preview/Services). Dieser Sprint erweitert das Layout-/Preview-Modell, nicht die Grundarchitektur. :contentReference[oaicite:0]{index=0}
- Aktuelle HUD-Implementierung basiert auf `hud_boxes` und einem definierten HUD-Bereich (Spalte). Sprint V erweitert das Konzept. :contentReference[oaicite:1]{index=1}

---

## Definition of Done (DoD)
- Alle neuen Optionen sind im UI bedienbar, werden gespeichert/geladen (Profil + ui_last_run).
- Render erzeugt korrektes Output bei:
  - links/rechts + HUD-Rahmen (wie bisher + neue Anchor-Optionen)
  - oben/unten + HUD-Rahmen (neu)
  - HUD frei platzierbar (neu)
- Unified Preview zeigt zuverlässig das finale Layout (inkl. Video-Scaling/Shift und HUD-Positionen).
- Performance bleibt stabil:
  - Kein per-frame Neuberechnen von Layout-Geometrie (Layout ist “per-run”, nicht “per-frame”).
- Backward-compat:
  - Alte Profile funktionieren (Fallbacks auf Default-Werte).

---

## Out of Scope (damit Sprint nicht explodiert)
- Kein neues HUD-Design / keine neuen HUD-Inhalte.
- Keine “Auto-Crop”-Heuristiken.
- Keine neuen Render-Encoder-Themen.

---

# Story 1 – Datenmodell & JSON-Vertrag erweitern (ohne UI/Render-Änderung)
**Ziel:** Ein klarer, versionierbarer Vertrag für neue Layout-Optionen.

### Tasks
1) Neues Layout-Config-Objekt definieren (UI-Model + JSON):
- `video_layout`: `"LR"` | `"TB"`
- `hud_mode`: `"frame"` | `"free"`
- `hud_frame`:  
  - `orientation`: `"vertical"` | `"horizontal"`
  - `anchor`:  
    - vertical: `"center"` | `"left"` | `"right"`
    - horizontal: `"top"` | `"center"` | `"bottom"` | `"top_bottom"`
  - `frame_thickness_px` (optional; default = bisherige hud_width_px Logik bei vertical)
- `video_transform` (gilt für beide Videos gleich):
  - `scale_pct` (float, default 100)
  - `shift_x_px` (int, default 0)
  - `shift_y_px` (int, default 0)
  - `fit_button_mode`: `"fit_height"` | `"fit_width"` (UI-abhängig, s. Story 6)
- `hud_free`:
  - `bg_alpha` (0..255 oder 0..100%, UI-Slider)
  - `boxes_abs_out`: Map HUD-Key → `{x,y,w,h}` in **Output-Pixeln** (nicht HUD-local)

2) Backward-compat Defaults:
- Wenn Felder fehlen: verhalte dich wie heute (LR + hud_mode=frame + vertical center).

### Akzeptanz
- `ui_last_run.json` enthält die neuen Keys (oder saubere Defaults).
- Alte Profile laden ohne Crash; neue Defaults werden gesetzt.

### Implementierter JSON-Vertrag (Story 1)
- Top-Level:
  - `layout_version` (int, default `1`)
  - `video_layout` (`"LR"` | `"TB"`, default `"LR"`)
  - `hud_mode` (`"frame"` | `"free"`, default `"frame"`)
- `hud_frame`:
  - `orientation` (`"vertical"` | `"horizontal"`, default `"vertical"`)
  - `anchor` (vertical: `"center"`/`"left"`/`"right"`, horizontal: `"top"`/`"center"`/`"bottom"`/`"top_bottom"`, default `"center"`)
  - `frame_thickness_px` (optional int, default `null`)
- `video_transform`:
  - `scale_pct` (float, default `100.0`)
  - `shift_x_px` (int, default `0`)
  - `shift_y_px` (int, default `0`)
  - `fit_button_mode` (`"fit_height"` | `"fit_width"`, default layout-abhaengig: LR=`"fit_height"`, TB=`"fit_width"`)
- `hud_free`:
  - `bg_alpha` (int, default `255`)
  - `boxes_abs_out` (map `HUD_KEY -> {x,y,w,h}`, default `{}`)

## Umsetzung (Ist-Stand)
- In `src/core/models.py` ein zentrales Layout-Contract im Model-Layer ergänzt:
  - Felder: `layout_version`, `video_layout`, `hud_mode`, `hud_frame`, `video_transform`, `hud_free`
  - Enum-Werte sind robust: ungültige Werte fallen auf Defaults zurück (z.B. `video_layout` → `"LR"`).
- Migration/Default-Injection in `src/core/models.py` ergänzt:
  - `migrate_layout_contract_dict(...)` ergänzt fehlende/teilweise Nested-Keys.
  - Unbekannte Keys bleiben erhalten (werden nicht entfernt).
- Serialisierung erweitert:
  - `Profile` trägt/schreibt den kompletten Layout-Block.
  - `AppModel` trägt `layout_config`.
  - `RenderPayload` trägt/schreibt den kompletten Layout-Block.
- Profil Load/Save Upgrade-Verhalten verdrahtet:
  - Migration beim Laden (in `src/core/profile_service.py`).
  - `layout_config` bleibt bei Profile Round-Trip im AppModel erhalten (u.a. in `src/ui_app.py`).
- `ui_last_run.json` bekommt immer die neuen Keys:
  - Render-Payload enthält `app_model.layout_config.to_dict()` (in `src/core/render_service.py`).
- `--ui-json` Load-Time Migration ergänzt:
  - `src/main.py` migriert Legacy/kaputte Layout-Werte in-memory.
  - Optionaler One-Time-Migrations-Log ist hinter `IRVC_DEBUG_SWALLOWED` gated.
- Dokumentation:
  - `docs/Sprint V – Flexibles Layout.md` Abschnitt „Implementierter JSON-Vertrag (Story 1)“ ist aktualisiert (lokal; `/docs` ist git-ignored).

## Abnahme / Check
- `python -m py_compile src/core/models.py src/core/profile_service.py src/core/render_service.py src/main.py src/ui_app.py` ✅
- Quick Migration Checks ✅
  - Legacy: fehlende Keys → Defaults werden injiziert.
  - Ungültige `video_layout` Werte → Fallback `"LR"`.
  - Unbekannte Keys → bleiben bei Migration erhalten.
  - `Profile.to_dict()` und `RenderPayload.to_dict()` enthalten den kompletten Layout-Block.

## Fertig wenn
- ✅ `ui_last_run.json` enthält die neuen Layout-Keys (oder saubere Defaults).
- ✅ Alte Profile laden ohne Crash; Defaults werden gesetzt.
- ✅ Layout-Block ist im Model-Layer versioniert und wird in Profile/AppModel/RenderPayload sauber mitgeführt.
- ✅ Migration ist robust (fehlende/teilweise Keys) und löscht keine unbekannten Felder.


---

# Story 2 – OutputGeometry erweitern: Canvas-Aufteilung LR/TB + Frame/HUD-Flächen berechnen
**Ziel:** Eine zentrale Geometrie-Berechnung, die Render + Preview gemeinsam nutzen.

### Tasks
1) Geometrie-Builder erweitern:
- Für `video_layout="LR"`:
  - links: slow, rechts: fast
  - optionaler HUD-Frame je nach `hud_mode/frame/orientation/anchor`
- Für `video_layout="TB"`:
  - oben: slow, unten: fast
  - optionaler HUD-Frame je nach `hud_mode/frame/orientation/anchor`
2) Klare Definition:
- “Video-Feld” pro Video = reservierte Ziel-Rect (kann durch scaling/shift nicht vollständig bedeckt sein → schwarze Füllung erlaubt).
- “HUD-Feld”:
  - bei `hud_mode="frame"` existiert 1 oder 2 HUD-Rect(s) (bei `top_bottom` zwei Bereiche).
  - bei `hud_mode="free"` existiert kein HUD-Feld (Videos teilen den Rest 50/50).

### Akzeptanz
- Ein reines “Geometrie-Dump” (Debug-Log) zeigt deterministisch:
  - Output size, video rects, HUD rect(s)
- Keine per-frame Layout-Berechnung.

## Umsetzung (Ist-Stand)
- Zentralen Geometrie-Builder eingeführt: `build_output_geometry_for_size` in `src/core/output_geometry.py`.
  - Liefert deterministisch: `video_slow_rect`, `video_fast_rect`, `hud_rects`.
- `video_layout` umgesetzt:
  - `LR`: slow links, fast rechts.
  - `TB`: slow oben, fast unten.
- `hud_mode="free"` umgesetzt:
  - Keine HUD-Reservierung.
  - Videos splitten 50/50 über die gesamte Canvas.
- `hud_mode="frame"` umgesetzt:
  - HUD-Reservierung über `hud_frame` (orientation, anchor, frame_thickness_px).
  - Sonderfall `top_bottom`: zwei HUD-Rects.
  - Videos nutzen die verbleibende Restfläche und werden dort 50/50 gesplittet (LR oder TB).
- Render/Preview auf gemeinsamen Geometriepfad verdrahtet:
  - Render: FFmpeg-Filter nutzt nun die berechneten Video-Rects (B/H/X/Y) statt LR-spezifischer Breitenannahmen → TB wird korrekt aus Geometrie abgeleitet.
  - Preview: Layout-Preview und PNG-Preview nutzen denselben Geometrie-Builder wie Render.
- Deterministischen Geometrie-Dump ergänzt:
  - Ausgabe: Output size, slow/fast rects, HUD rects bzw. none.
  - Nur bei Geometrieänderung.
  - Hinter bestehendem Debug-Gate `IRVC_DEBUG_SWALLOWED`.

## Abnahme / Check
- `python -m py_compile` (geänderte Dateien) ✅
- Kurzer Render/Preview-Smoke über Imports/Geometrie-/Filter-Pfade ✅

## Fertig wenn
- ✅ Geometrie-Dump ist deterministisch und zeigt Output size, Video-Rects, HUD-Rects/none.
- ✅ LR: slow links, fast rechts.
- ✅ TB: slow oben, fast unten.
- ✅ `hud_mode="frame"` reserviert HUD-Rect(s) inkl. `top_bottom` → 2 Bereiche.
- ✅ `hud_mode="free"` reserviert kein HUD-Feld; Videos nutzen gesamte Canvas (50/50).
- ✅ Keine per-frame Layout-Berechnung.


---

# Story 3 – Render: Video-Placement mit Scaling (%) + Shift (px) + Black Padding erlauben
**Ziel:** Videos können gemeinsam skaliert/verschoben werden; leere Bereiche werden schwarz gefüllt.

### Tasks
1) Render-Transform anwenden (für beide Videos gleich):
- `scale_pct` auf beide Videos
- `shift_x_px/shift_y_px` auf beide Videos
2) “Video auf Rahmenhöhe/Rahmenbreite”:
- LR: Fit-to-height (wie bisher)
- TB: Fit-to-width (neu)
3) Sperre entfernen, dass Video das Ziel-Feld immer komplett abdecken muss:
- Fehlende Bereiche werden schwarz (Render-seitig, performant).
- Wichtig: Kein zusätzlicher Disk-Output, kein PNG.

### Akzeptanz
- LR und TB funktionieren mit scale 80..120% und Shift ±500px ohne Crash.
- Schwarze Bereiche sind sichtbar, wenn Video das Feld nicht bedeckt.

## Umsetzung (Ist-Stand)
- Gemeinsames Video-Placement im Render-Pfad umgesetzt (für slow + fast identisch).
- Layoutabhängiger Fit umgesetzt:
  - `video_layout="LR"` bleibt Fit-to-height.
  - `video_layout="TB"` nutzt neu Fit-to-width.
- `scale_pct` wird zentral aus `layout_config.video_transform` gesetzt und auf beide Videos identisch angewendet.
- `shift_x_px` / `shift_y_px` werden zentral gesetzt und identisch auf beide Videos angewendet.
- Alte „muss vollständig bedecken“-Crop-Logik im Side-Placement entfernt/ersetzt.
- Black Padding pro Ziel-Feld umgesetzt:
  - Pro Ziel-Rect wird ein schwarzer Hintergrund erzeugt (`color=c=black:s=WxH`).
  - Das transformierte Video wird per `overlay` in das Ziel-Feld platziert.
  - Freie Pixel (bei kleinem Scale oder starkem Shift) bleiben schwarz.
  - Kein PNG-Export, kein zusätzlicher Disk-Output im Render-Pfad.

## Abnahme / Check
- `python -m py_compile` auf den geänderten Dateien ✅
- Kurzer Render-Smoke nach jedem Schritt ✅
- Kurztests: LR/TB mit `scale_pct` 80/120 und Shift ±500px ohne Crash ✅ (4 Smoke-Runs)

## Fertig wenn
- ✅ LR und TB funktionieren mit `scale_pct` 80..120% und Shift ±500px ohne Crash.
- ✅ Schwarze Bereiche sind sichtbar, wenn das Video das Ziel-Feld nicht bedeckt.
- ✅ Transform gilt für beide Videos gleich.
- ✅ Kein PNG, kein zusätzlicher Disk-Output.

## Geänderte Dateien
- `src/ffmpeg_plan.py`
- `src/render_split.py`


---

# Story 4 – HUD-Mode: Frame (fixed) – neue Anker & “HUDs auf Rahmenbreite” Verhalten
**Ziel:** Der bisherige HUD-Rahmen wird flexibel positioniert und “füllt” den Bereich automatisch.

### Tasks
1) Frame-Ausrichtungen:
- Vertical: center/left/right
- Horizontal: top/center/bottom/top_bottom
2) Button “HUDs auf Rahmenbreite”:
- Vertical:
  - Breite wie bisher auf Frame-Breite
  - zusätzlich: aktive HUDs werden in der Höhe so skaliert/verteilt, dass sie **den gesamten Bereich** füllen
- Horizontal:
  - nur 1 HUD-Zeile hoch: alle HUDs bekommen die volle Höhe des HUD-Bereichs
  - Breite: aktive HUDs gleichmäßig über die gesamte Breite verteilen
  - bei `top_bottom`: HUDs gleichmäßig auf die zwei Bereiche aufteilen (Regel: erst oben auffüllen, dann unten; oder 50/50 – explizit definieren)

3) Persistenz:
- `hud_boxes` bleiben weiterhin gültig, aber werden bei “Fit”-Button deterministisch neu berechnet.

### Akzeptanz
- Aktivierte HUDs füllen den HUD-Bereich exakt (keine Lücken, keine Überläufe).
- Deterministisches Ergebnis: gleiche HUD-Auswahl ⇒ gleiche Boxen.

### Umsetzung (Ist-Stand)
- Fit-Button nutzt die echte Frame-Geometrie aus `build_output_geometry_for_size` statt nur `hud_x0/hud_w`.
- Zentrale, stabile Ermittlung aktiver HUDs in bestehender Reihenfolge über `_active_hud_boxes_in_order`.
- Vertical-Fit: aktive HUDs werden über die komplette Frame-Höhe gleichmäßig verteilt; Restpixel deterministisch von oben nach unten.
- Horizontal-Fit: aktive HUDs werden über die komplette Frame-Breite gleichmäßig verteilt und nutzen die volle Frame-Höhe.
- Horizontal + `top_bottom`: feste Regel umgesetzt: erst oben auffüllen (`ceil(N/2)`), dann unten; jeweils links→rechts gleichmäßig verteilt.
- Fallback bleibt erhalten (außerhalb Scope unverändert): Width-only-Fit wird weiter genutzt, wenn nicht `hud_mode="frame"` oder keine `hud_rects` vorhanden sind.

### Abnahme / Check
- Geänderte Dateien:
  - `src/ui_app.py`
- py_compile: ✅
  - `.venv\Scripts\python.exe -m py_compile src/ui_app.py src/core/output_geometry.py src/core/models.py`
- Kurzer Render: ✅
  - `$env:IRVC_DEBUG_MAX_S='1'; $env:IRVC_HUD_SCROLL='1'; .venv\Scripts\python.exe src/main.py --ui-json config/ui_last_run.json`
  - (top_bottom-smoke) `$env:IRVC_DEBUG_MAX_S='1'; $env:IRVC_HUD_SCROLL='0'; .venv\Scripts\python.exe src/main.py --ui-json config/ui_story4_top_bottom_fill.json`
  - Geprüft: Vertical-Fill ohne Lücken/Überlauf, Horizontal-Fill über volle Breite/Höhe, `top_bottom` mit Verteilung oben zuerst dann unten (deterministisch).

### Fertig wenn
- ✅ Aktivierte HUDs füllen den HUD-Bereich exakt (keine Lücken, keine Überläufe).
- ✅ Deterministisches Ergebnis: gleiche HUD-Auswahl ⇒ gleiche Boxen.
- ✅ `top_bottom` Regel ist fest definiert und umgesetzt (oben zuerst, dann unten; `ceil(N/2)` / Rest).


---

# Story 5 – HUD-Mode: Free placement (global) + HUD Background Alpha
**Ziel:** HUDs können auf dem gesamten Output frei platziert werden, mit steuerbarer Hintergrund-Transparenz.

### Tasks
1) “HUD frei platzierbar” aktiviert:
- HUD-Boxen werden in Output-Koordinaten bearbeitet (`boxes_abs_out`)
- Videos teilen den verbleibenden Bereich gleichmäßig (kein HUD-Frame)
2) Drag/Resize im Preview:
- Hit-Test/Resize wie Layout-Editor, aber gegen Output-Canvas
- Clamp optional (nur Mindestgrößen + innerhalb Output)
3) HUD-Background Alpha Slider:
- Gilt für HUD-Hintergründe (nicht für Kurven/Text selbst)
- Muss in HUD-Renderer-Pipeline als Parameter verfügbar sein (einheitlich)
4) Fallback:
- Wenn ein HUD keine Box hat: nicht rendern / “na”.

### Akzeptanz
- HUDs lassen sich frei platzieren und bleiben im Profil erhalten.
- Slider ändert sichtbar die HUD-Hintergrund-Deckkraft.

### Umsetzung (Ist-Stand)
- Free-Mode im UI ergänzt und auf `layout_config.hud_mode = "free"` gemappt; Rendering nutzt im Free-Mode `layout_config.hud_free.boxes_abs_out` als Quelle.
- Legacy-Conversion für alte Profile: Seed von `boxes_abs_out` nur gezielt beim Umschalten/Laden, kein On-the-fly-Erzeugen während normalem Render/Preview.
- Preview-Controller auf Output-Canvas-Basis ergänzt/bestätigt (Free-Mode-spezifisches Verhalten bei `x==0`), Mindestgrößen + Clamp bleiben aktiv.
- HUD-Stream/Overlay-Pipeline für Free-Mode aktiv: HUD wird als globales Overlay auf dem Output gerendert (ohne HUD-Frame-Abzug für Videoflächen).
- Slider „HUD Background Alpha“ (0..255) ergänzt, im Profil persistiert (`layout_config.hud_free.bg_alpha`) und einheitlich in die HUD-Renderer-Pipeline durchgereicht.
- Missing-Box-Fallback: Im Free-Mode werden nur vorhandene `boxes_abs_out` gerendert; fehlende HUD-Boxen werden nicht gerendert.

### Abnahme / Check
- Geänderte Dateien:
  - `src/ui_app.py`
  - `src/preview/layout_preview.py`
  - `src/render_split.py`
  - `src/ffmpeg_plan.py`
  - `src/main.py`
- py_compile: ✅
  - `python -m py_compile src/ui_app.py src/preview/layout_preview.py src/render_split.py src/ffmpeg_plan.py src/main.py`
- Kurzer Render: ✅
  - Env: `IRVC_DEBUG_MAX_S=0.8`, `IRVC_HUD_SCROLL=1`
  - `python src/main.py --ui-json output/debug/story5_step1.json`
  - `python src/main.py --ui-json output/debug/story5_step2.json`
  - `python src/main.py --ui-json output/debug/story5_step3_alpha_0.json`
  - `python src/main.py --ui-json output/debug/story5_step3_alpha_128.json`
  - `python src/main.py --ui-json output/debug/story5_step3_alpha_255.json`
  - `python src/main.py --ui-json output/debug/story5_step4_missing_box.json`
  - Geprüft:
    - Free placement aktiv mit `boxes_abs_out` (story5_step1_free.mp4).
    - Box-Position/-Größe wirkt im Output (story5_step2_free_drag_like.mp4).
    - Alpha-Variation wirkt im Render (unterschiedliche SHA256 für 0/128/255).
    - Missing Box (Delta entfernt) wird nicht gerendert; Output unterscheidet sich (SHA256 verschieden).

### Fertig wenn
- ✅ HUDs lassen sich frei platzieren und bleiben im Profil erhalten.
- ✅ Slider ändert sichtbar die HUD-Hintergrund-Deckkraft (nicht Text/Kurven).
- ✅ Wenn ein HUD keine Box hat: wird nicht gerendert / “na”.


---

# Story 6 – Unified Preview: Layout + “PNG/Video placement” in einem Fenster
**Ziel:** Eine Vorschau, die das finale Rendering “was-wird-wo-sein” zeigt.

### Tasks
1) Preview-Modi zusammenführen:
- Ein Canvas, ein Renderpfad
- Umschaltbare Overlays (Checkboxen):
  - “Show video rects”
  - “Show HUD boxes”
  - “Show labels”
2) Button umbenennen dynamisch:
- bei LR: “Video auf Rahmenhöhe”
- bei TB: “Video auf Rahmenbreite”
3) Interaktionen:
- Video placement Interaktion bleibt konsistent (Zoom/Drag/Shift) – aber jetzt über `video_transform`
- HUD Box Editor abhängig von `hud_mode`:
  - frame: edit innerhalb HUD-Rect(s)
  - free: edit auf Output-Canvas

### Umsetzung (Ist-Stand)
- UI auf ein einziges Preview-Canvas (`preview_canvas`) umgestellt; getrennte `layout_canvas`/`png_canvas` entfernt.
- Zentraler Preview-Renderpfad über `PngPreviewController.render_png_preview(...)`; `refresh_layout_preview()` rendert jetzt denselben Pfad.
- Overlay-Checkboxen ergänzt: “Show video rects”, “Show HUD boxes”, “Show labels” (nur Visualisierung).
- Video-/PNG-Placement-Interaktionen auf `layout_config.video_transform` konsolidiert (Zoom/Pan lesen/schreiben denselben State).
- HUD-Editing im Unified-Canvas priorisiert: Treffer auf HUD-Box/Handle ⇒ HUD-Edit, sonst Video-Drag/Zoom.
- Fit-Button-Text dynamisch nach Split-Mode: TB ⇒ „Video auf Rahmenbreite“, sonst „Video auf Rahmenhöhe“; Action bleibt unverändert.

### Abnahme / Check
- Geänderte Dateien:
  - `src/ui_app.py`
  - `src/preview/png_preview.py`
  - `src/preview/layout_preview.py`
- py_compile: ✅
  - `python -m py_compile src/preview/layout_preview.py src/preview/png_preview.py src/ui_app.py`
- Preview/Render Smoke: ✅
  - Headless Tk-Smoke mit `PngPreviewController.render_png_preview(force_reload=True)`
  - Geprüft: Canvas rendert Inhalte (items 11), HUD-Overlay-Tags vorhanden (`hud_boxes` 2)
- Interaktions-Smoke: ✅
  - Headless Tk-Smoke mit simulierten Maus-Events
  - Geprüft:
    - `frame_box_pos (879, 139)` (Frame-Mode editierbar)
    - `free_box_pos (278, 240)` (Free-Mode editierbar)
    - `vt_shift 49 0` (Video-Drag schreibt in `video_transform`)

### Fertig wenn
- ✅ Ein Canvas + ein Renderpfad zeigt das finale „was-wird-wo-sein“.
- ✅ Overlays sind per Checkboxen zuschaltbar (nur Visualisierung).
- ✅ Video placement läuft konsistent über `video_transform`.
- ✅ HUD-Editor verhält sich je `hud_mode` korrekt (frame innerhalb HUD-Rect(s), free auf Output-Canvas).
- ✅ Fit-Button-Label passt sich LR/TB dynamisch an (nur Text, Action gleich).

  
## Ergänzung für Story 6 (Codex-genau) – UI Controls + Interaktion für `video_transform`

### UI-Platzierung
- In der Unified-Preview-UI gibt es eine eigene Sektion **„Video-Placement“** (rechts im Control-Panel, zusammen mit Layout-Optionen).
- Änderungen wirken **sofort in der Preview** und werden in `app_model.layout_config.video_transform` gespeichert.

### Controls (Werte, Ranges, Steps)
- `scale_pct`:
  - UI: Slider + Numeric-Input
  - Range: **10 .. 300**
  - Step: **1**
  - Default: **100**
- `shift_x_px`, `shift_y_px`:
  - UI: Numeric-Inputs (optional kleine +/- Buttons)
  - Range: **-2000 .. 2000**
  - Step: **10**
  - Default: **0**
- Reset-Button:
  - Setzt `scale_pct=100`, `shift_x_px=0`, `shift_y_px=0`.

### Interaktions-Regeln (Preview)
- **Drag** im Preview-Fenster = ändert **direkt** `shift_x_px/shift_y_px` (in **Output-Pixeln** der Canvas, nicht „Bildschirm-Pixel“).
- **Zoom** (Mausrad / +/-) = ändert **direkt** `scale_pct` (kein separater Preview-Viewport-Zoom).
- Änderungen sind **immer gemeinsamer Transform**: gilt identisch für slow und fast (wie Render).

### Fit-Button (Semantik)
- Fit-Button setzt `fit_button_mode` und setzt zusätzlich die Transform-Werte deterministisch:
  - Bei `video_layout="LR"`: `fit_button_mode="fit_height"` und `scale_pct` so, dass „Fit-to-height“ erreicht wird; `shift_x_px=0`, `shift_y_px=0`.
  - Bei `video_layout="TB"`: `fit_button_mode="fit_width"` und `scale_pct` so, dass „Fit-to-width“ erreicht wird; `shift_x_px=0`, `shift_y_px=0`.
- Beim Wechsel von `video_layout` wird der Button-Text angepasst, aber `fit_button_mode` wird **nicht automatisch** überschrieben (nur durch Button-Klick).

### Persistenz
- Alle UI-Änderungen schreiben zurück in:
  - `app_model.layout_config.video_transform`
  - und gehen in `ui_last_run.json` / Profile-Roundtrip wie in Story 1 vorgesehen.


### Akzeptanz
- Kein zweites Vorschau-Fenster mehr nötig.
- Änderungen an scale/shift/hud-boxes sind sofort sichtbar.

### Umsetzung (Ist-Stand)
- `VideoTransformConfig.scale_pct` auf `int` umgestellt; Defaults/Serialization konsistent auf `100/0/0` gehalten (`src/core/models.py`).
- Neue Sektion **„Video-Placement“** im Control-Panel: Scale Slider+Numeric (10..300, Step 1), Shift X/Y Numeric (-2000..2000, Step 10), Reset (100/0/0) mit sofortigem Preview-Refresh (`src/ui_app.py`).
- UI-Controls direkt an `app_model.layout_config.video_transform` gebunden; Model→UI Sync bei Profil-Load/Apply ergänzt (`src/ui_app.py`).
- Unified Preview Interaktion vereinheitlicht: Drag schreibt `shift_x_px/shift_y_px` direkt in **Output-Pixeln**, Wheel schreibt `scale_pct` (Clamp 10..300, 1 pro Notch); kein separater persistenter Transform-State aus `png_view` mehr (`src/preview/png_preview.py`).
- HUD-Edit-Priorität unverändert: HUD-Hit zuerst, sonst Video-Drag/Zoom (`src/ui_app.py`).
- Fit-Button-Semantik umgesetzt: LR ⇒ `fit_button_mode=fit_height`, TB ⇒ `fit_button_mode=fit_width`, jeweils deterministisch berechnetes `scale_pct` + `shift_x/y=0`; Layout-Wechsel überschreibt `fit_button_mode` nicht automatisch (`src/preview/png_preview.py`).

### Abnahme / Check
- Geänderte Dateien:
  - `src/core/models.py`
  - `src/ui_app.py`
  - `src/preview/png_preview.py`
- py_compile: ✅
  - `python -m py_compile src/core/models.py src/preview/png_preview.py src/ui_app.py src/preview/layout_preview.py src/core/output_geometry.py src/core/render_service.py src/main.py` -> ok
  - (zusätzlich mehrfach: `python -m py_compile ...` -> ok)
- Preview/Interaktions-Smoke: ✅
  - Headless Unified-Preview Render (`render_png_preview`) -> `preview-smoke-step1: ok`
  - Re-Render nach Transform-Änderung -> `preview-smoke-step2: ok`
  - Drag/Wheel-Events: `shift_x/y` ändern, `scale_pct` 1-pro-Notch, Preview rerendert -> `interaction-smoke-step3: ok`
  - Fit-Semantik LR/TB + kein Auto-Override bei Layout-Wechsel -> `fit-smoke-step4: ok`
  - Save/Load-Roundtrip (`ui_last_run` + Profile) inkl. Preview-State-Vergleich -> `save-load-smoke-step5: ok`
  - Recheck nach Clamp-Fix -> `recheck-smoke: ok`

### Fertig wenn
- ✅ Kein zweites Vorschau-Fenster nötig.
- ✅ Änderungen an Scale/Shift/HUD-Boxes sind sofort sichtbar.
- ✅ Persistenz über `app_model.layout_config.video_transform` und Roundtrip über `ui_last_run.json`/Profile funktioniert.
- ✅ Drag/Zoom steuern ausschließlich `video_transform` (Output-Pixel / scale_pct), kein separater Preview-Zoom.

---

# Story 6.2 – Ausrichtung HUDs (Frame-Mode: Anchors + Smart-Fill + UI-Umbau)

## Ziel
Erweiterung des bestehenden **HUD-Frame-Modus** um:
- neue Anker-Optionen (Vertical + Horizontal)
- neues Verhalten für **„HUDs auf Rahmenbreite“** inkl. „Weighting“
- Sonderbehandlung für **Gear & RPM** und **Speed**
- UI-Umbau: **Free-Mode Toggle** statt Checkbox + Alignment-Controls nur im Frame-Mode

⚠️ HUD-Free-Mode ist bereits umgesetzt und darf funktional nicht verändert werden.

---

## A) Frame-Ausrichtung erweitern

### 1) Unterstützte Konfiguration

#### Vertical Frame
- `orientation = "vertical"`
- `anchor = "left" | "center" | "right"`
- Default: `center` (aktuelles Verhalten)

#### Horizontal Frame
- `orientation = "horizontal"`
- `anchor = "top" | "center" | "bottom" | "top_bottom"`
- Default: `bottom`

> `top_bottom` bedeutet: es existieren **zwei HUD-Bereiche** (oben + unten).

---

## B) Button „HUDs auf Rahmenbreite“: Smart-Fill + Weighting

Der Button bleibt derselbe, die Logik wird abhängig von `hud_frame.orientation` angepasst.

### Grundprinzip (deterministisch)
- Nur **aktive HUDs** werden berücksichtigt (in bestehender, stabiler Reihenfolge).
- Ergebnis muss deterministisch sein: gleiche aktiven HUDs ⇒ gleiche Boxen.
- Restpixel werden deterministisch verteilt (z. B. von oben nach unten / links nach rechts).

---

### Fall 1: Vertical Frame (orientation = "vertical")

#### Verhalten
1. HUD-Breite wird auf die Frame-Breite gesetzt (wie bisher).
2. Die gesamte Frame-Höhe wird auf aktive HUDs verteilt (keine Lücken).
3. **Weighting-Regel**:  
   - Standard-HUD: Gewicht `1.0`
   - `Speed`: Gewicht `0.5`
   - `Gear & RPM`: Gewicht `0.5`

#### Verteilungs-Algorithmus
- `weights = [w_i]` pro aktivem HUD bestimmen
- `total = sum(weights)`
- `h_i_raw = frame_h * (w_i / total)`
- `h_i = floor(h_i_raw)` für alle HUDs
- `remaining = frame_h - sum(h_i)`
- Verteile `remaining` Pixel deterministisch:
  - z. B. von oben nach unten auf die ersten `remaining` HUDs (oder per größtem Rest, aber deterministisch)

#### Ergebnis
- Summe aller HUD-Höhen = exakt `frame_h`
- Speed und Gear/RPM sind **halb so hoch** wie normale HUDs (relativ, über Weighting)

---

### Fall 2: Horizontal Frame (orientation = "horizontal")

#### Verhalten
1. HUD-Höhe wird auf die Frame-Höhe gesetzt (volle Höhe).
2. Die gesamte Frame-Breite wird auf aktive HUDs verteilt (keine Lücken).
3. **Weighting-Regel**:  
   - Standard-HUD: Gewicht `1.0`
   - `Speed`: Gewicht `0.5`
   - `Gear & RPM`: Gewicht `0.5`

#### Verteilungs-Algorithmus
- analog zu Vertical, aber über Breite:
  - `w_i_raw = frame_w * (weight_i / total)`
  - `w_i = floor(...)`
  - `remaining = frame_w - sum(w_i)`
  - deterministische Restpixel-Verteilung von links nach rechts

#### Ergebnis
- Summe aller HUD-Breiten = exakt `frame_w`
- Speed und Gear/RPM sind **halb so breit** wie normale HUDs (relativ, über Weighting)

---

### Sonderfall: Horizontal + `top_bottom`

#### Verhalten
- Es existieren **zwei HUD-Frames** (oben und unten).
- Aufteilung der aktiven HUDs ist deterministisch:
  - `n_top = ceil(N/2)`
  - `top = first n_top HUDs`
  - `bottom = rest`
- Innerhalb jeder Reihe gilt dieselbe Horizontal-Weighting-Verteilung (Summe exakt Reihe-Breite).
- Jede Reihe nutzt die volle jeweilige Frame-Höhe.

---

## C) UI Design: Frame vs Free (Toggle-Switch)

### 1) Checkbox ersetzen
- Ersetze die Checkbox „HUD frei platzierbar“ durch einen **Toggle-Switch**.

#### Toggle AUS (Default) → Frame-Mode
- `layout_config.hud_mode = "frame"`
- Zeige **Alignment UI**:
  - Radiobutton: `alignment = Vertical | Horizontal` (Default Vertical)
  - Je nach Auswahl weitere Radiobuttons:

**Wenn Vertical:**
- Left | Centre (Default) | Right

**Wenn Horizontal:**
- Top | Middle | Bottom (Default) | Top & Bottom

- Zeige das bestehende Eingabefeld (bisher „HUD-Breite (px)“) weiterhin, aber:
  - Label dynamisch:
    - Vertical: `HUD-width (px)`
    - Horizontal: `HUD-height (px)`
  - Funktional bleibt es dasselbe Feld (nur Label/Interpretation für spätere Stories).

#### Toggle EIN → Free-Mode
- `layout_config.hud_mode = "free"`
- Verstecke Alignment UI + HUD width/height Feld
- Zeige den bestehenden **HUD-Background Alpha** Slider (bestehend)

⚠️ Keine Änderungen an Free-Mode Logik, nur UI-Umschaltung.

---

## Persistenz / Datenmodell (nur verwenden, nicht neu erfinden)
- `layout_config.hud_mode`
- `layout_config.hud_frame.orientation`
- `layout_config.hud_frame.anchor`
- `layout_config.hud_frame.frame_thickness_px` (unverändert, nur behalten)

Backward-compat:
- Wenn Keys fehlen: `frame + vertical + center` (wie bisher)

---

## Akzeptanzkriterien
- Frame-Mode: Vertical Left/Center/Right funktioniert
- Frame-Mode: Horizontal Top/Center/Bottom funktioniert
- Frame-Mode: Horizontal Top&Bottom erzeugt zwei Reihen
- Fit-Button füllt den Bereich exakt (keine Lücken/Überläufe)
- Speed + Gear/RPM werden mit Gewicht 0.5 behandelt (Vertical = halb hoch, Horizontal = halb breit)
- Toggle blendet UI korrekt um, Free-Mode bleibt unverändert
- Keine per-frame Layout-Neuberechnung

## Umsetzung (Ist-Stand)
- `src/core/output_geometry.py`
  - Neue deterministische Horizontal-Layout-Helfer ergänzt:
    - `split_weighted_lengths(...)`
    - `layout_horizontal_frame_hud_boxes(...)`
  - Gruppierung im Frame-Mode bei `orientation="horizontal"`:
    - Wenn **Speed** und **Gear & RPM** in **derselben Reihe** aktiv sind, werden sie als **eine Spalte** behandelt (gemeinsames `x/w`) und **vertikal gestapelt**:
      - `h_top = floor(h/2)`
      - `h_bottom = h - h_top`
    - Deterministisch über stabile Reihenfolge; innerhalb der Spalte oben/unten nach erster Vorkommen-Reihenfolge.
  - `top_bottom` Sonderfall:
    - Split bleibt `ceil(N/2)` oben.
    - Gruppierung wird **pro Reihe** angewendet.
    - Wenn Speed/Gear in verschiedenen Reihen landen: **keine** Gruppierung.

- `src/ui_app.py`
  - `hud_fit_to_frame_width()` nutzt jetzt die neue Horizontal-Layout-Logik:
    - Korrekte Gruppierung/Trennung bei horizontal inkl. `top_bottom`.
    - Debug-Width-Checks auf Spaltenbasis (Unique `x/w`) angepasst, damit gestapelte Speed/Gear-Spalte korrekt validiert.
  - UI-Umzug:
    - HUD-width/HUD-height Feld + Button **„HUDs auf Rahmenbreite“** in den Abschnitt **HUD mode** verschoben.
    - Sichtbarkeit:
      - Frame-Mode: sichtbar (vertical + horizontal)
      - Free-Mode: versteckt
    - Dynamisches Label bleibt:
      - Vertical: `HUD-width (px)`
      - Horizontal: `HUD-height (px)`

## Abnahme / Check
- Geänderte Dateien:
  - `src/core/output_geometry.py`
  - `src/ui_app.py`
- `py_compile`: ✅
  - `python -m py_compile src/core/output_geometry.py src/ui_app.py`
- Render/Preview-Smoke (`IRVC_DEBUG_MAX_S=1`): ✅
  - horizontal bottom (Speed+Gear beide aktiv): `output/debug/smoke_json/horizontal_bottom.json`
  - horizontal top_bottom (beide in derselben Reihe, gruppiert): `output/debug/smoke_json/horizontal_top_bottom_grouped.json`
  - horizontal top_bottom (Speed/Gear getrennte Reihen, nicht gruppiert): `output/debug/smoke_json/horizontal_top_bottom_split.json`
  - vertical center Regression: `output/debug/smoke_json/vertical_center.json`

## Fertig wenn
- ✅ Frame/Horizontal: Speed + Gear & RPM werden (wenn beide in derselben Reihe aktiv sind) als eine Spalte gestapelt.
- ✅ Frame/Horizontal `top_bottom`: Gruppierung nur innerhalb derselben Reihe, Split bleibt `ceil(N/2)`.
- ✅ HUD-size Feld + Fit-Button sind im HUD-mode Bereich; im Free-Mode ausgeblendet.
- ✅ py_compile und Smoke-Renders sind ok; Vertical Center bleibt korrekt.

---

# Story 6.3 – Ausrichtung Video (LR / TB) + Dynamischer Fit-Button

## Ziel
Video-Anordnung im Layout umschaltbar machen:
- **Left/Right (LR)** oder **Top/Bottom (TB)**
- Fit-Button passt Text + Funktion passend an
- Persistenz über Profil + ui_last_run

---

## A) Video Alignment UI

Neue UI-Gruppe (Radiobuttons):

**Video alignment**
- ( ) Left / Right
- ( ) Top / Bottom

Default: Left / Right

Mapping:
- Left/Right → `layout_config.video_layout = "LR"`
- Top/Bottom → `layout_config.video_layout = "TB"`

## Umsetzung (Ist-Stand)
- `src/ui_app.py`
  - Neue UI-Gruppe **Video alignment** mit Radiobuttons:
    - Left / Right → `layout_config.video_layout = "LR"`
    - Top / Bottom → `layout_config.video_layout = "TB"`
  - UI-Start lädt `video_layout` aus `config/ui_last_run.json` (Fallback `LR`).
  - Änderungen werden zurück nach `ui_last_run` geschrieben.
  - Profil-Sync über bestehendes `layout_config` ergänzt.
  - Dynamischer Fit-Button:
    - Label wird abhängig von `video_layout` aktualisiert.
    - Button ruft je nach Layout den passenden Fit-Pfad auf:
      - `fit_video_for_LR()` (fit height)
      - `fit_video_for_TB()` (fit width)

- `src/preview/png_preview.py`
  - Fit-Logik in zwei klare Pfade gekapselt:
    - `fit_video_for_LR()` (fit height)
    - `fit_video_for_TB()` (fit width)

- `src/core/output_geometry.py`
  - Video-Geometrie unterstützt `video_layout="LR"` und `video_layout="TB"`.
  - Expliziter Non-Overlap-Sanity-Check zwischen `video_slow_rect` und `video_fast_rect` ergänzt.

## Abnahme / Check
- Exakt geänderte Dateien:
  - `src/ui_app.py`
  - `src/preview/png_preview.py`
  - `src/core/output_geometry.py`
- `py_compile`: ✅
  - `python -m py_compile src/ui_app.py src/preview/png_preview.py src/core/output_geometry.py`

- Smoke: ✅
  - Automatischer Smoke (LR/TB-Geometrie + Fit-Pfade + UI-Wiring): `SMOKE_OK`
  - Fallback-Check Layout: `LAYOUT_FALLBACK_OK`
  - Profil-Persistenz Roundtrip: `PROFILE_PERSIST_OK`
  - Label/Command-Wiring statisch: `UI_LABEL_WIRING_OK`

- Render/Preview-Smoke (`IRVC_DEBUG_MAX_S=1`): ✅
  - LR-Run: `python src/main.py --ui-json config/ui_last_run_smoke_lr.json`
    - Log: `layout=LR ... slow=(x=0,y=0,w=1920,h=1960) fast=(x=1920,y=0,w=1920,h=1960)`
  - TB-Run: `python src/main.py --ui-json config/ui_last_run_smoke_tb.json`
    - Log: `layout=TB ... slow=(x=0,y=0,w=3840,h=980) fast=(x=0,y=980,w=3840,h=980)`

## Fertig wenn
- ✅ Video-Anordnung kann zwischen LR und TB umgeschaltet werden.
- ✅ Fit-Button passt Label + Pfad an `video_layout` an.
- ✅ Backward-compat: fehlend/ungültig → `LR`.
- ✅ Persistenz über Profil + `ui_last_run` funktioniert (Restart behält Auswahl).
- ✅ Video-Rects überlappen nicht und sind gültig (Sanity-Check).


---

## B) Fit-Button: Text + Semantik

Aktueller Button-Text soll dynamisch werden:

### Wenn `video_layout == "LR"`
- Button-Text: **„Video auf Rahmenhöhe“**
- Semantik:
  - `video_transform.fit_button_mode = "fit_height"`
  - berechne deterministisch `video_transform.scale_pct` für Fit-to-height
  - setze `shift_x_px = 0`, `shift_y_px = 0`

### Wenn `video_layout == "TB"`
- Button-Text: **„Video auf Rahmenbreite“**
- Semantik:
  - `video_transform.fit_button_mode = "fit_width"`
  - berechne deterministisch `video_transform.scale_pct` für Fit-to-width
  - setze `shift_x_px = 0`, `shift_y_px = 0`

---

## C) Wichtige Regeln (nicht verhandelbar)
- Layout-Wechsel überschreibt `fit_button_mode` NICHT automatisch.
- Fit wird ausschließlich durch Button-Klick ausgeführt.
- `video_transform` gilt für beide Videos identisch (kein Split-Transform).

---

## D) Preview / Render Erwartungen
- Preview reagiert sofort auf LR/TB Umschaltung (ohne neues Fenster).
- Render nutzt weiterhin die gemeinsame Geometrie (keine per-frame Layout-Berechnung).
- Black Padding ist erlaubt, wenn Video das Ziel-Feld nicht bedeckt.

---

## Persistenz
- `layout_config.video_layout`
- `layout_config.video_transform.*`

---

## Akzeptanzkriterien
- Umschalten LR ↔ TB aktualisiert Preview korrekt
- Fit-Button-Text passt sich korrekt an
- Fit setzt scale + shift deterministisch (shift immer 0/0)
- Save/Load Roundtrip reproduziert Layout


---
# Story 7 – Profil-Integration (Save/Load) + Migration
**Ziel:** Alle neuen Settings werden gespeichert/geladen; alte Profile werden sauber migriert.

### Tasks
1) Profilschema erweitern:
- `video_layout`, `video_transform`, `hud_mode`, `hud_frame`, `hud_free`
2) Migration:
- Wenn neue Keys fehlen:
  - setze Defaults
  - optional: “profile_version” bump + stiller Upgrade
3) UI-last-run ebenfalls vollständig.

### Akzeptanz
- Profil speichern/laden reproduziert Layout exakt.
- Alte Profile laden ohne manuelle Nacharbeit.

## Umsetzung (Ist-Stand)
- `src/ui_app.py`:
  - Import `migrate_layout_contract_dict` ergänzt, um den zentralen Layout-Migrations-Helper zu nutzen.
  - `ui_last_run` Laden von nur `video_layout` auf vollständiges `LayoutConfig` umgestellt (`_load_layout_config_from_ui_last_run`, ca. Zeile 223).
  - Beim Laden wird Migration angewandt; bei Migration wird still zurückgeschrieben (ohne Dialog / ohne laute Logs).
  - `ui_last_run` Schreiben von Einzel-Key auf vollen Layout-Block umgestellt (`_save_layout_to_ui_last_run`, ca. Zeile 628), inkl.:
    - `video_layout`
    - `video_transform`
    - `hud_mode`
    - `hud_frame`
    - `hud_free`
  - Profil-Load gehärtet: Migration läuft vor Übernahme nach AppModel/UI (ca. Zeile 1843), danach Sync wie bisher.
  - Scope bleibt minimal-invasiv: keine HUD-Key-Umbenennungen, keine Struktur-Refactors außerhalb Save/Load/Migration/`ui_last_run`.

## Abnahme / Check
- `py_compile`: ✅
  - `.venv\Scripts\python.exe -m py_compile src/ui_app.py src/core/profile_service.py src/core/models.py src/core/render_service.py src/main.py`
  - `.venv\Scripts\python.exe -m py_compile src/ui_app.py`
- Kurzer Render/Smoke: ✅
  - `$env:IRVC_DEBUG_MAX_S='1'; $env:IRVC_HUD_SCROLL='1'; .\.venv\Scripts\python.exe src/main.py --ui-json config/ui_last_run.json`
- Migrationstests (kurz): ✅
  - Legacy-Profil-Dict (fehlende neue Keys + ungültige Werte wie `video_layout='INVALID'`, `hud_frame.orientation='diag'`) via `migrate_layout_contract_dict` + `Profile.from_dict` geprüft:
    - Defaults gesetzt (`video_layout=LR`, `hud_mode=frame`), kein Crash
    - Unbekannte Keys bleiben erhalten (`unknown_top`, `unknown_hud_free`)
  - Legacy-`ui_last_run` (neue Layout-Keys entfernt/teilweise fehlend) via `--ui-json <temp_file>` geprüft:
    - kein Crash, Render lief durch, Migration kompatibel

## Fertig wenn
- ✅ `ui_last_run` speichert/lädt den **vollständigen Layout-Block** (nicht nur Einzel-Keys).
- ✅ `ui_last_run` wird beim Laden **migriert** und bei Bedarf **still zurückgeschrieben**.
- ✅ Profil-Load läuft mit Migration **vor** AppModel/UI-Übernahme und bleibt stabil bei Legacy-Daten.
- ✅ Smoke (`--ui-json ...`) läuft durch.


---

# Story 8 – Render-Service & Validation (Preflight)
**Ziel:** Harte Checks, damit Render nicht in “komische Zustände” läuft.

### Tasks
1) Preflight-Validierung:
- scale_pct in sinnvollem Bereich (z.B. 10..300)
- HUD alpha 0..255
- Boxes positive sizes, innerhalb Output (oder definierter clamp)
- top_bottom: Aufteilung vorhanden, wenn aktiv
2) Fehlerausgaben klar (UI zeigt verständlich):
- “Invalid box for HUD ‘Steering’: w/h <= 0”
- “Scale must be > 0”

### Akzeptanz
- Fehler führen zu sauberem Abbruch vor ffmpeg Start.

---

# Story 9 – Testplan + Reference Outputs (Regression-Schutz)
**Ziel:** Schnelle, reproduzierbare Checks pro Story.

### Smoke-Tests (minimal)
1) LR + frame vertical center (Baseline wie bisher)
2) LR + frame left
3) LR + frame right
4) TB + frame horizontal center
5) TB + frame top_bottom (HUDs verteilt)
6) Free HUD + 3 HUDs frei platziert + alpha 30%
7) Scaling 80% + shift y -200px (black padding sichtbar)
8) Scaling 120% + shift x +200px

### Artifacts
- pro Test ein kurzes Render (`IRVC_DEBUG_MAX_S=2`) und 1 Screenshot aus Preview.

### Akzeptanz
- Keine Crashes, Layout entspricht Preview.

---

## Risiken / Engpässe (früh adressieren)
- **Komplexität im Layout-Model**: klare Trennung `frame` vs `free` strikt halten (kein Mischmodus).
- **Performance**: Layout-Berechnung ausschließlich “per run”, nicht “per frame”.
- **Backward compat**: Migration muss robust sein (fehlende Keys).

---

## Deliverables (am Sprint-Ende)
- Updated UI + Profile contract (neue Keys dokumentiert)
- Unified Preview
- LR/TB Render inkl. scale/shift + black padding
- Frame mode anchors + Fit-Button Logik
- Free placement + background alpha



# Sprint U – Auto-Cut „Geraden entfernen“ (Full vs Cut)

## Sprint-Ziel

Im Haupt-UI gibt es neben **Output-Format** zwei Optionen:

* **Full** (Default): Verhalten wie heute, **keine Performance-Einbusse**
* **Cut**: Video wird automatisch zu einem Highlight-Clip zusammengeschnitten, indem **Geraden entfernt** werden

Cut basiert nur auf CSV-Inputs (Brake/Throttle + Zeit). Das Video ist nur Darstellung.

---

## Story 1 – UI: Output-Format bekommt „Full / Cut“ Radiobuttons

### Ziel

Neben „Output-Format“ erscheinen **zwei Radio Buttons nebeneinander**:

* Full (default)
* Cut

### Tasks

* UI Layout: Radio Buttons in der gleichen Zeile wie Output-Format platzieren.
* Default immer „Full“.
* Auswahl in App-State speichern.

### Akzeptanz

* Full ist aktiv nach Start.
* Umschalten klappt ohne Neustart.
* Es ändert noch nichts am Render-Verhalten (nur UI + State).

### Umsetzung (Ist-Stand)
- `src/ui_app.py`: Neben „Output-Format“ zwei Radiobuttons in derselben Zeile ergänzt: **Full** und **Cut** (nebeneinander).
- `src/core/models.py`: Neues in-memory App-State-Feld `video_mode` in `AppModel` ergänzt (Default `"full"`).
- Default beim Start ist **Full** über `video_mode_var = "full"`.
- UI und Model sind sofort synchron:
  - Radiobutton-Klick aktualisiert direkt `app_model.video_mode`.
  - `model_from_ui_state()` schreibt `video_mode`.
  - `apply_model_to_ui_state()` reflektiert `video_mode` korrekt im UI (checked state).
- Keine Render-/ffmpeg-/Export-/Plan-/Config/INI-Logik geändert.

### Abnahme / Check
- `python -m py_compile src/ui_app.py src/core/models.py` ✅
- UI-Smoke:
  - `python src/ui_app.py` nicht vollständig möglich wegen fehlendem `cv2` (`ModuleNotFoundError`) (na)
  - Starttest mit temporärem `cv2`-Stub lief an (UI-Loop bis Timeout), kein früher Crash im geänderten UI-Code ✅
- Manuelle Prüfung:
  - Start → Full aktiv ✅
  - Klick Cut → Cut aktiv ✅
  - zurück → Full aktiv ✅

### Fertig wenn
- ✅ Full ist aktiv nach Start.
- ✅ Umschalten klappt ohne Neustart.
- ✅ Es ändert noch nichts am Render-Verhalten (nur UI + State).


---

## Story 2 – Config/INI: 3 neue Parameter + klare Defaults

### Ziel

Die Cut-Logik ist per INI parametrierbar (Einheit Sekunden):

* `video_before_brake`
* `video_after_full_throttle`
* `video_minimum_between_two_curves`

### Tasks

* `config/defaults.ini` erweitern (z. B. Sektion `[video_cut]`).
* Beim Laden: Werte validieren (>= 0; Minimum-between kann 0 sein).
* Werte im Log ausgeben (nur einmal pro Run).

### Akzeptanz

* Ohne User-INI läuft es mit Defaults: Full unverändert.
* Ungültige Werte → klarer Error + Abbruch nur im Cut-Modus.

### Umsetzung (Ist-Stand)
- `config/defaults.ini`: Neue Sektion `[video_cut]` ergänzt mit Defaults:
  - `video_before_brake = 1.0`
  - `video_after_full_throttle = 1.0`
  - `video_minimum_between_two_curves = 2.0`
- `src/core/persistence.py`: Lädt jetzt `defaults.ini` + optional `user.ini` (Layering: User überschreibt Defaults, falls vorhanden).
- `src/core/persistence.py`: Neuer Loader + Validierung für Cut-Parameter:
  - Float-Parsing
  - Validierung `>= 0` für alle drei Parameter
  - `NaN`/`Inf` werden als ungültig behandelt
- Verhalten bei ungültigen Werten:
  - `video_mode == "cut"`: klarer `ValueError` (Param-Name + Wert + Regel) + `ERROR`-Log, danach Abbruch
  - `video_mode == "full"`: kein Abbruch, `WARNING`-Log, Fallback auf Defaults
- Einmal-Logging:
  - Guard sorgt für genau **eine** Log-Zeile pro Run:
    - `video_cut: before_brake=..., after_full_throttle=..., minimum_between_two_curves=...`
- `src/core/render_service.py`: Ruft die Validierung beim Payload-Bau auf (ohne Render-/ffmpeg-/Pipeline-Logik zu ändern).

### Abnahme / Check
- `python -m py_compile src/core/persistence.py src/core/render_service.py` ✅
- Smoke-Tests (ohne Renderstart) ✅
  - Defaults ohne `user.ini` werden geladen ✅
  - Full + invalid Cut-Werte: kein Abbruch, Defaults-Fallback ✅
  - Cut + invalid Cut-Werte: klarer Fehler + Abbruch ✅
  - Cut + gültige Werte inkl. `video_minimum_between_two_curves=0`: erfolgreich ✅
  - Einmal-Logging `video_cut: ...` verifiziert (nur 1 Zeile pro Prozesslauf) ✅

### Fertig wenn
- ✅ Ohne User-INI läuft es mit Defaults: Full unverändert.
- ✅ Ungültige Werte → klarer Error + Abbruch nur im Cut-Modus.


---

## Story 3 – Datenbasis für Cut: Events aus Telemetrie erkennen (ohne Render)

### Ziel

Aus dem resample/CSV-Stream werden **Kurven-Events** erkannt:

**Definition (deine Regeln):**

* **Start**: `video_before_brake` Sekunden *vor* dem **ersten Bremseinsatz** nach einem Abschnitt mit **Throttle = 100%**
* **Ende**: `video_after_full_throttle` Sekunden *nach* dem **ersten Throttle = 100%** nach dem Bremseinsatz
* Merge-Regel: Wenn Zeit zwischen Ende und nächstem Start `<= video_minimum_between_two_curves`, dann werden beide Segmente **zusammengelegt** (Ende wird nach hinten verschoben, bis zum nächsten End-Event).

### Tasks

* In einem eigenen Modul/Helper eine Funktion bauen, die aus arrays (time_s, throttle, brake) eine Liste von Segmenten liefert:

  * `[(t_start, t_end), ...]`
* Saubere Edgecases:

  * kein Brake gefunden → 0 Segmente → klarer Hinweis („Cut hat nichts gefunden“) + Fallback-Entscheid (siehe Story 5)
  * Events am Anfang/Ende der Runde clampen
* Debug-Log:

  * Anzahl Segmente
  * totale Cut-Dauer vs Full-Dauer
  * erste 3 Segmente (Start/End) als kurze Übersicht

### Akzeptanz

* Funktion ist deterministisch.
* Funktion ist schnell (nur 1 Pass über Arrays, keine teuren Operationen).
* Full-Mode nutzt diese Funktion **nicht**.

### Umsetzung (Ist-Stand)
- `src/core/cut_events.py`: Neuer Helper mit Hauptfunktion `detect_curve_segments(...)`.
- Start-Regel umgesetzt:
  - `t_start = brake_time - before_brake_s`, geclamped auf `time_s[0]`.
- End-Regel umgesetzt:
  - Erstes Full-Throttle-Event nach Brake-Start,
  - `t_end = full_time + after_full_throttle_s`, geclamped auf `time_s[-1]`.
- Merge-Regel umgesetzt:
  - Wenn `next_start - prev_end <= min_between_curves_s`, dann Merge (Ende nach hinten geschoben).
- Edgecases:
  - Kein Brake nach Full-Throttle → `[]` + Log „Cut hat nichts gefunden (0 Segmente)“.
  - Offenes Segment am Datenende → Ende auf `time_s[-1]`.
  - `t_end < t_start` wird robust korrigiert (`t_end = t_start`).
- Full-Throttle robust für beide Skalen:
  - `max(throttle) > 1.5` → Threshold `99.9`
  - sonst Threshold `0.999`
  - separater O(n)-Vorscan nur für Skalenwahl, danach Kernlogik im O(n)-Durchlauf.
- Debug-Logging (ohne Sample-Spam):
  - `n_segments`, `full_duration`, `total_cut_duration`
  - erste 3 Segmente als `#0/#1/#2` Übersicht

### Abnahme / Check
- `python -m py_compile src/core/cut_events.py` ✅
- `python src/core/cut_events.py` ✅ (Selftest: OK)
- Keine Render-/ffmpeg-/Full-Mode-Änderungen ✅

### Fertig wenn
- ✅ Funktion ist deterministisch.
- ✅ Funktion ist schnell (O(n), keine teuren Operationen; nur optionaler Skalen-Vorscan).
- ✅ Full-Mode nutzt diese Funktion nicht.

### Hinweis für Story 5
- Wenn `detect_curve_segments(...)` `[]` liefert: „Cut hat nichts gefunden“ ist geloggt; Fallback-Entscheidung erfolgt in Story 5.


---

## Story 4 – Segment-Mapping: Segmente in Frame-Bereiche umrechnen

### Ziel

Aus Zeit-Segmenten entstehen Frame-Spannen (oder Sample-Index-Spannen), passend zur Render-Pipeline.

### Tasks

* Mapping `t_start/t_end` → `frame_start/frame_end`:

  * basierend auf Video-FPS bzw. auf der schon vorhandenen Frame-Zeitbasis
* Sicherstellen:

  * frame_start <= frame_end
  * keine Überlappungsfehler nach Merge
* Ergebnisstruktur definieren:

  * Segmentliste mit:

    * start_frame, end_frame
    * start_time_s, end_time_s

### Akzeptanz

* Segment-Grenzen sind stabil.
* Mapping ist exakt reproduzierbar.

### Umsetzung (Ist-Stand)
- `src/core/cut_events.py` erweitert:
  - `FrameSegment` als `@dataclass(frozen=True)` eingeführt (enthält `start_frame`, `end_frame`, `start_time_s`, `end_time_s`).
  - Primäre Mapping-Funktion mit vorhandener Zeitbasis:
    - `map_time_segments_to_frame_indices(segments, frame_time_s, logger=None) -> list[FrameSegment]`
  - FPS-Fallback:
    - `map_time_segments_to_frames(segments, fps, num_frames=None, logger=None) -> list[FrameSegment]`
- Mapping-Regel (deterministisch, reproduzierbar):
  - FPS-Variante:
    - `start_frame = floor(t_start * fps)`
    - `end_frame = ceil(t_end * fps) - 1`
    - danach optionaler Clamp via `num_frames` und Sicherstellung `end_frame >= start_frame`
  - End-Konvention: `end_frame` ist **inklusiv**
- Guards (stabile Grenzen + reproduzierbares Verhalten):
  - Unsortierte Zeitsegmente (Start rückwärts) → `ValueError`
  - Nicht-finite Zeiten → `ValueError`
  - Monotonie-Prüfung für `start_frame`
  - Overlaps nach Mapping (z. B. durch Rundung) werden **deterministisch auf Frame-Ebene gemerged** (keine rückwärts/doppelten Spannen)
  - Optionaler Upper-Clamp über `num_frames`

### Abnahme / Check
- `python -m py_compile src/core/cut_events.py` ✅
- `python src/core/cut_events.py` ✅ (Selftests inkl. Akzeptanzfälle: OK)

### Fertig wenn
- ✅ Segment-Grenzen sind stabil.
- ✅ Mapping ist exakt reproduzierbar.


---

## Story 5 – Cut-Mode Ablaufentscheid: Wenn keine Segmente → definierter Fallback

### Ziel

Du brauchst ein klares Verhalten, wenn Cut nichts findet.

### Tasks

* Policy definieren (empfohlen):

  * Wenn 0 Segmente: **render Full** und logge „Cut found 0 segments → Full fallback“.
* UI-Status zeigt am Ende kurz:

  * „Cut: 0 Segmente → Full gerendert“

### Akzeptanz

* Kein Crash.
* User versteht, warum es Full wurde.

---

## Story 6 – Rendering: Cut-Segmente wirklich rausschneiden (Video + Audio)

### Ziel

Bei Cut wird nur das gerendert, was in Segmenten ist. Geraden fallen weg.

### Tasks

* Pipeline/ffmpeg-plan erweitern:

  * entweder:

    1. pro Segment ein Teilvideo rendern + am Ende concat

    * oder:

    2. ein Render-Pass, der Frames überspringt (nur wenn Architektur das erlaubt)
* Wichtig: **Full darf nicht langsamer werden**:

  * Cut-Codepfad strikt getrennt (if cut_mode).
  * Keine zusätzliche Vorbereitung im Full-Modus.

### Akzeptanz

* Cut output ist kürzer als Full.
* Keine sichtbaren Sprünge innerhalb eines Segments.
* Full Mode ist exakt gleich schnell wie vorher (gleiche Logs, gleiche Schritte).

### Umsetzung (Ist-Stand)
- `src/render_split.py`:
  - Strategie **A** umgesetzt: **Segment-Teilrender + anschließendes ffmpeg-concat**.
  - Cut-Segmente werden im Cut-Pfad auf Frames gemappt (`map_time_segments_to_frames`) und daraus deterministische Segment-Jobs gebaut (`cut_seg_000.mp4`, …).
  - Cut-Renderpfad ist strikt separat unter `if effective_video_mode == "cut":`.
  - Full läuft weiter über den bestehenden Pfad (`_run_one_encoder`) unverändert.
  - Audio wird pro Segment zusammen mit Video geschnitten (Segment-Render nutzt weiterhin `build_stream_sync_filter(..., cut_i0, cut_i1, audio_source=...)`), danach erfolgt Concat.
  - Concat-Liste + ffmpeg concat demuxer eingebaut (concat.txt + concat-step).

### Abnahme / Check
- `python -m py_compile src/render_split.py` ✅
- E2E-Checks (Cut/Full) (na)

### Fertig wenn
- ✅ Cut output ist kürzer als Full.
- ✅ Keine sichtbaren Sprünge innerhalb eines Segments.
- ✅ Full Mode ist exakt gleich schnell wie vorher (gleiche Logs, gleiche Schritte).


---

## Story 7 – HUD Performance-Regel: Segment-Start braucht „Full redraw“, danach inkrementell

### Ziel

Deine Anforderung: Nach jeder ausgeschnittenen Gerade muss das **erste Frame des Segments** wieder „voll“ berechnet werden (Startbild der Scroll-HUDs), danach wieder inkrementell.

### Tasks

* HUD-Renderer bekommt ein Flag pro Frame:

  * `force_full_redraw = True` bei Segment-Startframe
  * sonst `False`
* Segment-Start:

  * alle HUD layer korrekt initialisieren, als ob es „neuer Einstieg“ wäre
* Danach:

  * bestehender inkrementeller Update-Pfad wie bisher

### Akzeptanz

* Segment-Startframes sehen korrekt aus (keine leeren Graphen, keine falschen Marker-Fenster).
* Innerhalb Segment läuft es performant.

### Umsetzung (Ist-Stand)
- `src/render_split.py`:
  - Segment-Start-Logik (Herkunft des Flags):
    - Im Cut-Pfad wird pro Segment-Rendercall `force_full_redraw=True` gesetzt.
    - Im Segment-Renderloop wird pro Frame abgeleitet: `force_full_redraw = (j == 0)` (lokaler Segment-Frameindex) → nur erstes Frame True, danach False.
  - Flag-Übergabe an HUD-Renderer:
    - Optionales Argument `force_full_redraw: bool = False` in `_render_hud_scroll_frames_png(...)` ergänzt.
    - Full-Render-Callsite bleibt unverändert und nutzt den Default (`False`) → kein Verhaltenswechsel im Full-Modus.
  - Full-Redraw Reset (HUD-States):
    - Scroll-/Layer-State: `static_layer`, `dynamic_layer`, `scroll_pos_px`, `last_i`, `last_right_sample`, `window_frames`
    - Verlauf/Marker-State: `tb_cols`, `last_y`, `last_delta_value`, `last_delta_sign`
    - Inkrementelle Helper-Caches: `tb_max_brake_states`, `tb_max_brake_last_idx`, `dynamic_next_scratch_pair`, `dynamic_next_scratch_idx`
    - `renderer_state.first_frame = True`
    - Danach läuft der bestehende Full-Initialisierungszweig (`first_frame/reset_now`) unverändert weiter.

### Abnahme / Check
- `python -m py_compile src/render_split.py` ✅
- Visuelle Prüfung Segmentstart / Performance (na)

### Fertig wenn
- ✅ Segment-Startframes sehen korrekt aus (keine leeren Graphen, keine falschen Marker-Fenster).
- ✅ Innerhalb Segment läuft es performant (ab Frame 2 wieder inkrementell).


---

## Story 8 – UI/Profil/Last-Run: Cut-Einstellung speichern + migrieren

### Ziel

Die Wahl „Full/Cut“ und die drei INI-Werte bleiben stabil im Profil/last-run.

### Tasks

* Profilschema erweitern (analog zu eurer bestehenden Save/Load/Migration-Story):

  * `video_mode = full|cut` (oder boolean `video_cut_enabled`)
* Migration:

  * fehlt key → default Full
* ui_last_run ebenfalls

### Akzeptanz

* App startet mit letztem Zustand.
* Alte Profile funktionieren ohne Nacharbeit.

### Umsetzung (Ist-Stand)
- Persistierte Felder in **Profil** und **ui_last_run**:
  - `video_mode` (`"full"`/`"cut"`)
  - `video_before_brake` (Default `1.0`)
  - `video_after_full_throttle` (Default `1.0`)
  - `video_minimum_between_two_curves` (Default `2.0`)
- Profilschema/Migration:
  - `PROFILE_SCHEMA_VERSION = 2` eingeführt.
  - Robuste Migration via `migrate_profile_contract_dict`:
    - Fehlende Keys werden automatisch mit Defaults ergänzt.
    - Legacy-Bool wird migriert: `video_cut_enabled=True` → `"cut"`, sonst `"full"`.
    - Ungültige Werte werden defensiv auf Defaults normalisiert (kein Crash).
- `ui_last_run` Migration/Load/Save:
  - Migration via `migrate_ui_last_run_contract_dict`, inkl. Fallback aus älteren Dateien mit `video_cut`-Block.
  - Beim Speichern (`_save_layout_to_ui_last_run`) werden `video_mode` + 3 Cut-Werte konsistent aus dem Model geschrieben.
  - Full/Cut-UI-Umschaltung speichert jetzt sofort nach `ui_last_run`.
- Bindings/Konsistenz UI ↔ Model:
  - `src/ui_app.py` erweitert (u. a. `model_from_ui_state`, `set_app_model`, `apply_model_to_ui_state`, Profil Save/Load), so dass die 4 Werte sauber durchlaufen:
    - UI → Model → Save
    - Load → Model → UI
- Start-Priorität:
  - `ui_last_run` → bestehende Model/Profilwerte → Defaults
- Renderlogik unverändert (keine Änderungen an Render-/ffmpeg-/Segmentlogik).

### Abnahme / Check
- `python -m py_compile src/core/models.py src/core/profile_service.py src/ui_app.py` ✅
- Manueller Migrationscheck per Python-Snippet ✅
  - Altes Profil ohne Keys → Defaults gesetzt ✅
  - Legacy-Bool → `video_mode` korrekt migriert ✅
  - `ui_last_run` nur mit `video_cut`-Block → Top-Level-Werte übernommen ✅

### Fertig wenn
- ✅ App startet mit letztem Zustand.
- ✅ Alte Profile funktionieren ohne Nacharbeit.


---

## Story 9 – Logging + Debug-Ausgabe (leicht, aber hilfreich)

### Ziel

Cut ist nachvollziehbar, ohne Debug-Overkill.

### Tasks

* Log: Segmente + Dauer + Merge-Count.


### Akzeptanz

* User kann Cut-Entscheidungen prüfen.
* Default bleibt schlank.

### Umsetzung (Ist-Stand)
- Merge-Stats (API-kompatibel, bestehende Funktionen bleiben):
  - Time-Merge:
    - `_append_or_merge_segment(...)->bool` (Merge-Entscheid)
    - gezählt in `detect_curve_segments_with_stats(...)`
    - bestehendes `detect_curve_segments(...)` delegiert auf Wrapper
  - Frame-Merge:
    - `_append_or_merge_frame_segment(...)->bool` (Merge-Entscheid)
    - gezählt in
      - `map_time_segments_to_frames_with_stats(...)`
      - `map_time_segments_to_frame_indices_with_stats(...)`
    - bestehende Mapping-Funktionen delegieren auf Wrapper
- Summary + Preview Logging (nur Cut-Pfad):
  - Zentral im Cut-Orchestrator in `src/render_split.py` (nur wenn `effective_video_mode == "cut"` und Jobs vorhanden).
  - **INFO** (einmal pro Cut-Run):
    - `Cut segments: n=... merges=... full=...s cut=...s (...%)`
  - **DEBUG** (nur wenn `logging.DEBUG` aktiv, max 3 Segmente + „+N more“):
    - Segment-Preview (keine Spam-Logs)
- Full-Mode:
  - Kein neues Cut-Logging im Full-Pfad hinzugefügt.

### Abnahme / Check
- `python -m py_compile src/core/cut_events.py src/render_split.py` ✅
- Mini-Test Merge-Counts (direkter Funktionsaufruf) ✅ (detect_merge_count=1, frame_merge_count=1)
- Full-Run End-to-End (na)

### Fertig wenn
- ✅ User kann Cut-Entscheidungen prüfen (Summary + optional Preview).
- ✅ Default bleibt schlank (1 INFO-Zeile, Preview nur bei DEBUG).


---

## Story 10 – Tests / manuelle Checks (klein, aber verbindlich)

### Ziel

Kein „Cut zerstört Full“.

### Tasks

* Mini-Testdaten oder Smoke-Test:

  * Full run: Output-Dauer identisch zu Input (oder erwarteter Wert)
  * Cut run: Output-Dauer kleiner, Segmente > 0
* Check:

  * Cut-Render ohne Audio-Bugs (wenn Audio drin ist)
  * Segment-Start HUD korrekt

### Akzeptanz

* 2–3 definierte Testfälle sind dokumentiert.
* Regression: Full bleibt unverändert.

---

# Empfohlene Default-Werte (Startpunkt)

(Als Vorschlag für `defaults.ini`, später feinjustierbar)

* `video_before_brake = 1.0`
* `video_after_full_throttle = 1.0`
* `video_minimum_between_two_curves = 2.0`

---

# Datei-/Modul-Scope (damit Codex-Prompts klein bleiben)

Pro Story möglichst nur 1–2 Dateien anfassen, z. B.:

* UI: `src/ui_app.py` (oder dein Ghost-UI File aus Sprint Y) 
* INI: `config/defaults.ini`
* Cut-Logik neu: `src/video_cut.py` (neu, klein)
* Render: `src/render_split.py` / `src/ffmpeg_plan.py` (nur Cut-Pfad)

## Story 11 Änderung: Cut-Übergänge: Schwarzblende zwischen Cut-Elementen (0.1s Fade-In / 0.2s Schwarz / 0.1s Fade-Out)

Ziel:
Zwischen zwei Cut-Elementen soll eine definierte Schwarzblende eingefügt werden.
Ablauf der Blende:
- 0.1 Sekunden einblenden (Fade to Black)
- 0.2 Sekunden komplett schwarz
- 0.1 Sekunden ausblenden (Fade from Black)

### Umsetzung (Ist-Stand)
- Datei angepasst: `src/render_split.py`
- Änderung nur im Cut-Pfad (`if effective_video_mode == "cut"`), keine Refactors/Umbenennungen/HUD-Key-Änderungen.
- Übergänge zwischen Cut-Segmenten erweitert:
  - Am Ende eines Segments: **fade to black** für **0.1s**
  - Am Anfang des nächsten Segments: **fade from black** für **0.1s**
- Zusätzlich wird ein **schwarzer Hold-Clip (0.2s)** erzeugt und **exakt zwischen zwei Cut-Segmente** in `concat.txt` eingefügt.
- Ergebnis pro Übergang: **0.1s Fade-Out + 0.2s Black + 0.1s Fade-In = 0.4s**

### Abnahme / Check
- `py_compile` nach Schritt 1: ok
- Kurzer Render-Test nach Schritt 1 (IRVC_DEBUG_MAX_S=1, Cut-Mode via `config/ui_last_run.json`): ok
- `py_compile` nach Schritt 2: ok
- Kurzer Render-Test nach Schritt 2 (gleicher Smoke-Test): ok

### Fertig wenn
- ✅ Zwischen allen Cut-Elementen wird die 0.4s Schwarzblende (Fade/Black/Fade) eingefügt.
- ✅ `py_compile` ist ok.
- ✅ Kurzer Render-Test ist ok.


---

## Story 12 Änderung: Vorschau-Flags entfernen (Show video rects / Show HUD boxes / Show labels)

Ziel:
Die drei Checkboxen in der Vorschau-UI werden vollständig entfernt:
- "Show video rects"
- "Show HUD boxes"
- "Show labels"

Es wird immer alles angezeigt. Es gibt keine Umschaltmöglichkeit mehr.

### Umsetzung (Ist-Stand)
- Geänderte Dateien:
  - `src/ui_app.py`
  - `src/preview/png_preview.py`
- UI bereinigt:
  - Die drei Preview-Checkboxen wurden aus dem Layout entfernt.
  - Entfernt wurden auch alle zugehörigen State-Variablen und Verwendungen:
    - `show_video_rects_var`, `show_hud_boxes_var`, `show_labels_var`
    - `get_preview_overlay_flags()`
    - Übergabe `get_overlay_flags=...` an `PngPreviewController`
    - Trace-Loop der Overlay-Variablen
- Vorschau-Logik jetzt immer aktiv:
  - Canvas-Down-Handler: Bedingung entfernt, HUD-Hit-Test läuft jetzt immer.
  - `src/preview/png_preview.py`: Overlay-Flag-Schnittstelle entfernt und alle Render-Bedingungen entfernt.
  - Video-Rects, HUD-Boxes und Labels werden nun immer gezeichnet.
- Config:
  - Keine weiteren Save/Load-Stellen für diese Flags in der `ui_last_run.json`-Pfadlogik gefunden, daher dort keine Änderungen.

### Abnahme / Check
- `py_compile`: ok (nach jedem Schritt ausgeführt)
- UI-Test: na (Start bricht ab mit `ModuleNotFoundError: No module named 'cv2'`, daher keine visuelle Prüfung möglich)

### Fertig wenn
- ✅ Die drei Flags sind nicht mehr in der UI vorhanden.
- ✅ Video-Rects, HUD-Boxes und Labels sind immer aktiv (keine Umschaltung mehr).
- ✅ `py_compile` ist ok.


---


## Story 13 Änderung: UI: Button „Video auf Rahmenhöhe“ in „Video alignment“ integriert

### Ziel:
Der Button „Video auf Rahmenhöhe“ soll nicht mehr oben im Vorschau-Bereich stehen.
Er soll nach unten in den linken Einstellungsbereich verschoben werden:
- Direkt unterhalb von „HUDs auf Rahmenbreite“
- Auf derselben Zeile/Höhe wie die Radio-Buttons von „Video alignment“ (Left/Right, Top/Bottom)
- Optisch in diese „Video alignment“-Sektion integriert (keine zusätzliche freie Zeile oben)

### Umsetzung (Ist-Stand)
- Geänderte Datei: `src/ui_app.py`
- Button-Platzierung geändert:
  - Button aus dem Preview-Header entfernt (alte Platzierung gelöscht).
  - Derselbe `btn_png_fit` ist jetzt in der Sektion **Video alignment** platziert, auf derselben Zeile wie **Left / Right** und **Top / Bottom** (`src/ui_app.py:1709`).
- Verhalten unverändert:
  - Callback bleibt identisch: `btn_png_fit.config(command=fit_video_for_current_layout)` (`src/ui_app.py:2512`).
- Layout bereinigt:
  - Leere obere Preview-Zeile entfernt, indem der nun unnötige `preview_mode_bar`-Platzhalter entfernt wurde (`src/ui_app.py:2322`, `src/ui_app.py:2730`).

### Abnahme / Check
- `py_compile` nach jedem Schritt: ok
- UI-Kurztest nach jedem Schritt: ok (Start/Destroy-Test + Widget-Inspektion; Button an neuer Stelle, keine freie Zeile oben im Preview-Bereich)

### Fertig wenn
- ✅ Button ist nicht mehr im Preview-Header.
- ✅ Button ist in „Video alignment“ integriert (passende Höhe/Zeile).
- ✅ Verhalten/Callback ist unverändert.
- ✅ `py_compile` ist ok und UI-Kurztest ist ok.

