"""Deterministic QA evaluator agent for SIM Phase 1 integration."""

from __future__ import annotations

import hashlib
from typing import Any


class QAEvaluatorAgent:
    def __init__(
        self,
        qa_template_json: dict[str, Any],
        temperature: float = 0.0,
        parse_retry_max: int = 2,
        simulate_parse_fail_once: bool = False,
    ) -> None:
        self.template = qa_template_json
        self.temperature = temperature
        self.model_id = "qa-deterministic-v1"
        self.parse_retry_max = parse_retry_max
        self.simulate_parse_fail_once = simulate_parse_fail_once
        self._sim_fail_consumed = False

    def evaluate(self, events: list[dict[str, Any]], incident_type: str) -> dict[str, Any]:
        merged_sections = self._merged_sections(incident_type)
        items = []
        awarded = 0.0
        possible = 0.0
        parse_retries = 0

        text_blob = "\n".join(
            f"{ev.get('call_taker','')} {ev.get('caller','')}"
            for ev in events
            if ev.get("event_type") == "conversation"
        ).lower()

        for section in merged_sections:
            for item in section.get("items", []):
                item_id = str(item.get("id", "unknown"))
                points = float(item.get("points", 0))
                possible += points
                # Simulate one malformed parse, then retry up to parse_retry_max.
                if self.simulate_parse_fail_once and not self._sim_fail_consumed:
                    self._sim_fail_consumed = True
                    parse_retries += 1
                    if parse_retries > self.parse_retry_max:
                        raise ValueError("qa_parse_retry_exhausted")
                yes = self._heuristic_yes(item_id=item_id, prompt=str(item.get("question", "")).lower(), text=text_blob)
                answer = "YES" if yes else "NO"
                item_awarded = points if yes else 0.0
                awarded += item_awarded
                items.append(
                    {
                        "id": item_id,
                        "answer": answer,
                        "points_awarded": item_awarded,
                        "points_possible": points,
                        "rationale": "deterministic_heuristic",
                        "evidence_turns": self._turn_refs(events, yes),
                    }
                )

        normalize_to = float(self.template.get("normalize_to", 100))
        normalized = (awarded / possible * normalize_to) if possible > 0 else 0.0

        return {
            "evaluator_agent_id": "qa-agent",
            "qa_template_id": str(self.template.get("version", "unknown")),
            "incident_type": incident_type,
            "sections_applied": ["COMMON", incident_type.upper()],
            "items": items,
            "total_points_awarded": awarded,
            "total_points_possible": possible,
            "normalized_score": normalized,
            "evaluator_model": self.model_id,
            "evaluator_prompt_hash": hashlib.sha256(b"deterministic-qa-prompt").hexdigest(),
            "parse_retry_count": parse_retries,
        }

    def _merged_sections(self, incident_type: str) -> list[dict[str, Any]]:
        templates = self.template.get("templates", {})
        common = (templates.get("COMMON") or {}).get("sections", [])
        specific = (templates.get(incident_type.upper()) or {}).get("sections", [])
        return list(common) + list(specific)

    def _heuristic_yes(self, item_id: str, prompt: str, text: str) -> bool:
        if "location" in prompt:
            return "location" in text or "address" in text or "main" in text or "granville" in text
        return (hash(item_id) % 2) == 0

    def _turn_refs(self, events: list[dict[str, Any]], include: bool) -> list[int]:
        if not include:
            return []
        turns = [int(ev.get("turn", 0)) for ev in events if ev.get("event_type") == "conversation"]
        return turns[:2]
