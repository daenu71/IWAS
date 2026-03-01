# iWAS Project Memory

## Key Tools
- `tools/audit_coaching_session.py` — audits coaching session folders (Parquet columns, YAML SessionType, JSON/Markdown reports)
- `tools/diagnose_incident_offtrack.py` — standalone diagnostic: reads `debug_samples.jsonl`, detects `PlayerCarMyIncidentCount` deltas, classifies offtrack/loose_control/crash events, prints context tables [idx-10..idx+5], computes signal hit-rate statistics (PlayerTrackSurface, IsOnTrackCar, OnPitRoad), writes `incident_report.json` next to the JSONL.

## Data Structure (debug_samples.jsonl)
Each line is a JSON object with:
- `kind`, `seq`, `sample_count`, `timestamp_wall` (Unix float), `timestamp_monotonic`
- `changed_probe_keys` — list of keys that changed vs. prior sample
- `probe` — current probe state (only changed fields may be present)
- `raw` — full telemetry dict; the authoritative source for all field reads

## Current Field Availability
The existing JSONL files (as of 2026-02-28 sessions) do NOT contain:
- `PlayerCarMyIncidentCount`
- `PlayerTrackSurface`
These must be added to the recorder for incident diagnostics to work.
Currently present: `IsOnTrackCar`, `OnPitRoad`, `LapDistPct`, `Speed`, `Lap`, `LapCompleted`, etc.

## iRacing SDK Notes
- `PlayerTrackSurface` values vary by SDK version:
  - Most versions: 0=OffTrack, 1=InPitStall, 2=ApproachingPits, 3=OnTrack
  - Newer SDK: -1=NotInWorld, 0=OffTrack, 1=InPitStall, 2=ApproachingPits, 3=OnTrack
- Incident delta classification: 1=offtrack, 2=loose_control, 4=crash

## Architecture Notes
- All `src/` imports are forbidden in `tools/` scripts — must be standalone
- Coaching data lives under `data/coaching/<session_dir>/`
- Session dirs follow pattern: `YYYY-MM-DD__HHMMSS__<track>__<car>__<type>__<id>`