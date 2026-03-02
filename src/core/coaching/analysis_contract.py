"""Analysis Contract v1 – channel validation and hash logic."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_CONTRACT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "config" / "coaching" / "analysis_contract.json"
)


@dataclass
class ContractCheckResult:
    can_compute: bool
    missing_required: list[str]
    missing_optional: list[str]
    contract_hash: str


class AnalysisContract:
    """Loads analysis_contract.json and validates a channel list against it."""

    def __init__(self, contract_path: Path | str | None = None) -> None:
        path = Path(contract_path) if contract_path else _DEFAULT_CONTRACT_PATH
        raw = path.read_bytes()
        self._hash = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        self._required: list[str] = data["required_channels"]
        self._optional: list[str] = data["optional_channels"]

    @property
    def contract_hash(self) -> str:
        """SHA-256 hex digest of the raw JSON file."""
        return self._hash

    def check(self, channels: list[str]) -> ContractCheckResult:
        """Check *channels* against the contract.

        Returns a :class:`ContractCheckResult` with:
        - ``can_compute``: False when any required channel is absent.
        - ``missing_required``: required channels not found in *channels*.
        - ``missing_optional``: optional channels not found in *channels*.
        - ``contract_hash``: SHA-256 of the loaded JSON file.
        """
        channel_set = set(channels)
        missing_required = [c for c in self._required if c not in channel_set]
        missing_optional = [c for c in self._optional if c not in channel_set]
        return ContractCheckResult(
            can_compute=len(missing_required) == 0,
            missing_required=missing_required,
            missing_optional=missing_optional,
            contract_hash=self._hash,
        )
