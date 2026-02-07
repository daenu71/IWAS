from __future__ import annotations

# Shared HUD colors (RGBA). Keep exact values to preserve output.
COL_SLOW_DARKRED = (234, 0, 0, 255)
COL_SLOW_BRIGHTRED = (255, 137, 117, 255)
COL_FAST_DARKBLUE = (36, 0, 250, 255)
COL_FAST_BRIGHTBLUE = (1, 253, 255, 255)
COL_WHITE = (255, 255, 255, 255)

# HUD-name groups used by the orchestrator.
SCROLL_HUD_NAMES: set[str] = {
    "Throttle / Brake",
    "Steering",
    "Delta",
    "Line Delta",
    "Under-/Oversteer",
}

TABLE_HUD_NAMES: set[str] = {
    "Speed",
    "Gear & RPM",
}
