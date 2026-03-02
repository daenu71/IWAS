# Coaching Sprint 2 – Analysedaten.md

## Ziel des Sprints

Sprint 2 baut **auf Sprint 1 (Daten & Grundlagen)** auf und liefert die **komplette Datenaufbereitung** für spätere LLM-Interpretation – ohne Coaching-Text, ohne Scoring „gut/schlecht“, ohne Vergleiche.

**Sprint 2 liefert:**
- UI: **„Analyse“ Button pro Lap** (Einzelrunde) als Trigger für Datenaufbereitung + Cache-Build
- Eine deterministische **Corner Physics Engine** (Hybrid: Geometrie + Events)
- Pro Lap und pro Corner einen **Feature-Vektor (30–50 Scalars)** + **Mini-Snapshots** (kurze Fensterreihen)
- **CornerType Klassifikation (minimal)** + Roh-Indikatoren
- **Event-Engine** (deterministisch) als separate Schicht (nur extrahieren und speichern)
- **Deterministisches Resampling auf LapDistPct-Grid**
- **Persistenter Cache** (versioniert) + Recompute/Invalidation
- **Feature-Schema / Engine-Versioning** (LLM-tauglicher Contract)
- Minimaler **Data-Quality Gate** (compute möglich/partial/blocked, Gründe)

**Explizit NICHT Teil von Sprint 2:**
- Aggregation für Run/Session/Car (Konstanz/Fortschritt) → *späterer Sprint*
- LLM-Interpretation / Text-Coaching / Empfehlungen
- Slow-vs-Fast Vergleiche / Referenzfahrer-Matching / Best-Lap Auswahl-Logik (außer „welche Lap wird analysiert“)

---

## Scope & Deliverables (konkret)

### Deliverable A — Analyse-UI (Lap-Level)
- In der Coaching Browser Tree-View (Lap-Zeile) erscheint rechts ein Button: **„Analyse“**
- Klick auf „Analyse“:
  1) prüft Data-Quality + Contract
  2) baut/aktualisiert Cache für diese Lap (Resampling → Events → CornerMap-Join → Feature-Vektor)
  3) zeigt rechts im Detailbereich **nur**:
     - Status (computed/partial/blocked)
     - Engine-Version
     - Anzahl Corner-Vektoren
     - Pfad/Artefaktliste
     - optional: einfache Tabelle „CornerID → CornerType → FeatureCount“

> Keine Graphen, keine Bewertungen, keine Interpretationen.

### Deliverable B — CornerMap pro Track/Config (persistiert)
- Einmal pro Track/Config erzeugen:
  - Corner-IDs mit LapDistPct-Start/End
  - CornerType (minimal)
  - Geometrie-Indikatoren (curvature stats etc.)
  - optional: sparsified polyline (xy/z) als Baseline

### Deliverable C — Event-Engine pro Lap (persistiert)
- Deterministische Ereignisse (Times + LapDistPct) extrahieren und speichern:
  - `turn_in`
  - `brake_start`, `peak_brake`, `brake_release_start`, `brake_release_end`
  - `min_speed` (Apex proxy)
  - `throttle_on`, `throttle_full`
  - `gear_change[]`
  - `oversteer_event[]`, `understeer_event[]` (proxy)
  - `crest`, `compression` (proxy)

### Deliverable D — Resampling auf LapDistPct-Grid (persistiert)
- Pro Lap ein resampled Dataset:
  - konstante LapDistPct-Schritte (konfigurierbar)
  - deterministisch reproduzierbar

### Deliverable E — Feature-Vektoren (30–50 Scalars + Snapshots)
- Pro Lap:
  - pro Corner: Feature-Vektor JSON/Parquet
  - optional: Mini-Snapshots (kleine arrays mit 21–51 Samples) für 2–3 Kernsignale

### Deliverable F — Feature-Schema + Engine Versioning
- Ein Feature-Registry/Schema (maschinenlesbar) mit:
  - Feature-ID, Einheit, Beschreibung
  - benötigte Kanäle
  - Berechnungsfenster
  - Output-Typ (scalar vs snapshot)

### Deliverable G — Cache/Invalidation
- Cache muss „stale“ werden, wenn:
  - Engine-Version steigt
  - Schema-Hash sich ändert
  - Contract-Hash sich ändert (IRSDK Channel Layout)
  - CornerMap-Version steigt (Track-Map neu gerechnet)

---

## Architektur-Entwurf (Sprint 2)

### 1) Datenfluss (Lap Analyse)

1. **Load Lap Raw** (Sprint 1 Storage)
2. **Contract Check** (Pflichtkanäle, Units)
3. **Quality Gate** (Coverage, no-flatline, min samples)
4. **Resample** auf LapDistPct-Grid → `lap_resampled`
5. **CornerMap** laden/erzeugen für Track/Config
6. **Event-Engine** auf `lap_resampled` → `lap_events`
7. **Corner Join** (CornerID pro Sample)
8. **Feature Extraction** (Corner-wise windows) → Feature-Vektor + Snapshots
9. **Persist Cache** (feature files + meta)
10. UI zeigt Status + Artefakte

### 2) Module (neu/erweitert)

**Neu (vorgeschlagen):**
- `src/core/coaching/analysis_contract.py`
- `src/core/coaching/resample_lapdist.py` (oder reuse/alias aus bestehendem resample-Modul, wenn vorhanden)
- `src/core/coaching/corner_map.py`
- `src/core/coaching/event_engine.py`
- `src/core/coaching/feature_engine.py`
- `src/core/coaching/feature_schema.py`
- `src/core/coaching/analysis_cache.py`
- `src/ui/coaching_browser.py` (Button + status)
- `src/ui/coaching_details.py` (oder vorhandener Detailbereich erweitern)

> Ziel: Analyse-Pipeline bleibt **komplett getrennt** von Video-Render-Pipeline.

---

## Datenverträge & Speicherformate

### A) Analysis Contract (Pflicht/Optional)

**Pflichtkanäle (Minimum für Sprint-2 Thesen/Features):**
- Position/Progress: `LapDistPct`, `LapDist`, `SessionTime`
- Kinematik: `Speed`, `YawRate`, `LatAccel`, `LongAccel`, `VertAccel`
- Inputs: `Throttle`, `Brake`, `SteeringWheelAngle`
- Powertrain: `Gear`, `RPM`
- Meta: Track/Config/Car aus SessionInfo/Meta (Sprint 1)

**Optional (wenn vorhanden, dann Features ergänzen; wenn nicht, Feature = na):**
- `SteeringWheelTorque`, `SteeringWheelPctTorque` (Light Hands)
- Tire: `LFTireTemp*`, `RFTireTemp*`, `LRTireTemp*`, `RRTireTemp*`, Pressures
- Suspension: `*shockDefl`, `*rideHeight`
- ABS/TC: `ABSactive`, `TractionControlActive`

**Units Normalization:**
- Speed: m/s intern, optional km/h nur für UI/Export
- Angles: rad intern, optional deg
- Accel: m/s² intern, optional „g“ als derived

Contract Output:
- `analysis_contract.json` (global) + `contract_hash` in jedem Cache Artefakt

### B) Resampling

- Konfig:
  - `analysis_lapdist_step = 0.0005` (Default; als ini key)
- Output:
  - `lap_resampled.parquet` pro Lap
  - enthält alle relevanten Channels resampled auf Grid
- Determinismus:
  - LapDistPct unwrap (wrap 1→0) muss robust sein
  - Resampling Methode: linear für float; nearest für int/bool (Gear, flags)

### C) CornerMap (pro Track/Config)

- Key:
  - `track_key = TrackDisplayName + "__" + TrackConfigName` (oder gleichwertig aus Sprint 1 Meta)
- Output:
  - `corner_map_v1.json` (oder parquet)
- Inhalte:
  - `corner_id`
  - `start_lapdist_pct`, `end_lapdist_pct`
  - `corner_type` (minimal)
  - `curvature_peak`, `curvature_mean`, `radius_est`
  - `curvature_trend` (inc/dec/const)
  - `elevation_slope_stats` (min/max dZ/dS) (wenn Z verfügbar)
  - `vertical_signature` (min/max/percentiles VertAccel) (aus baseline lap oder aggregated)

### D) Events (pro Lap)

- Output:
  - `lap_events.json` (oder parquet)
- Jeder Event:
  - `name`
  - `lapdist_pct`
  - `session_time`
  - optional: `value` (z. B. gear from→to)

### E) Feature-Vektoren (pro Lap / pro Corner)

- Output:
  - `corner_features.parquet` (preferred) oder `corner_features.jsonl`
- Pro Row:
  - identifiers: `run_id`, `lap_id`, `corner_id`, `track_key`, `car_key`, timestamps
  - meta: `engine_version`, `schema_hash`, `contract_hash`, `corner_map_version`
  - features: 30–50 Scalars
  - snapshots: optional arrays (compressed) oder separate file per corner

**Snapshots (Scalars+Snapshots Entscheidung):**
- max 3 snapshots pro corner, jeweils 21–51 samples:
  - `brake_entry_window[]`
  - `yawrate_entry_window[]`
  - `gripusage_window[]` (derived)

---

## Feature-Set v1.0 (abgeleitet aus 12 Thesen – nur Daten, kein Urteil)

> Wichtig: Sprint 2 berechnet Features + speichert sie.  
> Keine Schwellenwerte „gut/schlecht“ – nur Rohwerte + Derived Indizes.

### Core Derived Channels (für Features)
- `grip_usage = sqrt(lat_accel^2 + long_accel^2)` (proxy)
- `ideal_yawrate = speed / radius_est` (radius aus curvature/xy)
- `oversteer_index = yawrate - ideal_yawrate` (proxy)
- `understeer_index = ideal_yawrate - yawrate` (proxy)
- `input_jerk_brake = d2(brake)/dt2` (smoothness proxy)
- `input_jerk_throttle = d2(throttle)/dt2`

### Feature-Gruppen (Beispiele, v1)
1) **Rotation via Load (These: Rotation nicht via Lenkwinkel)**
   - `rot_efficiency_load = d(yawrate)/d(|long_accel|)` (entry window)
   - `rot_dependency_steer = d(yawrate)/d(|steer|)` (entry window)
   - `yawrate_rise_before_steer` (bool/ratio: yawrate steigt bevor steer steigt)

2) **Friction Ellipse / Limit**
   - `grip_usage_p95`, `grip_usage_max`
   - `grip_usage_std` (corner window)
   - `yawrate_spike_count` (proxy instability)
   - `abs_active_ratio` (optional)

3) **Trail Transition (Long→Lat)**
   - `corr_long_lat` (entry)
   - `brake_release_slope`
   - `lataccel_ramp_slope`
   - `yawrate_stability_std`

4) **Downshift Stability (GearChange unter Querlast)**
   - `gear_change_count_entry`
   - `lataccel_at_gear_change_p95`
   - `yawrate_var_post_shift`
   - `rpm_delta_on_shift` (if calculable)

5) **Light Hands / Feedback Nutzung (Torque optional)**
   - `torque_response_latency_ms` (event-based, optional)
   - `angle_correction_latency_ms` (optional)
   - `torque_peak_during_correction` (optional)

6) **Crest / Compression**
   - `vertaccel_min`, `vertaccel_max`
   - `decel_efficiency_vs_vertload` (requires brake/longaccel)
   - `crest_present` / `compression_present` (bool)

7) **Thermal / Tire Response (optional)**
   - `front_tiretemp_delta_post_brake`
   - `tiretemp_gradient_entry`
   - `midcorner_lataccel_vs_temp`

8) **Compound Strategy Context (needs curvature profile)**
   - `curvature_peaks_count`
   - `phase1_grip_usage_mean`
   - `phase2_exit_speed` (sample at +X lapdist after apex)

> Finaler Feature-Katalog wird in `feature_schema_v1.json` festgezurrt.

---

## UI-Design (Sprint 2)

### Coaching Browser Tree (Screenshot-Kontext)
- Für jede Lap-Zeile:
  - rechts ein „Analyse“-Button
  - daneben ein kleiner Status (Text oder Icon):
    - `—` (not computed)
    - `v1` (computed)
    - `v1*` (partial)
    - `!` (blocked)

### Detailbereich (rechts)
Wenn Lap selektiert:
- Lap Meta (Zeit, offtrack/incomplete aus Sprint 1)
- Analyse Status:
  - computed/partial/blocked
  - engine_version, schema_hash, contract_hash
  - Artefakt-Pfade
  - CornerCount, FeatureCount

**Wichtig:** Kein Chart-Rendering in Sprint 2.

---

## Cache- & Versioning-Strategie

### Engine Version
- `analysis_engine_version = "0.1.0"` in Code (single source)
- In jedem Artefakt gespeichert.

### Hashes
- `schema_hash` = Hash von `feature_schema_v1.json`
- `contract_hash` = Hash von `analysis_contract.json` + IRSDK var headers
- `corner_map_version` (int) + optional hash

### Invalidation
Cache gilt als **stale**, wenn:
- engine_version != artefakt.engine_version
- schema_hash != artefakt.schema_hash
- contract_hash != artefakt.contract_hash
- corner_map_version != artefakt.corner_map_version

### Partial Results
Wenn Optionalkanäle fehlen:
- Artefakt wird erstellt mit `partial=true`
- Features, die Kanäle benötigen, werden `na`
- `missing_channels[]` wird in meta gespeichert

---

## Data Quality Gates (Minimal)

### Gate 1 — Coverage
- LapDistPct Coverage: mind. 95% (konfigurierbar)
- Mindestanzahl Samples nach Resampling > N

### Gate 2 — Flatline Detection
- YawRate/Accel/Speed dürfen nicht über lange Zeit konstant sein (indikativ für Recording/Channel)
- Wenn erkannt: blocked mit reason

### Gate 3 — Lap Validity
- Sprint 1 Flags (incomplete/offtrack) werden nicht als Blocker genutzt – aber als Meta gespeichert.
- Blocker nur bei technisch unbrauchbaren Daten.

Output:
- `analysis_status.json` pro Lap: `can_compute`, `partial`, `reasons[]`

---

## Arbeitsstruktur & Stories (Sprint 2)

### Phase 0 — Definitions & Contracts
**Story 2.0.1 — Analysis Contract v1**
- Deliverable: `analysis_contract.json` + contract_hash logic
- AC:
  - Pflicht/Optional Kanäle definiert
  - Units normalization dokumentiert
  - Missing channels → partial statt crash

**Story 2.0.2 — Feature Schema v1**
- Deliverable: `feature_schema_v1.json`
- AC:
  - 30–50 Scalar features gelistet (IDs, units, requires)
  - 2–3 snapshots definiert (window, sample count)

### Phase 1 — Resampling Layer
**Story 2.1.1 — Deterministic LapDistPct Resampler**
- Deliverable: `lap_resampled.parquet` pro Lap + tests
- AC:
  - reproduzierbar (same input → same output)
  - nearest/linear Regeln umgesetzt
  - wrap-around robust

### Phase 2 — CornerMap Engine (Hybrid Baseline)
**Story 2.2.1 — Track CornerMap Builder**
- Inputs: eine „baseline lap“ pro Track/Config (z. B. erste vollständige Lap)
- Output: `corner_map_v1.json`
- AC:
  - CornerIDs stabil zwischen Sessions
  - CornerType minimal (hairpin/medium/sweeper/chicane/compound)
  - curvature + radius stats gespeichert

### Phase 3 — Event Engine
**Story 2.3.1 — Deterministic Lap Event Extraction**
- Output: `lap_events.json`
- AC:
  - events in LapDistPct + SessionTime
  - alle event types aus Liste vorhanden (oder explizit na)
  - deterministisch (keine random thresholds ohne Versionierung)

### Phase 4 — Feature Engine
**Story 2.4.1 — Corner Feature Extraction v1**
- Output: `corner_features.parquet` + snapshots
- AC:
  - pro Corner: 30–50 Scalars
  - snapshots optional, wenn Daten verfügbar
  - partial handling sauber

### Phase 5 — Cache & UI Trigger
**Story 2.5.1 — Analysis Cache Manager**
- AC:
  - stale detection korrekt
  - rebuild on-demand
  - writes meta status files

**Story 2.5.2 — UI: Lap Analyse Button + Status**
- AC:
  - Button pro Lap vorhanden
  - Klick triggert cache build
  - Detailbereich zeigt Status + Artefakte

---

## Tests & Qualität

### Unit Tests (Pflicht)
- Resampler determinism
- CornerMap reproducibility
- Event extraction stability
- Feature engine outputs schema-conform
- Cache invalidation

### Integration Tests (Pflicht)
- Auf einem echten Run-Ordner aus Sprint 1:
  - Lap Analyse Button baut Artefakte
  - UI bleibt responsive (keine UI-freeze; ggf. background thread + progress state)

### Abbruchkriterien (Stop-Regel)
Sprint 2 ist **nicht erfolgreich**, wenn:
- CornerIDs nicht stabil sind (CornerMap driftet)
- Resampling nicht deterministisch ist
- Feature-Cache nicht versioniert invalidiert wird
- UI Analyse Button „arbeitet“, aber keine reproduzierbaren Artefakte entstehen

---

## Geplante Artefakt-Ordnerstruktur (unter Coaching Storage)

Beispielpfade (konzeptuell; final an Sprint-1 Storage-Layout anpassen):

- `<run>/laps/<lap_id>/raw/…` (Sprint 1)
- `<run>/laps/<lap_id>/analysis/`
  - `analysis_status.json`
  - `lap_resampled.parquet`
  - `lap_events.json`
  - `corner_features.parquet`
  - `snapshots/` (optional)
- `<track_key>/corner_map/`
  - `corner_map_v1.json`

---

## Was Sprint 3 (später) dann übernimmt

- Aggregation:
  - Run/Session Konstanz (mean/std/percentiles der Features)
  - Car Progress (Trend über Zeit)
- Vergleich:
  - vs Best Lap, vs Referenzfahrer, vs Session optimal
- LLM:
  - Interpretation aus Feature-Vektoren + Context (CornerType, Crest etc.)
  - Text/Coaching-Ausgabe + Priorisierung

---

## Offene Entscheidungen (müssen vor Implementierung fixiert werden)

1) `analysis_lapdist_step` Default (0.0005 vs 0.001)  
2) Baseline Lap Wahl für CornerMap (erste vollständige? best valid lap?)  
3) CornerType-Kategorien (finale Liste)  
4) Event thresholds (BrakeStart/ThrottleOn etc.) als versionierte Konfig  
5) Snapshot windows: 21 vs 51 samples (Tradeoff UI/LLM)

> Diese Entscheidungen werden in Sprint 2 einmalig festgezurrt und versioniert, sonst werden Caches nie stabil.

---