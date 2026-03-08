"""Deterministic call-taker agent for SIM Phase 1 integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CTDecision:
    text: str
    cad_updates: dict[str, Any]
    end_call: bool = False
    end_reason: str | None = None
    end_reason_detail: str | None = None
    parse_error: bool = False
    checkpoint_decisions: list[dict[str, Any]] = field(default_factory=list)
    call_taker_metadata: dict[str, Any] | None = None


class CallTakerAgent:
    def __init__(
        self,
        incident_json: dict[str, Any],
        temperature: float = 0.0,
        dispatch_enabled: bool = True,
        resolve_no_dispatch_turn: int | None = None,
        parse_fail_turns: set[int] | None = None,
        ask_name_turn: int | None = None,
    ) -> None:
        self.incident = incident_json
        self.temperature = temperature
        self.turn = 0
        self.greeting = "911, what is your emergency?"
        self.dispatch_enabled = dispatch_enabled
        self.resolve_no_dispatch_turn = resolve_no_dispatch_turn
        self.parse_fail_turns = set(parse_fail_turns or set())
        self.ask_name_turn = ask_name_turn

    def next_turn(
        self,
        caller_text: str,
        cad_state: dict[str, Any],
        system_events: list[dict[str, Any]],
        pending_checkpoints: list[dict[str, Any]] | None = None,
    ) -> CTDecision:
        self.turn += 1
        caller_low = caller_text.lower()

        if any(ev.get("subtype") == "responders_arrived" for ev in system_events):
            return CTDecision(
                text="Responders are now with you. We are ending this call.",
                cad_updates={},
                end_call=True,
                end_reason="responders_arrived",
            )

        if self.turn == 1:
            return CTDecision(text=self.greeting, cad_updates={})

        if self.turn in self.parse_fail_turns:
            # Simulate malformed model output with deterministic fallback.
            return CTDecision(
                text="I did not receive a valid structured update. Continuing with verbal guidance.",
                cad_updates={},
                parse_error=True,
            )
        if self.ask_name_turn is not None and self.turn == self.ask_name_turn:
            return CTDecision(text="Please confirm your full name for the record.", cad_updates={})

        updates: dict[str, Any] = {}
        if "location" not in cad_state:
            # Use seed location as best available address when caller indicates location.
            if any(k in caller_low for k in ("main", "granville", "broadway", "oak", "king", "address", "at ")):
                updates["location"] = str(self.incident.get("location", {}).get("address_line", "Unknown"))

        incident_type = str(self.incident.get("type", "Unknown"))
        if "incident_type" not in cad_state:
            updates["incident_type"] = incident_type

        if self.dispatch_enabled and self.turn <= 4 and not bool(cad_state.get("dispatch_triggered", False)):
            updates["dispatch_triggered"] = True
            updates["priority"] = "high"

        question = self._followup_question(incident_type, cad_state)
        if updates:
            return CTDecision(text=question, cad_updates=updates)
        if self.turn <= 4:
            return CTDecision(text=question, cad_updates={})
        if self.resolve_no_dispatch_turn is not None and self.turn >= self.resolve_no_dispatch_turn:
            return CTDecision(
                text="Based on the information provided, no dispatch is required and we can close this call.",
                cad_updates={},
                end_call=True,
                end_reason="resolved_no_dispatch",
            )
        return CTDecision(text="Stay on the line and keep me updated.", cad_updates={})

    def _followup_question(self, incident_type: str, cad_state: dict[str, Any]) -> str:
        if "location" not in cad_state:
            return "Please confirm your exact location and nearest cross street."
        if incident_type == "Fire":
            return "Are there occupants inside, and do you see any hazards or exposures?"
        if incident_type == "Police":
            return "Are there weapons involved and is anyone currently in immediate danger?"
        if incident_type == "EMS":
            return "Is the patient conscious and breathing right now?"
        return "Please provide any additional details that help responders."
