# Coaching Sprint 3 – Visualisierung (Single Lap)

## Ziel

Analysierte Lap-Daten grafisch darstellen. Keine LLM-Logik. Kein Multi-Lap-Vergleich. Alles wird so gebaut, dass Sprint 4 (Multi-Lap), Sprint 5 (Session-Vergleich) und Sprint 6 (LLM) ohne Strukturumbauten aufsatteln können.

---

## Architektur-Entscheidungen (vor Implementierung)

### Neues Modul: `src/ui/coaching_detail.py`

Rechte Seite der Coaching-View wird in ein dediziertes Widget ausgelagert. Klar getrennt von `coaching_browser.py`. Erhält Daten über ein simples `LapViewModel` – kein direkter Parquet-Zugriff im UI.

### Neues Modul: `src/core/coaching/lap_view_model.py`

Lädt und aggregiert alle Analyseartefakte für eine Lap zu einem `LapViewModel` Objekt. Das ist der einzige Datenzugriffspunkt für die gesamte Visualisierungs-UI. Sprint 4/5/6 erweitern dieses Modell, ohne die UI anfassen zu müssen.

```
LapViewModel
  ├── meta: LapMeta
  ├── track_xy: list[tuple[float, float]]   # generiert aus VelocityX/Y
  ├── track_road_geometry: TrackRoadGeometry | None  # aus IBT-Header, optional
  ├── corners: list[CornerData]
  ├── events: dict[corner_id, list[Event]]
  ├── features: dict[corner_id, dict]
  ├── resampled: DataFrame (lazy, on demand)
  └── source_id: LapSourceRef              # Sprint 5: ermöglicht Fremd-Sessions
```

### Canvas-Architektur

Alle Maps (Trackmap + Corner-Zoom) werden auf `tk.Canvas` gerendert. Kein Matplotlib in der Haupt-UI (zu schwer, zu langsam). Matplotlib nur für Telemetrie-Traces als eingebettete Figure.

### IBT-Straßengeometrie (optional)

iRacing speichert in IBT-Dateien Geometriedaten inkl. Streckenmittellinie und Randpunkte. Diese sind nicht über den IRSDK Live-Channel zugänglich, sondern werden per IBT-Postprocessing einmalig extrahiert und als `track_road_geometry.json` im Storage abgelegt.

**Datenstruktur:**
```json
{
  "track_key": "Sebring__Full Course",
  "source": "ibt_header",
  "center_line": [[x, y], ...],
  "left_edge": [[x, y], ...],
  "right_edge": [[x, y], ...]
}
```

- Extraktion erfolgt einmalig beim ersten IBT-Import für diesen Track/Config.
- Fehlt die Datei → kein Crash, kein Straßenband → nur Fahrlinie.
- TrackMap und Corner-Zoom nutzen sie wenn vorhanden.

---

## Stories

---

### Story 3.0 – LapViewModel: Datenzugriffs-Layer

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
- `track_road_geometry.json` (optional, für Straßenband)

**Interface**
```python
class LapViewModel:
    @staticmethod
    def load(session_dir, run_id, lap_no) -> "LapViewModel"
    
    meta: LapMeta                              # track, car, lap_no, lap_time, validity
    track_xy: np.ndarray                       # (N, 2) normiert auf [0,1]
    lap_dist_pct: np.ndarray                   # korrespondierend zu track_xy
    track_road_geometry: TrackRoadGeometry | None  # Straßenband aus IBT, None wenn nicht verfügbar
    corners: list[CornerInfo]                  # CornerID, start/end_lapdist, type
    events: dict[int, list[Event]]             # corner_id → Events
    features: dict[int, dict]                  # corner_id → Feature-Dict
    
    def get_resampled_channel(self, channel: str) -> np.ndarray  # lazy load
    def is_loaded(self) -> bool
```

**Sprint-Erweiterbarkeit**
- Sprint 4: `LapViewModel.load_batch(laps)` → `list[LapViewModel]`
- Sprint 5: `LapSourceRef` mit `driver_id`, `session_origin` für Fremd-Sessions
- Sprint 6: `LapViewModel` liefert bereits alle Daten die der LLM-Prompt braucht

**Akzeptanzkriterien**
- `LapViewModel.load()` mit echten Analyseartefakten liefert valides Objekt
- Kein Crash bei fehlenden optionalen Feldern (null-safe), inkl. fehlendem `track_road_geometry.json`
- `python -m py_compile src/core/coaching/lap_view_model.py` fehlerfrei

**Unit Test**
- `tests/test_lap_view_model.py`
- Test 1: Load mit vollständigem Artefakt-Set → alle Felder befüllt
- Test 2: Load mit fehlendem `lap_events.json` → kein Crash, `events` leer
- Test 3: Load ohne `track_road_geometry.json` → `track_road_geometry` ist `None`, kein Crash
- Test 4: `get_resampled_channel()` gibt korrekte np.ndarray zurück

**Deliverables**
- **Titel:** LapViewModel – Datenzugriffs-Layer für Visualisierungs-UI
- **Zusammenfassung:** Neue Klasse `LapViewModel` konsolidiert alle Analyseartefakte einer Lap. Einzige Datenquelle für alle Sprint-3-UI-Komponenten. `track_road_geometry` als optionales Feld ergänzt.
- **Geänderte Dateien:** `src/core/coaching/lap_view_model.py` (neu), `tests/test_lap_view_model.py` (neu)

---

### Story 3.1 – TrackMap Canvas: XY-Rekonstruktion und Rendering

**Ziel:** Aus `VelocityX`/`VelocityY` (oder falls vorhanden direkten XY-Kanälen) wird eine 2D-Streckenlinie rekonstruiert und auf einem `tk.Canvas` gerendert. Wenn `track_road_geometry` verfügbar ist, wird zusätzlich das Straßenband angezeigt.

**Neue Dateien**
- `src/core/coaching/track_geometry.py`

**Algorithmus**
```
1. VelocityX, VelocityY, SessionTime aus lap_resampled.parquet
2. Integration: X[i] = sum(VelocityX * dt), Y[i] = sum(VelocityY * dt)
3. Normierung: beide Achsen auf [0, 1], Aspect Ratio erhalten
4. Wrap-around robust: LapDistPct 0→1 ergibt geschlossene Kurve
5. Optional: TrackRoadGeometry auf gleiche Normierung transformieren
```

**Interface**
```python
def reconstruct_xy(resampled_df: DataFrame) -> np.ndarray  # (N, 2) normiert

def render_trackmap(canvas: tk.Canvas,
                    xy: np.ndarray,
                    corners: list[CornerInfo],
                    selected_corner_id: int | None,
                    width: int, height: int,
                    road_geometry: TrackRoadGeometry | None = None,
                    lap_color: str = "#E53935") -> None
```

**Render-Details**
- Straßenband (wenn `road_geometry` vorhanden): linke/rechte Kante als dunkle Linie (#444), Fläche dazwischen als sehr dunkles Polygon (#2A2A2A)
- Fahrlinie (Lap): Primärfarbe (#E53935), über Straßenband gezeichnet
- Corner-Segmente: leicht farbig hinterlegt (Polygon, halbtransparent)
- Gewählter Corner: highlight (heller Rahmen)
- Corner-Label (ID): kleiner Text am Apex-Punkt
- Click-Mapping: `canvas.tag_bind` pro Corner-Polygon
- Fehlt `road_geometry` → nur Fahrlinie wie bisher, kein Unterschied im Verhalten

**Akzeptanzkriterien**
- Streckenlinie wird korrekt geschlossen (Start = End)
- Klick auf Corner-Segment löst `on_corner_selected(corner_id)` Callback aus
- Resize des Canvas → Neuzeichnung ohne Flicker
- Straßenband rendert korrekt wenn vorhanden; fehlt es → kein Crash
- `python -m py_compile src/core/coaching/track_geometry.py` fehlerfrei

**Deliverables**
- **Titel:** TrackMap Canvas – XY-Rekonstruktion, Straßenband und interaktives Corner-Rendering
- **Zusammenfassung:** `track_geometry.py` mit XY-Integration und `render_trackmap()`. Optionales Straßenband aus `TrackRoadGeometry`. Klickbare Corner-Segmente via Canvas-Tags.
- **Geänderte Dateien:** `src/core/coaching/track_geometry.py` (neu)

---

### Story 3.2 – CoachingDetailView: Layout-Rahmen ✅ (umgesetzt)

**Ziel:** Rechte Seite der Coaching-View wird durch `CoachingDetailView` ersetzt. Definiert das stabile Layout-Gerüst das Sprint 4–6 füllen.

**Neue Dateien**
- `src/ui/coaching_detail.py`

**Layout**
```
┌─────────────────────────────────────────────┐
│ [Header: Track / Car / Lap / Zeit]          │
├──────────────────────┬──────────────────────┤
│ TrackMap Canvas      │ Datenbereich         │
│ (klickbar)           │ (Corner-Zoom         │
│                      │  überlagert TrackMap │
│                      │  bei Corner-Klick)   │
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
- Klick auf Corner → Corner-Zoom überlagert TrackMap, Scorecard aktualisiert sich
- `python -m py_compile src/ui/coaching_detail.py` fehlerfrei

**Deliverables**
- **Titel:** CoachingDetailView – Layout-Gerüst für Lap-Visualisierung
- **Zusammenfassung:** Neues Widget `CoachingDetailView` als stabiler Layout-Container. Verdrahtet mit `CoachingView` in `app.py` via `load_lap()` Callback.
- **Geänderte Dateien:** `src/ui/coaching_detail.py` (neu), `src/ui/app.py` (CoachingDetailView eingehängt)

---

### Story 3.3 – Corner-Zoom Canvas mit Event-Markern

**Ziel:** Klick auf Corner in der TrackMap öffnet einen vergrößerten Ausschnitt des Corner-Bereichs direkt über dem TrackMap-Canvas. Der Corner-Zoom belegt denselben Raum wie die TrackMap und zeigt oben einen „← Zurück"-Button der zur TrackMap zurückführt. Rechts vom Canvas-Bereich steht Platz für Daten (Scorecard, Trace) – dieser Bereich wird durch den Corner-Zoom nicht berührt.

Wenn `track_road_geometry` verfügbar ist, wird auch im Corner-Zoom das Straßenband gezeigt, sodass die Fahrlinie realistisch auf der Strecke positioniert erscheint.

**Erweiterung in**
- `src/core/coaching/track_geometry.py` (neue Funktion)
- `src/ui/coaching_detail.py` (Corner-Zoom-Overlay-Logik)

**Neue Funktion**
```python
def render_corner_zoom(canvas: tk.Canvas,
                       xy: np.ndarray,
                       corner: CornerInfo,
                       events: list[Event],
                       width: int, height: int,
                       road_geometry: TrackRoadGeometry | None = None) -> None
```

**Overlay-Verhalten**
- Klick auf Corner in TrackMap → Corner-Zoom-Frame erscheint über dem TrackMap-Canvas (gleiche Größe, gleiche Position, `place`-basiertes Overlay)
- Oben im Corner-Zoom: kompakter „← Zurück"-Button → blendet Overlay aus, TrackMap ist wieder sichtbar
- Größe und Position des Canvas-Bereichs bleiben unverändert; der rechte Datenbereich wird nicht tangiert
- Resize des Fensters → Overlay passt sich mit an

**Straßenband im Corner-Zoom**
- Wenn `road_geometry` vorhanden: Straßenrand und -fläche analog zur TrackMap rendern (gleiche Farben)
- Fahrlinie des Corners wird über dem Straßenband gezeichnet
- Fehlt `road_geometry` → nur Fahrlinie, kein Crash

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
- Corner-Zoom erscheint als Overlay über der TrackMap (gleiche Größe/Position)
- „← Zurück"-Button blendet Overlay aus und stellt TrackMap wieder her
- Rechter Datenbereich (Scorecard etc.) bleibt durch den Overlay unangetastet
- Korrekte Projektion der Events auf XY-Linie via `lapdist_pct`
- Straßenband im Zoom wenn `road_geometry` vorhanden; fehlt es → kein Crash
- Tooltip erscheint bei Hover ohne Flicker
- Bei `events = []` → nur Fahrlinie, kein Crash

**Deliverables**
- **Titel:** Corner-Zoom Canvas – Overlay mit Straßenband, Event-Projektion und Zurück-Navigation
- **Zusammenfassung:** `render_corner_zoom()` projiziert Events per LapDistPct auf die Fahrlinie. Overlay-Mechanismus ersetzt TrackMap in-place. Straßenband aus `TrackRoadGeometry` wenn verfügbar. „← Zurück"-Button stellt TrackMap wieder her.
- **Geänderte Dateien:** `src/core/coaching/track_geometry.py` (erweitert), `src/ui/coaching_detail.py` (Overlay-Logik ergänzt)

---

### Story 3.3a – IBT-Extraktor: TrackRoadGeometry aus IBT-Dateien

**Ziel:** Einmaliges Extrahieren der Streckengeometrie (Mittellinie, linke/rechte Kante) aus einer IBT-Datei und Ablage als `track_road_geometry.json` im Storage. Dieser Schritt ist optional und blockiert keine andere Story.

**Neue Dateien**
- `src/core/coaching/ibt_track_extractor.py`

**Input**
- Eine beliebige IBT-Datei für den Track/Config (Pfad vom User angegeben oder automatisch aus bekannten iRacing-Pfaden)

**Output**
- `<storage_root>/track_geometries/<track_key>/track_road_geometry.json`

**Algorithmus**
```
1. IBT-Header lesen: TrackLength, track surface path data
2. Mittellinie extrahieren (IBT stellt Centerline-Punkte bereit)
3. Linke/rechte Kante aus Spurbreiten-Metadaten ableiten (falls vorhanden)
   Fallback: feste Versatzbreite (z.B. ±5 m) entlang Normalvektor
4. Alle Punkte in lokales XY (Meter) transformieren (equirectangular, gleiche Projektion wie track_geometry.py)
5. Normierung analog track_xy (auf [0,1], Aspect Ratio erhalten)
6. Als JSON schreiben
```

**Track-Key Format**
`TrackDisplayName__TrackConfigName` (aus `session_meta.json`, identisch zu CornerMap)

**Akzeptanzkriterien**
- Extraktion läuft durch ohne Crash für valide IBT-Datei
- Ausgabe-JSON enthält `center_line`, `left_edge`, `right_edge` als Listen von [x, y]-Paaren
- Fehlt ein Feld im IBT-Header → Fallback (Normalvektor-Offset) greift, kein Crash
- `python -m py_compile src/core/coaching/ibt_track_extractor.py` fehlerfrei
- Normierung ist identisch zur Normierung in `track_geometry.py` (gleicher Origin, gleiche Skalierung)

**Unit Test**
- `tests/test_ibt_track_extractor.py`
- Test 1: Synthetic IBT-ähnlicher Input → JSON-Output valide
- Test 2: Fehlende Spurbreite → Fallback-Offset greift, kein Crash
- Test 3: Normierung konsistent mit `reconstruct_xy()` auf gleichen Koordinaten

**Deliverables**
- **Titel:** IBT-Extraktor – TrackRoadGeometry aus IBT-Header
- **Zusammenfassung:** `ibt_track_extractor.py` extrahiert Streckengeometrie einmalig aus IBT-Datei. Output als JSON im Storage. Normierung identisch zur Fahrlinie in `track_geometry.py`.
- **Geänderte Dateien:** `src/core/coaching/ibt_track_extractor.py` (neu), `tests/test_ibt_track_extractor.py` (neu)

---

### Story 3.4 – Telemetrie-Trace (Single Lap, pro Corner)

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

---

### Story 3.5 – Corner Scorecard (Feature-Tabelle)

**Ziel:** Für den gewählten Corner werden die berechneten Features aus `corner_features.parquet` als kompakte Tabelle angezeigt. Die Scorecard bleibt sichtbar, wenn der Corner-Zoom aktiv ist (sie befindet sich im rechten Datenbereich, nicht im Overlay).

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
- Scorecard bleibt sichtbar wenn Corner-Zoom-Overlay aktiv ist

**Deliverables**
- **Titel:** CornerScorecard – Feature-Tabelle mit Schema-Gruppen
- **Zusammenfassung:** `CornerScorecard` zeigt alle Features des gewählten Corners, gruppiert nach `feature_schema_v1.json`. Dritte Spalte als Platzhalter für Sprint 4/6. Bleibt im rechten Datenbereich unabhängig vom Corner-Zoom-Overlay.
- **Geänderte Dateien:** `src/ui/coaching_scorecard.py` (neu), `src/ui/coaching_detail.py` (Scorecard eingehängt)

---

### Story 3.6 – Feature-Heatmap auf TrackMap

**Ziel:** TrackMap-Corners werden farbkodiert basierend auf einem wählbaren Feature-Wert. Heatmap gilt nur für die TrackMap-Ansicht; im Corner-Zoom-Overlay hat sie keinen Effekt.

**Erweiterung in**
- `src/core/coaching/track_geometry.py`
- `src/ui/coaching_detail.py`

**Interface**
```python
def render_trackmap_heatmap(canvas: tk.Canvas,
                             xy: np.ndarray,
                             corners: list[CornerInfo],
                             feature_values: dict[int, float],
                             road_geometry: TrackRoadGeometry | None = None,
                             colormap: str = "RdYlGn_r") -> None
```

**UI-Element**
- Dropdown über der TrackMap: „Heatmap: [Feature auswählen]" + „Aus"
- Feature-Liste aus `feature_schema_v1.json` (nur required Features)
- Colormap: grün (gut) → gelb → rot (schlecht), Normierung min-max über sichtbare Corners
- Legende: Farbbalken unterhalb der Map, min/max-Wert
- Heatmap-Dropdown bleibt sichtbar wenn Corner-Zoom aktiv ist, wird aber erst nach Rückkehr zur TrackMap wieder wirksam

**Akzeptanzkriterien**
- Feature-Dropdown befüllt sich aus Schema
- Corners werden korrekt eingefärbt
- „Aus" stellt Standarddarstellung wieder her
- Straßenband (wenn vorhanden) wird auch im Heatmap-Modus gerendert

**Deliverables**
- **Titel:** TrackMap Heatmap – Corner-Einfärbung nach Feature-Wert
- **Zusammenfassung:** Feature-Heatmap auf der TrackMap. Dropdown-Selektor, Colormap, Legende. `road_geometry`-Parameter an `render_trackmap_heatmap()` ergänzt.
- **Geänderte Dateien:** `src/core/coaching/track_geometry.py` (erweitert), `src/ui/coaching_detail.py` (Dropdown + Heatmap-Toggle)

---

### Story 3.7 – Analyse-Trigger: Klick auf Lap öffnet Detail-View

**Ziel:** Klick auf „Analyze"-Button im CoachingBrowser lädt `LapViewModel` und übergibt ihn an `CoachingDetailView`.

**Änderungen in**
- `src/ui/app.py`

**Flow**
```
User klickt Analyze (Lap-Node)
→ app.py: _handle_analyze_lap()
→ LapViewModel.load(session_dir, run_id, lap_no)
→ coaching_detail_view.load_lap(vm)
→ TrackMap rendert (inkl. Straßenband wenn track_road_geometry vorhanden)
→ Erster Corner automatisch selektiert
→ Corner-Zoom-Overlay initial nicht sichtbar
```

**Akzeptanzkriterien**
- Analyse-Status `computed` → DetailView wird geladen
- Analyse-Status `not_computed` → Button startet Analyse, dann DetailView
- Analyse-Status `blocked` → Fehlermeldung im Detail-Header, kein Crash
- UI friert nicht ein (LapViewModel-Load im Thread, dann UI-Update im Main-Thread)
- Corner-Zoom-Overlay ist nach `load_lap()` initial ausgeblendet

**Deliverables**
- **Titel:** Analyse-Trigger – Lap-Klick öffnet CoachingDetailView
- **Zusammenfassung:** Verdrahtung CoachingBrowser → LapViewModel → CoachingDetailView. Threading für Load, erster Corner auto-selektiert, Corner-Zoom-Overlay initial ausgeblendet.
- **Geänderte Dateien:** `src/ui/app.py` (erweitert)

---

## Sprint 3 – Definition of Done

- [ ] `LapViewModel.load()` funktioniert mit echten Artefakten
- [ ] `LapViewModel` lädt `track_road_geometry.json` wenn vorhanden, kein Crash wenn fehlend
- [ ] TrackMap wird korrekt gerendert und ist klickbar
- [ ] TrackMap zeigt Straßenband wenn `track_road_geometry` verfügbar
- [ ] Corner-Zoom erscheint als Overlay über TrackMap (gleiche Größe/Position)
- [ ] Corner-Zoom zeigt Straßenband wenn `track_road_geometry` verfügbar
- [ ] „← Zurück"-Button im Corner-Zoom stellt TrackMap wieder her
- [ ] Rechter Datenbereich (Scorecard, Trace) bleibt durch Corner-Zoom-Overlay unangetastet
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
| **4** | `LapViewModel.load_batch()`, `CornerTraceView` überlagert mehrere VMs, Scorecard Δ-Spalte, mehrere Fahrlinien in Corner-Zoom |
| **5** | `LapSourceRef` mit `driver_id`, Export/Import via ZIP, Fremd-Session in gleichen View-Widgets |
| **6** | `LapViewModel` → LLM-Prompt-Builder, Scorecard dritte Spalte = LLM-Bewertung |