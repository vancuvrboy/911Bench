"""Version compatibility and migration checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import VersionCompatibilityError

JSONObject = dict[str, Any]


@dataclass(frozen=True)
class CompatibilityConfig:
    server_version: str = "0.1.0"
    mcp_protocol_version: str = "2025-03-26"
    min_policy_major: int = 1
    max_policy_major: int = 1


class CompatibilityManager:
    def __init__(self, config: CompatibilityConfig | None = None) -> None:
        self.config = config or CompatibilityConfig()

    def validate_policy(self, policy: JSONObject) -> None:
        raw = str(policy.get("policy_version", "")).strip()
        if not raw:
            raise VersionCompatibilityError("missing_policy_version")
        major = self._parse_major(raw)
        if major < self.config.min_policy_major or major > self.config.max_policy_major:
            raise VersionCompatibilityError(
                f"incompatible_policy_version:{raw}:supported_major={self.config.min_policy_major}-{self.config.max_policy_major}"
            )

    def version_matrix(self, active_policy: JSONObject | None = None) -> JSONObject:
        return {
            "server_version": self.config.server_version,
            "mcp_protocol_version": self.config.mcp_protocol_version,
            "policy_version_support": {
                "min_major": self.config.min_policy_major,
                "max_major": self.config.max_policy_major,
            },
            "active_policy": {
                "policy_id": (active_policy or {}).get("policy_id"),
                "policy_version": (active_policy or {}).get("policy_version"),
            },
        }

    @staticmethod
    def _parse_major(version: str) -> int:
        head = version.split(".", 1)[0]
        try:
            return int(head)
        except ValueError as exc:
            raise VersionCompatibilityError(f"invalid_policy_version:{version}") from exc
