from __future__ import annotations

# Central list of channels we want to record when available.
# Keep order stable: recorder discovery preserves this order for recorded/missing lists.
REQUESTED_CHANNELS: list[str] = [
    # Session / state
    "SessionTime",
    "SessionState",
    "SessionFlags",
    # Lap / run detection
    "Lap",
    "LapCompleted",
    "LapDistPct",
    "OnPitRoad",
    "IsOnTrackCar",
    # Vehicle / controls
    "Speed",
    "RPM",
    "Gear",
    "Throttle",
    "Brake",
    "SteeringWheelAngle",
]
