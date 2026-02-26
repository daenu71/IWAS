from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionMeta:
    recorded_channels: list[str] = field(default_factory=list)
    missing_channels: list[Any] = field(default_factory=list)
    sample_hz: float | int = 0
    dtype_decisions: dict[str, str] = field(default_factory=dict)

    def to_dict(self, base: dict[str, Any] | None = None) -> dict[str, Any]:
        data: dict[str, Any] = dict(base or {})
        data["recorded_channels"] = list(self.recorded_channels)
        data["missing_channels"] = list(self.missing_channels)
        data["sample_hz"] = self.sample_hz
        data["dtype_decisions"] = dict(self.dtype_decisions)
        return data
