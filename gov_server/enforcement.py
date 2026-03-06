"""Deterministic governance enforcement engine (Architecture Section 3.6)."""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field

from .evidence import EvidenceValidator
from .policy_loader import PolicyBundle
from .predicates import PredicateContext, PredicateEngine
from .schema_utils import SchemaError, validate_payload, validate_required
from .shims import CheckpointResponse, CheckpointShim, PlantStateShim
from .types import JSONObject


@dataclass
class StepTimer:
    name: str
    started_at: float = field(default_factory=time.perf_counter)

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)


class Engine:
    def __init__(
        self,
        policy_bundle: PolicyBundle,
        plant: PlantStateShim,
        checkpoint: CheckpointShim,
        predicate_engine: PredicateEngine | None = None,
        max_escalation_depth: int = 2,
    ) -> None:
        self.bundle = policy_bundle
        self.plant = plant
        self.checkpoint = checkpoint
        self.predicate_engine = predicate_engine or PredicateEngine()
        self.evidence_validator = EvidenceValidator(policy_bundle.evidence_config)
        self.max_escalation_depth = max_escalation_depth
        self.audit_log: dict[str, JSONObject] = {}

    def propose_action(self, proposal: JSONObject, context_snapshot: JSONObject | None = None) -> JSONObject:
        context_snapshot = context_snapshot or {
            "transcript_turns": [1, 2, 3, 4, 5],
            "sop_ids": ["fire-res-v2", "common-v1"],
        }
        trace: list[JSONObject] = []
        payload = copy.deepcopy(proposal.get("proposed_payload", {}))
        escalated_before_checkpoint = False

        outcome = self._base_outcome(proposal)
        action_class_name = proposal.get("action_class")
        policy_class = self.bundle.policy_by_action_class.get(action_class_name)
        registry_class = self.bundle.registry_by_action_class.get(action_class_name)

        # Step 1: Validate
        timer = StepTimer("validate")
        validation_error = self._validate_preconditions(proposal, payload, policy_class, registry_class, context_snapshot)
        if validation_error:
            trace.append(self._trace("validate", "deny", {"reason": validation_error}, timer))
            outcome.update({"decision": "denied", "denial_reason": validation_error})
            return self._finalize(outcome, proposal, trace, payload, None)
        trace.append(self._trace("validate", "pass", None, timer))

        # Step 2: Prohibit
        timer = StepTimer("prohibit")
        prohibit_result = self._evaluate_prohibit(proposal, payload, policy_class, registry_class)
        if prohibit_result:
            trace.append(self._trace("prohibit", "deny", prohibit_result, timer))
            outcome.update(
                {
                    "decision": "denied",
                    "denial_reason": "prohibited",
                    "denial_rule_id": prohibit_result.get("rule_id"),
                }
            )
            return self._finalize(outcome, proposal, trace, payload, None)
        trace.append(self._trace("prohibit", "pass", None, timer))

        # Step 3: Bound
        timer = StepTimer("bound")
        bound_result = self._apply_bounds(proposal, payload, policy_class, registry_class)
        payload = bound_result["payload"]
        trace.append(self._trace("bound", bound_result["result"], bound_result.get("detail"), timer))

        if bound_result["result"] == "deny":
            outcome.update(
                {
                    "decision": "denied",
                    "denial_reason": bound_result["detail"]["reason"],
                    "denial_rule_id": bound_result["detail"].get("rule_id"),
                    "degraded_mode_available": bool(bound_result["detail"].get("degraded_mode_available", False)),
                }
            )
            return self._finalize(outcome, proposal, trace, payload, None)

        if bound_result["result"] == "escalate":
            esc = self._run_escalation(
                proposal,
                payload,
                policy_class,
                registry_class,
                source="bound",
                trigger=bound_result["detail"]["trigger"],
                target_hint=bound_result["detail"].get("target"),
            )
            trace.append(self._trace("escalate", esc["trace_result"], esc.get("detail"), StepTimer("escalate")))
            if esc["status"] == "denied":
                reason = esc.get("reason", "escalation_denied")
                outcome.update(
                    {
                        "decision": "denied",
                        "denial_reason": reason,
                        "degraded_mode_available": reason in {"escalation_timeout", "max_escalation_depth"},
                        "escalation": esc["escalation"],
                    }
                )
                return self._finalize(outcome, proposal, trace, payload, None)
            payload = esc["payload"]
            outcome["escalation"] = esc["escalation"]
            escalated_before_checkpoint = True

        # Step 4: Freshness
        timer = StepTimer("freshness")
        stale = self.plant.check_read_set(proposal.get("read_set", {}))
        if stale is not None:
            trace.append(
                self._trace(
                    "freshness",
                    "conflict",
                    {"stale_fields": stale.stale_fields, "current_versions": stale.current_versions},
                    timer,
                )
            )
            outcome.update(
                {
                    "decision": "needs_retry_conflict",
                    "conflict_detail": {
                        "stale_fields": stale.stale_fields,
                        "current_versions": stale.current_versions,
                    },
                }
            )
            return self._finalize(outcome, proposal, trace, payload, None)
        trace.append(self._trace("freshness", "pass", None, timer))

        # Step 5: Escalate (proactive)
        timer = StepTimer("escalate")
        proactive = self._evaluate_proactive_escalate(proposal, payload, policy_class, registry_class)
        if proactive:
            esc = self._run_escalation(
                proposal,
                payload,
                policy_class,
                registry_class,
                source="explicit_rule",
                trigger=proactive["rule_id"],
                target_hint=proactive.get("target"),
            )
            trace.append(self._trace("escalate", esc["trace_result"], esc.get("detail"), timer))
            if esc["status"] == "denied":
                reason = esc.get("reason", "escalation_denied")
                outcome.update(
                    {
                        "decision": "denied",
                        "denial_reason": reason,
                        "degraded_mode_available": reason in {"escalation_timeout", "max_escalation_depth"},
                        "escalation": esc["escalation"],
                    }
                )
                return self._finalize(outcome, proposal, trace, payload, None)
            payload = esc["payload"]
            outcome["escalation"] = esc["escalation"]
            escalated_before_checkpoint = True
        else:
            trace.append(self._trace("escalate", "pass", None, timer))

        # Step 6: Checkpoint
        timer = StepTimer("checkpoint")
        checkpoint_rule = self._match_checkpoint_rule(proposal, payload, policy_class, registry_class)
        checkpoint_ref: str | None = None
        if escalated_before_checkpoint:
            trace.append(self._trace("checkpoint", "pass", {"skipped_due_to_escalation": True}, timer))
        elif checkpoint_rule:
            checkpoint_ref, response, latency_ms = self.checkpoint.request(
                payload=payload,
                approver_role=checkpoint_rule.get("approver", policy_class["controlling_entity"]["role"]),
                source="checkpoint",
                timeout_ms=checkpoint_rule.get("timeout_ms", 30000),
            )
            checkpoint_block = {
                "request_id": checkpoint_ref,
                "response": response.response,
                "edited_payload": response.edited_payload,
                "deferred_to": response.deferred_to,
                "latency_ms": latency_ms,
            }
            outcome["checkpoint"] = checkpoint_block
            trace.append(self._trace("checkpoint", "pass", checkpoint_block, timer))

            if response.response == "denied":
                outcome.update({"decision": "denied", "denial_reason": response.denial_reason or "checkpoint_denied"})
                return self._finalize(outcome, proposal, trace, payload, checkpoint_ref)
            if response.response == "timeout":
                outcome.update({"decision": "denied", "denial_reason": "checkpoint_timeout"})
                return self._finalize(outcome, proposal, trace, payload, checkpoint_ref)
            if response.response == "edited_approved" and isinstance(response.edited_payload, dict):
                payload = copy.deepcopy(response.edited_payload)
                rebound = self._apply_bounds(proposal, payload, policy_class, registry_class)
                payload = rebound["payload"]
                if rebound["result"] == "deny":
                    outcome.update(
                        {
                            "decision": "denied",
                            "denial_reason": "edited_payload_bound_violation",
                            "denial_rule_id": rebound.get("detail", {}).get("rule_id"),
                        }
                    )
                    return self._finalize(outcome, proposal, trace, payload, checkpoint_ref)
            if response.response == "deferred_escalated":
                esc = self._run_escalation(
                    proposal,
                    payload,
                    policy_class,
                    registry_class,
                    source="checkpoint_deferred",
                    trigger=checkpoint_rule["rule_id"],
                    target_hint=response.deferred_to,
                )
                if esc["status"] == "denied":
                    reason = esc.get("reason", "escalation_denied")
                    outcome.update(
                        {
                            "decision": "denied",
                            "denial_reason": reason,
                            "degraded_mode_available": reason in {"escalation_timeout", "max_escalation_depth"},
                            "escalation": esc["escalation"],
                        }
                    )
                    return self._finalize(outcome, proposal, trace, payload, checkpoint_ref)
                payload = esc["payload"]
                outcome["escalation"] = esc["escalation"]
        else:
            trace.append(self._trace("checkpoint", "pass", None, timer))

        # Step 7: Execute
        timer = StepTimer("execute")
        execution = self.plant.apply_cad_patch(
            payload=payload,
            read_set=proposal.get("read_set", {}),
            policy_id=self.bundle.policy["policy_id"],
            checkpoint_ref=checkpoint_ref,
        )
        if not execution.get("success", False):
            detail = execution.get("conflict", {})
            trace.append(self._trace("execute", "conflict", detail, timer))
            outcome.update(
                {
                    "decision": "needs_retry_conflict",
                    "conflict_detail": {
                        "stale_fields": detail.get("stale_fields", []),
                        "current_versions": detail.get("current_versions", {}),
                    },
                }
            )
            return self._finalize(outcome, proposal, trace, payload, checkpoint_ref)

        trace.append(self._trace("execute", "executed", execution, timer))
        outcome.update(
            {
                "decision": "executed",
                "execution": {
                    "success": True,
                    "new_record_version": execution["new_record_version"],
                    "new_field_versions": execution["new_field_versions"],
                },
            }
        )
        return self._finalize(outcome, proposal, trace, payload, checkpoint_ref)

    def _validate_preconditions(
        self,
        proposal: JSONObject,
        payload: JSONObject,
        policy_class: JSONObject | None,
        registry_class: JSONObject | None,
        context_snapshot: JSONObject,
    ) -> str | None:
        try:
            validate_required(
                proposal,
                ["action_id", "incident_id", "action_class", "proposed_payload", "evidence_refs", "uncertainty", "read_set", "proposer"],
            )
        except SchemaError as exc:
            return str(exc)

        if policy_class is None or registry_class is None:
            return "unknown_action_class"

        proposer = proposal.get("proposer", {})
        if proposer.get("autonomy_level") != policy_class.get("autonomy_level"):
            return "autonomy_level_mismatch"

        allowed_agents = policy_class.get("allowed_agents")
        if isinstance(allowed_agents, list) and proposer.get("agent_id") not in allowed_agents:
            return "agent_not_authorized"

        try:
            validate_payload(payload, registry_class.get("payload_schema", {}))
        except SchemaError as exc:
            return str(exc)

        evidence_errors = self.evidence_validator.validate(proposal, registry_class, context_snapshot)
        if evidence_errors:
            return evidence_errors[0].code
        return None

    def _evaluate_prohibit(self, proposal: JSONObject, payload: JSONObject, policy_class: JSONObject, registry_class: JSONObject) -> JSONObject | None:
        for rule in policy_class.get("operators", {}).get("prohibit", []):
            pred_ctx = PredicateContext(proposal=proposal, payload=payload, action_registry_entry=registry_class)
            if self.predicate_engine.evaluate(rule.get("trigger", ""), pred_ctx):
                return {"rule_id": rule.get("rule_id"), "trigger": rule.get("trigger")}
        return None

    def _apply_bounds(self, proposal: JSONObject, payload: JSONObject, policy_class: JSONObject, registry_class: JSONObject) -> JSONObject:
        transformed = False
        for rule in policy_class.get("operators", {}).get("bound", []):
            if "transform" in rule:
                payload = self.predicate_engine.apply_transform(rule["transform"], payload)
                transformed = True
                continue

            field_name = rule.get("field")
            if not field_name:
                continue

            current_value = proposal.get("uncertainty", {}).get("p_correct") if field_name == "confidence" else payload.get(field_name)
            violation = False

            if "allowed_values" in rule and current_value not in rule["allowed_values"]:
                violation = True
            if "min" in rule and isinstance(current_value, (int, float)) and current_value < rule["min"]:
                violation = True
            if "max" in rule and isinstance(current_value, (int, float)) and current_value > rule["max"]:
                violation = True

            if not violation:
                continue

            on_violation = rule.get("on_violation", "escalate")
            if on_violation == "clamp":
                if isinstance(current_value, (int, float)):
                    low = rule.get("min", current_value)
                    high = rule.get("max", current_value)
                    clamped = min(max(current_value, low), high)
                    if field_name == "confidence":
                        proposal["uncertainty"]["p_correct"] = clamped
                    else:
                        payload[field_name] = clamped
                    transformed = True
                continue

            if on_violation == "deny_and_revert":
                return {
                    "result": "deny",
                    "payload": payload,
                    "detail": {
                        "rule_id": rule.get("rule_id"),
                        "reason": "bound_violation_deny_and_revert",
                        "degraded_mode_available": True,
                    },
                }

            target = policy_class.get("controlling_entity", {}).get("escalation_target")
            for esc_rule in policy_class.get("operators", {}).get("escalate", []):
                if esc_rule.get("condition") == "bound_violation":
                    target = esc_rule.get("target", target)
                    break
            return {
                "result": "escalate",
                "payload": payload,
                "detail": {
                    "rule_id": rule.get("rule_id"),
                    "trigger": rule.get("rule_id", "bound_violation"),
                    "target": target,
                },
            }

        if transformed:
            return {"result": "transform", "payload": payload, "detail": {"transformed": True}}
        return {"result": "pass", "payload": payload}

    def _evaluate_proactive_escalate(self, proposal: JSONObject, payload: JSONObject, policy_class: JSONObject, registry_class: JSONObject) -> JSONObject | None:
        for rule in policy_class.get("operators", {}).get("escalate", []):
            condition = rule.get("condition", "")
            if condition == "bound_violation":
                continue
            pred_ctx = PredicateContext(proposal=proposal, payload=payload, action_registry_entry=registry_class, bound_violation=False)
            if self.predicate_engine.evaluate(condition, pred_ctx):
                return rule
        return None

    def _match_checkpoint_rule(self, proposal: JSONObject, payload: JSONObject, policy_class: JSONObject, registry_class: JSONObject) -> JSONObject | None:
        for rule in policy_class.get("operators", {}).get("checkpoint", []):
            pred_ctx = PredicateContext(proposal=proposal, payload=payload, action_registry_entry=registry_class)
            if self.predicate_engine.evaluate(rule.get("trigger", ""), pred_ctx):
                return rule
        return None

    def _run_escalation(
        self,
        proposal: JSONObject,
        payload: JSONObject,
        policy_class: JSONObject,
        registry_class: JSONObject,
        source: str,
        trigger: str,
        target_hint: str | None = None,
    ) -> JSONObject:
        target = target_hint or policy_class.get("controlling_entity", {}).get("escalation_target")
        depth = 0
        escalation_block: JSONObject = {
            "trigger": trigger,
            "source": source,
            "target": target,
            "resolution": "timeout",
            "edited_payload": None,
            "re_escalated_to": None,
            "latency_ms": 0,
        }

        while depth <= self.max_escalation_depth:
            request_id, response, latency_ms = self.checkpoint.request(
                payload=payload,
                approver_role=target,
                source="escalation_proactive" if source == "explicit_rule" else "escalation_reactive",
            )
            escalation_block["latency_ms"] += latency_ms

            if response.response == "approved":
                escalation_block["resolution"] = "approved"
                return {
                    "status": "approved",
                    "payload": payload,
                    "escalation": escalation_block,
                    "trace_result": "escalate",
                    "detail": {"request_id": request_id, "resolution": "approved"},
                }

            if response.response == "edited_approved" and isinstance(response.edited_payload, dict):
                payload = copy.deepcopy(response.edited_payload)
                rebound = self._apply_bounds(proposal, payload, policy_class, registry_class)
                payload = rebound["payload"]
                escalation_block["resolution"] = "edited_approved"
                escalation_block["edited_payload"] = payload
                if rebound["result"] == "deny":
                    return {
                        "status": "denied",
                        "reason": "edited_payload_bound_violation",
                        "payload": payload,
                        "escalation": escalation_block,
                        "trace_result": "deny",
                        "detail": {"reason": "edited_payload_bound_violation"},
                    }
                return {
                    "status": "approved",
                    "payload": payload,
                    "escalation": escalation_block,
                    "trace_result": "escalate",
                    "detail": {"request_id": request_id, "resolution": "edited_approved"},
                }

            if response.response == "denied":
                escalation_block["resolution"] = "denied"
                return {
                    "status": "denied",
                    "reason": response.denial_reason or "escalation_denied",
                    "payload": payload,
                    "escalation": escalation_block,
                    "trace_result": "deny",
                    "detail": {"reason": response.denial_reason or "escalation_denied"},
                }

            if response.response == "timeout":
                escalation_block["resolution"] = "timeout"
                return {
                    "status": "denied",
                    "reason": "escalation_timeout",
                    "payload": payload,
                    "escalation": escalation_block,
                    "trace_result": "deny",
                    "detail": {"reason": "escalation_timeout"},
                }

            if response.response == "re_escalated":
                depth += 1
                escalation_block["resolution"] = "re_escalated"
                escalation_block["re_escalated_to"] = response.re_escalate_to
                target = response.re_escalate_to or target
                escalation_block["target"] = target
                if depth > self.max_escalation_depth:
                    return {
                        "status": "denied",
                        "reason": "max_escalation_depth",
                        "payload": payload,
                        "escalation": escalation_block,
                        "trace_result": "deny",
                        "detail": {"reason": "max_escalation_depth"},
                    }
                continue

            if response.response == "deferred_escalated":
                depth += 1
                target = response.deferred_to or target
                escalation_block["target"] = target
                if depth > self.max_escalation_depth:
                    return {
                        "status": "denied",
                        "reason": "max_escalation_depth",
                        "payload": payload,
                        "escalation": escalation_block,
                        "trace_result": "deny",
                        "detail": {"reason": "max_escalation_depth"},
                    }
                continue

            break

        return {
            "status": "denied",
            "reason": "escalation_unresolved",
            "payload": payload,
            "escalation": escalation_block,
            "trace_result": "deny",
            "detail": {"reason": "escalation_unresolved"},
        }

    def _trace(self, step: str, result: str, detail: JSONObject | None, timer: StepTimer) -> JSONObject:
        return {
            "step": step,
            "result": result,
            "detail": detail,
            "duration_ms": timer.elapsed_ms(),
        }

    def _base_outcome(self, proposal: JSONObject) -> JSONObject:
        return {
            "action_id": proposal.get("action_id", ""),
            "decision": "denied",
            "denial_reason": None,
            "denial_rule_id": None,
            "degraded_mode_available": False,
            "policy_id": self.bundle.policy["policy_id"],
            "policy_hash": self.bundle.policy_hash,
            "checkpoint": None,
            "escalation": None,
            "execution": None,
            "conflict_detail": None,
            "audit_ref": "",
            "sim_event_ref": None,
            "enforcement_trace": [],
        }

    def _finalize(
        self,
        outcome: JSONObject,
        proposal: JSONObject,
        trace: list[JSONObject],
        final_payload: JSONObject,
        checkpoint_ref: str | None,
    ) -> JSONObject:
        policy_class = self.bundle.policy_by_action_class.get(proposal.get("action_class"), {})
        registry_class = self.bundle.registry_by_action_class.get(proposal.get("action_class"), {})
        audit_cfg = policy_class.get("operators", {}).get("audit", {}) if isinstance(policy_class, dict) else {}
        selector = audit_cfg.get("level_selector") if isinstance(audit_cfg, dict) else None
        audit_level = "standard"
        if selector:
            pred_ctx = PredicateContext(
                proposal=proposal,
                payload=final_payload,
                action_registry_entry=registry_class if isinstance(registry_class, dict) else {},
            )
            if self.predicate_engine.evaluate(str(selector), pred_ctx):
                audit_level = "extended"

        audit_timer = StepTimer("audit")
        trace.append(self._trace("audit", "pass", {"captured": True, "level": audit_level}, audit_timer))
        outcome["enforcement_trace"] = trace

        audit_ref = f"audit-{uuid.uuid4().hex[:12]}"
        outcome["audit_ref"] = audit_ref
        self.audit_log[audit_ref] = {
            "proposal": copy.deepcopy(proposal),
            "decision": outcome["decision"],
            "denial_reason": outcome.get("denial_reason"),
            "trace": copy.deepcopy(trace),
            "final_payload": copy.deepcopy(final_payload),
            "checkpoint_ref": checkpoint_ref,
            "audit_level": audit_level,
        }
        return outcome
