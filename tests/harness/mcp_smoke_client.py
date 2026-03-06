"""Descriptor-driven MCP smoke client for governance server."""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from gov_server.mcp_server import MCPHTTPServer
from gov_server.service import GovernanceConfig, GovernanceService

JSONObject = dict[str, Any]


def http_get_json(base_url: str, path: str, query: dict[str, Any] | None = None) -> JSONObject:
    q = f"?{urllib.parse.urlencode(query or {})}" if query else ""
    url = f"{base_url}{path}{q}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(base_url: str, path: str, payload: JSONObject) -> JSONObject:
    url = f"{base_url}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class SpawnedServer:
    server: MCPHTTPServer
    thread: threading.Thread
    base_url: str

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def spawn_local_server(root: str, host: str = "127.0.0.1") -> SpawnedServer:
    config = GovernanceConfig(
        policy_file="policies/test_full_ecc.yaml",
        registry_file="registries/test_registry.yaml",
        evidence_config_file="policies/domain_evidence_config.yaml",
        auth_config_file="policies/agent_auth_config.json",
    )
    service = GovernanceService(root_dir=root, config=config)
    server = MCPHTTPServer((host, 0), service=service)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    return SpawnedServer(server=server, thread=thread, base_url=f"http://{host}:{port}")


def run_smoke(base_url: str) -> JSONObject:
    caps = http_get_json(base_url, "/mcp/capabilities")
    descriptor = http_get_json(base_url, caps["descriptor"]["endpoint"])
    tools = http_get_json(base_url, caps["tools"]["list_endpoint"])

    # Seed context for a deterministic smoke flow.
    _ = http_post_json(
        base_url,
        "/mcp/admin/seed_context",
        {
            "incident_id": "inc-smoke-1",
            "transcript": [{"turn": 1, "text": "Caller: 500 Smoke Ave"}],
            "cad_view": {"location": "Unknown"},
            "location": {"ani_ali": "500 Smoke Ave"},
            "sop_refs": ["fire-res-v2"],
        },
    )

    tool_names = {item["name"] for item in tools.get("tools", [])}
    if "get_context_snapshot" not in tool_names:
        raise RuntimeError("missing_required_tool:get_context_snapshot")

    call = http_post_json(
        base_url,
        caps["tools"]["call_endpoint"],
        {
            "tool": "get_context_snapshot",
            "arguments": {
                "incident_id": "inc-smoke-1",
                "agent_id": "911buddy",
                "agent_secret": "dev-911buddy-secret",
            },
        },
    )

    rpc = http_post_json(
        base_url,
        caps["rpc"]["endpoint"],
        {
            "id": "smoke-rpc-1",
            "method": "tools/call",
            "params": {
                "tool": "propose_action",
                "arguments": {
                    "action_id": "smoke-action-1",
                    "incident_id": "inc-smoke-1",
                    "action_class": "cad_update.address",
                    "proposed_payload": {"location": "500 Smoke Ave", "city": "Vancouver"},
                    "evidence_refs": [
                        {
                            "type": "transcript_span",
                            "category": "human_communication",
                            "source": "turn:1",
                            "content": "500 Smoke Ave",
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
    )

    return {
        "capabilities_protocol": caps.get("protocol"),
        "descriptor_name": descriptor.get("name"),
        "tools_count": len(tools.get("tools", [])),
        "snapshot_incident": call.get("result", {}).get("incident_id"),
        "propose_decision": rpc.get("result", {}).get("result", {}).get("decision"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run descriptor-driven MCP smoke client.")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--spawn-local-server", action="store_true")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    spawned: SpawnedServer | None = None
    base_url = args.base_url
    if args.spawn_local_server:
        spawned = spawn_local_server(root=args.root)
        base_url = spawned.base_url
    if not base_url:
        raise SystemExit("Provide --base-url or use --spawn-local-server")

    try:
        result = run_smoke(base_url)
        print(json.dumps({"ok": True, "base_url": base_url, "result": result}, indent=2))
        return 0
    finally:
        if spawned is not None:
            spawned.close()


if __name__ == "__main__":
    raise SystemExit(main())

