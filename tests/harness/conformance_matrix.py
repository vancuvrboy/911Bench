"""Northbound client conformance matrix runner (initial implementation)."""

from __future__ import annotations

import argparse
import copy
import json
import threading
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
import urllib.request

from clients.python.governance_mcp_client import GovernanceMCPClient
from gov_server.mcp_server import MCPHTTPServer
from gov_server.service import GovernanceConfig, GovernanceService

JSONObject = dict[str, Any]


def _http_post_json(base_url: str, path: str, payload: JSONObject) -> JSONObject:
    req = urllib.request.Request(
        f"{base_url}{path}",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_json(base_url: str, path: str) -> JSONObject:
    req = urllib.request.Request(f"{base_url}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run_python_sdk_client(base_url: str, fixture: JSONObject) -> JSONObject:
    fixture = copy.deepcopy(fixture)
    fixture["seed_context"]["incident_id"] = "inc-conformance-sdk"
    fixture["snapshot_args"]["incident_id"] = "inc-conformance-sdk"
    fixture["proposal"]["incident_id"] = "inc-conformance-sdk"
    fixture["proposal"]["action_id"] = "act-conformance-sdk"
    client = GovernanceMCPClient(base_url=base_url)
    out: JSONObject = {}
    out["capabilities"] = client.capabilities()
    out["descriptor"] = client.descriptor()
    out["tools_list"] = client.tools_list()
    out["seed"] = client.seed_context(**fixture["seed_context"])
    out["snapshot"] = client.get_context_snapshot(**fixture["snapshot_args"])
    out["proposal"] = client.propose_action(fixture["proposal"])
    out["audit"] = client.get_audit_ref(fixture["proposal"]["action_id"])
    out["rpc_tools_list"] = client.rpc("tools/list", {}, request_id="matrix-1")
    return out


def _run_http_client(base_url: str, fixture: JSONObject) -> JSONObject:
    fixture = copy.deepcopy(fixture)
    fixture["seed_context"]["incident_id"] = "inc-conformance-http"
    fixture["snapshot_args"]["incident_id"] = "inc-conformance-http"
    fixture["proposal"]["incident_id"] = "inc-conformance-http"
    fixture["proposal"]["action_id"] = "act-conformance-http"
    out: JSONObject = {}
    out["capabilities"] = _http_get_json(base_url, "/mcp/capabilities")
    out["descriptor"] = _http_get_json(base_url, "/mcp/descriptor")
    out["tools_list"] = _http_get_json(base_url, "/mcp/tools/list")
    out["seed"] = _http_post_json(base_url, "/mcp/admin/seed_context", fixture["seed_context"])
    out["snapshot"] = _http_post_json(base_url, "/mcp/get_context_snapshot", fixture["snapshot_args"])
    out["proposal"] = _http_post_json(base_url, "/mcp/propose_action", fixture["proposal"])
    out["audit"] = _http_get_json(base_url, f"/mcp/get_audit_ref?action_id={fixture['proposal']['action_id']}")
    out["rpc_tools_list"] = _http_post_json(
        base_url,
        "/mcp/rpc",
        {"id": "matrix-1", "method": "tools/list", "params": {}},
    )
    return out


def _check_semantic_equivalence(python_sdk: JSONObject, raw_http: JSONObject) -> list[str]:
    failures: list[str] = []
    checks = [
        ("capabilities.protocol", python_sdk["capabilities"].get("protocol"), raw_http["capabilities"].get("protocol")),
        ("descriptor.name", python_sdk["descriptor"].get("name"), raw_http["descriptor"].get("name")),
        ("proposal.decision", python_sdk["proposal"].get("decision"), raw_http["proposal"].get("decision")),
        ("audit.has_entry", bool(python_sdk["audit"].get("audit_entry")), bool(raw_http["audit"].get("audit_entry"))),
    ]
    for key, lhs, rhs in checks:
        if lhs != rhs:
            failures.append(f"{key}: {lhs!r} != {rhs!r}")
    return failures


def run_matrix(root: Path, fixture_path: Path, output_dir: Path) -> int:
    config = GovernanceConfig(
        policy_file="policies/test_full_ecc.yaml",
        registry_file="registries/test_registry.yaml",
        evidence_config_file="policies/domain_evidence_config.yaml",
        auth_config_file="policies/agent_auth_config.json",
        proposals_per_sec=100,
    )
    service = GovernanceService(root_dir=root, config=config)
    server = MCPHTTPServer(("127.0.0.1", 0), service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        python_sdk_out = _run_python_sdk_client(base_url, fixture)
        raw_http_out = _run_http_client(base_url, fixture)
        failures = _check_semantic_equivalence(python_sdk_out, raw_http_out)

        report = {
            "ok": len(failures) == 0,
            "clients": ["python_sdk", "raw_http"],
            "failures": failures,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "northbound_conformance_matrix.json"
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"conformance-matrix report: {out_path}")
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    except HTTPError as exc:
        print(f"HTTPError: {exc.code} {exc.reason}")
        return 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run northbound client conformance matrix.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--fixture", default="tests/conformance/fixtures/basic_roundtrip.json")
    parser.add_argument("--output-dir", default="tests/results")
    args = parser.parse_args()
    return run_matrix(Path(args.root).resolve(), Path(args.fixture).resolve(), Path(args.output_dir).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
