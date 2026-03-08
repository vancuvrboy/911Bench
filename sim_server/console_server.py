"""Simple web call console UI backend for Section 2.6."""

from __future__ import annotations

import argparse
import json
import mimetypes
from dataclasses import dataclass
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
    replay_steps: list[dict[str, Any]] | None = None
    replay_idx: int = 0
    last_qa_score: dict[str, Any] | None = None


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "911BenchSIMConsole/0.1"

    @property
    def app(self) -> ConsoleState:
        return self.server.app_state  # type: ignore[attr-defined]

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
            self._send_json({"profiles": list_profiles()})
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
        if path == "/api/sop":
            incident_type = (query.get("incident_type", ["Fire"])[0] or "Fire").title()
            step = (query.get("step", ["initial"])[0] or "initial").lower()
            self._send_json({"incident_type": incident_type, "step": step, "snippets": self._sop_snippets(incident_type, step)})
            self._log_sop_retrieval(incident_type, step)
            return
        self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

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
            self._prime_caller_for_manual_calltaker(incident_id)
            out = self.app.engine.calltaker_post_turn(
                incident_id=incident_id,
                text=str(payload.get("text", "")),
                cad_updates=cad_updates,
            )
            self._send_json(out)
            return
        if path == "/api/end_call":
            out = self.app.engine.calltaker_end_call(
                incident_id=incident_id,
                reason=str(payload.get("reason", "other")),
                reason_detail=str(payload.get("reason_detail", "")) or None,
            )
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
            self._send_json(out)
            return
        if path == "/api/qa/evaluate":
            out = self._api_qa_evaluate()
            self._send_json(out)
            return

        self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def _api_load_start(self, payload: dict[str, Any]) -> None:
        root = self.app.root
        scenario_id = str(payload.get("scenario_id", "ui_session"))
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

        self.app.engine = SimulationEngine(execution_id=f"console-{scenario_id}")
        loaded = self.app.engine.admin_load_scenario(
            scenario_id=scenario_id,
            caller_json=caller,
            incident_json=incident,
            qa_template_id=str(qa.get("version", "003")),
            qa_template_json=qa,
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
        self.app.caller_agent = create_caller_agent(caller_agent_id, caller_json=caller, incident_json=incident)
        self.app.calltaker_agent = create_calltaker_agent(calltaker_agent_id, incident_json=incident, dispatch_enabled=True)
        self.app.qa_agent = create_qa_agent(qa_agent_id, qa_template_json=qa)
        self.app.replay_steps = self._load_replay_for_console(scenario_id, incident)
        self.app.replay_idx = 0
        self.app.last_qa_score = None
        self._send_json({"loaded": loaded, "started": started})

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
            caller_manual = is_manual("caller", self.app.caller_agent_id)
            calltaker_manual = is_manual("calltaker", self.app.calltaker_agent_id)
            caller_replay = is_replay("caller", self.app.caller_agent_id)
            calltaker_replay = is_replay("calltaker", self.app.calltaker_agent_id)

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
                if not caller_manual:
                    if not self.app.caller_agent:
                        raise SimError("agent_mode_invalid", "caller profile is not callable")
                    last_ct = self._latest_calltaker_text(incident_id)
                    caller_text, caller_meta = self.app.caller_agent.next_turn(call_taker_text=last_ct, system_events=system_events)
                if not calltaker_manual:
                    if not self.app.calltaker_agent:
                        raise SimError("agent_mode_invalid", "calltaker profile is not callable")
                    input_caller_text = caller_text if caller_text else self._pending_or_latest_caller_text(incident_id)
                    decision = self.app.calltaker_agent.next_turn(
                        caller_text=input_caller_text,
                        cad_state=snap.get("cad_state", {}),
                        system_events=system_events,
                    )
                    calltaker_text = decision.text
                    cad_updates = decision.cad_updates
                    end_call = bool(decision.end_call)
                    end_reason = str(decision.end_reason or "other")

            if not caller_manual:
                self.app.engine.caller_post_turn(incident_id=incident_id, text=caller_text, metadata=caller_meta)
                queued_caller_turns += 1
                last_queued_caller_text = caller_text
            if not calltaker_manual:
                self.app.engine.calltaker_post_turn(incident_id=incident_id, text=calltaker_text, cad_updates=cad_updates)
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

    def _api_qa_evaluate(self) -> dict[str, Any]:
        incident_id = self._incident_or_400()
        if is_manual("qa", self.app.qa_agent_id) or self.app.qa_agent is None:
            raise SimError("qa_mode_invalid", "qa agent profile is manual; cannot evaluate")

        events = self.app.engine.episode_events(incident_id)
        incident_type = str((self.app.incident_seed or {}).get("type", "Unknown"))
        qa_score = self.app.qa_agent.evaluate(events=events, incident_type=incident_type)
        self.app.last_qa_score = qa_score
        return {"status": "ok", "qa_score": qa_score}

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

    def _prime_caller_for_manual_calltaker(self, incident_id: str) -> None:
        if not is_manual("calltaker", self.app.calltaker_agent_id):
            return
        if is_manual("caller", self.app.caller_agent_id):
            return
        ep = self.app.engine._get_episode(incident_id)  # type: ignore[attr-defined]
        if str(getattr(ep, "pending_caller_text", "") or "").strip():
            return
        events = self.app.engine.episode_events(incident_id)
        system_events = [ev for ev in events[-30:] if ev.get("event_type") == "system"]
        if is_replay("caller", self.app.caller_agent_id):
            if not self.app.replay_steps or self.app.replay_idx >= len(self.app.replay_steps):
                return
            step = self.app.replay_steps[self.app.replay_idx]
            self.app.replay_idx += 1
            caller_text = str(step.get("caller_text", ""))
            caller_meta = step.get("caller_metadata") if isinstance(step.get("caller_metadata"), dict) else None
        else:
            if not self.app.caller_agent:
                return
            last_ct = self._latest_calltaker_text(incident_id)
            caller_text, caller_meta = self.app.caller_agent.next_turn(call_taker_text=last_ct, system_events=system_events)
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
    args = parser.parse_args()

    root = Path(args.root).resolve()
    ui_root = root / "ui"
    state = ConsoleState(root=root, ui_root=ui_root, engine=SimulationEngine(execution_id="console-init"))

    server = ThreadingHTTPServer((args.host, args.port), ConsoleHandler)
    server.app_state = state  # type: ignore[attr-defined]

    print(f"SIM console server listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
