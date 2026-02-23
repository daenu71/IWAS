from __future__ import annotations

import os
import subprocess
from typing import Any


def windows_no_window_subprocess_kwargs() -> dict[str, Any]:
    """
    Hide spawned console windows on Windows (ffmpeg/ffprobe/etc.).
    No-op on non-Windows platforms.
    """
    if os.name != "nt":
        return {}

    kwargs: dict[str, Any] = {}

    try:
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if creationflags:
            kwargs["creationflags"] = creationflags
    except Exception:
        pass

    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
        si.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        kwargs["startupinfo"] = si
    except Exception:
        pass

    return kwargs
