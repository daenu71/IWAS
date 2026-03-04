"""Lap analysis entry point — bridges flat Sprint-1 storage to AnalysisCache pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from .analysis_cache import AnalysisCache


def analyze_lap(session_dir: Path, run_id: int, lap_no: int) -> bool:
    """Run full Sprint-2 analysis pipeline for one lap.

    Reads flat Sprint-1 meta (run_XXXX_lap_YYYY_meta.json), creates the
    expected laps/lap_YYYY/ directory structure, then calls AnalysisCache.compute().

    Returns True if analysis produced a result (computed/partial), False on error.
    """
    session_dir = Path(session_dir)

    # Build the lap directory AnalysisCache expects
    lap_dir = session_dir / "laps" / f"lap_{lap_no:04d}"
    lap_dir.mkdir(parents=True, exist_ok=True)

    # Read the flat Sprint-1 lap meta and translate key names
    flat_meta_path = session_dir / f"run_{run_id:04d}_lap_{lap_no:04d}_meta.json"
    adapted_meta: dict = {}
    if flat_meta_path.exists():
        try:
            raw = json.loads(flat_meta_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        adapted_meta = {
            # index keys AnalysisCache._resolve_start/end_idx recognises
            "start_idx": raw.get("lap_start_sample"),
            "end_idx": raw.get("lap_end_sample"),
            # validity flags AnalysisCache._lap_validity_meta recognises
            "incomplete": not raw.get("lap_complete", True),
            "offtrack": raw.get("offtrack_surface", False),
            # keep originals as well for traceability
            **raw,
        }

    lap_meta_path = lap_dir / "lap_meta.json"
    try:
        lap_meta_path.write_text(json.dumps(adapted_meta, indent=2), encoding="utf-8")
    except Exception:
        pass

    try:
        cache = AnalysisCache()
        result = cache.compute(lap_dir)
        status = result.get("status", "blocked")
        return status in {"computed", "partial"}
    except Exception:
        return False
