"""
diagnose_run_split.py — Run-split diagnostic for iWAS coaching sessions.

Usage:
    python tools/diagnose_run_split.py <session_ordner_pfad>

Loads every run_NNNN.parquet in the session folder, prints a ~1s-sampled
telemetry table per file, and marks:
  - OnPitRoad True→False  (pit exit / potential run-split)
  - LapCompleted increase  (lap boundary)
  - Speed < 1.0 AND IsOnTrackCar == False  (slow / off-track)

Summarises the number of expected run splits and writes the full report
as diagnose_run_split.txt in the session folder.

Requires pyarrow (pip install pyarrow) or pandas (pip install pandas).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SEP = "=" * 76
SUB_SEP = "-" * 76

_RUN_PARQUET_RE = re.compile(r"^run_(\d{4})\.parquet$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Type coercion helpers
# ---------------------------------------------------------------------------

def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Parquet loading
# ---------------------------------------------------------------------------

def load_parquet_rows(path: Path) -> list[dict[str, Any]]:
    """Load a parquet file and return a list of row dicts.

    Tries pyarrow first, falls back to pandas.
    """
    try:
        import pyarrow.parquet as pq  # type: ignore
        table = pq.read_table(str(path))
        col_names = table.schema.names
        col_data = {name: table.column(name).to_pylist() for name in col_names}
        n = table.num_rows
        return [{name: col_data[name][i] for name in col_names} for i in range(n)]
    except ImportError:
        pass
    try:
        import pandas as pd  # type: ignore
        df = pd.read_parquet(str(path))
        return df.to_dict(orient="records")
    except ImportError:
        pass
    raise RuntimeError(
        "pyarrow or pandas is required.  Install with: pip install pyarrow"
    )


# ---------------------------------------------------------------------------
# ~1s subsampling
# ---------------------------------------------------------------------------

def subsample_1s(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[int]]:
    """Return (display_rows, orig_indices) — one row per wall-time second.

    Falls back to stride-based sampling when 'ts' is absent.
    """
    if not rows:
        return [], []

    t0 = None
    for row in rows:
        v = _to_float(row.get("ts"))
        if v is not None:
            t0 = v
            break

    if t0 is not None:
        seen: set[int] = set()
        display: list[dict[str, Any]] = []
        indices: list[int] = []
        for i, row in enumerate(rows):
            v = _to_float(row.get("ts"))
            if v is None:
                continue
            bucket = int(v - t0)
            if bucket not in seen:
                seen.add(bucket)
                display.append(row)
                indices.append(i)
        if display:
            return display, indices

    # Fallback: stride to at most 300 display rows
    stride = max(1, len(rows) // 300)
    indices = list(range(0, len(rows), stride))
    return [rows[i] for i in indices], indices


# ---------------------------------------------------------------------------
# Event detection (runs on ALL rows at full resolution)
# ---------------------------------------------------------------------------

def detect_events(
    rows: list[dict[str, Any]],
) -> tuple[list[int], list[int], list[int]]:
    """Scan all rows and return three lists of row indices:

    pit_exits   — OnPitRoad changed True → False
    lap_changes — LapCompleted increased
    slow_off    — onset of Speed < 1.0 AND IsOnTrackCar == False
    """
    pit_exits: list[int] = []
    lap_changes: list[int] = []
    slow_off: list[int] = []

    prev_pit: bool | None = None
    prev_lap: int | None = None
    in_slow_off = False

    for i, row in enumerate(rows):
        pit = _to_bool(row.get("OnPitRoad"))
        on_track = _to_bool(row.get("IsOnTrackCar"))
        speed = _to_float(row.get("Speed"))
        lap = _to_int(row.get("LapCompleted"))

        # OnPitRoad True → False
        if prev_pit is True and pit is False:
            pit_exits.append(i)

        # LapCompleted increased
        if prev_lap is not None and lap is not None and lap > prev_lap:
            lap_changes.append(i)

        # Speed < 1.0 AND IsOnTrackCar == False — only mark onset
        currently_slow_off = (
            speed is not None and speed < 1.0 and on_track is False
        )
        if currently_slow_off and not in_slow_off:
            slow_off.append(i)
        in_slow_off = currently_slow_off

        if pit is not None:
            prev_pit = pit
        if lap is not None:
            prev_lap = lap

    return pit_exits, lap_changes, slow_off


# ---------------------------------------------------------------------------
# Table formatting
# ---------------------------------------------------------------------------

_COL_HDR = (
    f"{'idx':>6}  {'ts_rel':>8}  {'OnPitRoad':>9}  {'IsOnTrack':>9}  "
    f"{'Speed':>8}  {'LapCmpl':>7}  {'LapDist%':>8}  Flags"
)
_COL_SEP = (
    f"{'------':>6}  {'--------':>8}  {'---------':>9}  {'---------':>9}  "
    f"{'--------':>8}  {'-------':>7}  {'--------':>8}  -----"
)


def _fmt_bool(val: bool | None, width: int) -> str:
    if val is None:
        return "N/A".rjust(width)
    return str(val).rjust(width)


def _fmt_float(val: float | None, width: int, decimals: int = 1) -> str:
    if val is None:
        return "N/A".rjust(width)
    return f"{val:.{decimals}f}".rjust(width)


def _fmt_int(val: int | None, width: int) -> str:
    if val is None:
        return "N/A".rjust(width)
    return str(val).rjust(width)


def _table_row(
    orig_idx: int,
    t0: float | None,
    row: dict[str, Any],
    flags: list[str],
) -> str:
    ts = _to_float(row.get("ts"))
    if ts is not None and t0 is not None:
        ts_rel = f"{ts - t0:.1f}s"
    else:
        ts_rel = "N/A"

    pit = _to_bool(row.get("OnPitRoad"))
    on_track = _to_bool(row.get("IsOnTrackCar"))
    speed = _to_float(row.get("Speed"))
    lap = _to_int(row.get("LapCompleted"))
    dist = _to_float(row.get("LapDistPct"))

    flags_str = "  ".join(flags) if flags else ""
    return (
        f"{orig_idx:>6}  {ts_rel:>8}  {_fmt_bool(pit, 9)}  "
        f"{_fmt_bool(on_track, 9)}  {_fmt_float(speed, 8)}  "
        f"{_fmt_int(lap, 7)}  {_fmt_float(dist, 8, 3)}  {flags_str}"
    )


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_parquet(path: Path, lines: list[str]) -> int:
    """Analyse one parquet file; append text to *lines*.

    Returns the number of OnPitRoad True→False transitions found.
    """
    try:
        all_rows = load_parquet_rows(path)
    except Exception as exc:
        lines.append(f"  ERROR loading {path.name}: {exc}")
        lines.append("")
        return 0

    if not all_rows:
        lines.append(f"  {path.name}: empty file, skipped.")
        lines.append("")
        return 0

    # Detect events on full-resolution data
    pit_exits, lap_changes, slow_off = detect_events(all_rows)

    # Subsample for display
    display_rows, orig_indices = subsample_1s(all_rows)

    # t0 for relative timestamps
    t0: float | None = None
    for r in all_rows:
        v = _to_float(r.get("ts"))
        if v is not None:
            t0 = v
            break

    t_end: float | None = None
    for r in reversed(all_rows):
        v = _to_float(r.get("ts"))
        if v is not None:
            t_end = v
            break
    dur_str = f"{t_end - t0:.1f}s" if (t0 is not None and t_end is not None) else "?"

    lines.append(f"--- {path.name}  ({len(all_rows)} rows, {dur_str}) ---")
    lines.append("")
    lines.append(_COL_HDR)
    lines.append(_COL_SEP)

    # Map full-row event indices to display rows via window [orig_i, next_orig_i)
    pit_exit_set = set(pit_exits)
    lap_change_set = set(lap_changes)
    slow_off_set = set(slow_off)

    for di, (orig_i, disp_row) in enumerate(zip(orig_indices, display_rows)):
        next_orig_i = orig_indices[di + 1] if di + 1 < len(orig_indices) else len(all_rows)
        window = range(orig_i, next_orig_i)

        row_flags: list[str] = []
        if any(j in pit_exit_set for j in window):
            row_flags.append("← PIT-EXIT")
        if any(j in lap_change_set for j in window):
            row_flags.append("LAP+1")
        if any(j in slow_off_set for j in window):
            row_flags.append("SLOW-OFF")

        lines.append(_table_row(orig_i, t0, disp_row, row_flags))

    lines.append("")

    # Event detail list
    def ts_str(idx: int) -> str:
        if idx >= len(all_rows) or t0 is None:
            return "?"
        v = _to_float(all_rows[idx].get("ts"))
        return f"{v - t0:.1f}s" if v is not None else "?"

    if pit_exits or lap_changes or slow_off:
        lines.append(f"  Events in {path.name}:")
        for i in pit_exits:
            lines.append(
                f"    [t={ts_str(i):>8}]  OnPitRoad True→False"
                "  ← pit exit / potential run-split"
            )
        for i in lap_changes:
            prev = _to_int(all_rows[i - 1].get("LapCompleted")) if i > 0 else None
            cur = _to_int(all_rows[i].get("LapCompleted"))
            lines.append(f"    [t={ts_str(i):>8}]  LapCompleted {prev}→{cur}")
        for i in slow_off:
            speed = _to_float(all_rows[i].get("Speed"))
            spd_str = f"{speed:.2f} m/s" if speed is not None else "N/A"
            lines.append(
                f"    [t={ts_str(i):>8}]  Speed<1 & IsOnTrackCar=False"
                f"  (speed={spd_str})"
            )
        lines.append("")
    else:
        lines.append(f"  No events detected in {path.name}.")
        lines.append("")

    return len(pit_exits)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose run-split issues in an iWAS coaching session.\n"
            "Loads run_NNNN.parquet files, prints ~1s telemetry tables,\n"
            "marks OnPitRoad transitions and lap events, and outputs a\n"
            "diagnose_run_split.txt report in the session folder."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("session_path", help="Path to the session folder")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session_dir = Path(args.session_path).expanduser().resolve()

    if not session_dir.exists():
        print(f"ERROR: Path not found: {session_dir}", file=sys.stderr)
        return 1
    if not session_dir.is_dir():
        print(f"ERROR: Not a directory: {session_dir}", file=sys.stderr)
        return 1

    parquet_files = sorted(
        p for p in session_dir.iterdir()
        if p.is_file() and _RUN_PARQUET_RE.match(p.name)
    )

    lines: list[str] = []
    lines.append(SEP)
    lines.append("  iWAS Run-Split Diagnostic")
    lines.append(f"  Session : {session_dir}")
    lines.append(
        f"  Created : "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    lines.append(SEP)
    lines.append("")

    if not parquet_files:
        lines.append("  No run_NNNN.parquet files found in this session folder.")
        lines.append("")
    else:
        lines.append(f"  Found {len(parquet_files)} parquet file(s):")
        for p in parquet_files:
            lines.append(f"    {p.name}")
        lines.append("")

    total_pit_exits = 0
    for p in parquet_files:
        count = process_parquet(p, lines)
        total_pit_exits += count

    # Summary
    lines.append(SEP)
    lines.append("  SUMMARY")
    lines.append(SEP)
    lines.append(f"  Parquet-Dateien gefunden     : {len(parquet_files)}")
    lines.append(
        f"  Erwartete Run-Splits           : {total_pit_exits}"
        "  (OnPitRoad True→False Übergänge)"
    )
    if total_pit_exits > 0:
        lines.append("")
        lines.append(
            "  Hinweis: Falls iWAS weniger Runs zeigt als erwartet, ist der"
        )
        lines.append(
            "  wahrscheinliche Bug: _prev_on_pit_road wird in _end() nicht auf"
        )
        lines.append(
            "  None zurückgesetzt → nach Run-Ende kein neuer Pit-Exit erkennbar."
        )
        lines.append(
            "  Fix: self._prev_on_pit_road = None  in RunDetector._end()"
        )
    lines.append(SEP)
    lines.append("")

    full_text = "\n".join(lines)
    print(full_text)

    out_path = session_dir / "diagnose_run_split.txt"
    try:
        out_path.write_text(full_text, encoding="utf-8")
        print(f"Report written: {out_path}")
    except Exception as exc:
        print(f"[WARN] Could not write report: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
