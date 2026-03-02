# Coaching Sprint 2 – ClaudeCode Stories

> **Ziel:** Deterministische Datenaufbereitung pro Lap für spätere LLM-Interpretation.
> Kein Coaching-Text, kein Scoring, kein Vergleich.
> Jede Story ist eigenständig und in ClaudeCode ausführbar.

---

## Abhängigkeiten & Reihenfolge

```
Story 2.0.1  →  Story 2.0.2          (Contracts zuerst)
Story 2.1.1                           (Resampling unabhängig)
Story 2.2.1                           (CornerMap unabhängig, braucht echte Parquet-Daten)
Story 2.3.1  →  braucht 2.1.1        (Events auf resampleten Daten)
Story 2.4.1  →  braucht 2.1.1, 2.2.1, 2.3.1
Story 2.5.1  →  braucht 2.4.1        (Cache Manager)
Story 2.5.2  →  braucht 2.5.1        (UI zuletzt)
```

---

## Offene Entscheidungen (vor Start fixieren)

Diese Werte müssen vor der ersten Story festgelegt werden.
Sie werden als Konstanten in den jeweiligen Modulen verwendet.

| ID | Frage | Entscheidung |
|----|-------|--------------|
| OD-1 | `analysis_lapdist_step` | `0.0005` |
| OD-2 | Baseline Lap für CornerMap | beste valide Lap |
| OD-3 | Snapshot window sample count | `21` |
| OD-4 | 4D Balance Normalisierung | z-score per Kanal per Lap |
| OD-5 | CornerType-Kategorien | `hairpin / medium / sweeper / chicane / compound / unknown` |
| OD-6 | Event Thresholds | in `event_config_v1.json` versioniert |

---

---

# Story 2.0.1 — Analysis Contract v1

## Ziel
Definiere welche IRSDK-Kanäle für Sprint-2-Analysen Pflicht sind, welche optional, und wie Units normalisiert werden. Das Ergebnis ist eine maschinenlesbare Datei `analysis_contract.json` + Python-Modul das diesen Contract prüft.

## Neue Dateien
- `src/core/coaching/analysis_contract.py`
- `config/coaching/analysis_contract.json`

## Keine Änderungen an
- Bestehender Recorder-Logik
- `lap_segmenter.py`, `indexer.py`, `storage.py`

## Akzeptanzkriterien
- `analysis_contract.json` existiert mit Pflicht-/Optional-Kanal-Listen und Unit-Definitionen
- `AnalysisContract.check(channels: list[str]) -> ContractCheckResult` gibt zurück:
  - `can_compute: bool`
  - `missing_required: list[str]`
  - `missing_optional: list[str]`
  - `contract_hash: str` (SHA256 der JSON-Datei)
- Bei fehlendem Pflichtkanal: `can_compute = False`
- Bei fehlendem Optionalkanal: `can_compute = True`, Feature wird später `null`
- `python -m py_compile src/core/coaching/analysis_contract.py` fehlerfrei

## Pflichtkanäle (hardcoded in JSON)
```
LapDistPct, LapDist, SessionTime,
Speed, YawRate, LatAccel, LongAccel, VertAccel,
Throttle, Brake, SteeringWheelAngle,
Gear, RPM
```

## Optionale Kanäle
```
SteeringWheelTorque, SteeringWheelPctTorque,
LFTireTempL, LFTireTempM, LFTireTempR,
RFTireTempL, RFTireTempM, RFTireTempR,
ABSactive, TractionControl
```

## Unit-Normalisierung (in JSON dokumentiert)
- Speed: m/s intern
- Angles: rad intern
- Accel: m/s² intern

## Deliverables
**Titel der Änderung:** Analysis Contract v1 – Kanal-Vertrag und Hash-Logik

**Zusammenfassung:**
- `analysis_contract.json` mit Pflicht/Optional/Units angelegt
- `AnalysisContract` Klasse mit `check()` und `contract_hash` Property
- Kein Crash bei fehlenden Kanälen, nur `can_compute=False`

**Geänderte Dateien:**
- `src/core/coaching/analysis_contract.py` (neu)
- `config/coaching/analysis_contract.json` (neu)

---

---

# Story 2.0.2 — Feature Schema v1 als Python-Vertrag

## Ziel
Das `feature_schema_v1.json` (bereits erstellt) wird als Python-Klasse `FeatureSchema` zugänglich gemacht. Jedes Feature hat eine ID, Unit, required_channels und output_type. Das Schema erzeugt seinen eigenen Hash für Cache-Invalidierung.

## Neue Dateien
- `src/core/coaching/feature_schema.py`

## Voraussetzung
- `feature_schema_v1.json` liegt in `config/coaching/feature_schema_v1.json`

## Akzeptanzkriterien
- `FeatureSchema.load() -> FeatureSchema` liest die JSON-Datei
- `FeatureSchema.schema_hash: str` gibt SHA256 des JSON zurück
- `FeatureSchema.features_for_group(group_id: str) -> list[FeatureDefinition]`
- `FeatureSchema.required_channels_for_feature(feature_id: str) -> list[str]`
- `FeatureDefinition` hat Felder: `id`, `label`, `unit`, `output_type`, `requires`, `requires_optional`, `low_confidence`
- `python -m py_compile src/core/coaching/feature_schema.py` fehlerfrei

## Keine Business-Logik
Dieses Modul liest nur — keine Berechnung, kein Feature-Extraction.

## Deliverables
**Titel der Änderung:** Feature Schema v1 – Python-Wrapper und Hash-Vertrag

**Zusammenfassung:**
- `FeatureSchema` Klasse mit Laden, Hash, Feature-Lookup
- Kein direkter Bezug zur Berechnung – nur Schema-Definition

**Geänderte Dateien:**
- `src/core/coaching/feature_schema.py` (neu)
- `config/coaching/feature_schema_v1.json` (kopiert/angelegt)

---

---

# Story 2.1.1 — Deterministischer LapDistPct Resampler

## Ziel
Pro Lap wird ein resampled Parquet-File erzeugt: alle relevanten Kanäle auf ein gleichmäßiges `LapDistPct`-Grid interpoliert. Das Ergebnis ist deterministisch (gleicher Input → gleicher Output).

## Neue Dateien
- `src/core/coaching/resample_lapdist.py`

## Liest
- `run_XXXX.parquet` aus Sprint-1-Storage
- Lap-Segmente aus `run_XXXX_meta.json` (start/end sample index)

## Schreibt
- `<session>/<run>/laps/<lap_id>/analysis/lap_resampled.parquet`

## Akzeptanzkriterien
- Grid-Step: `0.0005` (konfigurierbar via `analysis_lapdist_step` in `defaults.ini`)
- Float-Kanäle: lineare Interpolation
- Int/Bool-Kanäle (Gear, ABSactive): nearest-neighbor
- Wrap-around robust: LapDistPct 0.99 → 0.01 wird korrekt behandelt (unwrap vor Resampling)
- Gleicher Input → identisches Parquet (Byte-für-Byte reproduzierbar)
- Bei LapDistPct-Coverage < 95%: Datei wird erstellt, `coverage_pct` in Meta gespeichert
- `python -m py_compile src/core/coaching/resample_lapdist.py` fehlerfrei

## Unit Test (Pflicht)
- `tests/test_resample_lapdist.py`
- Test 1: Synthetic data mit bekanntem Grid → Output prüfen
- Test 2: Wrap-around Test (0.99 → 0.01 in Rohdaten)
- Test 3: Determinismus-Test (zwei Aufrufe → identisches Ergebnis)

## Keine Analyse-Logik
Nur Resampling. Kein Feature-Extraction, keine Events.

## Deliverables
**Titel der Änderung:** LapDistPct Resampler – deterministisches Grid-Resampling pro Lap

**Zusammenfassung:**
- `resample_lapdist.py` mit `resample_lap()` Funktion
- Wrap-around-robustes LapDistPct-Handling
- Unit Tests für Determinismus und Wrap

**Geänderte Dateien:**
- `src/core/coaching/resample_lapdist.py` (neu)
- `tests/test_resample_lapdist.py` (neu)
- `config/defaults.ini` (neuer Key `analysis_lapdist_step = 0.0005`)

---

---

# Story 2.2.1 — Track CornerMap Builder

## Ziel
Pro Track/Config wird einmalig eine `corner_map_v1.json` erzeugt. Sie enthält alle Kurven mit LapDistPct-Grenzen, Kurventyp und Geometrie-Kennwerten. Die CornerMap ist die Grundlage für alle kornerweisen Feature-Berechnungen.

## Neue Dateien
- `src/core/coaching/corner_map.py`

## Schreibt
- `<storage_root>/corner_maps/<track_key>/corner_map_v1.json`

## Track-Key Format
`TrackDisplayName__TrackConfigName` (aus `session_meta.json`, analog Sprint-1-Storage)

## Inputs
- Eine "Baseline Lap": beste valide Lap aus dem ersten verfügbaren Run für diesen Track/Config
- Resampled Parquet dieser Lap (`lap_resampled.parquet` aus Story 2.1.1)

## Algorithmus (Hybrid-Ansatz)
1. Krümmung berechnen aus XY-Position (VelocityX/VelocityY Integration oder falls verfügbar direkte XY)
2. Kurven-Segmentierung: Bereiche mit Krümmung > Schwelle = Kurve
3. CornerID: stabile Integer-IDs, sortiert nach LapDistPct-Auftreten
4. CornerType klassifizieren:
   - `hairpin`: radius_est < 30m
   - `medium`: 30m–80m
   - `sweeper`: 80m–200m
   - `chicane`: zwei Peaks entgegengesetzter Krümmung < 0.05 LapDistPct Abstand
   - `compound`: zwei gleichsinnige Peaks > 0.02 LapDistPct Abstand
   - `unknown`: wenn Klassifikation nicht eindeutig

## Pro Corner in JSON
```json
{
  "corner_id": 1,
  "start_lapdist_pct": 0.123,
  "end_lapdist_pct": 0.145,
  "corner_type": "medium",
  "radius_est": 62.4,
  "curvature_peak": 0.016,
  "curvature_mean": 0.011,
  "curvature_trend": "inc",
  "crest_present": false,
  "compression_present": true,
  "corner_map_version": 1
}
```

## Akzeptanzkriterien
- CornerIDs sind stabil: gleiche Baseline Lap → gleiche IDs
- Wenn neue Baseline Lap gewählt wird: Version steigt (`corner_map_version += 1`)
- Kein Crash wenn Z-Achse fehlt (`crest_present` / `compression_present` = `null`)
- `python -m py_compile src/core/coaching/corner_map.py` fehlerfrei

## Unit Test (Pflicht)
- `tests/test_corner_map.py`
- Test 1: Synthetic circular track → 0 corners (straight)
- Test 2: Synthetic single corner → 1 corner, radius plausibel
- Test 3: Reproducibility (gleiche Input → gleiche IDs)

## Deliverables
**Titel der Änderung:** CornerMap Builder v1 – Track-Segmentierung und Kurvenklassifikation

**Zusammenfassung:**
- `corner_map.py` mit `build_corner_map()` und `load_corner_map()`
- Stabile CornerIDs, versioniert
- CornerType-Klassifikation mit 5 Typen + unknown

**Geänderte Dateien:**
- `src/core/coaching/corner_map.py` (neu)
- `tests/test_corner_map.py` (neu)

---

---

# Story 2.3.1 — Deterministische Event Engine pro Lap

## Ziel
Pro Lap werden deterministische Fahrereignisse extrahiert und als `lap_events.json` gespeichert. Alle Schwellenwerte sind versioniert konfigurierbar — keine hardcodierten Magic Numbers.

## Neue Dateien
- `src/core/coaching/event_engine.py`
- `config/coaching/event_config_v1.json`

## Liest
- `lap_resampled.parquet` (aus Story 2.1.1)

## Schreibt
- `<session>/<run>/laps/<lap_id>/analysis/lap_events.json`

## Event-Typen (alle oder explizit `null`)
```
turn_in           – SteeringWheelAngle überschreitet Threshold (erste Einlenkung)
brake_start       – Brake > threshold_brake_start
peak_brake        – max(Brake) in Bremszone
brake_release_start – Brake fällt unter threshold nach Peak
brake_release_end – Brake < threshold_brake_off
min_speed         – min(Speed) im Kurvenbereich (Apex-Proxy)
throttle_on       – Throttle > threshold_throttle_on
throttle_full     – Throttle > threshold_throttle_full
gear_change[]     – Array: jeder Gear-Wechsel mit from/to
oversteer_event[] – Array: YawRate > threshold_oversteer für > N samples
understeer_event[]– Array: (ideal_yawrate - yawrate) > threshold (wenn CornerMap verfügbar)
crest[]           – VertAccel < threshold_crest
compression[]     – VertAccel > threshold_compression
```

## Event-Format (pro Event)
```json
{
  "name": "brake_start",
  "lapdist_pct": 0.1234,
  "session_time": 45.123,
  "value": null
}
```

## `event_config_v1.json` (versioniert)
```json
{
  "version": 1,
  "threshold_brake_start": 0.05,
  "threshold_throttle_on": 0.05,
  "threshold_throttle_full": 0.95,
  "threshold_yawrate_spike": 0.5,
  "threshold_oversteer_yawrate": 0.3,
  "threshold_crest": -2.0,
  "threshold_compression": 3.0
}
```

## Akzeptanzkriterien
- Jeder Event-Typ ist im Output vorhanden (entweder mit Wert oder als leere Liste / `null`)
- Deterministisch: gleicher Input → gleiche Events
- Bei fehlendem Kanal: Event wird `null` mit `missing_channel` im Meta
- Config-Version wird in `lap_events.json` meta gespeichert
- `python -m py_compile src/core/coaching/event_engine.py` fehlerfrei

## Unit Test (Pflicht)
- `tests/test_event_engine.py`
- Test 1: Synthetic lap mit klarem Bremsvorgang → `brake_start`, `peak_brake`, `brake_release_end` vorhanden
- Test 2: Synthetic lap ohne Bremsen → alle Brems-Events `null`
- Test 3: Determinismus

## Deliverables
**Titel der Änderung:** Event Engine v1 – deterministisches Lap-Event-Extraction

**Zusammenfassung:**
- `event_engine.py` mit `extract_lap_events()` Funktion
- `event_config_v1.json` als versionierter Schwellenwert-Vertrag
- Alle Events als `null` wenn Kanal fehlt

**Geänderte Dateien:**
- `src/core/coaching/event_engine.py` (neu)
- `config/coaching/event_config_v1.json` (neu)
- `tests/test_event_engine.py` (neu)

---

---

# Story 2.4.1 — Corner Feature Extraction v1

## Ziel
Pro Lap und pro Corner werden 30–50 Scalar-Features berechnet und als `corner_features.parquet` gespeichert. Features basieren direkt auf `feature_schema_v1.json`. Optionale Features werden `null` wenn Kanal fehlt — kein Crash.

## Neue Dateien
- `src/core/coaching/feature_engine.py`

## Liest
- `lap_resampled.parquet` (Story 2.1.1)
- `lap_events.json` (Story 2.3.1)
- `corner_map_v1.json` (Story 2.2.1)
- `feature_schema_v1.json` (Story 2.0.2)
- `analysis_contract.json` (Story 2.0.1)

## Schreibt
- `<session>/<run>/laps/<lap_id>/analysis/corner_features.parquet`
- `<session>/<run>/laps/<lap_id>/analysis/snapshots/` (optional, 3 Snapshots pro Corner)

## Pro Corner (eine Zeile in Parquet)
**Identifier-Spalten:**
- `run_id`, `lap_id`, `corner_id`, `track_key`, `car_key`
- `engine_version`, `schema_hash`, `contract_hash`, `corner_map_version`
- `corner_type`, `corner_radius_est`, `crest_present_map`, `compression_present_map`
- `lap_validity_flag` (aus Sprint-1-Meta)

**Feature-Spalten (Auswahl, gemäß `feature_schema_v1.json`):**

*Gruppe rotation_via_load:*
- `rot_efficiency_load`, `rot_dependency_steer`, `yawrate_rise_before_steer`

*Gruppe friction_ellipse:*
- `grip_usage_p95`, `grip_usage_max`, `grip_usage_std`, `yawrate_spike_count`
- `abs_active_ratio` (null wenn ABSactive fehlt)

*Gruppe trail_braking:*
- `corr_long_lat`, `brake_release_slope`, `lataccel_ramp_slope`, `yawrate_stability_std_entry`

*Gruppe downshift_stability:*
- `gear_change_count_entry`, `lataccel_at_gear_change_p95`, `yawrate_var_post_shift`, `rpm_delta_on_shift`

*Gruppe light_hands (alle optional):*
- `torque_response_latency_ms`, `angle_correction_latency_ms`, `torque_peak_during_correction`

*Gruppe vertical_dynamics:*
- `vertaccel_min`, `vertaccel_max`, `crest_present`, `compression_present`, `decel_efficiency_vs_vertload`

*Gruppe limit_consistency:*
- `grip_usage_consistency_std`, `grip_usage_mean`

*Gruppe exit_timing:*
- `throttle_onset_vs_yawrate_peak`, `yawrate_at_throttle_on`, `throttle_progression_slope`

*Gruppe compound_strategy (nur wenn corner_type == compound):*
- `phase1_grip_usage_mean`, `phase2_exit_speed`, `curvature_peaks_count`

*Gruppe dynamic_balance:*
- `balance_4d_variance`, `balance_4d_variance_peak_lapdist`

## Snapshots (optional, wenn Kanäle vorhanden)
- `brake_entry_window`: 21 Samples, Kanäle Brake + LongAccel
- `yawrate_entry_window`: 21 Samples, Kanäle YawRate + SteeringWheelAngle
- `gripusage_window`: 21 Samples, Kanal grip_usage (derived)

Snapshots als `npy` oder komprimiertes Array in `snapshots/<corner_id>_<snapshot_id>.json`

## Akzeptanzkriterien
- Für jeden Corner in der CornerMap existiert genau eine Zeile im Output
- Fehlende Kanäle → Feature `null`, kein Crash
- `low_confidence`-Features bekommen zusätzliche Spalte `<feature_id>_confidence = "low"` im Meta
- Output ist schema-konform (alle Feature-IDs aus `feature_schema_v1.json` vorhanden)
- `python -m py_compile src/core/coaching/feature_engine.py` fehlerfrei

## Unit Test (Pflicht)
- `tests/test_feature_engine.py`
- Test 1: Alle Required-Features haben nicht-null Wert bei vollständigem Input
- Test 2: Optional-Features sind `null` wenn Kanal fehlt (kein Crash)
- Test 3: Output-Schema stimmt mit `feature_schema_v1.json` überein

## Deliverables
**Titel der Änderung:** Feature Engine v1 – Corner-wise Feature Extraction für LLM-Vorbereitung

**Zusammenfassung:**
- `feature_engine.py` mit `extract_corner_features()` Funktion
- Alle 11 Feature-Gruppen implementiert
- Partial-Handling: null statt Crash bei fehlenden Kanälen

**Geänderte Dateien:**
- `src/core/coaching/feature_engine.py` (neu)
- `tests/test_feature_engine.py` (neu)

---

---

# Story 2.5.1 — Analysis Cache Manager

## Ziel
Der Cache Manager orchestriert die gesamte Analyse-Pipeline für eine Lap. Er prüft ob der Cache aktuell ist (stale detection), triggert Recompute wenn nötig, und schreibt `analysis_status.json`.

## Neue Dateien
- `src/core/coaching/analysis_cache.py`

## Schreibt / Verwaltet
- `<session>/<run>/laps/<lap_id>/analysis/analysis_status.json`
- Alle Analyse-Artefakte (via Aufruf der anderen Module)

## `analysis_status.json` Format
```json
{
  "status": "computed",
  "engine_version": "0.1.0",
  "schema_hash": "abc123...",
  "contract_hash": "def456...",
  "corner_map_version": 1,
  "corner_count": 12,
  "feature_count": 38,
  "partial": false,
  "missing_channels": [],
  "reasons": [],
  "artifacts": [
    "lap_resampled.parquet",
    "lap_events.json",
    "corner_features.parquet"
  ],
  "computed_at": "2026-03-02T10:00:00"
}
```

## Status-Werte
- `computed` – alles OK
- `partial` – läuft, aber optionale Kanäle fehlen
- `blocked` – technisch unbrauchbar (Coverage < 95%, Flatline-Detection, o.ä.)
- `stale` – Cache veraltet (Version/Hash mismatch)
- `not_computed` – noch nie analysiert

## Stale Detection (Cache ungültig wenn)
- `engine_version` != aktuell
- `schema_hash` != aktuell
- `contract_hash` != aktuell
- `corner_map_version` != aktuell

## Data Quality Gates
- **Gate 1 Coverage:** LapDistPct-Coverage nach Resampling < 95% → `blocked`
- **Gate 2 Flatline:** YawRate/LatAccel/Speed für > 5s konstant → `blocked` mit Reason
- **Gate 3 Lap Validity:** `incomplete`/`offtrack` aus Sprint-1 → nur als Meta, kein Blocker

## Pipeline-Aufruf (bei Recompute)
```
1. Contract Check        (analysis_contract.py)
2. Resample              (resample_lapdist.py)
3. Quality Gate prüfen
4. CornerMap laden/bauen (corner_map.py)
5. Event Extraction      (event_engine.py)
6. Feature Extraction    (feature_engine.py)
7. Status schreiben
```

## Akzeptanzkriterien
- `AnalysisCache.is_stale(lap_path) -> bool` korrekt
- `AnalysisCache.compute(lap_path)` führt Pipeline durch
- Partial-Results werden korrekt gespeichert (`partial=true`, `missing_channels` gefüllt)
- `python -m py_compile src/core/coaching/analysis_cache.py` fehlerfrei

## Unit Test (Pflicht)
- `tests/test_analysis_cache.py`
- Test 1: Stale detection bei verändertem `engine_version`
- Test 2: Stale detection bei verändertem `schema_hash`
- Test 3: `blocked` bei Flatline-Daten

## Deliverables
**Titel der Änderung:** Analysis Cache Manager – Pipeline-Orchestration und Stale Detection

**Zusammenfassung:**
- `analysis_cache.py` mit vollständiger Pipeline-Orchestration
- `analysis_status.json` mit allen Hash/Version-Informationen
- 3 Data Quality Gates implementiert

**Geänderte Dateien:**
- `src/core/coaching/analysis_cache.py` (neu)
- `tests/test_analysis_cache.py` (neu)

---

---

# Story 2.5.2 — UI: Analyse-Button pro Lap + Status-Anzeige

## Ziel
Im Coaching Browser (existierender `CoachingBrowser` in `src/ui/coaching_browser.py`) erhält jede Lap-Zeile einen „Analyse"-Button. Klick triggert den Cache Manager. Der Detailbereich zeigt den Analyse-Status ohne Charts, ohne Bewertungen.

## Geänderte Dateien
- `src/ui/coaching_browser.py`
- `src/ui/app.py` (Detailbereich / rechte Spalte)

## Keine neuen Dateien
Nur Erweiterung bestehender UI-Module.

## UI: Analyse-Button in Lap-Zeile

**In `CoachingBrowser`:**
- Für Nodes mit `kind == "lap"`: rechts in der Zeile ein Button „Analyse"
- Daneben ein Status-Text (klein):
  - `—` nicht berechnet
  - `v1` berechnet (engine_version)
  - `v1*` partial
  - `!` blocked
  - `↻` stale

**Implementierungs-Hinweis:**
- Treeview unterstützt keine echten Buttons in Zellen
- Lösung: Overlay-Button-Technik (wie bereits für andere Actions verwendet)
- Alternativ: Selektion der Lap-Zeile → Button im Detailbereich rechts aktiviert sich

## UI: Detailbereich (rechte Spalte) bei Lap-Selektion

Wenn eine Lap-Zeile selektiert ist, zeigt der Detailbereich:

**Lap Meta (aus Sprint 1):**
- Lap-Zeit, lap_no, validity_flag (incomplete/offtrack/OK)

**Analyse Status:**
- Status: `computed` / `partial` / `blocked` / `not_computed` / `stale`
- Engine Version: `0.1.0`
- Schema Hash: erste 8 Zeichen
- Contract Hash: erste 8 Zeichen
- Corner Count: `12`
- Feature Count: `38`
- Partial: `nein` / `ja`
- Missing Channels: Liste oder `—`
- Artefakt-Pfade: Liste der vorhandenen Dateien

**Kein Chart-Rendering. Keine Bewertungen. Nur Status und Metadaten.**

## Analyse-Trigger (Klick auf Button / Button im Detailbereich)
```
1. Disable Button (verhindert Doppelklick)
2. Background-Thread starten (kein UI-Freeze)
3. AnalysisCache.compute(lap_path) aufrufen
4. Nach Abschluss: Status in UI aktualisieren (thread-safe via after())
5. Button wieder aktivieren
```

## Akzeptanzkriterien
- Klick auf „Analyse" bei nicht berechneter Lap → Pipeline läuft, Status ändert sich auf `computed` oder `partial`
- Klick auf „Analyse" bei aktuellem Cache → Status bleibt, kein Recompute
- Klick auf „Analyse" bei stale Cache → Recompute
- UI friert nicht ein (Background-Thread)
- Detailbereich zeigt korrekte Werte aus `analysis_status.json`
- `python -m py_compile src/ui/coaching_browser.py src/ui/app.py` fehlerfrei

## Deliverables
**Titel der Änderung:** UI: Lap-Analyse-Button und Status-Anzeige im Coaching Browser

**Zusammenfassung:**
- Analyse-Button pro Lap-Zeile im CoachingBrowser
- Status-Overlay (`—` / `v1` / `v1*` / `!` / `↻`)
- Detailbereich zeigt vollständige Analyse-Meta
- Background-Thread verhindert UI-Freeze

**Geänderte Dateien:**
- `src/ui/coaching_browser.py`
- `src/ui/app.py`

---

---

## Sprint 2 — Definition of Done

Sprint 2 ist **abgeschlossen** wenn:

- [ ] `analysis_contract.json` + `feature_schema_v1.json` versioniert in `config/coaching/`
- [ ] `event_config_v1.json` mit allen Schwellenwerten in `config/coaching/`
- [ ] Alle 6 neuen Python-Module kompilieren fehlerfrei
- [ ] Alle Unit Tests grün (Resampling, CornerMap, Events, Features, Cache)
- [ ] Für einen echten Sprint-1-Run: Lap-Analyse-Button baut alle 5 Artefakte
- [ ] `analysis_status.json` enthält korrekte Hashes und Version
- [ ] UI friert bei Analyse nicht ein
- [ ] CornerIDs sind zwischen zwei Analyse-Läufen identisch (Reproduzierbarkeit)

**Sprint 2 ist NICHT abgeschlossen wenn:**
- CornerIDs zwischen Läufen driften
- Resampling nicht deterministisch ist
- Cache-Invalidierung bei Versions-Änderung nicht funktioniert
- UI bei Analyse einfriert

---

## Neue Ordnerstruktur (Artefakte)

```
C:\iWAS\data\coaching\
  <session_folder>\
    run_0001.parquet
    run_0001_meta.json
    laps\
      lap_001\
        analysis\
          analysis_status.json
          lap_resampled.parquet
          lap_events.json
          corner_features.parquet
          snapshots\
            corner_01_brake_entry_window.json
            corner_01_yawrate_entry_window.json
            ...

config\coaching\
  analysis_contract.json
  feature_schema_v1.json
  event_config_v1.json

<storage_root>\corner_maps\
  <track_key>\
    corner_map_v1.json
```