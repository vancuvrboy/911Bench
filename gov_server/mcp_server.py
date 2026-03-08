"""Minimal MCP-style HTTP/SSE server for governance northbound tools."""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .auth import AuthError, ConflictError, ForbiddenError, RateLimitedError
from .errors import VersionCompatibilityError
from .service import GovernanceConfig, GovernanceService

JSONObject = dict[str, Any]


class MCPHandler(BaseHTTPRequestHandler):
    server: "MCPHTTPServer"

    def _bearer_token(self) -> str | None:
        raw = self.headers.get("Authorization", "")
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return None

    def _correlation_id(self) -> str:
        existing = self.headers.get("X-Correlation-Id") or self.headers.get("X-Request-Id")
        return str(existing).strip() if existing else str(uuid.uuid4())

    def _tool_specs(self) -> list[JSONObject]:
        return [
            {
                "name": "propose_action",
                "description": "Submit ActionProposal for governance enforcement pipeline.",
                "input": {
                    "type": "object",
                    "required": ["action_id", "incident_id", "action_class", "proposed_payload", "evidence_refs", "uncertainty", "read_set", "proposer"],
                },
            },
            {
                "name": "get_context_snapshot",
                "description": "Get role-filtered context snapshot for incident.",
                "input": {"type": "object", "required": ["incident_id", "agent_id"]},
            },
            {
                "name": "get_context_since",
                "description": "Get role-filtered context deltas since cursor.",
                "input": {"type": "object", "required": ["incident_id", "agent_id", "cursor"]},
            },
            {
                "name": "list_action_classes",
                "description": "List available action classes for agent role/allow-list.",
                "input": {"type": "object", "required": []},
            },
            {
                "name": "get_action_schema",
                "description": "Get payload schema for an action class.",
                "input": {"type": "object", "required": ["action_class"]},
            },
            {
                "name": "get_audit_ref",
                "description": "Get audit entry by action_id.",
                "input": {"type": "object", "required": ["action_id"]},
            },
            {
                "name": "swap_policy",
                "description": "Hot-swap policy (experimental).",
                "input": {"type": "object", "required": ["policy_file"]},
            },
        ]

    def _call_tool(self, tool_name: str, arguments: JSONObject) -> tuple[int, JSONObject]:
        path = f"/mcp/{tool_name}"
        return self._route_post(path, arguments, correlation_id=self._correlation_id())

    def _send_json(self, payload: JSONObject, status: int = 200, correlation_id: str | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if correlation_id:
            self.send_header("X-Correlation-Id", correlation_id)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> JSONObject:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _route_post(self, path: str, body: JSONObject, correlation_id: str) -> tuple[int, JSONObject]:
        svc = self.server.service
        token = self._bearer_token()
        routes: dict[str, Callable[[JSONObject], JSONObject]] = {
            "/mcp/rpc": lambda payload: self._rpc_envelope(payload),
            "/mcp/tools/call": lambda payload: self._tools_call_envelope(payload),
            "/mcp/propose_action": lambda payload: svc.propose_action(
                payload,
                agent_token=token,
                correlation_id=correlation_id,
            ),
            "/mcp/get_context_snapshot": lambda payload: svc.get_context_snapshot(
                incident_id=str(payload.get("incident_id", "")),
                agent_id=str(payload.get("agent_id", "")),
                agent_secret=payload.get("agent_secret"),
                agent_token=token,
                correlation_id=correlation_id,
            ),
            "/mcp/get_context_since": lambda payload: svc.get_context_since(
                incident_id=str(payload.get("incident_id", "")),
                agent_id=str(payload.get("agent_id", "")),
                cursor=int(payload.get("cursor", 0)),
                agent_secret=payload.get("agent_secret"),
                agent_token=token,
                correlation_id=correlation_id,
            ),
            "/mcp/get_action_schema": lambda payload: svc.get_action_schema(str(payload.get("action_class", ""))),
            "/mcp/get_audit_ref": lambda payload: svc.get_audit_ref(str(payload.get("action_id", ""))),
            "/mcp/swap_policy": lambda payload: svc.swap_policy(str(payload.get("policy_file", ""))),
            "/mcp/admin/seed_context": lambda payload: svc.seed_incident_context(
                incident_id=str(payload.get("incident_id", "")),
                transcript=payload.get("transcript", []),
                cad_view=payload.get("cad_view", {}),
                location=payload.get("location", {}),
                sop_refs=payload.get("sop_refs", []),
                dsa_session_profile_id=payload.get("dsa_session_profile_id"),
                dsa_scenario_profile_id=payload.get("dsa_scenario_profile_id"),
                dsa_session_strategy=payload.get("dsa_session_strategy"),
                dsa_scenario_strategy=payload.get("dsa_scenario_strategy"),
            ),
        }
        handler = routes.get(path)
        if handler is None:
            return HTTPStatus.NOT_FOUND, {"error": f"unknown_route:{path}"}
        try:
            return HTTPStatus.OK, handler(body)
        except AuthError as exc:
            return HTTPStatus.UNAUTHORIZED, {"error": str(exc)}
        except ForbiddenError as exc:
            return HTTPStatus.FORBIDDEN, {"error": str(exc)}
        except RateLimitedError as exc:
            return HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)}
        except ConflictError as exc:
            return HTTPStatus.CONFLICT, {"error": str(exc)}
        except VersionCompatibilityError as exc:
            return HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)}
        except Exception as exc:  # pragma: no cover
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}

    def _route_get(self, path: str, query: dict[str, list[str]], correlation_id: str) -> tuple[int, JSONObject] | None:
        svc = self.server.service
        token = self._bearer_token()
        if path == "/mcp/list_action_classes":
            return HTTPStatus.OK, svc.list_action_classes(
                agent_id=query.get("agent_id", [None])[0],
                agent_secret=query.get("agent_secret", [None])[0],
                agent_token=token,
            )
        if path == "/mcp/list_dsa_profiles":
            include_disabled_raw = str(query.get("include_disabled", ["false"])[0] or "false").lower()
            include_disabled = include_disabled_raw in {"1", "true", "yes"}
            return HTTPStatus.OK, svc.list_dsa_profiles(
                action_class=query.get("action_class", [None])[0],
                requested_profile_id=query.get("requested_profile_id", [None])[0],
                include_disabled=include_disabled,
            )
        if path == "/mcp/admin/metrics":
            return HTTPStatus.OK, svc.get_metrics_snapshot()
        if path == "/mcp/admin/events":
            cursor = int(query.get("cursor", ["0"])[0])
            verbosity = str(query.get("verbosity", ["normal"])[0] or "normal")
            return HTTPStatus.OK, svc.get_events_since(cursor=cursor, verbosity=verbosity)
        if path == "/mcp/admin/verify_audit_chain":
            return HTTPStatus.OK, svc.verify_audit_chain()
        if path == "/mcp/admin/version_matrix":
            return HTTPStatus.OK, svc.get_version_matrix()
        if path == "/mcp/capabilities":
            return HTTPStatus.OK, self._capabilities_envelope()
        if path == "/mcp/descriptor":
            return HTTPStatus.OK, self._descriptor_envelope()
        if path == "/mcp/tools/list":
            return HTTPStatus.OK, {"tools": self._tool_specs()}
        if path == "/mcp/get_action_schema":
            return HTTPStatus.OK, svc.get_action_schema(query.get("action_class", [""])[0])
        if path == "/mcp/get_audit_ref":
            return HTTPStatus.OK, svc.get_audit_ref(query.get("action_id", [""])[0])
        if path == "/mcp/get_context_snapshot":
            return HTTPStatus.OK, svc.get_context_snapshot(
                incident_id=query.get("incident_id", [""])[0],
                agent_id=query.get("agent_id", [""])[0],
                agent_secret=query.get("agent_secret", [None])[0],
                agent_token=token,
                correlation_id=correlation_id,
            )
        if path == "/mcp/get_context_since":
            cursor = int(query.get("cursor", ["0"])[0])
            return HTTPStatus.OK, svc.get_context_since(
                incident_id=query.get("incident_id", [""])[0],
                agent_id=query.get("agent_id", [""])[0],
                cursor=cursor,
                agent_secret=query.get("agent_secret", [None])[0],
                agent_token=token,
                correlation_id=correlation_id,
            )
        return None

    def do_POST(self) -> None:  # noqa: N802
        started = time.perf_counter()
        correlation_id = self._correlation_id()
        body = self._read_json_body()
        status, payload = self._route_post(self.path, body, correlation_id=correlation_id)
        self.server.service.observability.event(
            "http_request",
            method="POST",
            path=self.path,
            status=int(status),
            latency_ms=int((time.perf_counter() - started) * 1000),
            correlation_id=correlation_id,
        )
        self.server.service.observability.incr(f"http.status.{int(status)}")
        self.server.service.observability.observe_latency_ms("http_request", (time.perf_counter() - started) * 1000.0)
        self._send_json(payload, status=status, correlation_id=correlation_id)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/mcp/admin/ui":
            self._send_html(self._admin_ui_html())
            return
        if parsed.path == "/mcp/admin/events/stream":
            self._handle_admin_event_stream(parsed.query)
            return
        started = time.perf_counter()
        correlation_id = self._correlation_id()
        if parsed.path == "/mcp/subscribe_context":
            self._handle_subscribe_context(parsed.query)
            return
        try:
            routed = self._route_get(parsed.path, urllib.parse.parse_qs(parsed.query), correlation_id=correlation_id)
        except AuthError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED, correlation_id=correlation_id)
            return
        except ForbiddenError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN, correlation_id=correlation_id)
            return
        except RateLimitedError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.TOO_MANY_REQUESTS, correlation_id=correlation_id)
            return
        if routed is None:
            self._send_json({"error": f"unknown_route:{parsed.path}"}, status=HTTPStatus.NOT_FOUND, correlation_id=correlation_id)
            return
        status, payload = routed
        self.server.service.observability.event(
            "http_request",
            method="GET",
            path=parsed.path,
            status=int(status),
            latency_ms=int((time.perf_counter() - started) * 1000),
            correlation_id=correlation_id,
        )
        self.server.service.observability.incr(f"http.status.{int(status)}")
        self.server.service.observability.observe_latency_ms("http_request", (time.perf_counter() - started) * 1000.0)
        self._send_json(payload, status=status, correlation_id=correlation_id)

    def _handle_subscribe_context(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        incident_id = params.get("incident_id", [""])[0]
        agent_id = params.get("agent_id", [""])[0]
        cursor = int(params.get("cursor", ["0"])[0])
        duration_sec = int(params.get("duration_sec", ["30"])[0])

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        deadline = time.time() + max(1, duration_sec)
        current = cursor
        while time.time() < deadline:
            payload = self.server.service.get_context_since(
                incident_id=incident_id,
                agent_id=agent_id,
                cursor=current,
                agent_secret=params.get("agent_secret", [None])[0],
                agent_token=self._bearer_token(),
                correlation_id=self._correlation_id(),
            )
            deltas = payload.get("deltas", [])
            if deltas:
                current = int(payload.get("new_cursor", current))
                self.wfile.write(f"event: context_delta\ndata: {json.dumps(payload)}\n\n".encode("utf-8"))
                self.wfile.flush()
            else:
                self.wfile.write(b"event: keepalive\ndata: {}\n\n")
                self.wfile.flush()
            time.sleep(1.0)

    def _handle_admin_event_stream(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        cursor = int(params.get("cursor", ["0"])[0])
        verbosity = str(params.get("verbosity", ["normal"])[0] or "normal")
        duration_sec = int(params.get("duration_sec", ["120"])[0])
        poll_interval = float(params.get("poll_interval_sec", ["1.0"])[0])

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        deadline = time.time() + max(1, duration_sec)
        current = cursor
        while time.time() < deadline:
            payload = self.server.service.get_events_since(cursor=current, verbosity=verbosity)
            events = payload.get("events", [])
            if events:
                current = int(payload.get("new_cursor", current))
                self.wfile.write(f"event: governance_event\ndata: {json.dumps(payload)}\n\n".encode("utf-8"))
            else:
                self.wfile.write(b"event: keepalive\ndata: {}\n\n")
            self.wfile.flush()
            time.sleep(max(0.1, poll_interval))

    @staticmethod
    def _admin_ui_html() -> str:
        return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ACAF Research Server Console</title>
  <style>
    body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0; background: #0b1020; color: #dce3ff; }
    header { padding: 12px 16px; background: #111a34; border-bottom: 1px solid #22305f; display: flex; gap: 12px; align-items: center; }
    main { padding: 12px 16px; }
    select, input, button { background: #101935; color: #dce3ff; border: 1px solid #2a3d77; padding: 6px 8px; }
    .row { display: grid; grid-template-columns: 180px 1fr; gap: 8px; margin-bottom: 8px; }
    #events { white-space: pre-wrap; background: #081024; border: 1px solid #21315c; padding: 10px; height: 62vh; overflow: auto; }
    .muted { color: #8fa2d9; font-size: 12px; }
  </style>
</head>
<body>
  <header>
    <strong>ACAF Research Server Console</strong>
    <span class="muted">Live governance activity</span>
  </header>
  <main>
    <div class="row">
      <label>Verbosity</label>
      <select id="verbosity">
        <option value="normal" selected>normal</option>
        <option value="debug">debug</option>
      </select>
    </div>
    <div class="row">
      <label>Duration (sec)</label>
      <input id="duration" type="number" value="120" min="5" />
    </div>
    <div class="row">
      <label></label>
      <div>
        <button id="connect">Connect Stream</button>
        <button id="clear">Clear</button>
      </div>
    </div>
    <div class="muted">Shows setup, client activity, and governance events from server observability.</div>
    <div id="events"></div>
  </main>
  <script>
    let cursor = 0;
    let src = null;
    const eventsEl = document.getElementById('events');
    function log(obj) {
      eventsEl.textContent += JSON.stringify(obj, null, 2) + "\\n\\n";
      eventsEl.scrollTop = eventsEl.scrollHeight;
    }
    function connect() {
      if (src) src.close();
      const verbosity = document.getElementById('verbosity').value;
      const duration = Number(document.getElementById('duration').value || 120);
      src = new EventSource(`/mcp/admin/events/stream?cursor=${cursor}&verbosity=${encodeURIComponent(verbosity)}&duration_sec=${duration}`);
      src.addEventListener('governance_event', (ev) => {
        const data = JSON.parse(ev.data);
        cursor = data.new_cursor || cursor;
        (data.events || []).forEach(log);
      });
      src.addEventListener('keepalive', () => {});
      src.onerror = () => { if (src) src.close(); };
    }
    document.getElementById('connect').onclick = connect;
    document.getElementById('clear').onclick = () => { eventsEl.textContent = ''; };
  </script>
</body>
</html>"""

    def _capabilities_envelope(self) -> JSONObject:
        return {
            "protocol": "mcp-http-sse",
            "version": "2025-03-26",
            "transport": {"http": True, "sse": True},
            "rpc": {"endpoint": "/mcp/rpc", "methods": ["capabilities", "tools/list", "tools/call"]},
            "tools": {"list_endpoint": "/mcp/tools/list", "call_endpoint": "/mcp/tools/call"},
            "descriptor": {"endpoint": "/mcp/descriptor"},
            "context": {
                "snapshot_endpoint": "/mcp/get_context_snapshot",
                "since_endpoint": "/mcp/get_context_since",
                "subscribe_endpoint": "/mcp/subscribe_context",
            },
            "observability": {
                "metrics_endpoint": "/mcp/admin/metrics",
                "events_endpoint": "/mcp/admin/events",
                "event_stream_endpoint": "/mcp/admin/events/stream",
                "ui_endpoint": "/mcp/admin/ui",
                "correlation_header": "X-Correlation-Id",
            },
        }

    def _descriptor_envelope(self) -> JSONObject:
        raw_mode = getattr(self.server.service.auth, "mode", "dev_secret")
        auth_mode = "agent_secret" if raw_mode == "dev_secret" else raw_mode
        auth_fields = ["agent_id", "agent_secret"] if raw_mode != "jwt_hs256" else ["agent_id", "Authorization: Bearer <JWT>"]
        return {
            "name": "911bench-governance-mcp",
            "version": "0.1.0",
            "description": "Governance enforcement MCP server for 911Bench WP1.",
            "transport": {
                "base_path": "/mcp",
                "http_endpoints": {
                    "capabilities": "/mcp/capabilities",
                    "descriptor": "/mcp/descriptor",
                    "tools_list": "/mcp/tools/list",
                    "tools_call": "/mcp/tools/call",
                    "rpc": "/mcp/rpc",
                    "subscribe_context": "/mcp/subscribe_context",
                    "list_dsa_profiles": "/mcp/list_dsa_profiles",
                    "admin_metrics": "/mcp/admin/metrics",
                    "admin_events": "/mcp/admin/events",
                    "admin_event_stream": "/mcp/admin/events/stream",
                    "admin_ui": "/mcp/admin/ui",
                },
            },
            "auth": {
                "mode": auth_mode,
                "location": "payload + Authorization header",
                "fields": auth_fields,
            },
            "tooling": {
                "tools": self._tool_specs(),
                "examples": {
                    "tools/call:get_context_snapshot": {
                        "tool": "get_context_snapshot",
                        "arguments": {
                            "incident_id": "inc-123",
                            "agent_id": "911buddy",
                            "agent_secret": "dev-911buddy-secret",
                        },
                    },
                    "rpc:tools/call:propose_action": {
                        "id": "req-1",
                        "method": "tools/call",
                        "params": {
                            "tool": "propose_action",
                            "arguments": {
                                "action_id": "act-1",
                                "incident_id": "inc-123",
                                "action_class": "cad_update.address",
                                "proposed_payload": {"location": "123 Main St", "city": "Vancouver"},
                                "evidence_refs": [
                                    {
                                        "type": "transcript_span",
                                        "category": "human_communication",
                                        "source": "turn:1",
                                        "content": "Caller gave 123 Main St",
                                        "confidence": 0.95,
                                    }
                                ],
                                "uncertainty": {"p_correct": 0.95, "conflict": False},
                                "read_set": {"record_version": 0, "field_versions": {"location": 0, "city": 0}},
                                "proposer": {
                                    "agent_id": "911buddy",
                                    "agent_secret": "dev-911buddy-secret",
                                    "agent_role": "dsa",
                                    "autonomy_level": "A3",
                                },
                            },
                        },
                    },
                },
            },
        }

    def _tools_call_envelope(self, payload: JSONObject) -> JSONObject:
        name = str(payload.get("tool", "")).strip()
        arguments = payload.get("arguments", {})
        if not name:
            return {"error": "missing_tool_name"}
        if not isinstance(arguments, dict):
            return {"error": "invalid_tool_arguments"}
        status, result = self._call_tool(name, arguments)
        if status != HTTPStatus.OK:
            return {"error": result.get("error", "tool_call_failed"), "status": int(status)}
        return {"tool": name, "result": result}

    def _rpc_envelope(self, payload: JSONObject) -> JSONObject:
        rpc_id = payload.get("id")
        method = str(payload.get("method", "")).strip()
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return {"id": rpc_id, "error": {"code": -32602, "message": "invalid params"}}

        try:
            if method == "capabilities":
                result = self._capabilities_envelope()
                return {"id": rpc_id, "result": result}
            if method == "tools/list":
                result = {"tools": self._tool_specs()}
                return {"id": rpc_id, "result": result}
            if method == "tools/call":
                tool_name = str(params.get("tool", "")).strip()
                arguments = params.get("arguments", {})
                if not tool_name:
                    return {"id": rpc_id, "error": {"code": -32602, "message": "missing tool name"}}
                if not isinstance(arguments, dict):
                    return {"id": rpc_id, "error": {"code": -32602, "message": "invalid tool arguments"}}
                status, result = self._call_tool(tool_name, arguments)
                if status != HTTPStatus.OK:
                    return {
                        "id": rpc_id,
                        "error": {
                            "code": self._http_status_to_rpc_code(status),
                            "message": result.get("error", "tool call failed"),
                            "data": {"status": int(status)},
                        },
                    }
                return {"id": rpc_id, "result": {"tool": tool_name, "result": result}}
            return {"id": rpc_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as exc:  # pragma: no cover
            return {"id": rpc_id, "error": {"code": -32000, "message": str(exc)}}

    @staticmethod
    def _http_status_to_rpc_code(status: int) -> int:
        if status == HTTPStatus.UNAUTHORIZED:
            return -32001
        if status == HTTPStatus.FORBIDDEN:
            return -32003
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            return -32029
        if status == HTTPStatus.CONFLICT:
            return -32009
        if status == HTTPStatus.UNPROCESSABLE_ENTITY:
            return -32022
        if status == HTTPStatus.NOT_FOUND:
            return -32601
        return -32000


class MCPHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], service: GovernanceService):
        super().__init__(server_address, MCPHandler)
        self.service = service


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run 911Bench Governance MCP server (WP1)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--policy-file", default="policies/test_full_ecc.yaml")
    parser.add_argument("--registry-file", default="registries/test_registry.yaml")
    parser.add_argument("--evidence-config-file", default="policies/domain_evidence_config.yaml")
    parser.add_argument("--auth-config-file", default="policies/agent_auth_config.json")
    parser.add_argument("--dsa-config-file", default="policies/dsa_profiles.yaml")
    parser.add_argument("--proposals-per-sec", type=int, default=10)
    parser.add_argument("--checkpoint-queue-cap", type=int, default=20)
    parser.add_argument("--escalation-queue-cap", type=int, default=5)
    parser.add_argument("--sim-base-url", default=None)
    parser.add_argument("--southbound-timeout-sec", type=float, default=10.0)
    parser.add_argument("--checkpoint-poll-interval-sec", type=float, default=0.25)
    parser.add_argument("--southbound-require-mtls", action="store_true")
    parser.add_argument("--southbound-ca-file", default=None)
    parser.add_argument("--southbound-client-cert-file", default=None)
    parser.add_argument("--southbound-client-key-file", default=None)
    parser.add_argument("--southbound-retry-attempts", type=int, default=2)
    parser.add_argument("--southbound-retry-backoff-sec", type=float, default=0.1)
    parser.add_argument("--southbound-circuit-fail-threshold", type=int, default=3)
    parser.add_argument("--southbound-circuit-open-sec", type=float, default=5.0)
    parser.add_argument("--state-db-file", default=None)
    return parser


def run_server(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    config = GovernanceConfig(
        policy_file=args.policy_file,
        registry_file=args.registry_file,
        evidence_config_file=args.evidence_config_file,
        auth_config_file=args.auth_config_file,
        dsa_config_file=args.dsa_config_file,
        proposals_per_sec=args.proposals_per_sec,
        checkpoint_queue_cap=args.checkpoint_queue_cap,
        escalation_queue_cap=args.escalation_queue_cap,
        sim_base_url=args.sim_base_url,
        southbound_timeout_sec=args.southbound_timeout_sec,
        checkpoint_poll_interval_sec=args.checkpoint_poll_interval_sec,
        southbound_require_mtls=args.southbound_require_mtls,
        southbound_ca_file=args.southbound_ca_file,
        southbound_client_cert_file=args.southbound_client_cert_file,
        southbound_client_key_file=args.southbound_client_key_file,
        southbound_retry_attempts=args.southbound_retry_attempts,
        southbound_retry_backoff_sec=args.southbound_retry_backoff_sec,
        southbound_circuit_fail_threshold=args.southbound_circuit_fail_threshold,
        southbound_circuit_open_sec=args.southbound_circuit_open_sec,
        state_db_file=args.state_db_file,
    )
    service = GovernanceService(root_dir=args.root, config=config)
    server = MCPHTTPServer((args.host, args.port), service=service)
    print(f"governance-mcp-server listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        service.close()
    return 0
