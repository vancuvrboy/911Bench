"""HTTP adapter exposing SIM southbound endpoints for Phase 3 integration."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from sim_server import SimulationEngine
from sim_server.errors import SimError
from sim_server.schema_utils import load_json

JSONObject = dict[str, Any]


@dataclass
class AppState:
    root: Path
    engine: SimulationEngine
    auto_approve_checkpoints: bool = True
    route_counts: dict[str, int] | None = None
    checkpoint_poll_mode: str = "normal"


class SouthboundHandler(BaseHTTPRequestHandler):
    server_version = "911BenchSimSouthbound/0.1"

    @property
    def app(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json({"status": "ok"})
            return
        self._send_json({"error": f"unknown_route:{parsed.path}"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            payload = self._read_json()
            if self.app.route_counts is None:
                self.app.route_counts = {}
            self.app.route_counts[parsed.path] = int(self.app.route_counts.get(parsed.path, 0)) + 1
            routes: dict[str, Callable[[JSONObject], JSONObject]] = {
                "/admin/load_start": self._admin_load_start,
                "/admin/post_turn": self._admin_post_turn,
                "/admin/end_call": self._admin_end_call,
                "/admin/events": self._admin_events,
                "/admin/stats": self._admin_stats,
                "/admin/config": self._admin_config,
                "/plant/get_state_snapshot": lambda body: self.app.engine.plant_get_state_snapshot(
                    incident_id=str(body.get("incident_id", ""))
                ),
                "/plant/get_transcript_since": lambda body: self.app.engine.plant_get_transcript_since(
                    incident_id=str(body.get("incident_id", "")),
                    cursor=int(body.get("cursor", 0)),
                ),
                "/plant/apply_cad_patch": self._plant_apply_patch,
                "/plant/emit_event": self._plant_emit_event_safe,
                "/checkpoint/request": self._checkpoint_request,
                "/checkpoint/poll": self._checkpoint_poll,
                "/checkpoint/list": lambda body: self.app.engine.checkpoint_list(
                    incident_id=str(body.get("incident_id", "")),
                    status_filter=(str(body.get("status_filter")) if body.get("status_filter") is not None else None),
                    role_filter=(str(body.get("role_filter")) if body.get("role_filter") is not None else None),
                ),
                "/checkpoint/submit": lambda body: self.app.engine.checkpoint_submit(
                    request_id=str(body.get("request_id", "")),
                    decision=str(body.get("decision", "")),
                    edited_payload=body.get("edited_payload") if isinstance(body.get("edited_payload"), dict) else None,
                    re_escalate_to=(str(body.get("re_escalate_to")) if body.get("re_escalate_to") else None),
                    rationale=(str(body.get("rationale")) if body.get("rationale") else None),
                ),
            }
            handler = routes.get(parsed.path)
            if handler is None:
                self._send_json({"error": f"unknown_route:{parsed.path}"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(handler(payload))
        except SimError as exc:
            self._send_json({"error": exc.code, "message": exc.message}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self._send_json({"error": "invalid_json"}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover
            self._send_json({"error": "server_error", "message": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _admin_load_start(self, payload: JSONObject) -> JSONObject:
        root = self.app.root
        scenario_id = str(payload.get("scenario_id", "phase3_smoke"))
        caller_fixture = str(payload.get("caller_fixture", "fixtures/caller_cooperative_calm.json"))
        incident_fixture = str(payload.get("incident_fixture", "fixtures/incident_fire_residential.json"))
        qa_fixture = str(payload.get("qa_fixture", "fixtures/qaTemplate_003.json"))
        max_turns = payload.get("max_turns")

        caller = load_json(root / caller_fixture)
        incident = load_json(root / incident_fixture)
        if max_turns is not None:
            incident = dict(incident)
            incident["max_turns"] = int(max_turns)
        qa = load_json(root / qa_fixture)

        loaded = self.app.engine.admin_load_scenario(
            scenario_id=scenario_id,
            caller_json=caller,
            incident_json=incident,
            qa_template_id=str(qa.get("version", "003")),
            qa_template_json=qa,
        )
        started = self.app.engine.episode_start(loaded["incident_id"])
        return {"loaded": loaded, "started": started}

    def _admin_post_turn(self, payload: JSONObject) -> JSONObject:
        incident_id = str(payload.get("incident_id", ""))
        caller_text = str(payload.get("caller", ""))
        call_taker_text = str(payload.get("call_taker", ""))
        cad_updates = payload.get("cad_updates", {})
        if not isinstance(cad_updates, dict):
            cad_updates = {}
        caller = self.app.engine.caller_post_turn(incident_id=incident_id, text=caller_text)
        calltaker = self.app.engine.calltaker_post_turn(
            incident_id=incident_id,
            text=call_taker_text,
            cad_updates=cad_updates,
        )
        return {"caller": caller, "call_taker": calltaker}

    def _admin_end_call(self, payload: JSONObject) -> JSONObject:
        incident_id = str(payload.get("incident_id", ""))
        reason = str(payload.get("reason", "other"))
        reason_detail = str(payload.get("reason_detail", "")) or None
        return self.app.engine.calltaker_end_call(incident_id=incident_id, reason=reason, reason_detail=reason_detail)

    def _admin_events(self, payload: JSONObject) -> JSONObject:
        incident_id = str(payload.get("incident_id", ""))
        events = self.app.engine.episode_events(incident_id=incident_id)
        return {"incident_id": incident_id, "events": events, "count": len(events)}

    def _admin_stats(self, payload: JSONObject) -> JSONObject:
        return {
            "route_counts": dict(self.app.route_counts or {}),
            "checkpoint_poll_mode": self.app.checkpoint_poll_mode,
        }

    def _admin_config(self, payload: JSONObject) -> JSONObject:
        mode = str(payload.get("checkpoint_poll_mode", self.app.checkpoint_poll_mode) or "normal")
        if mode not in {"normal", "force_timeout"}:
            raise SimError("invalid_config", f"unsupported checkpoint_poll_mode:{mode}")
        self.app.checkpoint_poll_mode = mode
        return {"checkpoint_poll_mode": self.app.checkpoint_poll_mode}

    def _checkpoint_request(self, payload: JSONObject) -> JSONObject:
        incident_id = str(payload.get("incident_id", ""))
        request_body = payload.get("request", payload)
        if not isinstance(request_body, dict):
            request_body = {}
        response = self.app.engine.checkpoint_request(incident_id=incident_id, request=request_body)
        if self.app.auto_approve_checkpoints:
            self.app.engine.checkpoint_submit(
                request_id=response["request_id"],
                decision="approved",
                rationale="auto_approved_by_phase3_southbound_server",
            )
        return response

    def _checkpoint_poll(self, payload: JSONObject) -> JSONObject:
        request_id = str(payload.get("request_id", ""))
        poll = self.app.engine.checkpoint_poll(request_id=request_id)
        if self.app.checkpoint_poll_mode == "force_timeout" and str(poll.get("status", "")) == "pending":
            return {"status": "timeout", "response": {"latency_ms": 0, "rationale": "forced_timeout"}}
        return poll

    def _plant_apply_patch(self, payload: JSONObject) -> JSONObject:
        return self.app.engine.plant_apply_cad_patch(
            incident_id=str(payload.get("incident_id", "")),
            action_id=str(payload.get("idempotency_key", payload.get("action_id", ""))),
            action_class=str(payload.get("action_class", "")),
            payload=payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {},
            read_set=payload.get("read_set", {}) if isinstance(payload.get("read_set"), dict) else {},
            policy_id=str(payload.get("policy_id", "")),
            policy_hash=str(payload.get("policy_hash", payload.get("policy_id", ""))),
            proposer_agent_id=str(payload.get("proposer_agent_id", "governance")),
            checkpoint_ref=(str(payload.get("checkpoint_ref")) if payload.get("checkpoint_ref") else None),
        )

    def _read_json(self) -> JSONObject:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _normalize_event(event: Any) -> JSONObject:
        if not isinstance(event, dict):
            return {}
        normalized = dict(event)
        if "event_type" not in normalized and "type" in normalized:
            normalized["event_type"] = str(normalized.get("type", "system"))
        if "incident_id" not in normalized and normalized.get("event_type") == "policy_swapped":
            # Governance can emit control-plane events not scoped to a single incident.
            normalized["incident_id"] = "__control_plane__"
        return normalized

    def _plant_emit_event_safe(self, payload: JSONObject) -> JSONObject:
        event = self._normalize_event(payload.get("event", {}))
        if event.get("incident_id") == "__control_plane__":
            return {"status": "ignored_control_plane"}
        return self.app.engine.plant_emit_event(event=event)

    def _send_json(self, payload: JSONObject, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server() -> None:
    parser = argparse.ArgumentParser(description="Run SIM southbound HTTP adapter for governance integration.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8300)
    parser.add_argument("--execution-id", default="phase3-sim")
    parser.add_argument("--auto-approve-checkpoints", action="store_true", default=True)
    parser.add_argument("--no-auto-approve-checkpoints", dest="auto_approve_checkpoints", action="store_false")
    args = parser.parse_args()

    app_state = AppState(
        root=Path(args.root).resolve(),
        engine=SimulationEngine(execution_id=args.execution_id),
        auto_approve_checkpoints=bool(args.auto_approve_checkpoints),
        route_counts={},
        checkpoint_poll_mode="normal",
    )
    httpd = ThreadingHTTPServer((args.host, int(args.port)), SouthboundHandler)
    httpd.app_state = app_state  # type: ignore[attr-defined]
    print(f"sim-southbound-server listening on http://{args.host}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    run_server()
