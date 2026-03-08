"""Standalone shims used by the SIM harness test plan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sim_server.engine import SimulationEngine


class GovernanceShim:
    """Calls southbound plant tools directly to mimic governance behavior."""

    def __init__(self, engine: SimulationEngine) -> None:
        self.engine = engine

    def get_state_snapshot(self, incident_id: str) -> dict[str, Any]:
        return self.engine.plant_get_state_snapshot(incident_id)

    def get_transcript_since(self, incident_id: str, cursor: int) -> dict[str, Any]:
        return self.engine.plant_get_transcript_since(incident_id, cursor)

    def request_checkpoint(self, incident_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return self.engine.checkpoint_request(incident_id, request)

    def poll_checkpoint(self, request_id: str) -> dict[str, Any]:
        return self.engine.checkpoint_poll(request_id)

    def apply_cad_patch(
        self,
        incident_id: str,
        action_id: str,
        action_class: str,
        payload: dict[str, Any],
        read_set: dict[str, Any],
        policy_id: str = "test-policy",
        policy_hash: str = "test-policy-hash",
        proposer_agent_id: str = "gov-shim",
        checkpoint_ref: str | None = None,
        checkpoint_decision: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.plant_apply_cad_patch(
            incident_id=incident_id,
            action_id=action_id,
            action_class=action_class,
            payload=payload,
            read_set=read_set,
            policy_id=policy_id,
            policy_hash=policy_hash,
            proposer_agent_id=proposer_agent_id,
            checkpoint_ref=checkpoint_ref,
            checkpoint_decision=checkpoint_decision,
            original_payload=payload,
            governance_operators_applied=["checkpoint", "execute", "audit"],
        )

    def emit_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return self.engine.plant_emit_event(event)


class RoleClientShim:
    """Posts scripted caller/calltaker turns."""

    def __init__(self, engine: SimulationEngine) -> None:
        self.engine = engine

    def caller_turn(self, incident_id: str, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.engine.caller_post_turn(incident_id=incident_id, text=text, metadata=metadata)

    def calltaker_turn(self, incident_id: str, text: str, cad_updates: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.engine.calltaker_post_turn(incident_id=incident_id, text=text, cad_updates=cad_updates)

    def end_call(self, incident_id: str, reason: str, reason_detail: str | None = None) -> dict[str, Any]:
        return self.engine.calltaker_end_call(incident_id=incident_id, reason=reason, reason_detail=reason_detail)


@dataclass
class CheckpointResponderShim:
    mode: str = "auto_approve"

    def respond(self, engine: SimulationEngine, incident_id: str, role_filter: str = "call_taker") -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        reqs = engine.checkpoint_list(incident_id=incident_id, status_filter="pending", role_filter=role_filter)["requests"]
        for req in reqs:
            request_id = req["request_id"]
            if self.mode == "auto_approve":
                responses.append(engine.checkpoint_submit(request_id=request_id, decision="approved"))
            elif self.mode == "auto_deny":
                responses.append(engine.checkpoint_submit(request_id=request_id, decision="denied"))
            elif self.mode == "auto_edit":
                edit = dict(req.get("proposed_payload", {}))
                edit["edited"] = True
                responses.append(engine.checkpoint_submit(request_id=request_id, decision="edited_approved", edited_payload=edit))
            elif self.mode == "auto_defer":
                responses.append(engine.checkpoint_submit(request_id=request_id, decision="deferred_escalated"))
            elif self.mode == "auto_re_escalate":
                responses.append(
                    engine.checkpoint_submit(request_id=request_id, decision="re_escalated", re_escalate_to="commander")
                )
        return responses
