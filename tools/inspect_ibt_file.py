from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from core.coaching.ibt_track_extractor import build_ibt_inventory_report, write_ibt_inventory_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect one or more iRacing .ibt files and write inventory reports.")
    parser.add_argument("ibt_files", nargs="+", help="Path(s) to .ibt files")
    parser.add_argument(
        "--output-dir",
        default=str(_ROOT / "_logs" / "ibt_inventory"),
        help="Directory for JSON reports (default: _logs/ibt_inventory)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    exit_code = 0
    for raw_path in args.ibt_files:
        ibt_path = Path(raw_path)
        try:
            report = build_ibt_inventory_report(ibt_path)
            written = write_ibt_inventory_report(ibt_path, output_dir=output_dir, report=report)
        except Exception as exc:
            exit_code = 1
            print(f"ERROR {ibt_path}: {exc}")
            continue

        file_summary = report.get("file_summary") or {}
        recommendation = report.get("recommendation") or {}
        viability = report.get("lat_lon_centerline_viability") or {}
        print(
            f"REPORT {written}\n"
            f"  track={file_summary.get('track_display_name') or 'unknown'}"
            f" config={file_summary.get('track_config_name') or '-'}"
            f" session={file_summary.get('session_type') or 'unknown'}\n"
            f"  recommendation={recommendation.get('summary') or 'n/a'}\n"
            f"  viability={viability.get('status') or 'n/a'}"
            f" joint_valid={viability.get('joint_valid_sample_count')}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
