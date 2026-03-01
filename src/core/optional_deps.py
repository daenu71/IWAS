"""Runtime module for core/optional_deps.py."""

from __future__ import annotations

import importlib
import importlib.util

_CV2_MISSING_MSG = "Video features unavailable: OpenCV (cv2) not installed.\nInstall: pip install opencv-python"


def has_cv2() -> bool:
    """Return whether cv2."""
    try:
        return importlib.util.find_spec("cv2") is not None
    except Exception:
        return False


def try_import_cv2() -> tuple[bool, str | None]:
    """Implement try import cv2 logic."""
    try:
        importlib.import_module("cv2")
        return True, None
    except ModuleNotFoundError as exc:
        text = str(exc)
        if getattr(exc, "name", None) == "cv2" or "No module named 'cv2'" in text or 'No module named "cv2"' in text:
            return False, _CV2_MISSING_MSG
        return False, f"Video features unavailable: {text}"
    except Exception as exc:
        return False, f"Video features unavailable: {exc}"
