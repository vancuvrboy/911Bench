"""Integration tests for runtime adapters built on the Python SDK."""

from __future__ import annotations

import threading
import time
import unittest

from clients.python.governance_mcp_client import (
    GovernanceMCPClient,
    LangChainRuntimeAdapter,
    OpenAIRuntimeAdapter,
)
from gov_server.mcp_server import MCPHTTPServer
from gov_server.service import GovernanceConfig, GovernanceService


class RuntimeAdaptersIntegrationTest(unittest.TestCase):
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
        cls.openai_adapter = OpenAIRuntimeAdapter(
            client=cls.client,
            agent_id="911buddy",
            agent_role="dsa",
            autonomy_level="A3",
            agent_secret="dev-911buddy-secret",
        )
        cls.langchain_adapter = LangChainRuntimeAdapter(
            client=cls.client,
            agent_id="911buddy",
            agent_role="dsa",
            autonomy_level="A3",
            agent_secret="dev-911buddy-secret",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.service.close()

    def test_openai_adapter_context_and_proposal(self) -> None:
        _ = self.client.seed_context(
            incident_id="inc-openai-adapter",
            transcript=[{"turn": 1, "text": "Caller gave 44 Adapter Way"}],
            cad_view={"location": "Unknown"},
            location={"ani_ali": "44 Adapter Way"},
            sop_refs=["fire-res-v2"],
        )
        ctx = self.openai_adapter.fetch_incident_context("inc-openai-adapter")
        self.assertEqual(ctx["incident_id"], "inc-openai-adapter")

        out = self.openai_adapter.evaluate_and_propose(
            action_id="adapter-openai-001",
            incident_id="inc-openai-adapter",
            action_class="cad_update.address",
            proposed_payload={"location": "44 Adapter Way", "city": "Vancouver"},
            evidence_refs=[
                {
                    "type": "transcript_span",
                    "category": "human_communication",
                    "source": "turn:1",
                    "content": "Caller gave 44 Adapter Way",
                    "confidence": 0.95,
                }
            ],
            uncertainty={"p_correct": 0.95, "conflict": False},
            read_set={"record_version": 0, "field_versions": {"location": 0, "city": 0}},
            idempotency_key="adapter-openai-idem-1",
        )
        self.assertIn(out["decision"], {"executed", "denied", "needs_retry_conflict"})

    def test_langchain_adapter_tools_and_run(self) -> None:
        tools = self.langchain_adapter.list_action_tools()
        names = {item["name"] for item in tools}
        self.assertIn("governance::cad_update.address", names)

        _ = self.client.seed_context(
            incident_id="inc-langchain-adapter",
            transcript=[{"turn": 1, "text": "Caller gave 55 Chain St"}],
            cad_view={"location": "Unknown"},
            location={"ani_ali": "55 Chain St"},
            sop_refs=["fire-res-v2"],
        )
        out = self.langchain_adapter.run_tool(
            action_id="adapter-lc-001",
            incident_id="inc-langchain-adapter",
            action_class="cad_update.address",
            payload={"location": "55 Chain St", "city": "Vancouver"},
            evidence_refs=[
                {
                    "type": "transcript_span",
                    "category": "human_communication",
                    "source": "turn:1",
                    "content": "Caller gave 55 Chain St",
                    "confidence": 0.95,
                }
            ],
            uncertainty={"p_correct": 0.95, "conflict": False},
            read_set={"record_version": 0, "field_versions": {"location": 0, "city": 0}},
        )
        self.assertIn(out["decision"], {"executed", "denied", "needs_retry_conflict"})


if __name__ == "__main__":
    unittest.main()
