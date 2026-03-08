"""Domain errors for the standalone 911Bench SIM server."""

from __future__ import annotations


class SimError(Exception):
    """Base simulation engine error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


class ValidationError(SimError):
    """Schema validation error."""


class StateError(SimError):
    """Invalid lifecycle state transition or operation."""


class ConflictError(SimError):
    """Optimistic concurrency conflict."""
