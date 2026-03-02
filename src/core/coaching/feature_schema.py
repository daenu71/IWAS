"""Feature Schema v1 – Python-Wrapper und Hash-Vertrag."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "config" / "coaching" / "feature_schema_v1.json"
)


@dataclass
class FeatureDefinition:
    id: str
    label: str
    unit: str
    output_type: str
    requires: list[str]
    requires_optional: list[str]
    low_confidence: bool


class FeatureSchema:
    """Loads feature_schema_v1.json and exposes feature lookups."""

    def __init__(
        self,
        schema_hash: str,
        groups: dict[str, list[FeatureDefinition]],
    ) -> None:
        self._hash = schema_hash
        self._groups = groups
        self._by_id: dict[str, FeatureDefinition] = {
            f.id: f
            for features in groups.values()
            for f in features
        }

    @classmethod
    def load(cls, path: Path | str | None = None) -> FeatureSchema:
        """Load from *path* (default: ``config/coaching/feature_schema_v1.json``)."""
        resolved = Path(path) if path else _DEFAULT_SCHEMA_PATH
        raw = resolved.read_bytes()
        schema_hash = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        groups: dict[str, list[FeatureDefinition]] = {}
        for group_id, entries in data["groups"].items():
            groups[group_id] = [
                FeatureDefinition(
                    id=e["id"],
                    label=e["label"],
                    unit=e["unit"],
                    output_type=e["output_type"],
                    requires=e["requires"],
                    requires_optional=e.get("requires_optional", []),
                    low_confidence=e.get("low_confidence", False),
                )
                for e in entries
            ]
        return cls(schema_hash=schema_hash, groups=groups)

    @property
    def schema_hash(self) -> str:
        """SHA-256 hex digest of the raw JSON file."""
        return self._hash

    def features_for_group(self, group_id: str) -> list[FeatureDefinition]:
        """Return all features belonging to *group_id*, empty list if unknown."""
        return list(self._groups.get(group_id, []))

    def required_channels_for_feature(self, feature_id: str) -> list[str]:
        """Return the ``requires`` list for *feature_id*, empty list if unknown."""
        feature = self._by_id.get(feature_id)
        if feature is None:
            return []
        return list(feature.requires)
