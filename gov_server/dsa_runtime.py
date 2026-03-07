"""Deterministic DSA runtime adapters used by governance service."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .dsa import DSAProfile
from .types import JSONObject


def _sha256_json(obj: JSONObject) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_transcript_text(context_snapshot: JSONObject) -> str:
    chunks: list[str] = []
    for row in context_snapshot.get("transcript", []):
        if not isinstance(row, dict):
            continue
        text = row.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    return " ".join(chunks).strip()


@dataclass(frozen=True)
class DSAAdvice:
    profile_id: str
    provider: str
    model: str
    mode: str
    action_class: str
    context_hash: str
    proposal_hash: str
    suggestions: list[JSONObject]
    chosen_payload: JSONObject
    chosen_payload_source: str
    selection_score: float

    def to_json(self) -> JSONObject:
        return {
            "profile_id": self.profile_id,
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "action_class": self.action_class,
            "context_hash": self.context_hash,
            "proposal_hash": self.proposal_hash,
            "suggestions": self.suggestions,
            "chosen_payload": self.chosen_payload,
            "chosen_payload_source": self.chosen_payload_source,
            "selection_score": self.selection_score,
        }


class DSAExecutionError(RuntimeError):
    """Raised when a selected DSA profile cannot be executed."""


class Deterministic911BuddyRuntime:
    """Simple deterministic advisory shim for research-phase DSA integration."""

    def advise(
        self,
        profile: DSAProfile,
        proposal: JSONObject,
        context_snapshot: JSONObject,
        apply_suggestion: bool = False,
    ) -> DSAAdvice:
        action_class = str(proposal.get("action_class", ""))
        proposed_payload = proposal.get("proposed_payload", {})
        if not isinstance(proposed_payload, dict):
            proposed_payload = {}

        transcript_text = _extract_transcript_text(context_snapshot)
        suggestion_payload = self._suggest_payload(action_class, proposed_payload, transcript_text)
        confidence = float(proposal.get("uncertainty", {}).get("p_correct", 0.5) or 0.5)
        suggestions = [
            {
                "rank": 1,
                "action_class": action_class,
                "payload": suggestion_payload,
                "confidence": max(0.0, min(1.0, confidence)),
                "rationale": "deterministic_911buddy_baseline",
            }
        ]

        chosen_payload = suggestion_payload if apply_suggestion else proposed_payload
        chosen_source = "dsa_suggestion" if apply_suggestion else "client_proposal"

        context_hash = _sha256_json(
            {
                "incident_id": proposal.get("incident_id", ""),
                "action_class": action_class,
                "transcript": context_snapshot.get("transcript", []),
                "cad_view": context_snapshot.get("cad_view", {}),
            }
        )
        proposal_hash = _sha256_json(
            {
                "action_id": proposal.get("action_id", ""),
                "action_class": action_class,
                "proposed_payload": proposed_payload,
            }
        )
        return DSAAdvice(
            profile_id=profile.id,
            provider=profile.provider,
            model=profile.model,
            mode=profile.mode,
            action_class=action_class,
            context_hash=context_hash,
            proposal_hash=proposal_hash,
            suggestions=suggestions,
            chosen_payload=chosen_payload,
            chosen_payload_source=chosen_source,
            selection_score=float(suggestions[0]["confidence"]),
        )

    def _suggest_payload(self, action_class: str, proposed_payload: JSONObject, transcript_text: str) -> JSONObject:
        if action_class != "cad_update.address":
            return dict(proposed_payload)

        suggestion = dict(proposed_payload)
        if suggestion.get("location"):
            return suggestion
        extracted = self._extract_address(transcript_text)
        if extracted:
            suggestion["location"] = extracted
        return suggestion

    @staticmethod
    def _extract_address(text: str) -> str | None:
        if not text:
            return None
        match = re.search(r"\b\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9\s.-]{2,40}", text)
        if not match:
            return None
        return match.group(0).strip()


class DSAOrchestrator:
    """Routes DSA profile execution and supports multi-profile strategies."""

    def __init__(self) -> None:
        self._deterministic = Deterministic911BuddyRuntime()

    def advise(self, profile: DSAProfile, proposal: JSONObject, context_snapshot: JSONObject, apply_suggestion: bool) -> DSAAdvice:
        provider = profile.provider.lower().strip()
        if provider == "builtin":
            return self._deterministic.advise(
                profile=profile,
                proposal=proposal,
                context_snapshot=context_snapshot,
                apply_suggestion=apply_suggestion,
            )
        if provider == "openai":
            raise DSAExecutionError("openai_profile_not_enabled_in_research_server")
        raise DSAExecutionError(f"unsupported_dsa_provider:{provider}")
