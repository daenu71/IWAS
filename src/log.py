from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Logger:
    log_file: Path

    def kv(self, key: str, value) -> None:
        line = f"{key}={value}"
        self._write(line)

    def msg(self, text: str) -> None:
        self._write(text)

    def _write(self, line: str) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")


def make_logger(project_root: str | Path, name: str = "main") -> Logger:
    root = Path(project_root).resolve()
    logs_dir = root / "_logs"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = logs_dir / f"{ts}_{name}.txt"
    return Logger(log_file=log_file)
