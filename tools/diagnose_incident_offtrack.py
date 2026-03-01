"""
diagnose_incident_offtrack.py — Standalone diagnostic tool for iWAS incident/offtrack analysis.

Usage:
    python tools/diagnose_incident_offtrack.py <path/to/debug_samples.jsonl>

Reads debug_samples.jsonl, detects PlayerCarMyIncidentCount deltas, classifies
each incident event, prints a context window of telemetry signals, computes
signal-hit statistics for delta=1 events, and writes incident_report.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTEXT_BEFORE = 10
CONTEXT_AFTER = 5

CLASSIFICATION = {
    1: "offtrack",
    2: "loose_control",
    4: "crash",
}

# iRacing PlayerTrackSurface enum (SDK ambiguity noted in output):
#   Older SDK:  0 = OffTrack, 1 = InPitStall, 2 = ApproachingPits, 3 = OnTrack
#   Newer SDK:  -1 = NotInWorld, 0 = OffTrack, 1 = InPitStall, 2 = ApproachingPits, 3 = OnTrack
# The diagnostic flags surface != 0 as a "not clearly on-track" signal.
SURFACE_ANOMALY_NOTE = (
    "PlayerTrackSurface != 0  "
    "(SDK note: 0=OffTrack in most versions; 3=OnTrack; -1=NotInWorld in newer SDK)"
)


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

def _get(raw: dict[str, Any], key: str) -> Any:
    """Return raw[key] or None if missing/KeyError."""
    return raw.get(key)


def _get_int(raw: dict[str, Any], key: str) -> int | None:
    val = raw.get(key)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _get_float(raw: dict[str, Any], key: str) -> float | None:
    val = raw.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get_bool(raw: dict[str, Any], key: str) -> bool | None:
    val = raw.get(key)
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return bool(val)
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return None


def _fmt_val(val: Any, width: int = 10) -> str:
    if val is None:
        return "N/A".rjust(width)
    if isinstance(val, float):
        return f"{val:.4f}".rjust(width)
    return str(val).rjust(width)


def _fmt_ts(record: dict[str, Any]) -> str:
    """Format a human-readable timestamp from a JSONL record."""
    ts_wall = record.get("timestamp_wall")
    ts_mono = record.get("timestamp_monotonic")
    if ts_wall is not None:
        try:
            dt = datetime.fromtimestamp(float(ts_wall), tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        except (TypeError, ValueError, OSError):
            pass
    if ts_mono is not None:
        try:
            return f"mono={float(ts_mono):.3f}s"
        except (TypeError, ValueError):
            pass
    return "N/A"


# ---------------------------------------------------------------------------
# Signal evaluation for statistics
# ---------------------------------------------------------------------------

def _eval_signals(raw: dict[str, Any]) -> dict[str, bool | None]:
    """
    Evaluate the three diagnostic signals for a single raw sample.
    Returns a dict with keys:
        surface_anomaly  — PlayerTrackSurface != 0
        not_on_track_car — IsOnTrackCar == False
        on_pit_road      — OnPitRoad == True
    None means the field was absent/unreadable.
    """
    surface = _get_int(raw, "PlayerTrackSurface")
    is_on_track = _get_bool(raw, "IsOnTrackCar")
    on_pit = _get_bool(raw, "OnPitRoad")

    return {
        "surface_anomaly": (surface != 0) if surface is not None else None,
        "not_on_track_car": (not is_on_track) if is_on_track is not None else None,
        "on_pit_road": on_pit if on_pit is not None else None,
    }


# ---------------------------------------------------------------------------
# JSONL loading
# ---------------------------------------------------------------------------

def load_samples(path: Path) -> list[dict[str, Any]]:
    """Load all lines from a JSONL file; skip malformed lines with a warning."""
    samples: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                print(
                    f"  [WARN] Line {lineno}: JSON parse error ({exc}), skipped.",
                    file=sys.stderr,
                )
                continue
            if not isinstance(obj, dict):
                print(
                    f"  [WARN] Line {lineno}: root is not a JSON object, skipped.",
                    file=sys.stderr,
                )
                continue
            samples.append(obj)
    return samples


# ---------------------------------------------------------------------------
# Event detection
# ---------------------------------------------------------------------------

def detect_events(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Walk samples and detect any index where PlayerCarMyIncidentCount rose by 1, 2, or 4
    compared to the previous sample that had the field.
    Returns a list of event dicts.
    """
    events: list[dict[str, Any]] = []
    prev_count: int | None = None
    prev_idx: int = -1

    for idx, record in enumerate(samples):
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        count = _get_int(raw, "PlayerCarMyIncidentCount")

        if count is None:
            # Field absent in this sample — skip without resetting prev
            continue

        if prev_count is not None and count > prev_count:
            delta = count - prev_count
            classification = CLASSIFICATION.get(delta, "unknown")
            events.append(
                {
                    "idx": idx,
                    "prev_idx": prev_idx,
                    "timestamp": _fmt_ts(record),
                    "delta": delta,
                    "classification": classification,
                    "count_after": count,
                    "count_before": prev_count,
                    "signals": _eval_signals(raw),
                    "raw_snapshot": {
                        "PlayerTrackSurface": _get_int(raw, "PlayerTrackSurface"),
                        "IsOnTrackCar": _get_bool(raw, "IsOnTrackCar"),
                        "OnPitRoad": _get_bool(raw, "OnPitRoad"),
                        "LapDistPct": _get_float(raw, "LapDistPct"),
                        "Speed": _get_float(raw, "Speed"),
                        "Lap": _get_int(raw, "Lap"),
                        "LapCompleted": _get_int(raw, "LapCompleted"),
                    },
                }
            )

        prev_count = count
        prev_idx = idx

    return events


# ---------------------------------------------------------------------------
# Context table
# ---------------------------------------------------------------------------

_COL_W = {
    "idx": 6,
    "PlayerTrackSurface": 18,
    "IsOnTrackCar": 13,
    "OnPitRoad": 10,
    "LapDistPct": 10,
    "Speed": 9,
    "Lap": 5,
}


def _table_header() -> str:
    cols = [
        "idx".rjust(_COL_W["idx"]),
        "PlayerTrackSurface".center(_COL_W["PlayerTrackSurface"]),
        "IsOnTrackCar".center(_COL_W["IsOnTrackCar"]),
        "OnPitRoad".center(_COL_W["OnPitRoad"]),
        "LapDistPct".center(_COL_W["LapDistPct"]),
        "Speed".center(_COL_W["Speed"]),
        "Lap".center(_COL_W["Lap"]),
    ]
    header = " | ".join(cols)
    separator = "-+-".join("-" * w for w in _COL_W.values())
    return f"{header}\n{separator}"


def _table_row(idx: int, raw: dict[str, Any], marker: str = " ") -> str:
    surface = _get_int(raw, "PlayerTrackSurface")
    is_on = _get_bool(raw, "IsOnTrackCar")
    on_pit = _get_bool(raw, "OnPitRoad")
    lap_pct = _get_float(raw, "LapDistPct")
    speed = _get_float(raw, "Speed")
    lap = _get_int(raw, "Lap")

    cols = [
        f"{marker}{str(idx).rjust(_COL_W['idx'] - 1)}",
        _fmt_val(surface, _COL_W["PlayerTrackSurface"]),
        _fmt_val(is_on, _COL_W["IsOnTrackCar"]),
        _fmt_val(on_pit, _COL_W["OnPitRoad"]),
        _fmt_val(lap_pct, _COL_W["LapDistPct"]),
        _fmt_val(speed, _COL_W["Speed"]),
        _fmt_val(lap, _COL_W["Lap"]),
    ]
    return " | ".join(cols)


def print_context_table(samples: list[dict[str, Any]], event_idx: int) -> None:
    start = max(0, event_idx - CONTEXT_BEFORE)
    end = min(len(samples) - 1, event_idx + CONTEXT_AFTER)

    print(_table_header())
    for i in range(start, end + 1):
        record = samples[i]
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        marker = ">" if i == event_idx else " "
        print(_table_row(i, raw, marker))


# ---------------------------------------------------------------------------
# Statistics (delta=1 events)
# ---------------------------------------------------------------------------

def compute_signal_stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    """
    For delta=1 (offtrack) events, count how often each signal was True/None/False.
    Returns a stats dict per signal + a combined 'none_active' count.
    """
    delta1 = [e for e in events if e["delta"] == 1]
    total = len(delta1)

    counters: dict[str, dict[str, int]] = {
        "surface_anomaly": {"true": 0, "false": 0, "unknown": 0},
        "not_on_track_car": {"true": 0, "false": 0, "unknown": 0},
        "on_pit_road": {"true": 0, "false": 0, "unknown": 0},
    }
    none_active_count = 0

    for ev in delta1:
        sigs = ev["signals"]
        any_active = False
        for sig_key, counts in counters.items():
            val = sigs.get(sig_key)
            if val is True:
                counts["true"] += 1
                any_active = True
            elif val is False:
                counts["false"] += 1
            else:
                counts["unknown"] += 1
        if not any_active:
            none_active_count += 1

    return {
        "total_delta1_events": total,
        "none_active_count": none_active_count,
        "signals": counters,
    }


def best_signal(stats: dict[str, Any]) -> str:
    """Return the signal name with the highest hit-rate for delta=1 events."""
    total = stats["total_delta1_events"]
    if total == 0:
        return "n/a (no delta=1 events)"

    best_name = ""
    best_rate = -1.0
    for sig, counts in stats["signals"].items():
        rate = counts["true"] / total
        if rate > best_rate:
            best_rate = rate
            best_name = sig

    if best_rate <= 0.0:
        return "none (no signal fired on any delta=1 event)"

    label_map = {
        "surface_anomaly": "PlayerTrackSurface != 0",
        "not_on_track_car": "IsOnTrackCar == False",
        "on_pit_road": "OnPitRoad == True",
    }
    return f"{label_map.get(best_name, best_name)}  ({best_rate:.0%} hit-rate, {int(best_rate * total)}/{total} events)"


# ---------------------------------------------------------------------------
# Report printing
# ---------------------------------------------------------------------------

SEP = "=" * 72
SUB_SEP = "-" * 72


def print_event(event: dict[str, Any], samples: list[dict[str, Any]], event_num: int) -> None:
    idx = event["idx"]
    delta = event["delta"]
    classification = event["classification"].upper()
    print(SEP)
    print(
        f"  EVENT #{event_num}  |  idx={idx}  |  ts={event['timestamp']}  "
        f"|  delta=+{delta}  |  [{classification}]"
    )
    print(
        f"  IncidentCount: {event['count_before']} -> {event['count_after']}"
    )
    sigs = event["signals"]
    sig_parts = []
    for label, key in [
        ("surface!=0", "surface_anomaly"),
        ("!IsOnTrackCar", "not_on_track_car"),
        ("OnPitRoad", "on_pit_road"),
    ]:
        val = sigs.get(key)
        sig_parts.append(f"{label}={'?' if val is None else ('Y' if val else 'N')}")
    print(f"  Signals at event: {' | '.join(sig_parts)}")
    print(SUB_SEP)
    print_context_table(samples, idx)


def print_statistics(stats: dict[str, Any]) -> None:
    total = stats["total_delta1_events"]
    print()
    print(SEP)
    print("  STATISTICS — delta=1 (offtrack) events")
    print(SEP)
    print(f"  Total delta=1 events: {total}")
    if total == 0:
        print("  No delta=1 events found.")
        return

    label_map = {
        "surface_anomaly": "PlayerTrackSurface != 0",
        "not_on_track_car": "IsOnTrackCar == False",
        "on_pit_road": "OnPitRoad == True",
    }
    for sig, counts in stats["signals"].items():
        label = label_map.get(sig, sig)
        hit = counts["true"]
        rate = hit / total if total > 0 else 0.0
        print(
            f"  {label:<28}  hit={hit}/{total}  ({rate:.0%})"
            f"  [false={counts['false']}  unknown={counts['unknown']}]"
        )

    none = stats["none_active_count"]
    none_rate = none / total if total > 0 else 0.0
    print(f"  {'None of the signals active':<28}  hit={none}/{total}  ({none_rate:.0%})")


def print_recommendation(stats: dict[str, Any]) -> None:
    print()
    print(SEP)
    print("  RECOMMENDATION")
    print(SEP)
    rec = best_signal(stats)
    print(f"  Most reliable signal for delta=1 (offtrack) detection:")
    print(f"    >> {rec}")
    total = stats["total_delta1_events"]
    if total > 0:
        # Extra note when signals are missing entirely (field not recorded)
        any_known = any(
            c["true"] + c["false"] > 0
            for c in stats["signals"].values()
        )
        if not any_known:
            print()
            print(
                "  NOTE: All signals returned 'unknown' — PlayerCarMyIncidentCount,\n"
                "        PlayerTrackSurface, IsOnTrackCar, and/or OnPitRoad may not\n"
                "        be present in this JSONL file. Enable them in the recorder."
            )
    print(SEP)


# ---------------------------------------------------------------------------
# JSON report writer
# ---------------------------------------------------------------------------

def write_incident_report(
    jsonl_path: Path,
    samples: list[dict[str, Any]],
    events: list[dict[str, Any]],
    stats: dict[str, Any],
) -> Path:
    report_path = jsonl_path.parent / "incident_report.json"

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_file": str(jsonl_path),
        "total_samples": len(samples),
        "total_events": len(events),
        "events": [
            {
                "event_num": i + 1,
                "idx": ev["idx"],
                "timestamp": ev["timestamp"],
                "delta": ev["delta"],
                "classification": ev["classification"],
                "count_before": ev["count_before"],
                "count_after": ev["count_after"],
                "signals": {
                    k: (None if v is None else bool(v))
                    for k, v in ev["signals"].items()
                },
                "raw_snapshot": ev["raw_snapshot"],
            }
            for i, ev in enumerate(events)
        ],
        "statistics": {
            "delta1_total": stats["total_delta1_events"],
            "none_active": stats["none_active_count"],
            "signal_hits": {
                sig: {
                    "true": c["true"],
                    "false": c["false"],
                    "unknown": c["unknown"],
                    "hit_rate": (
                        round(c["true"] / stats["total_delta1_events"], 4)
                        if stats["total_delta1_events"] > 0
                        else None
                    ),
                }
                for sig, c in stats["signals"].items()
            },
            "best_signal": best_signal(stats),
        },
    }

    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose incident / offtrack events from a debug_samples.jsonl file.\n"
            "Detects PlayerCarMyIncidentCount deltas, classifies events, prints\n"
            "context windows and signal statistics, and writes incident_report.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "jsonl_path",
        help="Path to debug_samples.jsonl",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    jsonl_path = Path(args.jsonl_path).expanduser().resolve()

    if not jsonl_path.exists():
        print(f"ERROR: File not found: {jsonl_path}", file=sys.stderr)
        return 1
    if not jsonl_path.is_file():
        print(f"ERROR: Not a file: {jsonl_path}", file=sys.stderr)
        return 1

    print(SEP)
    print(f"  iWAS Incident/Offtrack Diagnostic")
    print(f"  File : {jsonl_path}")
    print(SEP)

    # Load
    print("  Loading samples...", end="", flush=True)
    samples = load_samples(jsonl_path)
    print(f"  {len(samples)} samples loaded.")

    # Detect
    events = detect_events(samples)
    print(f"  Incidents detected: {len(events)}")
    if events:
        by_class: dict[str, int] = {}
        for ev in events:
            by_class[ev["classification"]] = by_class.get(ev["classification"], 0) + 1
        for cls, count in sorted(by_class.items()):
            print(f"    {cls}: {count}")
    print()

    # Check if PlayerCarMyIncidentCount was ever present
    incident_field_present = any(
        "PlayerCarMyIncidentCount" in (r.get("raw") or {})
        for r in samples
    )
    if not incident_field_present:
        print(
            "  [WARN] 'PlayerCarMyIncidentCount' was not found in any sample's raw dict.\n"
            "         The recorder may not be capturing this iRacing SDK variable yet.\n"
            "         No incident events can be detected from this file.\n",
            file=sys.stderr,
        )

    # Print each event
    for i, ev in enumerate(events):
        print_event(ev, samples, i + 1)

    if not events:
        print("  (No incident events found in this file.)")

    # Stats
    stats = compute_signal_stats(events)
    print_statistics(stats)
    print_recommendation(stats)

    # Write JSON report
    report_path = write_incident_report(jsonl_path, samples, events, stats)
    print(f"\n  Report written: {report_path}")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())