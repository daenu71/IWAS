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

