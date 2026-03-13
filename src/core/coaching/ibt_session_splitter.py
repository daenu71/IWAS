"""Post-processing: derive Run / Pit / Lap index files from run_0001.parquet.

Called by ibt_importer after a successful import.  Reads the existing Parquet
file (unchanged) and writes three index artefacts plus a patch to run meta:

    <session_dir>/run_index.json   – on-track run segments
    <session_dir>/pit_index.json   – pit-road phases
    <session_dir>/lap_index.json   – individual laps with validity flags
    <session_dir>/run_0001_meta.json – patched with run_count/lap_count/pit_count

Logic is derived from lap_segmenter.py (LapSegmenter) and
irsdk/recorder_service.py (RunDetector) but operates in batch on the full
Parquet DataFrame rather than streaming.

Public entry point
------------------
    split_session(session_dir: Path) -> None
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_PARQUET_FILENAME = "run_0001.parquet"
_RUN_META_FILENAME = "run_0001_meta.json"
_RUN_INDEX_FILENAME = "run_index.json"
_PIT_INDEX_FILENAME = "pit_index.json"
_LAP_INDEX_FILENAME = "lap_index.json"

# Lap-validity thresholds (mirrors LapSegmenter defaults)
_WRAP_HI: float = 0.99
_WRAP_LO: float = 0.01
_MIN_VALID_LAP_TIME_S: float = 30.0
_MIN_VALID_LAP_SAMPLES: int = 60


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def split_session(session_dir: Path) -> None:
    """Derive and write run/pit/lap index files for *session_dir*.

    Silently returns if the Parquet file is missing or empty.
    Raises on unexpected errors so the caller can decide how to handle them.
    """
    session_dir = Path(session_dir)
    parquet_path = session_dir / _PARQUET_FILENAME
    if not parquet_path.exists():
        _LOG.warning("[ibt_session_splitter] parquet not found: %s", parquet_path)
        return

    _LOG.info("[ibt_session_splitter] start session_dir=%s", session_dir.name)

    try:
        import pyarrow.parquet as pq  # type: ignore[import]
        table = pq.read_table(str(parquet_path))
    except Exception as exc:  # pragma: no cover
        _LOG.error("[ibt_session_splitter] failed to read parquet: %s", exc)
        raise

    n = table.num_rows
    if n == 0:
        _LOG.warning("[ibt_session_splitter] empty parquet, nothing to derive")
        return

    splitter = _Splitter(table, n)
    run_index, pit_index, lap_index = splitter.run()

    _write_json(session_dir / _RUN_INDEX_FILENAME, run_index)
    _write_json(session_dir / _PIT_INDEX_FILENAME, pit_index)
    _write_json(session_dir / _LAP_INDEX_FILENAME, lap_index)
    _patch_run_meta(
        session_dir / _RUN_META_FILENAME,
        run_count=len(run_index),
        pit_count=len(pit_index),
        lap_count=len(lap_index),
    )

    _LOG.info(
        "[ibt_session_splitter] done: %d run(s), %d pit(s), %d lap(s)",
        len(run_index),
        len(pit_index),
        len(lap_index),
    )


# ---------------------------------------------------------------------------
# Internal splitter
# ---------------------------------------------------------------------------

class _Splitter:
    """Stateful helper that derives all three index lists from a pyarrow Table."""

    def __init__(self, table: Any, n: int) -> None:
        self._n = n
        # Extract flat Python lists once for index access.
        # pyarrow ChunkedArray.to_pylist() converts to native Python types
        # (including None for null values) which simplifies coercion below.
        self._ts = self._col(table, "ts")
        self._on_pit = self._col(table, "OnPitRoad")
        self._lap = self._col(table, "Lap")
        self._lap_completed = self._col(table, "LapCompleted")
        self._lap_dist_pct = self._col(table, "LapDistPct")
        self._is_on_track_car = self._col(table, "IsOnTrackCar")
        self._incident_count = self._col(table, "PlayerCarMyIncidentCount")
        self._player_track_surface = self._col(table, "PlayerTrackSurface")

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def run(self) -> tuple[list[dict], list[dict], list[dict]]:
        pit_index = self._build_pit_index()
        run_index = self._build_run_index()
        lap_index = self._build_lap_index(run_index)
        return run_index, pit_index, lap_index

    # ------------------------------------------------------------------
    # Pit index
    # ------------------------------------------------------------------

    def _build_pit_index(self) -> list[dict[str, Any]]:
        """Contiguous blocks where OnPitRoad == True."""
        on_pit = self._on_pit
        if on_pit is None:
            return []

        pits: list[dict[str, Any]] = []
        pit_id = 1
        in_pit = False
        pit_start = 0

        for i in range(self._n):
            v = _coerce_bool(on_pit[i])
            if v is None:
                v = False  # treat unknown as not in pit

            if not in_pit and v:
                in_pit = True
                pit_start = i
            elif in_pit and not v:
                pits.append(self._pit_entry(pit_id, pit_start, i - 1))
                pit_id += 1
                in_pit = False

        if in_pit:
            pits.append(self._pit_entry(pit_id, pit_start, self._n - 1))

        return pits

    def _pit_entry(self, pit_id: int, start: int, end: int) -> dict[str, Any]:
        return {
            "pit_id": pit_id,
            "start_sample": int(start),
            "end_sample": int(end),
            "start_ts": self._ts_at(start),
            "end_ts": self._ts_at(end),
        }

    # ------------------------------------------------------------------
    # Run index
    # ------------------------------------------------------------------

    def _build_run_index(self) -> list[dict[str, Any]]:
        """On-track runs: contiguous blocks where OnPitRoad == False.

        ``reason`` describes the end condition:
        - ``"pit_entry"``   – run ended because car entered pit road
        - ``"session_end"`` – run ended at the last sample (data ended)
        """
        on_pit = self._on_pit
        if on_pit is None:
            # No OnPitRoad channel: treat the entire session as one run.
            return [self._run_entry(1, 0, self._n - 1, "session_end")]

        runs: list[dict[str, Any]] = []
        run_id = 1
        in_run = False
        run_start = 0

        for i in range(self._n):
            v = _coerce_bool(on_pit[i])
            if v is None:
                v = False

            if not in_run and not v:
                in_run = True
                run_start = i
            elif in_run and v:
                runs.append(self._run_entry(run_id, run_start, i - 1, "pit_entry"))
                run_id += 1
                in_run = False

        if in_run:
            runs.append(self._run_entry(run_id, run_start, self._n - 1, "session_end"))

        return runs

    def _run_entry(self, run_id: int, start: int, end: int, reason: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "start_sample": int(start),
            "end_sample": int(end),
            "start_ts": self._ts_at(start),
            "end_ts": self._ts_at(end),
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # Lap index
    # ------------------------------------------------------------------

    def _build_lap_index(self, run_index: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Lap segments – primary: Lap/LapCompleted counter; fallback: LapDistPct wrap.

        Logic mirrors LapSegmenter (lap_segmenter.py):
        - Counter change   → reason ``"counter_change"``
        - LapDistPct wrap  → reason ``"distpct_wrap"``  (only if no counter change ever seen)
        - Session end      → reason ``"session_end"``   (always incomplete)

        ``valid_lap``: True only when structurally complete, duration ≥ 30 s,
        sample count ≥ 60, no offtrack-incident step (incident +1/+2), and
        incident_delta == 0.
        """
        n = self._n
        lap_col = self._lap if self._lap is not None else self._lap_completed
        lap_dist = self._lap_dist_pct
        is_on_track = self._is_on_track_car
        inc_col = self._incident_count
        pts_col = self._player_track_surface

        laps: list[dict[str, Any]] = []
        lap_id = 1

        # -- lap-segment state --
        lap_start_sample = 0
        current_lap_no: int | None = None   # lap_no at start of current segment
        last_counter_val: int | None = None  # previous counter value for change detection
        counter_change_seen = False
        last_lap_dist: float | None = None
        wrap_cooldown = False

        # -- per-lap accumulators --
        inc_min: int | None = None
        inc_max: int | None = None
        offtrack_flag = False   # incident-step based (fallback when PlayerTrackSurface absent)
        pts_offtrack_flag = False  # PlayerTrackSurface based (preferred)
        prev_inc: int | None = None

        def _close(end_sample: int, reason: str) -> None:
            nonlocal lap_id, lap_start_sample, current_lap_no
            nonlocal inc_min, inc_max, offtrack_flag, pts_offtrack_flag, prev_inc

            sample_count = end_sample - lap_start_sample + 1
            s_ts = self._ts_at(lap_start_sample)
            e_ts = self._ts_at(end_sample)
            lap_time_s: float | None = None
            if s_ts is not None and e_ts is not None:
                d = e_ts - s_ts
                lap_time_s = d if d >= 0 else None

            structural_complete = reason in {"counter_change", "distpct_wrap"}
            lap_complete = (
                structural_complete
                and lap_time_s is not None
                and lap_time_s >= _MIN_VALID_LAP_TIME_S
                and sample_count >= _MIN_VALID_LAP_SAMPLES
            )
            incident_delta = 0
            if inc_min is not None and inc_max is not None:
                incident_delta = max(0, inc_max - inc_min)
            # Combine both signals: PlayerTrackSurface (surface=0/−1) OR incident-step (+1/+2).
            # Previously the incident-based fallback was discarded when PlayerTrackSurface was
            # present but never registered OffTrack — causing laps with kerb/grass incidents
            # (surface stays 3=OnTrack) to be reported as clean.
            offtrack_surface = pts_offtrack_flag or offtrack_flag
            valid_lap = bool(lap_complete and not offtrack_surface and incident_delta == 0)

            run_id = _find_run_id(run_index, lap_start_sample)

            entry: dict[str, Any] = {
                "lap_id": lap_id,
                "run_id": run_id,
                "start_sample": int(lap_start_sample),
                "end_sample": int(end_sample),
                "start_ts": s_ts,
                "end_ts": e_ts,
                "reason": reason,
                "valid_lap": valid_lap,
                "offtrack_surface": offtrack_surface,
                "incident_delta": incident_delta,
            }
            if lap_time_s is not None:
                entry["lap_time_s"] = round(lap_time_s, 3)
            if current_lap_no is not None:
                entry["lap_no"] = int(current_lap_no)

            laps.append(entry)
            lap_id += 1

            # Reset for next lap
            lap_start_sample = end_sample + 1
            current_lap_no = None
            inc_min = None
            inc_max = None
            offtrack_flag = False
            pts_offtrack_flag = False
            prev_inc = None

        for i in range(n):
            # ---- Lap counter detection (primary signal) ----
            handled_by_counter = False
            if lap_col is not None:
                cv = _coerce_int(lap_col[i])
                if cv is not None:
                    if last_counter_val is None:
                        last_counter_val = cv
                        if current_lap_no is None:
                            current_lap_no = cv
                    elif cv != last_counter_val:
                        counter_change_seen = True
                        handled_by_counter = True
                        _close(i - 1, "counter_change")
                        current_lap_no = cv
                        last_counter_val = cv
                    else:
                        last_counter_val = cv

            # ---- Wrap fallback (only while no counter change ever seen) ----
            if not handled_by_counter and not counter_change_seen and lap_dist is not None:
                curr_dist = _coerce_float(lap_dist[i])
                # Release wrap cooldown once LapDistPct is clearly past start
                if wrap_cooldown and curr_dist is not None and curr_dist > 0.1 and curr_dist < _WRAP_HI:
                    wrap_cooldown = False
                if (
                    not wrap_cooldown
                    and last_lap_dist is not None
                    and curr_dist is not None
                    and last_lap_dist >= _WRAP_HI
                    and curr_dist <= _WRAP_LO
                ):
                    # Check on-track gate (mirrors use_ontrack_gate=True default)
                    on_track = _coerce_bool(is_on_track[i]) if is_on_track is not None else None
                    if on_track is None or on_track is True:
                        if i - 1 >= lap_start_sample:
                            _close(i - 1, "distpct_wrap")
                        # Derive next lap_no (+1)
                        if current_lap_no is None and last_counter_val is not None:
                            current_lap_no = last_counter_val
                        elif current_lap_no is not None:
                            current_lap_no = current_lap_no + 1
                        wrap_cooldown = True
                last_lap_dist = curr_dist
            elif lap_dist is not None and last_lap_dist is None:
                # Seed last_lap_dist for first sample
                last_lap_dist = _coerce_float(lap_dist[i])

            # ---- Incident accumulation (per-lap) ----
            if inc_col is not None:
                inc = _coerce_int(inc_col[i])
                if inc is not None:
                    if inc_min is None or inc < inc_min:
                        inc_min = inc
                    if inc_max is None or inc > inc_max:
                        inc_max = inc
                    if prev_inc is not None:
                        step = inc - prev_inc
                        if step in (1, 2):
                            offtrack_flag = True
                    prev_inc = inc

            # ---- PlayerTrackSurface offtrack detection (preferred signal) ----
            # Values: -1 = NotInWorld, 0 = OffTrack (iRacing irsdk enum irsdk_TrkLoc)
            if pts_col is not None and not pts_offtrack_flag:
                pts_val = _coerce_int(pts_col[i])
                if pts_val is not None and pts_val in {-1, 0}:
                    pts_offtrack_flag = True

        # Close the final open lap segment
        if lap_start_sample <= n - 1:
            _close(n - 1, "session_end")

        # Post-process: laps inside a pit_entry run may have been closed with
        # "counter_change" because the LapCompleted counter ticked in the pit lane.
        # Only laps that are structurally complete (counter_change / distpct_wrap)
        # AND whose final LapDistPct is below _WRAP_HI are reclassified as pit_entry.
        # Using AND ensures that full laps in pit_entry runs (e.g. Laps 2–8 in a
        # Lamborghini session) keep their original reason unchanged.
        if lap_dist is not None:
            for run in run_index:
                if run.get("reason") != "pit_entry":
                    continue
                run_id = int(run["run_id"])
                for lap in laps:
                    if lap.get("run_id") != run_id:
                        continue
                    if lap.get("reason") not in {"counter_change", "distpct_wrap"}:
                        continue
                    end_sample = lap.get("end_sample")
                    last_dist: float | None = None
                    if end_sample is not None:
                        end_sample = int(end_sample)
                        last_dist = _coerce_float(lap_dist[end_sample]) if end_sample < n else None
                    if last_dist is not None and last_dist < _WRAP_HI:
                        lap["reason"] = "pit_entry"
                        lap["valid_lap"] = False

        return laps

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _col(table: Any, name: str):  # type: ignore[return]
        """Return column as a Python list (with None for nulls) or None if absent."""
        if name in table.schema.names:
            return table.column(name).to_pylist()
        return None

    def _ts_at(self, i: int) -> float | None:
        if self._ts is None:
            return None
        v = self._ts[i]
        return _coerce_float(v)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_run_id(run_index: list[dict[str, Any]], sample: int) -> int | None:
    """Return the run_id whose sample range contains *sample*, or None."""
    for run in run_index:
        if run["start_sample"] <= sample <= run["end_sample"]:
            return int(run["run_id"])
    return None


def _write_json(path: Path, data: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _LOG.debug("[ibt_session_splitter] wrote %s (%d entries)", path.name, len(data))


def _patch_run_meta(path: Path, *, run_count: int, pit_count: int, lap_count: int) -> None:
    """Add run_count / pit_count / lap_count to existing run_0001_meta.json."""
    if not path.exists():
        _LOG.warning("[ibt_session_splitter] run meta not found, skipping patch: %s", path)
        return
    try:
        existing: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _LOG.warning("[ibt_session_splitter] could not read run meta: %s", exc)
        return
    existing["run_count"] = run_count
    existing["pit_count"] = pit_count
    existing["lap_count"] = lap_count
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    _LOG.debug("[ibt_session_splitter] patched %s", path.name)


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            if math.isnan(float(value)):
                return None
        except Exception:
            pass
        return bool(value)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in {"true", "1", "yes"}:
            return True
        if key in {"false", "0", "no"}:
            return False
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return int(f)
    except Exception:
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except Exception:
        return None
