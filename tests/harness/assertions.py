"""Assertion helpers for governance harness expected outcomes."""

from __future__ import annotations

from typing import Any


class AssertionMismatch(Exception):
    pass


def assert_outcome(
    outcome: dict[str, Any],
    expected: dict[str, Any],
    checkpoint_invoked: bool,
    escalation_invoked: bool,
) -> None:
    decision = expected.get("decision")
    if decision and outcome.get("decision") != decision:
        raise AssertionMismatch(f"decision_mismatch:{outcome.get('decision')}!= {decision}")

    denial_rule_id = expected.get("denial_rule_id")
    if denial_rule_id and outcome.get("denial_rule_id") != denial_rule_id:
        raise AssertionMismatch("denial_rule_id_mismatch")

    denial_reason = expected.get("denial_reason")
    if denial_reason and outcome.get("denial_reason") != denial_reason:
        raise AssertionMismatch(f"denial_reason_mismatch:{outcome.get('denial_reason')}!= {denial_reason}")

    denial_reason_contains = expected.get("denial_reason_contains")
    if denial_reason_contains and denial_reason_contains not in str(outcome.get("denial_reason")):
        raise AssertionMismatch("denial_reason_contains_mismatch")

    if "degraded_mode_available" in expected:
        if bool(outcome.get("degraded_mode_available")) != bool(expected["degraded_mode_available"]):
            raise AssertionMismatch("degraded_mode_mismatch")

    if "audit_emitted" in expected:
        has_audit = any(step.get("step") == "audit" for step in outcome.get("enforcement_trace", []))
        if bool(expected["audit_emitted"]) != has_audit:
            raise AssertionMismatch("audit_mismatch")

    if "checkpoint_invoked" in expected and bool(expected["checkpoint_invoked"]) != checkpoint_invoked:
        raise AssertionMismatch("checkpoint_invocation_mismatch")

    if "escalation_invoked" in expected and bool(expected["escalation_invoked"]) != escalation_invoked:
        raise AssertionMismatch("escalation_invocation_mismatch")

    checkpoint_response = expected.get("checkpoint_response")
    if checkpoint_response:
        actual = ((outcome.get("checkpoint") or {}).get("response"))
        if actual != checkpoint_response:
            raise AssertionMismatch(f"checkpoint_response_mismatch:{actual}!= {checkpoint_response}")

    escalation_resolution = expected.get("escalation_resolution")
    if escalation_resolution:
        actual = ((outcome.get("escalation") or {}).get("resolution"))
        if actual != escalation_resolution:
            raise AssertionMismatch(f"escalation_resolution_mismatch:{actual}!= {escalation_resolution}")

    required_trace = expected.get("enforcement_trace_steps", [])
    if required_trace:
        actual = [(s.get("step"), s.get("result")) for s in outcome.get("enforcement_trace", [])]
        cursor = 0
        for item in required_trace:
            target = (item.get("step"), item.get("result"))
            while cursor < len(actual) and actual[cursor] != target:
                cursor += 1
            if cursor >= len(actual):
                raise AssertionMismatch(f"missing_trace_step:{target[0]}:{target[1]}")
            cursor += 1

    if "conflict_detail_has_fields" in expected:
        conflict = outcome.get("conflict_detail") or {}
        stale = set(conflict.get("stale_fields", []))
        required = set(expected["conflict_detail_has_fields"])
        if not required.issubset(stale):
            raise AssertionMismatch("conflict_fields_mismatch")

    if "predicate_result" in expected:
        detail = _find_validate_or_rule_detail(outcome)
        actual = bool(detail.get("predicate_result")) if isinstance(detail, dict) and "predicate_result" in detail else None
        if actual is not None and actual != bool(expected["predicate_result"]):
            raise AssertionMismatch("predicate_result_mismatch")

    if "significant_behavior_difference" in expected:
        detail = _find_validate_or_rule_detail(outcome)
        actual = bool(detail.get("significant")) if isinstance(detail, dict) else False
        if actual != bool(expected["significant_behavior_difference"]):
            raise AssertionMismatch("significance_mismatch")

    if "audit_level" in expected:
        actual_level = "standard"
        for step in outcome.get("enforcement_trace", []):
            if step.get("step") == "audit":
                detail = step.get("detail") or {}
                if isinstance(detail, dict):
                    actual_level = str(detail.get("level", "standard"))
                break
        if actual_level != expected["audit_level"]:
            raise AssertionMismatch(f"audit_level_mismatch:{actual_level}!= {expected['audit_level']}")


def normalize_for_determinism(outcome: dict[str, Any]) -> dict[str, Any]:
    import json

    obj = json.loads(json.dumps(outcome))
    obj["audit_ref"] = "<normalized>"
    for step in obj.get("enforcement_trace", []):
        step["duration_ms"] = 0
        detail = step.get("detail")
        if isinstance(detail, dict):
            detail.pop("request_id", None)
            if "latency_ms" in detail:
                detail["latency_ms"] = 0
    if isinstance(obj.get("checkpoint"), dict):
        obj["checkpoint"]["request_id"] = "<normalized>"
        obj["checkpoint"]["latency_ms"] = 0
    if isinstance(obj.get("escalation"), dict):
        obj["escalation"]["latency_ms"] = 0
    return obj


def _find_validate_or_rule_detail(outcome: dict[str, Any]) -> dict[str, Any]:
    for step in outcome.get("enforcement_trace", []):
        detail = step.get("detail")
        if isinstance(detail, dict):
            return detail
    return {}
