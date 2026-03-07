"""WP1 integration tests for governance MCP HTTP/SSE surface."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

from gov_server.dsa import DSAProfile, DSARegistry
from gov_server.mcp_server import MCPHTTPServer
from gov_server.service import GovernanceConfig, GovernanceService


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _mint_jwt_hs256(secret: str, claims: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    head = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{head}.{body}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{head}.{body}.{_b64url(sig)}"


class MCPServerIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = GovernanceConfig(
            policy_file="policies/test_full_ecc.yaml",
            registry_file="registries/test_registry.yaml",
            evidence_config_file="policies/domain_evidence_config.yaml",
            auth_config_file="policies/agent_auth_config.json",
            proposals_per_sec=100,
        )
        cls.service = GovernanceService(root_dir=".", config=config)
        cls.server = MCPHTTPServer(("127.0.0.1", 0), service=cls.service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.service.close()

    def _post(self, path: str, payload: dict) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str, query: dict[str, str] | None = None) -> dict:
        q = f"?{urllib.parse.urlencode(query or {})}" if query else ""
        url = f"http://127.0.0.1:{self.port}{path}{q}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get_with_headers(
        self,
        path: str,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict, dict]:
        q = f"?{urllib.parse.urlencode(query or {})}" if query else ""
        url = f"http://127.0.0.1:{self.port}{path}{q}"
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")), dict(resp.headers.items())

    def _get_text(self, path: str) -> str:
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")

    def _post_error(self, path: str, payload: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
        except HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            return exc.code, body
        raise AssertionError("expected HTTPError")

    def test_tool_surface_roundtrip(self) -> None:
        seed = self._post(
            "/mcp/admin/seed_context",
            {
                "incident_id": "inc-mcp-1",
                "transcript": [{"turn": 1, "text": "Caller: 123 Main St"}],
                "cad_view": {"location": "Unknown"},
                "location": {"ani_ali": "123 Main St"},
                "sop_refs": ["fire-res-v2"],
            },
        )
        self.assertEqual(seed["incident_id"], "inc-mcp-1")
        self.assertEqual(seed["cursor"], 1)

        classes = self._get("/mcp/list_action_classes")
        self.assertTrue(classes["classes"])

        snapshot = self._post(
            "/mcp/get_context_snapshot",
            {
                "incident_id": "inc-mcp-1",
                "agent_id": "911buddy",
                "agent_secret": "dev-911buddy-secret",
            },
        )
        self.assertEqual(snapshot["incident_id"], "inc-mcp-1")
        self.assertEqual(len(snapshot["transcript"]), 1)

        proposal = {
            "action_id": "mcp-act-001",
            "incident_id": "inc-mcp-1",
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
                "autonomy_level": "A2",
            },
        }
        outcome = self._post("/mcp/propose_action", proposal)
        self.assertEqual(outcome["action_id"], "mcp-act-001")
        self.assertIn(outcome["decision"], {"executed", "denied", "needs_retry_conflict"})
        self.assertTrue(outcome["audit_ref"])
        self.assertIn("dsa", outcome)
        self.assertEqual(outcome["dsa"]["profile_id"], "deterministic_911buddy_v1")
        self.assertEqual(outcome["dsa"]["chosen_payload_source"], "client_proposal")

        audit = self._get("/mcp/get_audit_ref", {"action_id": "mcp-act-001"})
        self.assertEqual(audit["action_id"], "mcp-act-001")
        self.assertIn("audit_entry", audit)
        self.assertEqual(
            audit["audit_entry"]["proposal"]["proposer"]["dsa_profile_id"],
            "deterministic_911buddy_v1",
        )

        delta = self._post(
            "/mcp/get_context_since",
            {
                "incident_id": "inc-mcp-1",
                "agent_id": "911buddy",
                "agent_secret": "dev-911buddy-secret",
                "cursor": 0,
            },
        )
        self.assertIn("new_cursor", delta)

    def test_action_schema_endpoint(self) -> None:
        schema = self._get("/mcp/get_action_schema", {"action_class": "cad_update.address"})
        self.assertEqual(schema["action_class"], "cad_update.address")
        self.assertIn("payload_schema", schema)

    def test_mcp_capabilities_and_tools_call_envelope(self) -> None:
        caps = self._get("/mcp/capabilities")
        self.assertEqual(caps["protocol"], "mcp-http-sse")
        self.assertTrue(caps["transport"]["http"])
        self.assertEqual(caps["descriptor"]["endpoint"], "/mcp/descriptor")

        descriptor = self._get("/mcp/descriptor")
        self.assertEqual(descriptor["name"], "911bench-governance-mcp")
        self.assertEqual(descriptor["auth"]["mode"], "agent_secret")
        self.assertIn("tools", descriptor["tooling"])
        desc_names = {item["name"] for item in descriptor["tooling"]["tools"]}
        self.assertIn("propose_action", desc_names)

        tools = self._get("/mcp/tools/list")
        names = {item["name"] for item in tools["tools"]}
        self.assertIn("propose_action", names)
        self.assertIn("get_context_snapshot", names)

        _ = self._post(
            "/mcp/admin/seed_context",
            {
                "incident_id": "inc-mcp-envelope",
                "transcript": [{"turn": 1, "text": "Caller: 55 Test St"}],
                "cad_view": {"location": "Unknown"},
                "location": {"ani_ali": "55 Test St"},
                "sop_refs": ["fire-res-v2"],
            },
        )

        envelope = self._post(
            "/mcp/tools/call",
            {
                "tool": "get_context_snapshot",
                "arguments": {
                    "incident_id": "inc-mcp-envelope",
                    "agent_id": "911buddy",
                    "agent_secret": "dev-911buddy-secret",
                },
            },
        )
        self.assertEqual(envelope["tool"], "get_context_snapshot")
        self.assertIn("result", envelope)
        self.assertEqual(envelope["result"]["incident_id"], "inc-mcp-envelope")

    def test_list_dsa_profiles(self) -> None:
        profiles = self._get("/mcp/list_dsa_profiles")
        self.assertEqual(profiles["default_profile_id"], "deterministic_911buddy_v1")
        ids = {item["id"] for item in profiles["profiles"]}
        self.assertIn("deterministic_911buddy_v1", ids)

        selected = self._get(
            "/mcp/list_dsa_profiles",
            {
                "action_class": "cad_update.address",
                "requested_profile_id": "openai_911buddy_v1",
            },
        )
        self.assertEqual(selected["selected_profile_id"], "deterministic_911buddy_v1")
        self.assertIn("deterministic_911buddy_v1", selected["allowed_profile_ids"])
        self.assertEqual(selected["strategy"], "fallback_chain")

    def test_dsa_can_apply_suggested_payload(self) -> None:
        _ = self._post(
            "/mcp/admin/seed_context",
            {
                "incident_id": "inc-mcp-dsa-apply",
                "transcript": [{"turn": 1, "text": "Caller: There is a fire at 500 Smoke Ave"}],
                "cad_view": {"location": "Unknown"},
                "location": {"ani_ali": "500 Smoke Ave"},
                "sop_refs": ["fire-res-v2"],
            },
        )
        proposal = {
            "action_id": "mcp-dsa-apply-1",
            "incident_id": "inc-mcp-dsa-apply",
            "action_class": "cad_update.address",
            "proposed_payload": {"location": "", "city": "Vancouver"},
            "evidence_refs": [
                {
                    "type": "transcript_span",
                    "category": "human_communication",
                    "source": "turn:1",
                    "content": "There is a fire at 500 Smoke Ave",
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
            "dsa": {
                "profile_id": "deterministic_911buddy_v1",
                "apply_suggested_payload": True,
            },
        }
        outcome = self._post("/mcp/propose_action", proposal)
        self.assertEqual(outcome["decision"], "executed")
        self.assertEqual(outcome["dsa"]["chosen_payload_source"], "dsa_suggestion")
        self.assertTrue(str(outcome["dsa"]["chosen_payload"].get("location", "")))

    def test_dsa_fallback_chain_uses_next_profile_on_error(self) -> None:
        original = self.service.dsa_registry
        try:
            self.service.dsa_registry = DSARegistry(
                default_profile_id="broken_profile",
                profiles=(
                    DSAProfile(
                        id="broken_profile",
                        provider="broken",
                        model="n/a",
                        mode="model",
                        enabled=True,
                        description="always fails",
                        action_classes=(),
                        runtime={},
                    ),
                    DSAProfile(
                        id="deterministic_911buddy_v1",
                        provider="builtin",
                        model="rule-based",
                        mode="deterministic",
                        enabled=True,
                        description="deterministic fallback",
                        action_classes=(),
                        runtime={},
                    ),
                ),
                routing_by_action_class={
                    "cad_update.address": {
                        "strategy": "fallback_chain",
                        "profiles": ["broken_profile", "deterministic_911buddy_v1"],
                    }
                },
            )
            _ = self._post(
                "/mcp/admin/seed_context",
                {
                    "incident_id": "inc-mcp-dsa-fallback",
                    "transcript": [{"turn": 1, "text": "Caller: 700 Fallback St"}],
                    "cad_view": {"location": "Unknown"},
                    "location": {"ani_ali": "700 Fallback St"},
                    "sop_refs": ["fire-res-v2"],
                },
            )
            proposal = {
                "action_id": "mcp-dsa-fallback-1",
                "incident_id": "inc-mcp-dsa-fallback",
                "action_class": "cad_update.address",
                "proposed_payload": {"location": "700 Fallback St", "city": "Vancouver"},
                "evidence_refs": [
                    {
                        "type": "transcript_span",
                        "category": "human_communication",
                        "source": "turn:1",
                        "content": "700 Fallback St",
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
                "dsa": {"apply_suggested_payload": False},
            }
            outcome = self._post("/mcp/propose_action", proposal)
            self.assertEqual(outcome["decision"], "executed")
            self.assertEqual(outcome["dsa"]["selected_profile_id"], "deterministic_911buddy_v1")
            self.assertEqual(outcome["dsa"]["attempts"][0]["status"], "error")
        finally:
            self.service.dsa_registry = original

    def test_mcp_rpc_envelope(self) -> None:
        rpc_caps = self._post(
            "/mcp/rpc",
            {
                "id": "rpc-1",
                "method": "capabilities",
                "params": {},
            },
        )
        self.assertEqual(rpc_caps["id"], "rpc-1")
        self.assertIn("result", rpc_caps)
        self.assertEqual(rpc_caps["result"]["rpc"]["endpoint"], "/mcp/rpc")

        rpc_tools = self._post(
            "/mcp/rpc",
            {
                "id": "rpc-2",
                "method": "tools/list",
                "params": {},
            },
        )
        self.assertEqual(rpc_tools["id"], "rpc-2")
        names = {item["name"] for item in rpc_tools["result"]["tools"]}
        self.assertIn("propose_action", names)

        _ = self._post(
            "/mcp/admin/seed_context",
            {
                "incident_id": "inc-mcp-rpc",
                "transcript": [{"turn": 1, "text": "Caller: 77 RPC St"}],
                "cad_view": {"location": "Unknown"},
                "location": {"ani_ali": "77 RPC St"},
                "sop_refs": ["fire-res-v2"],
            },
        )

        rpc_call = self._post(
            "/mcp/rpc",
            {
                "id": "rpc-3",
                "method": "tools/call",
                "params": {
                    "tool": "get_context_snapshot",
                    "arguments": {
                        "incident_id": "inc-mcp-rpc",
                        "agent_id": "911buddy",
                        "agent_secret": "dev-911buddy-secret",
                    },
                },
            },
        )
        self.assertEqual(rpc_call["id"], "rpc-3")
        self.assertIn("result", rpc_call)
        self.assertEqual(rpc_call["result"]["tool"], "get_context_snapshot")
        self.assertEqual(rpc_call["result"]["result"]["incident_id"], "inc-mcp-rpc")

        rpc_bad = self._post(
            "/mcp/rpc",
            {
                "id": "rpc-4",
                "method": "tools/call",
                "params": {
                    "tool": "propose_action",
                    "arguments": {
                        "action_id": "rpc-authz-1",
                        "incident_id": "inc-mcp-rpc",
                        "action_class": "cad_update.address",
                        "proposed_payload": {"location": "77 RPC St", "city": "Vancouver"},
                        "evidence_refs": [
                            {
                                "type": "transcript_span",
                                "category": "human_communication",
                                "source": "turn:1",
                                "content": "77 RPC St",
                                "confidence": 0.95,
                            }
                        ],
                        "uncertainty": {"p_correct": 0.95, "conflict": False},
                        "read_set": {"record_version": 0, "field_versions": {"location": 0, "city": 0}},
                        "proposer": {
                            "agent_id": "translator",
                            "agent_secret": "dev-translator-secret",
                            "agent_role": "translation",
                            "autonomy_level": "A4",
                        },
                    },
                },
            },
        )
        self.assertEqual(rpc_bad["id"], "rpc-4")
        self.assertIn("error", rpc_bad)
        self.assertEqual(rpc_bad["error"]["code"], -32003)

    def test_authz_and_rate_limit(self) -> None:
        proposal = {
            "action_id": "mcp-act-authz",
            "incident_id": "inc-mcp-authz",
            "action_class": "cad_update.address",
            "proposed_payload": {"location": "10 Test Ave", "city": "Vancouver"},
            "evidence_refs": [
                {
                    "type": "transcript_span",
                    "category": "human_communication",
                    "source": "turn:1",
                    "content": "10 Test Ave",
                    "confidence": 0.95,
                }
            ],
            "uncertainty": {"p_correct": 0.95, "conflict": False},
            "read_set": {"record_version": 0, "field_versions": {"location": 0, "city": 0}},
            "proposer": {
                "agent_id": "translator",
                "agent_secret": "dev-translator-secret",
                "agent_role": "translation",
                "autonomy_level": "A4",
            },
        }
        code, body = self._post_error("/mcp/propose_action", proposal)
        self.assertEqual(code, 403)
        self.assertEqual(body.get("error"), "action_class_forbidden")

        proposal["proposer"]["agent_secret"] = "wrong-secret"
        code, body = self._post_error("/mcp/propose_action", proposal)
        self.assertEqual(code, 401)
        self.assertEqual(body.get("error"), "invalid_agent_secret")

        # Rate limit: temporarily set limit to 10, then 11 quick requests should trigger 429.
        self.service.rate_limiter.proposals_per_sec = 10
        time.sleep(1.1)
        base = {
            "action_id": "mcp-rate",
            "incident_id": "inc-mcp-rate",
            "action_class": "cad_update.address",
            "proposed_payload": {"location": "11 Test Ave", "city": "Vancouver"},
            "evidence_refs": [
                {
                    "type": "transcript_span",
                    "category": "human_communication",
                    "source": "turn:1",
                    "content": "11 Test Ave",
                    "confidence": 0.95,
                }
            ],
            "uncertainty": {"p_correct": 0.95, "conflict": False},
            "read_set": {"record_version": 0, "field_versions": {"location": 0, "city": 0}},
            "proposer": {
                "agent_id": "911buddy",
                "agent_secret": "dev-911buddy-secret",
                "agent_role": "dsa",
                "autonomy_level": "A2",
            },
        }
        got_rate_limited = False
        try:
            for i in range(11):
                req = json.loads(json.dumps(base))
                req["action_id"] = f"mcp-rate-{i}"
                if i < 10:
                    _ = self._post("/mcp/propose_action", req)
                else:
                    code, body = self._post_error("/mcp/propose_action", req)
                    got_rate_limited = (code == 429 and body.get("error") == "rate_limited")
            self.assertTrue(got_rate_limited)
        finally:
            self.service.rate_limiter.proposals_per_sec = 100

    def test_context_redaction_by_role(self) -> None:
        _ = self._post(
            "/mcp/admin/seed_context",
            {
                "incident_id": "inc-mcp-redact",
                "transcript": [{"turn": 1, "text": "Caller phone is +1-604-555-0000"}],
                "cad_view": {"caller_phone_number": "+1-604-555-0000", "location": "10 Test Ave"},
                "location": {"ani_ali": "10 Test Ave"},
                "sop_refs": ["fire-res-v2"],
            },
        )
        redacted = self._post(
            "/mcp/get_context_snapshot",
            {
                "incident_id": "inc-mcp-redact",
                "agent_id": "translator",
                "agent_secret": "dev-translator-secret",
            },
        )
        self.assertEqual(redacted["cad_view"]["caller_phone_number"], "[REDACTED]")
        self.assertEqual(redacted["location"]["ani_ali"], "[REDACTED]")

    def test_correlation_header_and_metrics_endpoint(self) -> None:
        before = self._get("/mcp/admin/metrics")
        before_count = int(before.get("counts", {}).get("http.status.200", 0))

        _, headers = self._get_with_headers(
            "/mcp/capabilities",
            headers={"X-Correlation-Id": "corr-test-001"},
        )
        self.assertEqual(headers.get("X-Correlation-Id"), "corr-test-001")

        after = self._get("/mcp/admin/metrics")
        after_count = int(after.get("counts", {}).get("http.status.200", 0))
        self.assertGreaterEqual(after_count, before_count + 1)
        self.assertIn("http_request", after.get("latency_ms", {}))

    def test_verify_audit_chain_endpoint(self) -> None:
        payload = self._get("/mcp/admin/verify_audit_chain")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "state_store_not_configured")

    def test_version_matrix_endpoint_and_incompatible_policy_swap(self) -> None:
        matrix = self._get("/mcp/admin/version_matrix")
        self.assertEqual(matrix["mcp_protocol_version"], "2025-03-26")
        self.assertEqual(matrix["active_policy"]["policy_version"], "1.0")

        code, body = self._post_error(
            "/mcp/swap_policy",
            {"policy_file": "policies/test_incompatible_policy_version.yaml"},
        )
        self.assertEqual(code, 422)
        self.assertIn("incompatible_policy_version", body.get("error", ""))

    def test_admin_events_and_ui(self) -> None:
        normal = self._get("/mcp/admin/events", {"cursor": "0", "verbosity": "normal"})
        self.assertIn("events", normal)
        self.assertIn("new_cursor", normal)
        if normal["events"]:
            first = normal["events"][0]
            self.assertIn("event_type", first)
            self.assertIn("seq", first)

        debug = self._get("/mcp/admin/events", {"cursor": "0", "verbosity": "debug"})
        self.assertIn("events", debug)
        if debug["events"]:
            # Debug mode should expose full payload fields.
            self.assertIn("component", debug["events"][0])

        html = self._get_text("/mcp/admin/ui")
        self.assertIn("ACAF Research Server Console", html)
        self.assertIn("Verbosity", html)

    def test_propose_action_idempotency(self) -> None:
        _ = self._post(
            "/mcp/admin/seed_context",
            {
                "incident_id": "inc-mcp-idempotency",
                "transcript": [{"turn": 1, "text": "Caller: 88 Replay Rd"}],
                "cad_view": {"location": "Unknown"},
                "location": {"ani_ali": "88 Replay Rd"},
                "sop_refs": ["fire-res-v2"],
            },
        )
        proposal = {
            "action_id": "mcp-idem-001",
            "incident_id": "inc-mcp-idempotency",
            "action_class": "cad_update.address",
            "proposed_payload": {"location": "88 Replay Rd", "city": "Vancouver"},
            "evidence_refs": [
                {
                    "type": "transcript_span",
                    "category": "human_communication",
                    "source": "turn:1",
                    "content": "Caller gave 88 Replay Rd",
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
                "idempotency_key": "idem-abc-123",
            },
        }
        first = self._post("/mcp/propose_action", proposal)
        second = self._post("/mcp/propose_action", proposal)
        self.assertEqual(first.get("audit_ref"), second.get("audit_ref"))
        self.assertEqual(first.get("decision"), second.get("decision"))

        mutated = json.loads(json.dumps(proposal))
        mutated["proposed_payload"]["city"] = "Burnaby"
        code, body = self._post_error("/mcp/propose_action", mutated)
        self.assertEqual(code, 409)
        self.assertEqual(body.get("error"), "idempotency_key_payload_mismatch")


class _FakeSimulationHandler(BaseHTTPRequestHandler):
    server: "_FakeSimulationHTTPServer"

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_json()
        path = self.path
        state = self.server.state

        if path == "/plant/get_state_snapshot":
            self._send_json(
                {
                    "incident_id": body.get("incident_id"),
                    "cad_state": state["cad_state"],
                    "record_version": state["record_version"],
                    "field_versions": state["field_versions"],
                    "location": {"ani_ali": state["cad_state"].get("location", "unknown")},
                    "sop_refs": ["fire-res-v2"],
                    "transcript_cursor": len(state["transcript"]),
                }
            )
            return

        if path == "/plant/get_transcript_since":
            cursor = int(body.get("cursor", 0))
            turns = [item for item in state["transcript"] if int(item.get("turn", 0)) > cursor]
            self._send_json({"cursor": cursor, "turns": turns, "new_cursor": len(state["transcript"])})
            return

        if path == "/checkpoint/request":
            state["checkpoint_requests"] += 1
            request_id = f"cp-{state['checkpoint_requests']}"
            state["checkpoint_by_id"][request_id] = body
            self._send_json({"request_id": request_id})
            return

        if path == "/checkpoint/poll":
            state["checkpoint_polls"] += 1
            request_id = str(body.get("request_id", ""))
            if request_id not in state["checkpoint_by_id"]:
                self._send_json({"status": "pending"})
                return
            self._send_json(
                {
                    "status": "approved",
                    "response": {"latency_ms": 5, "rationale": "approved by fake simulation"},
                }
            )
            return

        if path == "/plant/apply_cad_patch":
            read_set = body.get("read_set", {})
            requested_record = int(read_set.get("record_version", 0))
            if requested_record < state["record_version"]:
                self._send_json(
                    {
                        "status": "conflict",
                        "conflict_detail": {
                            "stale_fields": ["record_version"],
                            "current_versions": {"record_version": state["record_version"]},
                        },
                    }
                )
                return
            payload = body.get("payload", {})
            for field, value in payload.items():
                state["cad_state"][field] = value
                state["field_versions"][field] = int(state["field_versions"].get(field, 0)) + 1
            state["record_version"] += 1
            self._send_json(
                {
                    "status": "applied",
                    "new_record_version": state["record_version"],
                    "new_field_versions": state["field_versions"],
                }
            )
            return

        if path == "/plant/emit_event":
            state["events"].append(body.get("event", {}))
            self._send_json({"status": "ok"})
            return

        self._send_json({"error": f"unknown_route:{path}"}, status=404)


class _FakeSimulationHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int]):
        super().__init__(server_address, _FakeSimulationHandler)
        self.state = {
            "cad_state": {"location": "Unknown"},
            "record_version": 0,
            "field_versions": {"location": 0, "city": 0},
            "transcript": [{"turn": 1, "text": "Caller: 200 Southbound Ave"}],
            "checkpoint_requests": 0,
            "checkpoint_polls": 0,
            "checkpoint_by_id": {},
            "events": [],
        }


class MCPSouthboundIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sim_server = _FakeSimulationHTTPServer(("127.0.0.1", 0))
        cls.sim_port = cls.sim_server.server_address[1]
        cls.sim_thread = threading.Thread(target=cls.sim_server.serve_forever, daemon=True)
        cls.sim_thread.start()

        config = GovernanceConfig(
            policy_file="policies/test_full_ecc.yaml",
            registry_file="registries/test_registry.yaml",
            evidence_config_file="policies/domain_evidence_config.yaml",
            auth_config_file="policies/agent_auth_config.json",
            proposals_per_sec=100,
            sim_base_url=f"http://127.0.0.1:{cls.sim_port}",
            southbound_timeout_sec=5.0,
            checkpoint_poll_interval_sec=0.01,
        )
        cls.service = GovernanceService(root_dir=".", config=config)
        cls.server = MCPHTTPServer(("127.0.0.1", 0), service=cls.service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.sim_server.shutdown()
        cls.sim_server.server_close()
        cls.sim_thread.join(timeout=2)
        cls.service.close()

    def _post(self, path: str, payload: dict) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_propose_action_uses_real_southbound_tools(self) -> None:
        proposal = {
            "action_id": "sb-act-001",
            "incident_id": "inc-sb-1",
            "action_class": "cad_update.narrative",
            "proposed_payload": {"narrative": "Caller gave 200 Southbound Ave", "append": True},
            "evidence_refs": [
                {
                    "type": "transcript_span",
                    "category": "human_communication",
                    "source": "turn:1",
                    "content": "Caller gave 200 Southbound Ave",
                    "confidence": 0.95,
                }
            ],
            "uncertainty": {"p_correct": 0.95, "conflict": False},
            "read_set": {"record_version": 0, "field_versions": {"narrative": 0}},
            "proposer": {
                "agent_id": "911buddy",
                "agent_secret": "dev-911buddy-secret",
                "agent_role": "dsa",
                "autonomy_level": "A2",
            },
        }

        outcome = self._post("/mcp/propose_action", proposal)
        self.assertEqual(outcome["decision"], "executed")
        self.assertEqual(self.sim_server.state["cad_state"]["narrative"], "Caller gave 200 Southbound Ave")
        self.assertEqual(self.sim_server.state["checkpoint_requests"], 1)
        self.assertGreaterEqual(self.sim_server.state["checkpoint_polls"], 1)
        self.assertTrue(self.sim_server.state["events"])
        self.assertEqual(self.sim_server.state["events"][-1].get("type"), "governance_decision")


class MCPJWTAuthIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = GovernanceConfig(
            policy_file="policies/test_full_ecc.yaml",
            registry_file="registries/test_registry.yaml",
            evidence_config_file="policies/domain_evidence_config.yaml",
            auth_config_file="policies/agent_auth_jwt_config.json",
            proposals_per_sec=100,
        )
        cls.service = GovernanceService(root_dir=".", config=config)
        cls.server = MCPHTTPServer(("127.0.0.1", 0), service=cls.service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.service.close()

    def _post(self, path: str, payload: dict, token: str | None = None) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_error(self, path: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        raise AssertionError("expected HTTPError")

    def test_jwt_auth_success_and_failure(self) -> None:
        token = _mint_jwt_hs256(
            "dev-jwt-signing-secret-change-me",
            {
                "iss": "acaf-dev",
                "aud": "911bench-governance",
                "sub": "911buddy",
                "agent_id": "911buddy",
                "role": "dsa",
                "allowed_action_classes": ["cad_update.address"],
                "exp": int(time.time()) + 3600,
            },
        )

        _ = self._post(
            "/mcp/admin/seed_context",
            {
                "incident_id": "inc-jwt-1",
                "transcript": [{"turn": 1, "text": "Caller gave 901 JWT Ave"}],
                "cad_view": {"location": "Unknown"},
                "location": {"ani_ali": "901 JWT Ave"},
                "sop_refs": ["fire-res-v2"],
            },
        )
        proposal = {
            "action_id": "jwt-act-001",
            "incident_id": "inc-jwt-1",
            "action_class": "cad_update.address",
            "proposed_payload": {"location": "901 JWT Ave", "city": "Vancouver"},
            "evidence_refs": [
                {
                    "type": "transcript_span",
                    "category": "human_communication",
                    "source": "turn:1",
                    "content": "Caller gave 901 JWT Ave",
                    "confidence": 0.95,
                }
            ],
            "uncertainty": {"p_correct": 0.95, "conflict": False},
            "read_set": {"record_version": 0, "field_versions": {"location": 0, "city": 0}},
            "proposer": {
                "agent_id": "911buddy",
                "agent_role": "dsa",
                "autonomy_level": "A3",
            },
        }
        ok = self._post("/mcp/propose_action", proposal, token=token)
        self.assertIn(ok["decision"], {"executed", "denied", "needs_retry_conflict"})

        code, body = self._post_error("/mcp/propose_action", proposal, token=None)
        self.assertEqual(code, 401)
        self.assertEqual(body.get("error"), "missing_bearer_token")


if __name__ == "__main__":
    unittest.main()
