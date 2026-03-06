"""Shared typed aliases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JSONObject = dict[str, Any]


@dataclass(frozen=True)
class StaleConflict:
    stale_fields: list[str]
    current_versions: dict[str, int]
