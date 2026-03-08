"""Simple web call console UI backend for Section 2.6."""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import time
import uuid
import hashlib
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from agents import (
    CallTakerAgent,
    CallerAgent,
    QAEvaluatorAgent,
    create_calltaker_agent,
    create_caller_agent,
    create_qa_agent,
    get_profile,
    is_manual,
    is_replay,
    list_profiles,
)
from sim_server import SimulationEngine
from sim_server.errors import SimError
from sim_server.schema_utils import load_json


@dataclass
class ConsoleState:
    root: Path
    ui_root: Path
    engine: SimulationEngine
    incident_id: str | None = None
    scenario_id: str | None = None
    caller_seed: dict[str, Any] | None = None
    incident_seed: dict[str, Any] | None = None
    qa_seed: dict[str, Any] | None = None
    caller_agent_id: str = "manual"
    calltaker_agent_id: str = "manual"
    qa_agent_id: str = "deterministic_v1"
    caller_agent: CallerAgent | None = None
    calltaker_agent: CallTakerAgent | None = None
    qa_agent: QAEvaluatorAgent | None = None
    agent_config_root: Path | None = None
    artifacts_root: Path | None = None
    run_id: str = ""
    auto_save_on_end: bool = True
    saved_artifacts: list[dict[str, Any]] = field(default_factory=list)
    replay_steps: list[dict[str, Any]] | None = None
    replay_idx: int = 0
    last_qa_score: dict[str, Any] | None = None


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "911BenchSIMConsole/0.1"

    @property
    def app(self) -> ConsoleState:
        return self.server.app_state  # type: ignore[attr-defined]

    def handle(self) -> None:  # noqa: D401
        # EventSource clients may disconnect during long-lived SSE reads.
        # Treat connection resets as normal teardown instead of noisy traceback.
        try:
            super().handle()
        except ConnectionResetError:
            return

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._handle_api_get(parsed.path, parse_qs(parsed.query))
                return
            self._handle_static(parsed.path)
        except SimError as exc:
            self._send_json({"error": exc.code, "message": exc.message}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover
            self._send_json({"error": "server_error", "message": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
                return
            payload = self._read_json()
            self._handle_api_post(parsed.path, payload)
        except SimError as exc:
            self._send_json({"error": exc.code, "message": exc.message}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self._send_json({"error": "invalid_json"}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover
            self._send_json({"error": "server_error", "message": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            self._send_json({"status": "ok"})
            return
        if path == "/api/agent/catalog":
            self._send_json({"profiles": list_profiles(config_root=self.app.agent_config_root)})
            return
        if path == "/api/state":
            self._send_json(self._state_payload())
            return
        if path == "/api/transcript":
            search = (query.get("search", [""])[0] or "").lower()
            from_turn = int(query.get("from_turn", ["0"])[0] or 0)
            data = self._state_payload()
            rows = [t for t in data.get("transcript", []) if int(t.get("turn", 0)) >= from_turn]
            if search:
                rows = [t for t in rows if search in str(t.get("caller", "")).lower() or search in str(t.get("call_taker", "")).lower()]
            self._send_json({"turns": rows})
            return
        if path == "/api/events/stream":
            self._handle_sse_stream(query)
            return
        if path == "/api/sop":
            incident_type = (query.get("incident_type", ["Fire"])[0] or "Fire").title()
            step = (query.get("step", ["initial"])[0] or "initial").lower()
            self._send_json({"incident_type": incident_type, "step": step, "snippets": self._sop_snippets(incident_type, step)})
            self._log_sop_retrieval(incident_type, step)
            return
        self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def _state_signature(self, data: dict[str, Any]) -> tuple[Any, ...]:
        metrics = data.get("metrics", {}) if isinstance(data.get("metrics"), dict) else {}
        return (
            bool(data.get("loaded")),
            str(data.get("incident_id", "")),
            str(data.get("phase", "")),
            int(data.get("record_version", 0) or 0),
            int(metrics.get("event_count", 0) or 0),
            int(data.get("pending_turn", 0) or 0),
            str(data.get("pending_caller_text", "")),
            len(data.get("checkpoint_inbox", []) or []),
            len(data.get("escalation_inbox", []) or []),
        )

    def _sse_write(self, event_name: str, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload, ensure_ascii=True)
        self.wfile.write(f"event: {event_name}\n".encode("utf-8"))
        self.wfile.write(f"data: {blob}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _handle_sse_stream(self, query: dict[str, list[str]]) -> None:
        requested_incident = (query.get("incident_id", [""])[0] or "").strip()

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_sig: tuple[Any, ...] | None = None
        heartbeat_counter = 0
        while True:
            if self.app.incident_id and (not requested_incident or requested_incident == self.app.incident_id):
                data = self._state_payload()
            else:
                data = {
                    "loaded": bool(self.app.incident_id),
                    "incident_id": self.app.incident_id,
                    "phase": None,
                    "metrics": {"event_count": 0},
                    "checkpoint_inbox": [],
                    "escalation_inbox": [],
                }
            sig = self._state_signature(data)
            try:
                if sig != last_sig:
                    self._sse_write("state", {"state": data})
                    last_sig = sig
                else:
                    heartbeat_counter += 1
                    if heartbeat_counter % 15 == 0:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                time.sleep(0.35)
            except (BrokenPipeError, ConnectionResetError):
                return

    def _handle_api_post(self, path: str, payload: dict[str, Any]) -> None:
        if path == "/api/admin/load_start":
            self._api_load_start(payload)
            return
        incident_id = self._incident_or_400()

        if path == "/api/caller_turn":
            out = self.app.engine.caller_post_turn(
                incident_id=incident_id,
                text=str(payload.get("text", "")),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
            )
            self._send_json(out)
            return
        if path == "/api/calltaker_turn":
            cad_updates = payload.get("cad_updates", {})
            if not isinstance(cad_updates, dict):
                cad_updates = {}
            call_taker_text = str(payload.get("text", ""))
            # Prime caller against the current manual call-taker utterance so
            # caller/call-taker remain interleaved on the same turn.
            self._prime_caller_for_manual_calltaker(incident_id, call_taker_text=call_taker_text)
            out = self.app.engine.calltaker_post_turn(
                incident_id=incident_id,
                text=call_taker_text,
                cad_updates=cad_updates,
            )
            self._maybe_autosave_sealed(incident_id, reason="calltaker_turn")
            self._send_json(out)
            return
        if path == "/api/end_call":
            out = self.app.engine.calltaker_end_call(
                incident_id=incident_id,
                reason=str(payload.get("reason", "other")),
                reason_detail=str(payload.get("reason_detail", "")) or None,
            )
            self._maybe_autosave_sealed(incident_id, reason="end_call")
            self._send_json(out)
            return
        if path == "/api/checkpoint/request":
            out = self.app.engine.checkpoint_request(incident_id=incident_id, request=payload.get("request", {}))
            self._send_json(out)
            return
        if path == "/api/checkpoint/submit":
            out = self.app.engine.checkpoint_submit(
                request_id=str(payload.get("request_id", "")),
                decision=str(payload.get("decision", "denied")),
                edited_payload=payload.get("edited_payload") if isinstance(payload.get("edited_payload"), dict) else None,
                re_escalate_to=str(payload.get("re_escalate_to", "")) or None,
                rationale=str(payload.get("rationale", "")) or None,
            )
            self._send_json(out)
            return
        if path == "/api/agent/auto_step":
            out = self._api_agent_auto_step(turns=int(payload.get("turns", 1)))
            self._maybe_autosave_sealed(incident_id, reason="auto_step")
            self._send_json(out)
            return
        if path == "/api/qa/evaluate":
            out = self._api_qa_evaluate()
            self._send_json(out)
            return
        if path == "/api/artifacts/save":
            out = self._api_artifacts_save(payload)
            self._send_json(out)
            return
        if path == "/api/artifacts/list":
            out = self._api_artifacts_list(payload)
            self._send_json(out)
            return

        self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def _api_load_start(self, payload: dict[str, Any]) -> None:
        root = self.app.root
        requested_scenario = str(payload.get("scenario_id", "")).strip()
        scenario_id = requested_scenario or f"scenario_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        caller_fixture = str(payload.get("caller_fixture", "fixtures/caller_cooperative_calm.json"))
        incident_fixture = str(payload.get("incident_fixture", "fixtures/incident_fire_residential.json"))
        qa_fixture = str(payload.get("qa_fixture", "fixtures/qaTemplate_003.json"))
        max_turns = int(payload.get("max_turns", 20))
        caller_agent_id = str(payload.get("caller_agent_id", payload.get("caller_agent_mode", "manual")))
        calltaker_agent_id = str(payload.get("calltaker_agent_id", payload.get("calltaker_agent_mode", "manual")))
        qa_agent_id = str(payload.get("qa_agent_id", payload.get("qa_agent_mode", "deterministic_v1")))

        caller = load_json(root / caller_fixture)
        incident = load_json(root / incident_fixture)
        qa = load_json(root / qa_fixture)
        incident = json.loads(json.dumps(incident))
        incident["max_turns"] = max_turns
        event_agent_config = self._event_agent_config_snapshot(
            caller_agent_id=caller_agent_id,
            calltaker_agent_id=calltaker_agent_id,
            qa_agent_id=qa_agent_id,
        )

        self.app.engine = SimulationEngine(execution_id=f"console-{scenario_id}")
        loaded = self.app.engine.admin_load_scenario(
            scenario_id=scenario_id,
            caller_json=caller,
            incident_json=incident,
            qa_template_id=str(qa.get("version", "003")),
            qa_template_json=qa,
            agent_config_snapshot=event_agent_config,
        )
        started = self.app.engine.episode_start(loaded["incident_id"])
        self.app.incident_id = loaded["incident_id"]
        self.app.scenario_id = scenario_id
        self.app.caller_seed = caller
        self.app.incident_seed = incident
        self.app.qa_seed = qa
        self.app.caller_agent_id = caller_agent_id
        self.app.calltaker_agent_id = calltaker_agent_id
        self.app.qa_agent_id = qa_agent_id
        self.app.caller_agent = create_caller_agent(
            caller_agent_id,
            caller_json=caller,
            incident_json=incident,
            config_root=self.app.agent_config_root,
        )
        self.app.calltaker_agent = create_calltaker_agent(
            calltaker_agent_id,
            incident_json=incident,
            qa_template_json=qa,
            dispatch_enabled=True,
            config_root=self.app.agent_config_root,
        )
        self.app.qa_agent = create_qa_agent(
            qa_agent_id,
            qa_template_json=qa,
            config_root=self.app.agent_config_root,
        )
        if self.app.saved_artifacts is None:
            self.app.saved_artifacts = []
        self.app.replay_steps = self._load_replay_for_console(scenario_id, incident)
        self.app.replay_idx = 0
        self.app.last_qa_score = None
        self._send_json(
            {
                "loaded": loaded,
                "started": started,
                "scenario_id": scenario_id,
                "scenario_id_generated": bool(not requested_scenario),
            }
        )

    def _incident_or_400(self) -> str:
        if not self.app.incident_id:
            raise SimError("no_episode", "load and start an episode first")
        return self.app.incident_id

    def _state_payload(self) -> dict[str, Any]:
        if not self.app.incident_id:
            return {"loaded": False}
        incident_id = self.app.incident_id
        ep = self.app.engine._get_episode(incident_id)  # type: ignore[attr-defined]
        snapshot = self.app.engine.plant_get_state_snapshot(incident_id)
        events = self.app.engine.episode_events(incident_id)
        transcript = [ev for ev in events if ev.get("event_type") == "conversation"]
        system_events = [ev for ev in events if ev.get("event_type") == "system"]
        checkpoint_inbox = self.app.engine.checkpoint_list(incident_id, status_filter="pending", role_filter="call_taker")["requests"]
        escalation_inbox = self.app.engine.checkpoint_list(incident_id, status_filter="pending", role_filter="supervisor")["requests"]
        turn_count = max([0] + [int(ev.get("turn", 0)) for ev in transcript])
        checkpoint_latencies = [int(ev.get("latency_ms", 0)) for ev in events if ev.get("event_type") == "checkpoint_decision"]
        avg_checkpoint_latency_ms = int(sum(checkpoint_latencies) / len(checkpoint_latencies)) if checkpoint_latencies else 0

        location_panel = {
            "ani": (self.app.caller_seed or {}).get("identity", {}).get("phone_number", ""),
            "ali_address": (self.app.incident_seed or {}).get("location", {}).get("address_line", ""),
            "gps": {
                "lat": (self.app.incident_seed or {}).get("location", {}).get("lat", None),
                "lon": (self.app.incident_seed or {}).get("location", {}).get("lon", None),
                "accuracy_m": (self.app.incident_seed or {}).get("location", {}).get("accuracy_m", 25),
            },
            "confidence": 0.9 if snapshot.get("location", {}).get("address_line") else 0.5,
            "current_location": snapshot.get("location", {}),
        }

        return {
            "loaded": True,
            "scenario_id": self.app.scenario_id,
            "incident_id": incident_id,
            "phase": snapshot.get("episode_phase"),
            "agent_profiles": {
                "caller": self.app.caller_agent_id,
                "calltaker": self.app.calltaker_agent_id,
                "qa": self.app.qa_agent_id,
            },
            "pending_turn": int(getattr(ep, "awaiting_caller_for_turn", 0)),
            "pending_caller_text": str(getattr(ep, "pending_caller_text", "") or ""),
            "pending_caller_metadata": getattr(ep, "pending_caller_metadata", None),
            "cad_state": snapshot.get("cad_state"),
            "record_version": snapshot.get("record_version"),
            "field_versions": snapshot.get("field_versions"),
            "location_panel": location_panel,
            "transcript": transcript,
            "system_events": system_events[-20:],
            "checkpoint_inbox": checkpoint_inbox,
            "escalation_inbox": escalation_inbox,
            "metrics": {
                "turn_count": turn_count,
                "checkpoint_queue_depth": len(checkpoint_inbox),
                "escalation_queue_depth": len(escalation_inbox),
                "avg_checkpoint_latency_ms": avg_checkpoint_latency_ms,
                "event_count": len(events),
            },
            "last_qa_score": self.app.last_qa_score,
            "artifacts": {
                "run_id": self.app.run_id,
                "auto_save_on_end": bool(self.app.auto_save_on_end),
                "saved_count": len(self.app.saved_artifacts or []),
                "last_saved": (self.app.saved_artifacts[-1] if self.app.saved_artifacts else None),
            },
        }

    def _api_agent_auto_step(self, turns: int = 1) -> dict[str, Any]:
        incident_id = self._incident_or_400()
        turns = max(1, min(20, turns))
        executed = 0
        queued_caller_turns = 0
        posted_calltaker_turns = 0
        last_queued_caller_text = ""

        for _ in range(turns):
            snap = self.app.engine.plant_get_state_snapshot(incident_id)
            if snap.get("episode_phase") == "sealed":
                break

            events = self.app.engine.episode_events(incident_id)
            system_events = [ev for ev in events[-30:] if ev.get("event_type") == "system"]
            caller_manual = is_manual("caller", self.app.caller_agent_id, config_root=self.app.agent_config_root)
            calltaker_manual = is_manual("calltaker", self.app.calltaker_agent_id, config_root=self.app.agent_config_root)
            caller_replay = is_replay("caller", self.app.caller_agent_id, config_root=self.app.agent_config_root)
            calltaker_replay = is_replay("calltaker", self.app.calltaker_agent_id, config_root=self.app.agent_config_root)

            if caller_manual and calltaker_manual:
                raise SimError("agent_mode_invalid", "auto_step requires at least one callable or replay agent")

            if caller_replay or calltaker_replay:
                if not self.app.replay_steps or self.app.replay_idx >= len(self.app.replay_steps):
                    break
                step = self.app.replay_steps[self.app.replay_idx]
                self.app.replay_idx += 1
                caller_text = str(step.get("caller_text", ""))
                caller_meta = step.get("caller_metadata") if isinstance(step.get("caller_metadata"), dict) else None
                calltaker_text = str(step.get("calltaker_text", ""))
                cad_updates = step.get("cad_updates") if isinstance(step.get("cad_updates"), dict) else {}
                end_call = bool(step.get("end_call", False))
                end_reason = str(step.get("end_reason", "other"))
            else:
                caller_text = ""
                caller_meta = None
                calltaker_text = ""
                cad_updates = {}
                end_call = False
                end_reason = "other"
                if not calltaker_manual:
                    if not self.app.calltaker_agent:
                        raise SimError("agent_mode_invalid", "calltaker profile is not callable")
                    input_caller_text = self._pending_or_latest_caller_text(incident_id)
                    pending_checkpoints = self.app.engine.checkpoint_list(
                        incident_id=incident_id,
                        status_filter="pending",
                        role_filter="call_taker",
                    ).get("requests", [])
                    decision = self.app.calltaker_agent.next_turn(
                        caller_text=input_caller_text,
                        cad_state=snap.get("cad_state", {}),
                        system_events=system_events,
                        pending_checkpoints=pending_checkpoints,
                    )
                    calltaker_text = decision.text
                    cad_updates = decision.cad_updates
                    end_call = bool(decision.end_call)
                    end_reason = str(decision.end_reason or "other")
                    if end_call and not self._end_call_allowed(
                        incident_id=incident_id,
                        reason=end_reason,
                        pending_cad_updates=cad_updates,
                    ):
                        # Ask the agent to correct itself in the same turn and
                        # hide the invalid end-call draft from the transcript.
                        correction_events = list(system_events)
                        correction_events.append(
                            {
                                "event_type": "system",
                                "subtype": "generic",
                                "text": (
                                    "END_CALL_REJECTED: Call cannot end yet. "
                                    "If dispatch is triggered, end_call is allowed only after responders_arrived."
                                ),
                            }
                        )
                        for _ in range(2):
                            decision = self.app.calltaker_agent.next_turn(
                                caller_text=input_caller_text,
                                cad_state=snap.get("cad_state", {}),
                                system_events=correction_events,
                                pending_checkpoints=pending_checkpoints,
                            )
                            calltaker_text = decision.text
                            cad_updates = decision.cad_updates
                            end_call = bool(decision.end_call)
                            end_reason = str(decision.end_reason or "other")
                            if not end_call or self._end_call_allowed(
                                incident_id=incident_id,
                                reason=end_reason,
                                pending_cad_updates=cad_updates,
                            ):
                                break
                            correction_events.append(
                                {
                                    "event_type": "system",
                                    "subtype": "generic",
                                    "text": (
                                        "END_CALL_REJECTED_AGAIN: continue call and gather/monitor information."
                                    ),
                                }
                            )
                        if end_call and not self._end_call_allowed(
                            incident_id=incident_id,
                            reason=end_reason,
                            pending_cad_updates=cad_updates,
                        ):
                            end_call = False
                            end_reason = "other"
                if not caller_manual:
                    if not self.app.caller_agent:
                        raise SimError("agent_mode_invalid", "caller profile is not callable")
                    ct_input = calltaker_text if calltaker_text else self._latest_calltaker_text(incident_id)
                    caller_text, caller_meta = self.app.caller_agent.next_turn(call_taker_text=ct_input, system_events=system_events)

            if not caller_manual:
                self.app.engine.caller_post_turn(incident_id=incident_id, text=caller_text, metadata=caller_meta)
                queued_caller_turns += 1
                last_queued_caller_text = caller_text
            if not calltaker_manual:
                self.app.engine.calltaker_post_turn(
                    incident_id=incident_id,
                    text=calltaker_text,
                    cad_updates=cad_updates,
                    call_taker_metadata=getattr(decision, "call_taker_metadata", None) if not calltaker_replay else None,
                )
                for cp in getattr(decision, "checkpoint_decisions", []) if not calltaker_replay else []:
                    if not isinstance(cp, dict):
                        continue
                    req_id = str(cp.get("request_id", "")).strip()
                    cp_decision = str(cp.get("decision", "")).strip()
                    if not req_id or not cp_decision:
                        continue
                    self.app.engine.checkpoint_submit(
                        request_id=req_id,
                        decision=cp_decision,
                        edited_payload=cp.get("edited_payload") if isinstance(cp.get("edited_payload"), dict) else None,
                        re_escalate_to=str(cp.get("re_escalate_to", "")).strip() or None,
                        rationale=str(cp.get("rationale", "")).strip() or None,
                    )
                posted_calltaker_turns += 1
                executed += 1
                if end_call:
                    self.app.engine.calltaker_end_call(incident_id=incident_id, reason=end_reason)
                    break

        return {
            "status": "ok",
            "executed_turns": executed,
            "queued_caller_turns": queued_caller_turns,
            "posted_calltaker_turns": posted_calltaker_turns,
            "last_queued_caller_text": last_queued_caller_text,
            "phase": self.app.engine.plant_get_state_snapshot(incident_id).get("episode_phase"),
        }

    def _end_call_allowed(self, incident_id: str, reason: str, pending_cad_updates: dict[str, Any] | None = None) -> bool:
        snap = self.app.engine.plant_get_state_snapshot(incident_id)
        cad_state = snap.get("cad_state", {}) if isinstance(snap.get("cad_state"), dict) else {}
        updates = pending_cad_updates if isinstance(pending_cad_updates, dict) else {}
        dispatch_now = bool(updates.get("dispatch_triggered", cad_state.get("dispatch_triggered", False)))
        events = self.app.engine.episode_events(incident_id)
        responders_arrived = any(
            ev.get("event_type") == "system" and ev.get("subtype") == "responders_arrived"
            for ev in events
        )
        rsn = str(reason or "").strip() or "other"
        if dispatch_now and not responders_arrived and rsn != "responders_arrived":
            return False
        if rsn == "responders_arrived" and not responders_arrived:
            return False
        return True

    def _api_qa_evaluate(self) -> dict[str, Any]:
        incident_id = self._incident_or_400()
        if is_manual("qa", self.app.qa_agent_id, config_root=self.app.agent_config_root) or self.app.qa_agent is None:
            raise SimError("qa_mode_invalid", "qa agent profile is manual; cannot evaluate")

        events = self.app.engine.episode_events(incident_id)
        incident_type = str((self.app.incident_seed or {}).get("type", "Unknown"))
        qa_score = self.app.qa_agent.evaluate(events=events, incident_type=incident_type)
        self.app.last_qa_score = qa_score
        return {"status": "ok", "qa_score": qa_score}

    def _agent_config_manifest(self) -> dict[str, Any]:
        manifest: dict[str, Any] = {}
        root = self.app.agent_config_root
        if root is None:
            return manifest
        role_to_profile = {
            "caller": self.app.caller_agent_id,
            "calltaker": self.app.calltaker_agent_id,
            "qa": self.app.qa_agent_id,
        }
        for role, profile_id in role_to_profile.items():
            path = root / f"{role}.{profile_id}.yaml"
            if not path.exists():
                continue
            raw = path.read_bytes()
            manifest[role] = {
                "profile_id": profile_id,
                "config_file": str(path),
                "config_sha256": hashlib.sha256(raw).hexdigest(),
            }
        return manifest

    def _event_agent_config_snapshot(self, caller_agent_id: str, calltaker_agent_id: str, qa_agent_id: str) -> dict[str, Any]:
        return {
            "caller_agent": self._event_agent_entry(role="caller", profile_id=caller_agent_id),
            "calltaker_agent": self._event_agent_entry(role="calltaker", profile_id=calltaker_agent_id),
            "helper_agent": None,
            "qa_agent": self._event_agent_entry(role="qa", profile_id=qa_agent_id),
        }

    def _event_agent_entry(self, role: str, profile_id: str) -> dict[str, Any]:
        profile = get_profile(role, profile_id)
        cfg = self._load_agent_yaml_for_profile(role=role, profile_id=profile_id)
        system_prompt = cfg.get("system_prompt")
        prompt_hash = "none"
        if isinstance(system_prompt, str) and system_prompt.strip():
            prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        entry: dict[str, Any] = {
            "profile_id": profile.id,
            "provider": profile.provider,
            "mode": profile.mode,
            "model": str(cfg.get("model", profile.model)),
            "temperature": float(cfg.get("temperature", profile.temperature)),
            "prompt_hash": prompt_hash,
        }
        config_path = (self.app.agent_config_root or Path(".")) / f"{role}.{profile_id}.yaml"
        if config_path.exists():
            entry["config_sha256"] = hashlib.sha256(config_path.read_bytes()).hexdigest()
        return entry

    def _load_agent_yaml_for_profile(self, role: str, profile_id: str) -> dict[str, Any]:
        root = self.app.agent_config_root
        if root is None:
            return {}
        path = root / f"{role}.{profile_id}.yaml"
        if not path.exists():
            return {}
        try:
            import yaml  # type: ignore
        except Exception:
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _artifact_extra_meta(self, incident_id: str) -> dict[str, Any]:
        return {
            "console_runtime": {
                "run_id": self.app.run_id,
                "auto_save_on_end": bool(self.app.auto_save_on_end),
                "agent_profiles": {
                    "caller": self.app.caller_agent_id,
                    "calltaker": self.app.calltaker_agent_id,
                    "qa": self.app.qa_agent_id,
                },
                "agent_config": self._agent_config_manifest(),
            },
            "fixture_refs": {
                "incident_id": incident_id,
            },
        }

    def _save_current_episode_artifacts(self, incident_id: str, reason: str) -> dict[str, Any]:
        if self.app.artifacts_root is None:
            raise SimError("artifacts_root_unset", "artifact root is not configured")
        self._ensure_run_documents()
        saved = self.app.engine.save_artifact_bundle(
            incident_id=incident_id,
            output_root=self.app.artifacts_root,
            run_id=self.app.run_id,
            qa_score=self.app.last_qa_score,
            extra_meta=self._artifact_extra_meta(incident_id) | {"save_reason": reason},
        )
        if self.app.saved_artifacts is None:
            self.app.saved_artifacts = []
        self.app.saved_artifacts.append(saved)
        self._rebuild_run_index()
        return saved

    def _maybe_autosave_sealed(self, incident_id: str, reason: str) -> None:
        if not self.app.auto_save_on_end:
            return
        phase = self.app.engine.plant_get_state_snapshot(incident_id).get("episode_phase")
        if phase != "sealed":
            return
        if self.app.saved_artifacts and self.app.saved_artifacts[-1].get("incident_id") == incident_id:
            return
        self._save_current_episode_artifacts(incident_id=incident_id, reason=reason)

    def _api_artifacts_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        incident_id = str(payload.get("incident_id", "")).strip() or self._incident_or_400()
        reason = str(payload.get("reason", "manual_export"))
        return {"status": "saved", "artifact": self._save_current_episode_artifacts(incident_id=incident_id, reason=reason)}

    def _api_artifacts_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = str(payload.get("run_id", "")).strip() or self.app.run_id
        root = self.app.artifacts_root or (self.app.root / "runs")
        run_root = root / run_id
        self._ensure_run_documents(run_root=run_root, run_id=run_id)
        self._rebuild_run_index(run_root=run_root)
        rows: list[dict[str, Any]] = []
        if run_root.exists():
            for meta_path in sorted(run_root.rglob("meta.json")):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                rows.append(
                    {
                        "episode_dir": str(meta_path.parent),
                        "scenario_id": meta.get("scenario_id"),
                        "incident_id": meta.get("incident_id"),
                        "end_ts": meta.get("end_ts"),
                    }
                )
        return {"run_id": run_id, "root": str(run_root), "episodes": rows}

    def _ensure_run_documents(self, run_root: Path | None = None, run_id: str | None = None) -> None:
        root = run_root or ((self.app.artifacts_root or (self.app.root / "runs")) / (run_id or self.app.run_id))
        resolved_run_id = run_id or self.app.run_id
        root.mkdir(parents=True, exist_ok=True)

        manifest_path = root / "run_manifest.json"
        if not manifest_path.exists():
            manifest = {
                "run_id": resolved_run_id,
                "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "created_by": "sim_server.console_server",
                "root": str(root),
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

        batch_config_path = root / "batch_config.json"
        if not batch_config_path.exists():
            config = {
                "run_id": resolved_run_id,
                "artifacts_root": str(self.app.artifacts_root or (self.app.root / "runs")),
                "auto_save_on_end": bool(self.app.auto_save_on_end),
                "agent_profiles": {
                    "caller": self.app.caller_agent_id,
                    "calltaker": self.app.calltaker_agent_id,
                    "qa": self.app.qa_agent_id,
                },
                "agent_config_manifest": self._agent_config_manifest(),
            }
            batch_config_path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")

    def _rebuild_run_index(self, run_root: Path | None = None) -> None:
        root = run_root or ((self.app.artifacts_root or (self.app.root / "runs")) / self.app.run_id)
        if not root.exists():
            return

        rows: list[dict[str, Any]] = []
        for meta_path in sorted(root.rglob("meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            qa_score_value: float | None = None
            qa_path = meta_path.parent / "qa_score.json"
            if qa_path.exists():
                try:
                    qa_obj = json.loads(qa_path.read_text(encoding="utf-8"))
                    if isinstance(qa_obj, dict) and qa_obj.get("normalized_score") is not None:
                        qa_score_value = float(qa_obj.get("normalized_score"))
                except Exception:
                    qa_score_value = None
            rows.append(
                {
                    "episode_dir": str(meta_path.parent),
                    "scenario_id": str(meta.get("scenario_id", "")),
                    "incident_id": str(meta.get("incident_id", "")),
                    "end_ts": str(meta.get("end_ts", "")),
                    "total_turns": int(meta.get("total_turns", 0) or 0),
                    "total_events": int(meta.get("total_events", 0) or 0),
                    "qa_score": qa_score_value,
                }
            )

        (root / "index.json").write_text(json.dumps({"episodes": rows}, indent=2, sort_keys=True), encoding="utf-8")
        with (root / "index.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["episode_dir", "scenario_id", "incident_id", "end_ts", "total_turns", "total_events", "qa_score"],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _latest_calltaker_text(self, incident_id: str) -> str:
        turns = self.app.engine.plant_get_transcript_since(incident_id, 0).get("turns", [])
        if not turns:
            return ""
        return str(turns[-1].get("call_taker", ""))

    def _pending_or_latest_caller_text(self, incident_id: str) -> str:
        # Console backend has direct in-process access to episode state.
        ep = self.app.engine._get_episode(incident_id)  # type: ignore[attr-defined]
        pending = str(getattr(ep, "pending_caller_text", "") or "")
        if pending:
            return pending
        turns = self.app.engine.plant_get_transcript_since(incident_id, 0).get("turns", [])
        if not turns:
            return ""
        return str(turns[-1].get("caller", ""))

    def _prime_caller_for_manual_calltaker(self, incident_id: str, call_taker_text: str | None = None) -> None:
        if not is_manual("calltaker", self.app.calltaker_agent_id, config_root=self.app.agent_config_root):
            return
        if is_manual("caller", self.app.caller_agent_id, config_root=self.app.agent_config_root):
            return
        ep = self.app.engine._get_episode(incident_id)  # type: ignore[attr-defined]
        if str(getattr(ep, "pending_caller_text", "") or "").strip():
            return
        events = self.app.engine.episode_events(incident_id)
        system_events = [ev for ev in events[-30:] if ev.get("event_type") == "system"]
        if is_replay("caller", self.app.caller_agent_id, config_root=self.app.agent_config_root):
            if not self.app.replay_steps or self.app.replay_idx >= len(self.app.replay_steps):
                return
            step = self.app.replay_steps[self.app.replay_idx]
            self.app.replay_idx += 1
            caller_text = str(step.get("caller_text", ""))
            caller_meta = step.get("caller_metadata") if isinstance(step.get("caller_metadata"), dict) else None
        else:
            if not self.app.caller_agent:
                return
            ct_input = str(call_taker_text or "").strip() or self._latest_calltaker_text(incident_id)
            caller_text, caller_meta = self.app.caller_agent.next_turn(call_taker_text=ct_input, system_events=system_events)
        self.app.engine.caller_post_turn(incident_id=incident_id, text=caller_text, metadata=caller_meta)

    def _load_replay_for_console(self, scenario_id: str, incident_seed: dict[str, Any]) -> list[dict[str, Any]] | None:
        candidates: list[Path] = []
        base = self.app.root / "fixtures" / "sim"
        candidates.append(base / f"{scenario_id}_replay.json")
        incident_type = str(incident_seed.get("type", "")).lower()
        mapped = {
            "fire": "phase1_fire_replay.json",
            "police": "phase1_police_replay.json",
            "ems": "phase1_ems_replay.json",
        }.get(incident_type)
        if mapped:
            candidates.append(base / mapped)
        for path in candidates:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _sop_snippets(self, incident_type: str, step: str) -> list[dict[str, str]]:
        by_type = {
            "Fire": [
                {"step": "initial", "title": "Fire Initial Triage", "text": "Confirm exact location, occupants, flame/smoke conditions, hazards."},
                {"step": "dispatch", "title": "Fire Dispatch Guidance", "text": "Dispatch immediately if active fire is confirmed; maintain line safety guidance."},
            ],
            "Police": [
                {"step": "initial", "title": "Police Initial Triage", "text": "Assess immediate threat, weapons, suspect description, scene safety."},
                {"step": "dispatch", "title": "Police Dispatch Guidance", "text": "Prioritize active violence and officer safety information."},
            ],
            "Ems": [
                {"step": "initial", "title": "EMS Initial Triage", "text": "Assess consciousness, breathing, bleeding, patient age/condition."},
                {"step": "dispatch", "title": "EMS Dispatch Guidance", "text": "Dispatch for life threats; provide immediate pre-arrival instructions."},
            ],
        }
        snippets = by_type.get(incident_type.title(), [])
        if step == "all":
            return snippets
        return [row for row in snippets if row.get("step") == step] or snippets[:1]

    def _log_sop_retrieval(self, incident_type: str, step: str) -> None:
        if not self.app.incident_id:
            return
        phase = self.app.engine.plant_get_state_snapshot(self.app.incident_id).get("episode_phase")
        if phase == "sealed":
            return
        turn = self._state_payload().get("metrics", {}).get("turn_count", 0)
        self.app.engine.plant_emit_event(
            {
                "event_type": "system",
                "incident_id": self.app.incident_id,
                "turn": int(turn),
                "subtype": "generic",
                "text": f"sop_retrieval:{incident_type}:{step}",
                "detail": {"incident_type": incident_type, "step": step},
            }
        )

    def _handle_static(self, path: str) -> None:
        rel = "index.html" if path in {"/", ""} else path.lstrip("/")
        target = (self.app.ui_root / rel).resolve()
        if self.app.ui_root not in target.parents and target != self.app.ui_root:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b"{}"
        payload = json.loads(body.decode("utf-8") or "{}")
        return payload if isinstance(payload, dict) else {}

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SIM web call console UI backend")
    parser.add_argument("--root", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--agent-config-dir", default="agents/config")
    parser.add_argument("--artifacts-dir", default="runs")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--no-auto-save-on-end", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    ui_root = root / "ui"
    run_id = str(args.run_id).strip() or f"run_{int(time.time())}"
    state = ConsoleState(
        root=root,
        ui_root=ui_root,
        engine=SimulationEngine(execution_id="console-init"),
        agent_config_root=(root / args.agent_config_dir).resolve(),
        artifacts_root=(root / args.artifacts_dir).resolve(),
        run_id=run_id,
        auto_save_on_end=not bool(args.no_auto_save_on_end),
    )

    server = ThreadingHTTPServer((args.host, args.port), ConsoleHandler)
    server.app_state = state  # type: ignore[attr-defined]

    print(f"SIM console server listening on http://{args.host}:{args.port} (run_id={run_id})")
    server.serve_forever()


if __name__ == "__main__":
    main()
