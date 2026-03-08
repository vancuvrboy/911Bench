"""Deterministic caller agent for SIM Phase 1 integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DISCLOSURE_FIELDS = {
    "name": ("name",),
    "callback_number": ("phone", "number", "callback"),
    "current_location": ("where", "address", "location"),
    "relationship_to_incident": ("relationship",),
    "people_involved": ("who", "people"),
    "hazards_or_weapons": ("weapon", "hazard"),
}


@dataclass
class CallerState:
    turn: int = 0
    disclosed: dict[str, bool] = field(default_factory=dict)
    emotional_state: str = "concerned"


class CallerAgent:
    def __init__(self, caller_json: dict[str, Any], incident_json: dict[str, Any], temperature: float = 0.3) -> None:
        self.caller = caller_json
        self.incident = incident_json
        self.temperature = temperature
        self.state = CallerState(disclosed={k: False for k in DISCLOSURE_FIELDS})
        self.profile_id = str(caller_json.get("profile_id", "CALLER-UNKNOWN"))
        self._opening = str(incident_json.get("caller_view", {}).get("initial_opening_line", "I need help."))
        self._progression = {
            int(item.get("turn_offset", -1)): str(item.get("note", ""))
            for item in incident_json.get("progression_by_turn", [])
            if isinstance(item, dict)
        }

    def next_turn(self, call_taker_text: str, system_events: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        self.state.turn += 1
        low = call_taker_text.lower()

        # System-event-aware closing behavior.
        if any(ev.get("subtype") == "responders_arrived" for ev in system_events):
            self.state.emotional_state = "relieved"
            text = "Okay, I can see responders now. Thank you."
            return text, self._metadata(progression_note=None)

        if self.state.turn == 1:
            return self._opening, self._metadata(progression_note=self._progression.get(0))

        emitted: list[str] = []
        for field, triggers in DISCLOSURE_FIELDS.items():
            policy = str((self.caller.get("disclosure_policy", {}) or {}).get(field, "on_ask_once"))
            asked = any(token in low for token in triggers)
            if self.state.disclosed.get(field):
                continue
            if policy == "volunteer" and self.state.turn <= 2:
                emitted.append(self._value_for(field))
                self.state.disclosed[field] = True
            elif policy == "on_ask_once" and asked:
                emitted.append(self._value_for(field))
                self.state.disclosed[field] = True
            elif policy == "on_ask_twice" and asked:
                key = f"ask_count_{field}"
                count = int(self.state.disclosed.get(key, 0)) + 1
                self.state.disclosed[key] = count
                if count >= 2:
                    emitted.append(self._value_for(field))
                    self.state.disclosed[field] = True
            elif policy == "after_reassurance" and asked and any(word in low for word in ("safe", "okay", "help is on the way")):
                emitted.append(self._value_for(field))
                self.state.disclosed[field] = True

        progression_note = self._progression.get(self.state.turn)
        if progression_note:
            emitted.append(progression_note)

        if not emitted:
            emitted.append("I understand. I am staying on the line.")

        disfluencies = ((self.caller.get("speech", {}) or {}).get("disfluencies") or [])
        if disfluencies and self.state.turn % 2 == 0:
            emitted.insert(0, str(disfluencies[0]))

        text = " ".join(emitted)
        return text, self._metadata(progression_note=progression_note)

    def _value_for(self, field: str) -> str:
        identity = self.caller.get("identity", {}) or {}
        loc = self.incident.get("location", {}) or {}
        samples = self.caller.get("example_short_answers", {}) or {}
        if field == "name":
            return f"My name is {identity.get('name', samples.get('name', 'unknown'))}."
        if field == "callback_number":
            return f"My callback is {identity.get('phone_number', samples.get('callback_number', 'unknown'))}."
        if field == "current_location":
            return f"I am at {loc.get('address_line', 'the reported location')}."
        if field == "relationship_to_incident":
            return str(samples.get("relationship_to_incident", "I am nearby."))
        if field == "people_involved":
            return str(samples.get("people_involved", "I can see two people."))
        if field == "hazards_or_weapons":
            return "I do not see weapons, but there is a hazard."
        return ""

    def _metadata(self, progression_note: str | None) -> dict[str, Any]:
        return {
            "emotional_state": self.state.emotional_state,
            "disclosure_tracker": {k: bool(v) for k, v in self.state.disclosed.items() if not k.startswith("ask_count_")},
            "progression_note": progression_note,
        }
