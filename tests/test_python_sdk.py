"""Integration tests for Python governance MCP SDK."""

from __future__ import annotations

import threading
import time
import unittest

from clients.python.governance_mcp_client import GovernanceMCPClient, GovernanceMCPError
from gov_server.mcp_server import MCPHTTPServer
from gov_server.service import GovernanceConfig, GovernanceService


class PythonSDKIntegrationTest(unittest.TestCase):
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
        cls.client = GovernanceMCPClient(base_url=f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.service.close()

    def test_happy_path(self) -> None:
        caps = self.client.capabilities()
        self.assertEqual(caps["protocol"], "mcp-http-sse")

        _ = self.client.seed_context(
            incident_id="inc-sdk-1",
            transcript=[{"turn": 1, "text": "Caller gave 22 SDK Way"}],
            cad_view={"location": "Unknown"},
            location={"ani_ali": "22 SDK Way"},
            sop_refs=["fire-res-v2"],
        )
        snap = self.client.get_context_snapshot(
            incident_id="inc-sdk-1",
            agent_id="911buddy",
            agent_secret="dev-911buddy-secret",
        )
        self.assertEqual(snap["incident_id"], "inc-sdk-1")

        outcome = self.client.propose_action(
            {
                "action_id": "sdk-act-001",
                "incident_id": "inc-sdk-1",
                "action_class": "cad_update.address",
                "proposed_payload": {"location": "22 SDK Way", "city": "Vancouver"},
                "evidence_refs": [
                    {
                        "type": "transcript_span",
                        "category": "human_communication",
                        "source": "turn:1",
                        "content": "Caller gave 22 SDK Way",
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
            }
        )
        self.assertIn(outcome["decision"], {"executed", "denied", "needs_retry_conflict"})
        audit = self.client.get_audit_ref("sdk-act-001")
        self.assertEqual(audit["action_id"], "sdk-act-001")

    def test_error_surface(self) -> None:
        with self.assertRaises(GovernanceMCPError) as ctx:
            self.client.propose_action(
                {
                    "action_id": "sdk-act-auth",
                    "incident_id": "inc-sdk-2",
                    "action_class": "cad_update.address",
                    "proposed_payload": {"location": "Bad", "city": "Vancouver"},
                    "evidence_refs": [],
                    "uncertainty": {"p_correct": 0.95, "conflict": False},
                    "read_set": {"record_version": 0, "field_versions": {"location": 0, "city": 0}},
                    "proposer": {
                        "agent_id": "translator",
                        "agent_secret": "dev-translator-secret",
                        "agent_role": "translation",
                        "autonomy_level": "A4",
                    },
                }
            )
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
