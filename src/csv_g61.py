from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np


@dataclass(frozen=True)
class RunData:
    csv_path: Path
    columns: dict[str, list[Any]]  # floats, ints, bools oder str
    row_count: int


def load_g61_csv(csv_path: str | Path) -> RunData:
    p = Path(csv_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"CSV nicht gefunden: {p}")

    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV hat keinen Header.")

        cols: dict[str, list[Any]] = {name: [] for name in reader.fieldnames}

        row_count = 0
        for row in reader:
            row_count += 1
            for name in reader.fieldnames:
                cols[name].append(_parse_value(name, row.get(name, "")))

    if row_count <= 0:
        raise ValueError("CSV hat keine Datenzeilen.")

    # Minimal-Checks: diese Spalten brauchen wir sicher
    _require(cols, "LapDistPct")
    _require(cols, "Speed")

    return RunData(csv_path=p, columns=cols, row_count=row_count)


def get_float_col(run: RunData, name: str) -> list[float]:
    if name not in run.columns:
        raise KeyError(f"Spalte fehlt: {name}")
    out: list[float] = []
    for v in run.columns[name]:
        if isinstance(v, (int, float)):
            out.append(float(v))
        else:
            out.append(_to_float(v))
    return out


def has_col(run: RunData, name: str) -> bool:
    return name in run.columns


def sample_float_cols_to_frames(
    run: RunData,
    *,
    time_axis_s: Sequence[float],
    duration_s: float,
    fps: float,
    cols: Sequence[str],
    target_times_s: Sequence[float] | None = None,
) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for col in cols:
        out[str(col)] = np.empty((0,), dtype=np.float64)
    if not cols:
        return out

    t = [float(v) for v in time_axis_s]
    if not t:
        return out

    if target_times_s is None:
        n_frames = int(math.floor(max(0.0, float(duration_s)) * float(fps)))
        if n_frames <= 0:
            n_frames = 1
        fps_safe = max(1.0, float(fps))
        q_times = [float(i) / fps_safe for i in range(n_frames)]
    else:
        q_times = [float(v) for v in target_times_s]

    n_out = len(q_times)
    if n_out <= 0:
        return out

    ys_by_col: dict[str, np.ndarray] = {}
    for col in cols:
        col_name = str(col)
        if not has_col(run, col_name):
            continue
        y = get_float_col(run, col_name)
        if (not y) or (len(y) != len(t)):
            continue
        ys_by_col[col_name] = np.asarray(y, dtype=np.float64)
        out[col_name] = np.empty((n_out,), dtype=np.float64)

    if not ys_by_col:
        return out

    n_t = len(t)
    if n_t == 1:
        for col_name, ys in ys_by_col.items():
            out[col_name].fill(float(ys[0]))
        return out

    j = 0
    for i in range(n_out):
        tq = float(q_times[i])
        if tq <= t[0]:
            use_last = False
            t0 = float(t[0])
            t1 = float(t[1])
            idx0 = 0
            idx1 = 1
            j = 0
        elif tq >= t[n_t - 1]:
            use_last = True
            t0 = 0.0
            t1 = 0.0
            idx0 = n_t - 1
            idx1 = n_t - 1
            j = max(0, n_t - 2)
        else:
            if j < 0:
                j = 0
            if j > n_t - 2:
                j = n_t - 2
            while j < (n_t - 2) and t[j + 1] <= tq:
                j += 1
            while j > 0 and t[j] > tq:
                j -= 1
            idx0 = j
            idx1 = j + 1
            t0 = float(t[idx0])
            t1 = float(t[idx1])
            use_last = False

        if use_last:
            for col_name, ys in ys_by_col.items():
                out[col_name][i] = float(ys[idx0])
            continue

        if t1 <= t0:
            for col_name, ys in ys_by_col.items():
                out[col_name][i] = float(ys[idx0])
            continue

        a = (tq - t0) / (t1 - t0)
        if a < 0.0:
            a = 0.0
        if a > 1.0:
            a = 1.0

        for col_name, ys in ys_by_col.items():
            v0 = float(ys[idx0])
            v1 = float(ys[idx1])
            out[col_name][i] = (v0 * (1.0 - a)) + (v1 * a)

    return out


def _require(cols: dict[str, list[Any]], name: str) -> None:
    if name not in cols:
        raise ValueError(f"Pflicht-Spalte fehlt: {name}")


def _parse_value(col_name: str, raw: str) -> Any:
    s = (raw or "").strip()

    # Booleans (Garage61 nutzt oft "true/false")
    if col_name in ("ABSActive", "DRSActive"):
        return _to_bool(s)

    # Ganzzahlen
    if col_name in ("Gear", "PositionType"):
        return _to_int(s)

    # Floats (die wir sehr oft brauchen)
    if col_name in (
        "Time_s",
        "Speed",
        "LapDistPct",
        "Lat",
        "Lon",
        "Brake",
        "Throttle",
        "RPM",
        "SteeringWheelAngle",
        "Yaw",
        "YawRate",
        "LatAccel",
        "LongAccel",
        "VertAccel",
        "Clutch",
    ):
        return _to_float(s)

    # Sonst als String belassen
    return s


def _to_float(s: str) -> float:
    if s == "":
        return float("nan")
    try:
        return float(s)
    except Exception:
        return float("nan")


def _to_int(s: str) -> int:
    if s == "":
        return 0
    try:
        # Garage61 kann bei "Gear" saubere ints liefern, bei anderen Feldern notfalls float->int
        if "." in s or "e" in s.lower():
            return int(float(s))
        return int(s)
    except Exception:
        return 0


def _to_bool(s: str) -> bool:
    t = s.lower()
    if t in ("true", "1", "yes", "y"):
        return True
    return False
