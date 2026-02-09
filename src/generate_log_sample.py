import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

from core.render_service import _log_stage_durations

log_dir = Path('..') / '_logs'
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / 'duration_sample_test.txt'
_log_stage_durations(log_file, start=0.0, prep_end=2.381, render_start=2.381, render_end=18.425, final_end=21.102)
