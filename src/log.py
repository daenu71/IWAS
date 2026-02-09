from __future__ import annotations

import os
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


def build_log_file_path(project_root: str | Path, name: str = "main") -> Path:
    root = Path(project_root).resolve()
    logs_dir = root / "_logs"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return logs_dir / f"{ts}_{name}.txt"


def make_logger(project_root: str | Path, name: str = "main", log_file: Path | None = None) -> Logger:
    if log_file is None:
        env_path = str(os.environ.get("IRVC_LOG_FILE") or "").strip()
        if env_path:
            log_file = Path(env_path)
        else:
            log_file = build_log_file_path(project_root, name)
    return Logger(log_file=log_file)
