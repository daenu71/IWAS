from __future__ import annotations

# Central list of telemetry request specs for Sprint 1.
# Keep order stable: resolver/recorder preserve this order for recorded/missing lists.
# Array-like items are resolver specs and are expanded to flat columns using IRSDK headers.
REQUESTED_CHANNELS: list[str] = [
    # 2.2.1 Session / system
    "SessionTime",
    "SessionState",
    "SessionUniqueID",
    "SessionFlags",
    # 2.2.2 Rundendaten
    "Lap",
    "LapCompleted",
    "LapDist",
    "LapDistPct",
    "LapCurrentLapTime",
    "LapLastLapTime",
    "LapBestLapTime",
    "LapDeltaToBestLap",
    "LapDeltaToSessionBestLap",
    "LapDeltaToSessionOptimalLap",
    "LapDeltaToOptimalLap",
    # 2.2.3 Fahrzeugbewegung
    "Speed",
    "Yaw",
    "Pitch",
    "Roll",
    "VelocityX",
    "VelocityY",
    "VelocityZ",
    "VelocityLocalX",
    "VelocityLocalY",
    "VelocityLocalZ",
    "YawRate",
    "LatAccel",
    "LongAccel",
    "VertAccel",
    # 2.2.4 Eingaben
    "Throttle",
    "Brake",
    "Clutch",
    "SteeringWheelAngle",
    "SteeringWheelTorque",
    "SteeringWheelPctTorque",
    # 2.2.5 Motor / Getriebe
    "RPM",
    "Gear",
    "FuelLevel",
    "FuelLevelPct",
    "FuelUsePerHour",
    # 2.2.6 Reifen / Suspension (resolver expands to flat columns)
    "ShockDefl[4]",
    "RideHeight[4]",
    "TireTemp[4][L/M/R]",
    "TirePressure[4]",
    # 2.2.7 Elektronik
    "ABSactive",
    "TractionControl",
    "TractionControlActive",
    "BrakeBias",
    # 2.2.8 Position / Umwelt
    "Lat",
    "Lon",
    "Alt",
    "TrackTemp",
    "AirTemp",
    # 2.2.9 Zusatzfelder (telemetry only)
    "OnPitRoad",
    "IsOnTrack",
    "IsOnTrackCar",
]
