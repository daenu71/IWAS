from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
