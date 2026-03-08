"""Schema loading and light validation helpers for SIM seeds/events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sim_server.errors import ValidationError


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError("invalid_json", f"{path}: {exc}") from exc


def validate_seed_triplet(caller_json: dict[str, Any], incident_json: dict[str, Any], qa_template_json: dict[str, Any]) -> None:
    _require(caller_json, "version", "caller.json missing version")
    _require(caller_json, "profile_id", "caller.json missing profile_id")
    _require(caller_json, "identity", "caller.json missing identity")
    if not isinstance(caller_json.get("identity"), dict) or not caller_json["identity"].get("name"):
        raise ValidationError("seed_validation_error", "caller.json missing identity.name")
    allowed_disclosure = {"volunteer", "on_ask_once", "on_ask_twice", "after_reassurance", "refuse"}
    disclosure = caller_json.get("disclosure_policy", {})
    if isinstance(disclosure, dict):
        for key, value in disclosure.items():
            if value not in allowed_disclosure:
                raise ValidationError("seed_validation_error", f"caller.json disclosure_policy invalid value for {key}")

    _require(incident_json, "id", "incident.json missing id")
    incident_type = incident_json.get("type")
    if incident_type not in {"Fire", "Police", "EMS"}:
        raise ValidationError("seed_validation_error", "incident.json invalid type enum")
    frd = incident_json.get("first_responder_delay")
    if not isinstance(frd, int) or frd < 0:
        raise ValidationError("seed_validation_error", "incident.json first_responder_delay must be >= 0")

    _require(qa_template_json, "templates", "qaTemplate missing templates")
    templates = qa_template_json.get("templates", {})
    if "COMMON" not in templates:
        raise ValidationError("seed_validation_error", "qaTemplate missing COMMON section")


def validate_event_minimal(event: dict[str, Any]) -> None:
    for field in ("event_type", "ts", "event_seq"):
        if field not in event:
            raise ValidationError("event_validation_error", f"event missing {field}")


# Minimal conformance validator used by harness without extra dependencies.
def validate_event_against_schema_like_rules(event: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    event_type = event.get("event_type")
    required_by_type = {
        "meta": ["execution_id", "scenario_id", "incident_id", "policy_id", "policy_hash", "schema_version"],
        "system": ["turn", "subtype", "text"],
        "conversation": ["turn", "call_taker", "caller"],
        "tool_call": [
            "turn",
            "tool_call_id",
            "tool_name",
            "args",
            "fields_updated",
            "field_count",
            "actor",
            "new_record_version",
            "new_field_versions",
        ],
        "cad_patch_applied": [
            "action_id",
            "action_class",
            "proposer_agent_id",
            "policy_id",
            "policy_hash",
            "payload",
            "original_payload",
            "new_record_version",
            "new_field_versions",
        ],
        "checkpoint_decision": ["request_id", "action_class", "decision", "approver_role", "latency_ms"],
        "episode_end": ["reason", "total_turns", "total_events", "final_record_version", "final_cad_state", "duration_ms"],
        "governance_correlation": ["action_id", "matched_rule_ids", "policy_id"],
    }
    if event_type not in {
        "meta",
        "system",
        "conversation",
        "tool_call",
        "cad_patch_applied",
        "checkpoint_decision",
        "escalation",
        "episode_end",
        "governance_correlation",
        "ng911_message",
        "qa_score",
    }:
        errors.append("event_type_invalid")
        return errors
    for req in required_by_type.get(event_type, []):
        if req not in event:
            errors.append(f"missing:{req}")
    return errors


def _require(obj: dict[str, Any], field: str, message: str) -> None:
    if field not in obj:
        raise ValidationError("seed_validation_error", message)
