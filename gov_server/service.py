"""Governance service layer for MCP northbound tool handling."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth import (
    AgentAuthManager,
    AuthError,
    ConflictError,
    ForbiddenError,
    ProposalRateLimiter,
    QueueCaps,
    RateLimitedError,
)
from .context_cache import ContextCache
from .dsa import DSARegistry, load_dsa_registry
from .dsa_runtime import DSAAdvice, DSAExecutionError, DSAOrchestrator
from .enforcement import Engine
from .observability import Observability
from .policy_loader import PolicyBundle, PolicyLoader
from .predicates import PredicateEngine
from .shims import CheckpointShim, PlantStateShim
from .southbound import (
    SimulationSouthboundClient,
    SouthboundCheckpointAdapter,
    SouthboundPlantAdapter,
)
from .state_store import StateStore
from .versioning import CompatibilityManager

JSONObject = dict[str, Any]


@dataclass
class GovernanceConfig:
    policy_file: str
    registry_file: str
    evidence_config_file: str
    auth_config_file: str | None = None
    dsa_config_file: str | None = "policies/dsa_profiles.yaml"
    proposals_per_sec: int = 10
    checkpoint_queue_cap: int = 20
    escalation_queue_cap: int = 5
    sim_base_url: str | None = None
    southbound_timeout_sec: float = 10.0
    checkpoint_poll_interval_sec: float = 0.25
    southbound_require_mtls: bool = False
    southbound_ca_file: str | None = None
    southbound_client_cert_file: str | None = None
    southbound_client_key_file: str | None = None
    southbound_retry_attempts: int = 2
    southbound_retry_backoff_sec: float = 0.1
    southbound_circuit_fail_threshold: int = 3
    southbound_circuit_open_sec: float = 5.0
    state_db_file: str | None = None
    observability_enabled: bool = True


class GovernanceService:
    """Wraps governance engine with northbound MCP tool semantics."""

    def __init__(self, root_dir: str | Path, config: GovernanceConfig) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.config = config
        self.context_cache = ContextCache()
        self.observability = Observability(component="governance_service")
        self.checkpoint = CheckpointShim(mode="auto_approve")
        self.plant_by_incident: dict[str, Any] = {}
        self.action_to_audit_ref: dict[str, str] = {}
        self.idempotency_cache: dict[str, JSONObject] = {}
        self.state_store: StateStore | None = None
        self.southbound_client: SimulationSouthboundClient | None = None
        self._southbound_cursor_by_incident: dict[str, int] = {}
        self._southbound_bootstrapped: set[str] = set()
        self.auth = AgentAuthManager(
            self.root_dir / config.auth_config_file if config.auth_config_file else None
        )
        dsa_path = self.root_dir / config.dsa_config_file if config.dsa_config_file else None
        self.dsa_registry: DSARegistry = load_dsa_registry(dsa_path)
        self._dsa_runtime = DSAOrchestrator()
        self.rate_limiter = ProposalRateLimiter(proposals_per_sec=config.proposals_per_sec)
        self.queue_caps = QueueCaps(
            checkpoint_cap=config.checkpoint_queue_cap,
            escalation_cap=config.escalation_queue_cap,
        )

        self.predicate_engine = PredicateEngine(
            custom_predicates={
                "contains_keyword_urgent": lambda proposal: "urgent"
                in str(proposal.get("proposed_payload", {})).lower()
            }
        )
        self.loader = PolicyLoader(self.predicate_engine)
        self.compat = CompatibilityManager()
        self.bundle = self._load_bundle(
            policy_file=config.policy_file,
            registry_file=config.registry_file,
            evidence_config_file=config.evidence_config_file,
        )
        self.compat.validate_policy(self.bundle.policy)
        if config.state_db_file:
            self.state_store = StateStore(self._resolve_optional_path(config.state_db_file) or config.state_db_file)
        if config.sim_base_url:
            self.southbound_client = SimulationSouthboundClient(
                base_url=config.sim_base_url,
                timeout_sec=config.southbound_timeout_sec,
                require_mtls=config.southbound_require_mtls,
                ca_file=self._resolve_optional_path(config.southbound_ca_file),
                client_cert_file=self._resolve_optional_path(config.southbound_client_cert_file),
                client_key_file=self._resolve_optional_path(config.southbound_client_key_file),
                retry_attempts=config.southbound_retry_attempts,
                retry_backoff_sec=config.southbound_retry_backoff_sec,
                circuit_fail_threshold=config.southbound_circuit_fail_threshold,
                circuit_open_sec=config.southbound_circuit_open_sec,
            )
            self.checkpoint = SouthboundCheckpointAdapter(
                client=self.southbound_client,
                poll_interval_sec=config.checkpoint_poll_interval_sec,
            )
        self.engines: dict[str, Engine] = {}
        self.observability.event(
            "server_setup",
            policy_id=self.bundle.policy.get("policy_id"),
            policy_version=self.bundle.policy.get("policy_version"),
            auth_mode=getattr(self.auth, "mode", "dev_secret"),
            dsa_default_profile=self.dsa_registry.default_profile_id,
            southbound_enabled=bool(config.sim_base_url),
            persistence_enabled=bool(config.state_db_file),
        )

    def _resolve_optional_path(self, value: str | None) -> str | None:
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = self.root_dir / path
        return str(path)

    def _load_bundle(self, policy_file: str, registry_file: str, evidence_config_file: str) -> PolicyBundle:
        return self.loader.load_bundle(
            self.root_dir / policy_file,
            self.root_dir / registry_file,
            self.root_dir / evidence_config_file,
        )

    def _engine_for_incident(self, incident_id: str) -> Engine:
        if incident_id not in self.plant_by_incident:
            if self.southbound_client:
                self.plant_by_incident[incident_id] = SouthboundPlantAdapter(client=self.southbound_client)
            else:
                self.plant_by_incident[incident_id] = PlantStateShim(
                    cad_state={},
                    record_version=0,
                    field_versions={},
                )
        if incident_id not in self.engines:
            self.engines[incident_id] = Engine(
                policy_bundle=self.bundle,
                plant=self.plant_by_incident[incident_id],
                checkpoint=self.checkpoint,
                predicate_engine=self.predicate_engine,
            )
        return self.engines[incident_id]

    # MCP northbound tool handlers
    def propose_action(
        self,
        proposal: JSONObject,
        agent_token: str | None = None,
        correlation_id: str | None = None,
    ) -> JSONObject:
        started = time.perf_counter()
        incident_id = str(proposal.get("incident_id", "unknown"))
        action_id = str(proposal.get("action_id", ""))
        proposer = proposal.get("proposer", {})
        agent_id = str(proposer.get("agent_id", ""))
        agent_secret = proposer.get("agent_secret")
        action_class = str(proposal.get("action_class", ""))
        idempotency_key = str(proposer.get("idempotency_key", action_id or "")).strip()

        profile = self._authenticate_and_authorize(
            agent_id=agent_id,
            agent_secret=agent_secret,
            action_class=action_class,
            agent_token=agent_token,
        )
        self.rate_limiter.check_and_record(profile.agent_id)
        self.observability.incr("propose_action.total")
        if idempotency_key:
            cached = self.idempotency_cache.get(idempotency_key)
            if cached is None and self.state_store is not None:
                cached = self.state_store.get_idempotency(idempotency_key)
                if cached is not None:
                    self.idempotency_cache[idempotency_key] = cached
            fingerprint = self._proposal_fingerprint(proposal)
            if cached:
                if cached.get("fingerprint") != fingerprint:
                    self.observability.incr("propose_action.idempotency_conflict")
                    raise ConflictError("idempotency_key_payload_mismatch")
                self.observability.incr("propose_action.idempotency_replay")
                return copy.deepcopy(cached["outcome"])
        self._refresh_context_from_southbound(incident_id)

        self.queue_caps.reserve_checkpoint(incident_id)
        self.queue_caps.reserve_escalation(incident_id)
        engine = self._engine_for_incident(incident_id)
        try:
            self._set_engine_request_context(
                engine=engine,
                incident_id=incident_id,
                action_class=action_class,
                action_id=action_id,
            )
            snapshot = self.context_cache.get_context_snapshot(
                incident_id=incident_id,
                agent_id=proposer.get("agent_id", "unknown"),
            )
            proposal_for_engine = copy.deepcopy(proposal)
            dsa_advice = self._run_dsa_advice(proposal_for_engine, snapshot)
            transcript_turns = [turn.get("turn", 0) for turn in snapshot.get("transcript", [])]
            context_for_engine = {
                "transcript_turns": transcript_turns or [1, 2, 3, 4, 5],
                "sop_ids": snapshot.get("sop_refs", []) or ["fire-res-v2"],
            }

            outcome = engine.propose_action(proposal_for_engine, context_snapshot=context_for_engine)
            if dsa_advice is not None:
                envelope = dsa_advice.to_json()
                dsa_meta = proposal_for_engine.get("dsa", {})
                if isinstance(dsa_meta, dict):
                    envelope["strategy"] = dsa_meta.get("strategy")
                    envelope["attempts"] = dsa_meta.get("attempts", [])
                    envelope["requested_profile_id"] = dsa_meta.get("requested_profile_id")
                    envelope["selected_profile_id"] = dsa_meta.get("selected_profile_id")
                outcome["dsa"] = envelope
            action_id = str(outcome.get("action_id", ""))
            audit_ref = str(outcome.get("audit_ref", ""))
            chain_ref: JSONObject | None = None
            if action_id and audit_ref:
                self.action_to_audit_ref[action_id] = audit_ref
                audit_entry = engine.audit_log.get(audit_ref)
                if self.state_store is not None:
                    self.state_store.put_action_audit(action_id, audit_ref, audit_entry)
                    chain_ref = self.state_store.append_audit_chain_event(
                        {
                            "action_id": action_id,
                            "audit_ref": audit_ref,
                            "incident_id": incident_id,
                            "action_class": action_class,
                            "decision": outcome.get("decision"),
                            "denial_reason": outcome.get("denial_reason"),
                            "policy_id": self.bundle.policy.get("policy_id"),
                        }
                    )
                    outcome["audit_chain_ref"] = chain_ref
            self._emit_southbound_event(
                {
                    "type": "governance_decision",
                    "incident_id": incident_id,
                    "action_id": action_id,
                    "action_class": action_class,
                    "decision": outcome.get("decision"),
                    "denial_reason": outcome.get("denial_reason"),
                    "audit_ref": audit_ref,
                    "policy_id": self.bundle.policy.get("policy_id"),
                    "dsa_profile_id": (dsa_advice.profile_id if dsa_advice else None),
                }
            )

            execution = outcome.get("execution")
            if isinstance(execution, dict) and execution.get("success"):
                patch = proposal.get("proposed_payload", {})
                if isinstance(patch, dict):
                    self.context_cache.update_cad_view(incident_id, patch)
            if idempotency_key:
                self.idempotency_cache[idempotency_key] = {
                    "fingerprint": fingerprint,
                    "outcome": copy.deepcopy(outcome),
                }
                if self.state_store is not None:
                    self.state_store.put_idempotency(idempotency_key, fingerprint, outcome)
            self.observability.incr(f"propose_action.decision.{outcome.get('decision', 'unknown')}")
            self.observability.observe_latency_ms("propose_action", (time.perf_counter() - started) * 1000.0)
            self.observability.event(
                "propose_action",
                correlation_id=correlation_id or "",
                incident_id=incident_id,
                action_id=action_id,
                decision=outcome.get("decision"),
                denial_reason=outcome.get("denial_reason"),
                dsa_profile_id=(dsa_advice.profile_id if dsa_advice else None),
            )
            return outcome
        finally:
            self.queue_caps.release_checkpoint(incident_id)
            self.queue_caps.release_escalation(incident_id)

    def get_context_snapshot(
        self,
        incident_id: str,
        agent_id: str,
        agent_secret: str | None = None,
        agent_token: str | None = None,
        correlation_id: str | None = None,
    ) -> JSONObject:
        self.observability.incr("context_snapshot.total")
        profile = self._authenticate_read(agent_id=agent_id, agent_secret=agent_secret, agent_token=agent_token)
        self._refresh_context_from_southbound(incident_id)
        snapshot = self.context_cache.get_context_snapshot(incident_id=incident_id, agent_id=agent_id)
        self.observability.event(
            "context_snapshot",
            correlation_id=correlation_id or "",
            incident_id=incident_id,
            agent_id=agent_id,
        )
        return self._filter_snapshot_for_role(snapshot, profile.role)

    def get_context_since(
        self,
        incident_id: str,
        agent_id: str,
        cursor: int,
        agent_secret: str | None = None,
        agent_token: str | None = None,
        correlation_id: str | None = None,
    ) -> JSONObject:
        self.observability.incr("context_since.total")
        profile = self._authenticate_read(agent_id=agent_id, agent_secret=agent_secret, agent_token=agent_token)
        self._refresh_context_from_southbound(incident_id)
        payload = self.context_cache.get_context_since(incident_id=incident_id, agent_id=agent_id, cursor=int(cursor))
        # Apply same role filter semantics to embedded cad/location fields in deltas.
        redacted = copy.deepcopy(payload)
        for item in redacted.get("deltas", []):
            if item.get("type") == "cad_patch":
                patch = item.get("patch", {})
                item["patch"] = self._redact_cad_view(patch, profile.role)
        self.observability.event(
            "context_since",
            correlation_id=correlation_id or "",
            incident_id=incident_id,
            agent_id=agent_id,
            cursor=cursor,
            delta_count=len(redacted.get("deltas", [])),
        )
        return redacted

    def list_action_classes(
        self,
        agent_id: str | None = None,
        agent_secret: str | None = None,
        agent_token: str | None = None,
    ) -> JSONObject:
        allowed: set[str] | None = None
        if self.auth.has_profiles() and (agent_id or agent_token):
            profile = self._authenticate_read(agent_id=agent_id, agent_secret=agent_secret, agent_token=agent_token)
            allowed = set(profile.allowed_action_classes)

        classes: list[JSONObject] = []
        for action_policy in self.bundle.policy.get("action_classes", []):
            name = action_policy.get("name")
            if allowed is not None and allowed and name not in allowed:
                continue
            registry = self.bundle.registry_by_action_class.get(name, {})
            classes.append(
                {
                    "name": name,
                    "payload_schema": registry.get("payload_schema", {}),
                    "required_evidence": registry.get("required_evidence", []),
                    "risk_level": registry.get("risk_level"),
                    "autonomy_level": action_policy.get("autonomy_level"),
                }
            )
        return {"classes": classes}

    def list_dsa_profiles(
        self,
        action_class: str | None = None,
        requested_profile_id: str | None = None,
        include_disabled: bool = False,
    ) -> JSONObject:
        selected: str | None = None
        allowed_ids: list[str] = []
        strategy = "fallback_chain"
        if action_class:
            route = self.dsa_registry.route_for_action_class(action_class)
            strategy = str(route.get("strategy", "fallback_chain"))
            allowed_ids = self.dsa_registry.allowed_profile_ids_for_action_class(action_class)
            chosen = self.dsa_registry.select_profile(action_class=action_class, requested_profile_id=requested_profile_id)
            selected = chosen.id if chosen is not None else None
        return {
            "default_profile_id": self.dsa_registry.default_profile_id,
            "profiles": self.dsa_registry.list_profiles(include_disabled=include_disabled),
            "action_class": action_class,
            "strategy": strategy,
            "allowed_profile_ids": allowed_ids,
            "requested_profile_id": requested_profile_id,
            "selected_profile_id": selected,
        }

    def _run_dsa_advice(self, proposal: JSONObject, context_snapshot: JSONObject) -> DSAAdvice | None:
        action_class = str(proposal.get("action_class", ""))
        if not action_class:
            return None
        dsa_request = proposal.get("dsa", {})
        requested_profile_id: str | None = None
        apply_suggestion = False
        if isinstance(dsa_request, dict):
            requested_profile_id = str(dsa_request.get("profile_id", "")).strip() or None
            apply_suggestion = bool(dsa_request.get("apply_suggested_payload", False))
        route = self.dsa_registry.route_for_action_class(action_class)
        strategy = str(route.get("strategy", "fallback_chain") or "fallback_chain")
        candidate_ids = self.dsa_registry.allowed_profile_ids_for_action_class(action_class)
        if not candidate_ids:
            return None
        if requested_profile_id and requested_profile_id in candidate_ids:
            candidate_ids = [requested_profile_id] + [pid for pid in candidate_ids if pid != requested_profile_id]

        attempts: list[JSONObject] = []
        successful: list[DSAAdvice] = []
        for profile_id in candidate_ids:
            profile = self.dsa_registry.profile_by_id(profile_id)
            if profile is None:
                attempts.append({"profile_id": profile_id, "status": "error", "error": "unknown_profile"})
                continue
            if not profile.enabled:
                attempts.append({"profile_id": profile_id, "status": "skipped", "error": "profile_disabled"})
                continue
            try:
                advice = self._dsa_runtime.advise(
                    profile=profile,
                    proposal=proposal,
                    context_snapshot=context_snapshot,
                    apply_suggestion=apply_suggestion,
                )
            except DSAExecutionError as exc:
                attempts.append({"profile_id": profile_id, "status": "error", "error": str(exc)})
                if strategy == "fallback_chain":
                    continue
            else:
                attempts.append({"profile_id": profile_id, "status": "ok"})
                successful.append(advice)
                if strategy == "fallback_chain":
                    break
        if not successful:
            proposal["dsa"] = {
                "requested_profile_id": requested_profile_id,
                "selected_profile_id": None,
                "apply_suggested_payload": apply_suggestion,
                "strategy": strategy,
                "attempts": attempts,
                "chosen_payload_source": "client_proposal",
            }
            return None

        advice = max(successful, key=lambda row: row.selection_score) if strategy == "parallel_best" else successful[0]
        proposal.setdefault("proposer", {})
        if isinstance(proposal["proposer"], dict):
            proposal["proposer"]["dsa_profile_id"] = advice.profile_id
        proposal["dsa"] = {
            "requested_profile_id": requested_profile_id,
            "selected_profile_id": advice.profile_id,
            "apply_suggested_payload": apply_suggestion,
            "strategy": strategy,
            "attempts": attempts,
            "context_hash": advice.context_hash,
            "proposal_hash": advice.proposal_hash,
            "chosen_payload_source": advice.chosen_payload_source,
        }
        proposal["proposed_payload"] = copy.deepcopy(advice.chosen_payload)
        return advice

    def get_action_schema(self, action_class: str) -> JSONObject:
        registry = self.bundle.registry_by_action_class.get(action_class)
        if registry is None:
            return {"error": f"unknown_action_class:{action_class}"}
        return {"action_class": action_class, "payload_schema": registry.get("payload_schema", {})}

    def get_audit_ref(self, action_id: str) -> JSONObject:
        audit_ref = self.action_to_audit_ref.get(action_id)
        if not audit_ref:
            if self.state_store is not None:
                stored = self.state_store.get_action_audit(action_id)
                if stored is not None:
                    return {
                        "action_id": action_id,
                        "audit_ref": stored["audit_ref"],
                        "audit_entry": stored.get("audit_entry"),
                    }
            return {"error": f"audit_not_found_for_action_id:{action_id}"}
        for engine in self.engines.values():
            if audit_ref in engine.audit_log:
                return {"action_id": action_id, "audit_ref": audit_ref, "audit_entry": engine.audit_log[audit_ref]}
        return {"error": f"audit_ref_not_loaded:{audit_ref}"}

    def swap_policy(self, policy_file: str) -> JSONObject:
        old_policy_id = self.bundle.policy.get("policy_id")
        old_policy_hash = self.bundle.policy_hash
        self.bundle = self._load_bundle(
            policy_file=policy_file,
            registry_file=self.config.registry_file,
            evidence_config_file=self.config.evidence_config_file,
        )
        self.compat.validate_policy(self.bundle.policy)
        # Existing engines should point to new policy on next action.
        for incident_id in list(self.engines.keys()):
            self.engines[incident_id] = Engine(
                policy_bundle=self.bundle,
                plant=self.plant_by_incident[incident_id],
                checkpoint=self.checkpoint,
                predicate_engine=self.predicate_engine,
            )
        outcome = {
            "old_policy_id": old_policy_id,
            "old_policy_hash": old_policy_hash,
            "new_policy_id": self.bundle.policy.get("policy_id"),
            "new_policy_hash": self.bundle.policy_hash,
        }
        self._emit_southbound_event({"type": "policy_swapped", **outcome})
        return outcome

    # Helpers for seeding context from harness/admin during WP1
    def seed_incident_context(
        self,
        incident_id: str,
        transcript: list[JSONObject] | None = None,
        cad_view: JSONObject | None = None,
        location: JSONObject | None = None,
        sop_refs: list[str] | None = None,
    ) -> JSONObject:
        self.context_cache.set_snapshot(
            incident_id=incident_id,
            transcript=transcript or [],
            cad_view=cad_view or {},
            location=location or {},
            sop_refs=sop_refs or ["fire-res-v2"],
        )
        self._southbound_bootstrapped.add(incident_id)
        return self.context_cache.get_context_snapshot(incident_id=incident_id, agent_id="seed")

    def _set_engine_request_context(self, engine: Engine, incident_id: str, action_class: str, action_id: str) -> None:
        set_plant_context = getattr(engine.plant, "set_request_context", None)
        if callable(set_plant_context):
            set_plant_context(incident_id=incident_id, action_class=action_class, action_id=action_id)
        set_checkpoint_context = getattr(engine.checkpoint, "set_request_context", None)
        if callable(set_checkpoint_context):
            set_checkpoint_context(
                incident_id=incident_id,
                action_class=action_class,
                escalation_depth=0,
                action_id=action_id,
            )

    def _refresh_context_from_southbound(self, incident_id: str) -> None:
        if self.southbound_client is None:
            return
        current = self.context_cache.get_context_snapshot(incident_id=incident_id, agent_id="sync")

        state = self.southbound_client.get_state_snapshot(incident_id)
        state_cad = state.get("cad_state") or state.get("cad_view") or {}
        state_location = state.get("location") or current.get("location", {})
        state_sops = state.get("sop_refs") or current.get("sop_refs", []) or ["fire-res-v2"]
        state_transcript = state.get("transcript")

        if incident_id not in self._southbound_bootstrapped:
            transcript_seed = state_transcript if isinstance(state_transcript, list) else current.get("transcript", [])
            self.context_cache.set_snapshot(
                incident_id=incident_id,
                transcript=transcript_seed,
                cad_view=state_cad if isinstance(state_cad, dict) else current.get("cad_view", {}),
                location=state_location if isinstance(state_location, dict) else {},
                sop_refs=state_sops if isinstance(state_sops, list) else ["fire-res-v2"],
            )
            self._southbound_bootstrapped.add(incident_id)
            if isinstance(state.get("transcript_cursor"), int):
                self._southbound_cursor_by_incident[incident_id] = int(state["transcript_cursor"])
                if self.state_store is not None:
                    self.state_store.put_southbound_cursor(incident_id, int(state["transcript_cursor"]))

        if isinstance(state_cad, dict):
            cad_patch = {k: v for k, v in state_cad.items() if current.get("cad_view", {}).get(k) != v}
            if cad_patch:
                self.context_cache.update_cad_view(incident_id, cad_patch)

        cursor = int(self._southbound_cursor_by_incident.get(incident_id, 0))
        if cursor == 0 and self.state_store is not None:
            persisted = self.state_store.get_southbound_cursor(incident_id)
            if persisted is not None:
                cursor = int(persisted)
                self._southbound_cursor_by_incident[incident_id] = cursor
        transcript_delta = self.southbound_client.get_transcript_since(incident_id=incident_id, cursor=cursor)
        for entry in transcript_delta.get("turns", transcript_delta.get("transcript", [])) or []:
            if not isinstance(entry, dict):
                continue
            turn = int(entry.get("turn", 0))
            text = str(entry.get("text", entry.get("content", "")))
            if turn > 0 and text:
                self.context_cache.append_transcript_turn(incident_id=incident_id, turn=turn, text=text)
        next_cursor = transcript_delta.get("new_cursor", transcript_delta.get("cursor", cursor))
        if isinstance(next_cursor, int):
            self._southbound_cursor_by_incident[incident_id] = int(next_cursor)
            if self.state_store is not None:
                self.state_store.put_southbound_cursor(incident_id, int(next_cursor))

    def _emit_southbound_event(self, event: JSONObject) -> None:
        if self.southbound_client is None:
            return
        try:
            self.southbound_client.emit_event(event)
        except Exception:
            # Event forwarding is best-effort and should not block enforcement.
            return

    @staticmethod
    def _proposal_fingerprint(proposal: JSONObject) -> str:
        canonical = json.dumps(proposal, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def close(self) -> None:
        if self.state_store is not None:
            self.state_store.close()

    def get_metrics_snapshot(self) -> JSONObject:
        return self.observability.snapshot()

    def get_events_since(self, cursor: int = 0, verbosity: str = "normal") -> JSONObject:
        return self.observability.events_since(cursor=cursor, verbosity=verbosity)

    def verify_audit_chain(self) -> JSONObject:
        if self.state_store is None:
            return {"ok": False, "error": "state_store_not_configured"}
        return self.state_store.verify_audit_chain()

    def get_version_matrix(self) -> JSONObject:
        return self.compat.version_matrix(active_policy=self.bundle.policy)

    def _authenticate_read(self, agent_id: str, agent_secret: str | None, agent_token: str | None = None) -> Any:
        if not self.auth.has_profiles():
            return type("AnonProfile", (), {"agent_id": agent_id, "role": "unknown"})()
        return self.auth.authenticate(agent_id=agent_id, agent_secret=agent_secret, bearer_token=agent_token)

    def _authenticate_and_authorize(
        self,
        agent_id: str,
        agent_secret: str | None,
        action_class: str,
        agent_token: str | None = None,
    ) -> Any:
        if not self.auth.has_profiles():
            return type("AnonProfile", (), {"agent_id": agent_id, "role": "unknown"})()
        profile = self.auth.authenticate(agent_id=agent_id, agent_secret=agent_secret, bearer_token=agent_token)
        self.auth.authorize_action(profile, action_class=action_class)
        return profile

    def _filter_snapshot_for_role(self, snapshot: JSONObject, role: str) -> JSONObject:
        redacted = copy.deepcopy(snapshot)
        redacted["cad_view"] = self._redact_cad_view(redacted.get("cad_view", {}), role)
        if role in {"translation", "retrieval"}:
            loc = redacted.get("location", {})
            if isinstance(loc, dict) and "ani_ali" in loc:
                loc = copy.deepcopy(loc)
                loc["ani_ali"] = "[REDACTED]"
                redacted["location"] = loc
        return redacted

    @staticmethod
    def _redact_cad_view(cad_view: JSONObject, role: str) -> JSONObject:
        if role not in {"translation", "retrieval"}:
            return copy.deepcopy(cad_view)
        pii_fields = {"caller_phone_number", "caller_name", "callback_number"}
        out = copy.deepcopy(cad_view)
        for field in pii_fields:
            if field in out:
                out[field] = "[REDACTED]"
        return out
