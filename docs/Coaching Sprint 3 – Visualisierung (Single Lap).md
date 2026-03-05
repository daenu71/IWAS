# Coaching Sprint 3 – Visualisierung (Single Lap)

## Ziel

Analysierte Lap-Daten grafisch darstellen. Keine LLM-Logik. Kein Multi-Lap-Vergleich. Alles wird so gebaut, dass Sprint 4 (Multi-Lap), Sprint 5 (Session-Vergleich) und Sprint 6 (LLM) ohne Strukturumbauten aufsatteln können.

---

## Architektur-Entscheidungen (vor Implementierung)

### Neues Modul: `src/ui/coaching_detail.py`

Rechte Seite der Coaching-View wird in ein dediziertes Widget ausgelagert. Klar getrennt von `coaching_browser.py`. Erhält Daten über ein simples `LapViewModel` – kein direkter Parquet-Zugriff im UI.

### Neues Modul: `src/core/coaching/lap_view_model.py`

Lädt und aggregiert alle Analyseartefakte für eine Lap zu einem `LapViewModel` Objekt. Das ist der einzige Datenzugriffspunkt für die gesamte Visualisierungs-UI. Sprint 4/5/6 erweiterern dieses Modell, ohne die UI anfassen zu müssen.

```
LapViewModel
  ├── meta: LapMeta
  ├── track_xy: list[tuple[float, float]]   # generiert aus VelocityX/Y
  ├── corners: list[CornerData]
  ├── events: dict[corner_id, list[Event]]
  ├── features: dict[corner_id, dict]
  ├── resampled: DataFrame (lazy, on demand)
  └── source_id: LapSourceRef              # Sprint 5: ermöglicht Fremd-Sessions
```

### Canvas-Architektur

Alle Maps (Trackmap + Corner-Zoom) werden auf `tk.Canvas` gerendert. Kein Matplotlib in der Haupt-UI (zu schwer, zu langsam). Matplotlib nur für Telemetrie-Traces als eingebettete Figure.

---

## Stories

---

### Story 3.0 – LapViewModel: Datenzugriffs-Layer

**Quelle:** Beachte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md`

**Ziel:** Zentrale Klasse `LapViewModel` die alle Analyseartefakte einer Lap lädt. Einzige Schnittstelle zwischen Analyse-Backend und Visualisierungs-UI.

**Neue Dateien**
- `src/core/coaching/lap_view_model.py`

**Liest**
- `lap_resampled.parquet`
- `lap_events.json`
- `corner_features.parquet`
- `corner_map_v1.json`
- `session_meta.json`
- `run_XXXX_meta.json`

**Interface**
```python
class LapViewModel:
    @staticmethod
    def load(session_dir, run_id, lap_no) -> "LapViewModel"
    
    meta: LapMeta                    # track, car, lap_no, lap_time, validity
    track_xy: np.ndarray             # (N, 2) normiert auf [0,1]
    lap_dist_pct: np.ndarray         # korrespondierend zu track_xy
    corners: list[CornerInfo]        # CornerID, start/end_lapdist, type
    events: dict[int, list[Event]]   # corner_id → Events
    features: dict[int, dict]        # corner_id → Feature-Dict
    
    def get_resampled_channel(self, channel: str) -> np.ndarray  # lazy load
    def is_loaded(self) -> bool
```

**Sprint-Erweiterbarkeit**
- Sprint 4: `LapViewModel.load_batch(laps)` → `list[LapViewModel]`
- Sprint 5: `LapSourceRef` mit `driver_id`, `session_origin` für Fremd-Sessions
- Sprint 6: `LapViewModel` liefert bereits alle Daten die der LLM-Prompt braucht

**Akzeptanzkriterien**
- `LapViewModel.load()` mit echten Analyseartefakten liefert valides Objekt
- Kein Crash bei fehlenden optionalen Feldern (null-safe)
- `python -m py_compile src/core/coaching/lap_view_model.py` fehlerfrei

**Unit Test**
- `tests/test_lap_view_model.py`
- Test 1: Load mit vollständigem Artefakt-Set → alle Felder befüllt
- Test 2: Load mit fehlendem `lap_events.json` → kein Crash, `events` leer
- Test 3: `get_resampled_channel()` gibt korrekte np.ndarray zurück

**Deliverables**
- **Titel:** LapViewModel – Datenzugriffs-Layer für Visualisierungs-UI
- **Zusammenfassung:** Neue Klasse `LapViewModel` konsolidiert alle Analyseartefakte einer Lap. Einzige Datenquelle für alle Sprint-3-UI-Komponenten.
- **Geänderte Dateien:** `src/core/coaching/lap_view_model.py` (neu), `tests/test_lap_view_model.py` (neu)
- **Dokumentation:** Halte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md` aktuell, sofern es Abweichungen gibt.

---

### Story 3.1 – TrackMap Canvas: XY-Rekonstruktion und Rendering

**Quelle:** Beachte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md`

**Quelle:** Beachte C:\iWAS\docs\Coaching Sprint 3 – Visualisierung (Single Lap).md

**Ziel:** Aus `VelocityX`/`VelocityY` (oder falls vorhanden direkten XY-Kanälen) wird eine 2D-Streckenlinie rekonstruiert und auf einem `tk.Canvas` gerendert.

**Neue Dateien**
- `src/core/coaching/track_geometry.py`

**Algorithmus**
```
1. VelocityX, VelocityY, SessionTime aus lap_resampled.parquet
2. Integration: X[i] = sum(VelocityX * dt), Y[i] = sum(VelocityY * dt)
3. Normierung: beide Achsen auf [0, 1], Aspect Ratio erhalten
4. Wrap-around robust: LapDistPct 0→1 ergibt geschlossene Kurve
```

**Interface**
```python
def reconstruct_xy(resampled_df: DataFrame) -> np.ndarray  # (N, 2) normiert
def render_trackmap(canvas: tk.Canvas, xy: np.ndarray, 
                    corners: list[CornerInfo],
                    selected_corner_id: int | None,
                    width: int, height: int,
                    lap_color: str = "#E53935") -> None
```

**Render-Details**
- Streckenlinie: grau (#555)
- Fahrlinie (Lap): Primärfarbe (konfigurierbar, Sprint 4 mehrere Farben)
- Corner-Segmente: leicht farbig hinterlegt (Polygon, halbtransparent)
- Gewählter Corner: highlight (heller Rahmen)
- Corner-Label (ID): kleiner Text am Apex-Punkt
- Click-Mapping: `canvas.tag_bind` pro Corner-Polygon

**Akzeptanzkriterien**
- Streckenlinie wird korrekt geschlossen (Start = End)
- Klick auf Corner-Segment löst `on_corner_selected(corner_id)` Callback aus
- Resize des Canvas → Neuzeichnung ohne Flicker
- `python -m py_compile src/core/coaching/track_geometry.py` fehlerfrei

**Deliverables**
- **Titel:** TrackMap Canvas – XY-Rekonstruktion und interaktives Corner-Rendering
- **Zusammenfassung:** `track_geometry.py` mit XY-Integration und `render_trackmap()`. Klickbare Corner-Segmente via Canvas-Tags.
- **Geänderte Dateien:** `src/core/coaching/track_geometry.py` (neu)
- **Dokumentation:** Halte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md` aktuell, sofern es Abweichungen gibt.

---

### Story 3.2 – CoachingDetailView: Layout-Rahmen

**Quelle:** Beachte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md`

**Ziel:** Rechte Seite der Coaching-View wird durch `CoachingDetailView` ersetzt. Definiert das stabile Layout-Gerüst das Sprint 4–6 füllen.

**Neue Dateien**
- `src/ui/coaching_detail.py`

**Layout**
```
┌─────────────────────────────────────────────┐
│ [Header: Track / Car / Lap / Zeit]          │
├──────────────────────┬──────────────────────┤
│ TrackMap Canvas      │ Corner-Zoom Canvas   │
│ (klickbar)           │ (bei Auswahl)        │
├──────────────────────┴──────────────────────┤
│ Telemetrie-Trace (matplotlib, collapsible)  │
├─────────────────────────────────────────────┤
│ Corner Scorecard (Feature-Tabelle)          │
└─────────────────────────────────────────────┘
```

**Interface**
```python
class CoachingDetailView(ttk.Frame):
    def load_lap(self, vm: LapViewModel) -> None
    def clear(self) -> None
    def set_selected_corner(self, corner_id: int) -> None
```

**Wichtig:** Alle vier Bereiche sind eigenständige Sub-Widgets. Jeder davon kann in Sprint 4 separat erweitert werden ohne das andere anzufassen.

**Akzeptanzkriterien**
- `load_lap(None)` → leerer Zustand, kein Crash
- `load_lap(vm)` → Header befüllt, TrackMap gerendert
- Klick auf Corner → Corner-Zoom und Scorecard aktualisieren sich
- `python -m py_compile src/ui/coaching_detail.py` fehlerfrei

**Deliverables**
- **Titel:** CoachingDetailView – Layout-Gerüst für Lap-Visualisierung
- **Zusammenfassung:** Neues Widget `CoachingDetailView` als stabiler Layout-Container. Verdrahtet mit `CoachingView` in `app.py` via `load_lap()` Callback.
- **Geänderte Dateien:** `src/ui/coaching_detail.py` (neu), `src/ui/app.py` (CoachingDetailView eingehängt)
- **Dokumentation:** Halte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md` aktuell, sofern es Abweichungen gibt.

---

### Story 3.3 – Corner-Zoom Canvas mit Event-Markern

**Quelle:** Beachte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md`

**Ziel:** Klick auf Corner in der TrackMap öffnet vergrößerten Ausschnitt des Corner-Bereichs mit eingezeichneten Events.

**Erweiterung in**
- `src/core/coaching/track_geometry.py` (neue Funktion)
- `src/ui/coaching_detail.py` (Corner-Zoom-Bereich)

**Neue Funktion**
```python
def render_corner_zoom(canvas: tk.Canvas,
                       xy: np.ndarray,
                       corner: CornerInfo,
                       events: list[Event],
                       width: int, height: int) -> None
```

**Event-Visualisierung**

| Event | Symbol | Farbe |
|---|---|---|
| `brake_start` | ▼ (Dreieck) | Rot |
| `peak_brake` | ● | Dunkelrot |
| `turn_in` | ◀ | Orange |
| `min_speed` | ★ | Gelb |
| `throttle_on` | ▲ | Hellgrün |
| `throttle_full` | ▲ | Grün |
| `gear_change` | ⬡ | Blau |
| `oversteer_event` | ⚠ | Magenta |
| `crest` | ⌒ | Cyan |

- Events werden an ihrer `lapdist_pct`-Position auf die Fahrlinie projiziert
- Hover-Tooltip: Event-Name + Wert
- Legende rechts unten im Canvas (kompakt)

**Akzeptanzkriterien**
- Korrekte Projektion der Events auf XY-Linie via `lapdist_pct`
- Tooltip erscheint bei Hover ohne Flicker
- Bei `events = []` → nur Fahrlinie, kein Crash

**Deliverables**
- **Titel:** Corner-Zoom Canvas – Event-Projektion und Visualisierung
- **Zusammenfassung:** `render_corner_zoom()` projiziert Events per LapDistPct auf die Fahrlinie. Icon-basierte Darstellung mit Hover-Tooltip.
- **Geänderte Dateien:** `src/core/coaching/track_geometry.py` (erweitert), `src/ui/coaching_detail.py` (Corner-Zoom-Bereich aktiviert)
- **Dokumentation:** Halte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md` aktuell, sofern es Abweichungen gibt.

---

### Story 3.4 – Telemetrie-Trace (Single Lap, pro Corner)

**Quelle:** Beachte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md`

**Ziel:** Unterhalb der Maps wird für den gewählten Corner ein Telemetrie-Trace gerendert. X-Achse = LapDistPct, mehrere Kanäle überlagert.

**Neue Dateien**
- `src/ui/coaching_trace.py`

**Interface**
```python
class CornerTraceView(ttk.Frame):
    def load(self, vm: LapViewModel, corner_id: int, 
             channels: list[str]) -> None
    def clear(self) -> None
```

**Standard-Kanäle**
```
Brake, Throttle, Speed, YawRate, SteeringWheelAngle
```

**Render-Details**
- Matplotlib `Figure` eingebettet via `FigureCanvasTkAgg`
- Jeder Kanal in eigener normierter Spur (0–1, eigene Y-Achse rechts)
- Event-Marker als vertikale gestrichelte Linien mit Label oben
- X-Achse: LapDistPct des Corner-Fensters (start_lapdist − 0.005 bis end_lapdist + 0.005)
- Channel-Selektor: Checkboxen über dem Plot (welche Kanäle sichtbar)
- **Sprint-4-Hook:** `load()` akzeptiert bereits `List[LapViewModel]` — bei >1 werden überlagert

**Akzeptanzkriterien**
- Plot rendert korrekt für einen realen Corner
- Event-Marker sind mit den Events aus `lap_events.json` synchron
- Channel-Selektor schaltet Kanäle ohne Neuladen
- `python -m py_compile src/ui/coaching_trace.py` fehlerfrei

**Deliverables**
- **Titel:** CornerTraceView – Telemetrie-Trace mit Event-Markern pro Corner
- **Zusammenfassung:** Matplotlib-basierter Telemetrie-Trace für den gewählten Corner. Event-Marker synchron mit `lap_events.json`. Channel-Selektor per Checkbox.
- **Geänderte Dateien:** `src/ui/coaching_trace.py` (neu), `src/ui/coaching_detail.py` (CornerTraceView eingehängt)
- **Dokumentation:** Halte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md` aktuell, sofern es Abweichungen gibt.

---

### Story 3.5 – Corner Scorecard (Feature-Tabelle)

**Quelle:** Beachte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md`

**Ziel:** Für den gewählten Corner werden die berechneten Features aus `corner_features.parquet` als kompakte Tabelle angezeigt.

**Neue Dateien**
- `src/ui/coaching_scorecard.py`

**Interface**
```python
class CornerScorecard(ttk.Frame):
    def load(self, features: dict, schema: FeatureSchema) -> None
    def clear(self) -> None
```

**Darstellung**

```
┌─────────────────────────────────┬──────────┬──────┐
│ Feature                         │ Wert     │      │
├─────────────────────────────────┼──────────┼──────┤
│ grip_usage_p95                  │  0.87    │  ●   │
│ brake_release_slope             │ -0.42    │  ●   │
│ throttle_onset_vs_yawrate_peak  │ +0.003   │  ●   │
└─────────────────────────────────┴──────────┴──────┘
```

- Gruppen-Header aus `feature_schema_v1.json` (group_id als Separator)
- Null-Features ausgegraut mit Label „n/a (Kanal fehlt)"
- Dritte Spalte: reserviert für Sprint 4 (Vergleichsdelta) und Sprint 6 (LLM-Bewertung)
- Tooltip auf Feature-Label: `description` aus Schema

**Akzeptanzkriterien**
- Alle Feature-Gruppen korrekt gruppiert
- Null-Features sauber dargestellt, kein Crash
- Dritte Spalte vorhanden aber leer (Sprint-4-Platzhalter)

**Deliverables**
- **Titel:** CornerScorecard – Feature-Tabelle mit Schema-Gruppen
- **Zusammenfassung:** `CornerScorecard` zeigt alle Features des gewählten Corners, gruppiert nach `feature_schema_v1.json`. Dritte Spalte als Platzhalter für Sprint 4/6.
- **Geänderte Dateien:** `src/ui/coaching_scorecard.py` (neu), `src/ui/coaching_detail.py` (Scorecard eingehängt)
- **Dokumentation:** Halte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md` aktuell, sofern es Abweichungen gibt.

---

### Story 3.6 – Feature-Heatmap auf TrackMap

**Quelle:** Beachte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md`

**Ziel:** TrackMap-Corners werden farbkodiert basierend auf einem wählbaren Feature-Wert.

**Erweiterung in**
- `src/core/coaching/track_geometry.py`
- `src/ui/coaching_detail.py`

**Interface**
```python
def render_trackmap_heatmap(canvas: tk.Canvas, xy: np.ndarray,
                             corners: list[CornerInfo],
                             feature_values: dict[int, float],
                             colormap: str = "RdYlGn_r") -> None
```

**UI-Element**
- Dropdown über der TrackMap: „Heatmap: [Feature auswählen]" + „Aus"
- Feature-Liste aus `feature_schema_v1.json` (nur required Features)
- Colormap: grün (gut) → gelb → rot (schlecht), Normierung min-max über sichtbare Corners
- Legende: Farbbalken unterhalb der Map, min/max-Wert

**Akzeptanzkriterien**
- Feature-Dropdown befüllt sich aus Schema
- Corners werden korrekt eingefärbt
- „Aus" stellt Standarddarstellung wieder her

**Deliverables**
- **Titel:** TrackMap Heatmap – Corner-Einfärbung nach Feature-Wert
- **Zusammenfassung:** Feature-Heatmap auf der TrackMap. Dropdown-Selektor, Colormap, Legende.
- **Geänderte Dateien:** `src/core/coaching/track_geometry.py` (erweitert), `src/ui/coaching_detail.py` (Dropdown + Heatmap-Toggle)
- **Dokumentation:** Halte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md` aktuell, sofern es Abweichungen gibt.

---

### Story 3.7 – Analyse-Trigger: Klick auf Lap öffnet Detail-View

**Quelle:** Beachte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md`

**Ziel:** Klick auf „Analyze"-Button im CoachingBrowser lädt `LapViewModel` und übergibt ihn an `CoachingDetailView`.

**Änderungen in**
- `src/ui/app.py`

**Flow**
```
User klickt Analyze (Lap-Node)
→ app.py: _handle_analyze_lap()
→ LapViewModel.load(session_dir, run_id, lap_no)
→ coaching_detail_view.load_lap(vm)
→ TrackMap rendert
→ Erster Corner automatisch selektiert
```

**Akzeptanzkriterien**
- Analyse-Status `computed` → DetailView wird geladen
- Analyse-Status `not_computed` → Button startet Analyse, dann DetailView
- Analyse-Status `blocked` → Fehlermeldung im Detail-Header, kein Crash
- UI friert nicht ein (LapViewModel-Load im Thread, dann UI-Update im Main-Thread)

**Deliverables**
- **Titel:** Analyse-Trigger – Lap-Klick öffnet CoachingDetailView
- **Zusammenfassung:** Verdrahtung CoachingBrowser → LapViewModel → CoachingDetailView. Threading für Load, erster Corner auto-selektiert.
- **Geänderte Dateien:** `src/ui/app.py` (erweitert)
- **Dokumentation:** Halte `docs/Coaching Sprint 3 – Visualisierung (Single Lap).md` aktuell, sofern es Abweichungen gibt.

---

## Sprint 3 – Definition of Done

- [ ] `LapViewModel.load()` funktioniert mit echten Artefakten
- [ ] TrackMap wird korrekt gerendert und ist klickbar
- [ ] Corner-Zoom zeigt Events mit Icons
- [ ] Telemetrie-Trace rendert für gewählten Corner
- [ ] Feature-Scorecard zeigt gruppierte Features
- [ ] Heatmap-Toggle funktioniert
- [ ] Kein UI-Freeze beim Laden
- [ ] Alle neuen Module kompilieren fehlerfrei

---

## Sprint-Vorschau (Erweiterungspunkte)

| Sprint | Erweiterungspunkt |
|---|---|
| **4** | `LapViewModel.load_batch()`, `CornerTraceView` überlagert mehrere VMs, Scorecard Δ-Spalte |
| **5** | `LapSourceRef` mit `driver_id`, Export/Import via ZIP, Fremd-Session in gleichen View-Widgets |
| **6** | `LapViewModel` → LLM-Prompt-Builder, Scorecard dritte Spalte = LLM-Bewertung |