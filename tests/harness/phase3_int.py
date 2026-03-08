"""Phase 3 integration runner for INT-001..INT-006."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

JSONObject = dict[str, Any]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _post_json(url: str, payload: JSONObject, timeout: float = 8.0) -> JSONObject:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, timeout: float = 8.0) -> JSONObject:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_for(url: str, timeout_sec: float = 12.0) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            _get_json(url, timeout=1.0)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"timeout_waiting_for:{url}")


def _proposal(
    incident_id: str,
    action_id: str,
    read_record_version: int,
    field_versions: dict[str, int] | None = None,
    autonomy_level: str = "A3",
    location: str = "2421 Main St",
) -> JSONObject:
    read_fields = field_versions if isinstance(field_versions, dict) and field_versions else {"location": 0, "city": 0}
    return {
        "action_id": action_id,
        "incident_id": incident_id,
        "action_class": "cad_update.address",
        "proposed_payload": {"location": location, "city": "Vancouver"},
        "evidence_refs": [
            {
                "type": "transcript_span",
                "category": "human_communication",
                "source": "turn:1",
                "content": f"There is a fire at {location}",
                "confidence": 0.95,
            }
        ],
        "uncertainty": {"p_correct": 0.95, "conflict": False},
        "read_set": {"record_version": int(read_record_version), "field_versions": read_fields},
        "proposer": {
            "agent_id": "911buddy",
            "agent_secret": "dev-911buddy-secret",
            "agent_role": "dsa",
            "autonomy_level": autonomy_level,
        },
        "dsa": {"apply_suggested_payload": False},
    }


def run(root: Path, output_path: Path) -> int:
    sim_port = _free_port()
    gov_port = _free_port()
    sim_url = f"http://127.0.0.1:{sim_port}"
    gov_url = f"http://127.0.0.1:{gov_port}"

    sim_cmd = [
        sys.executable,
        "-m",
        "sim_server.southbound_server",
        "--root",
        str(root),
        "--port",
        str(sim_port),
    ]
    gov_cmd = [
        sys.executable,
        "-m",
        "gov_server",
        "--root",
        str(root),
        "--port",
        str(gov_port),
        "--sim-base-url",
        sim_url,
    ]

    sim_proc = subprocess.Popen(sim_cmd, cwd=root)
    gov_proc = subprocess.Popen(gov_cmd, cwd=root)
    report: JSONObject = {"tests": {}}
    try:
        _wait_for(f"{sim_url}/healthz")
        _wait_for(f"{gov_url}/mcp/capabilities")
        loaded = _post_json(
            f"{sim_url}/admin/load_start",
            {
                "scenario_id": "phase3_int",
                "caller_fixture": "fixtures/caller_cooperative_calm.json",
                "incident_fixture": "fixtures/incident_fire_residential.json",
                "qa_fixture": "fixtures/qaTemplate_003.json",
            },
        )
        incident_id = str(loaded["loaded"]["incident_id"])

        _post_json(
            f"{sim_url}/admin/post_turn",
            {
                "incident_id": incident_id,
                "caller": "There is a fire at 2421 Main St",
                "call_taker": "Confirming address and dispatching now.",
                "cad_updates": {"narrative": "Initial caller report"},
            },
        )

        state = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})
        pre_stats = _post_json(f"{sim_url}/admin/stats", {})
        _ = _post_json(
            f"{gov_url}/mcp/propose_action",
            _proposal(
                incident_id,
                "int001-probe",
                read_record_version=int(state.get("record_version", 0)),
                field_versions=state.get("field_versions", {}),
            ),
        )
        post_stats = _post_json(f"{sim_url}/admin/stats", {})
        state_calls_before = int(pre_stats.get("route_counts", {}).get("/plant/get_state_snapshot", 0))
        state_calls_after = int(post_stats.get("route_counts", {}).get("/plant/get_state_snapshot", 0))
        report["tests"]["INT-001"] = {
            "pass": state_calls_after > state_calls_before,
            "detail": {"state_calls_before": state_calls_before, "state_calls_after": state_calls_after},
        }

        state = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})
        pre_stats = _post_json(f"{sim_url}/admin/stats", {})
        _ = _post_json(
            f"{gov_url}/mcp/propose_action",
            _proposal(
                incident_id,
                "int002-probe",
                read_record_version=int(state.get("record_version", 0)),
                field_versions=state.get("field_versions", {}),
            ),
        )
        post_stats = _post_json(f"{sim_url}/admin/stats", {})
        tx_calls_before = int(pre_stats.get("route_counts", {}).get("/plant/get_transcript_since", 0))
        tx_calls_after = int(post_stats.get("route_counts", {}).get("/plant/get_transcript_since", 0))
        report["tests"]["INT-002"] = {
            "pass": tx_calls_after > tx_calls_before,
            "detail": {"transcript_calls_before": tx_calls_before, "transcript_calls_after": tx_calls_after},
        }

        state = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})
        out3 = _post_json(
            f"{gov_url}/mcp/propose_action",
            _proposal(
                incident_id,
                "int003-apply",
                read_record_version=int(state.get("record_version", 0)),
                field_versions=state.get("field_versions", {}),
            ),
        )
        events = _post_json(f"{sim_url}/admin/events", {"incident_id": incident_id})
        cad_patches = [e for e in events.get("events", []) if e.get("event_type") == "cad_patch_applied"]
        report["tests"]["INT-003"] = {
            "pass": out3.get("decision") == "executed" and bool(cad_patches),
            "detail": {"decision": out3.get("decision"), "cad_patch_events": len(cad_patches)},
        }

        _ = _post_json(
            f"{sim_url}/plant/apply_cad_patch",
            {
                "incident_id": incident_id,
                "action_id": "sim-manual-update",
                "action_class": "cad_update.address",
                "payload": {"location": "999 Conflict Ave"},
                "read_set": {"record_version": int(state.get("record_version", 0)), "field_versions": {"location": 0}},
                "policy_id": "sim-manual",
                "policy_hash": "sim-manual",
                "proposer_agent_id": "sim-admin",
            },
        )
        stale = _post_json(f"{gov_url}/mcp/propose_action", _proposal(incident_id, "int004-stale", read_record_version=0))
        fresh_state = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})
        retry = _post_json(
            f"{gov_url}/mcp/propose_action",
            _proposal(
                incident_id,
                "int004-retry",
                read_record_version=int(fresh_state.get("record_version", 0)),
                field_versions=fresh_state.get("field_versions", {}),
                location="1000 Retry Ave",
            ),
        )
        report["tests"]["INT-004"] = {
            "pass": stale.get("decision") == "needs_retry_conflict" and retry.get("decision") == "executed",
            "detail": {"first_decision": stale.get("decision"), "retry_decision": retry.get("decision")},
        }

        events = _post_json(f"{sim_url}/admin/events", {"incident_id": incident_id})
        gov_events = [e for e in events.get("events", []) if e.get("event_type") == "governance_decision"]
        report["tests"]["INT-005"] = {
            "pass": bool(gov_events),
            "detail": {"governance_decision_events": len(gov_events)},
        }

        _ = _post_json(f"{gov_url}/mcp/swap_policy", {"policy_file": "policies/test_prohibit_basic.yaml"})
        pre_stats = _post_json(f"{sim_url}/admin/stats", {})
        denied = _post_json(
            f"{gov_url}/mcp/propose_action",
            _proposal(
                incident_id,
                "int006-denied",
                read_record_version=int(fresh_state.get("record_version", 0)),
                field_versions=fresh_state.get("field_versions", {}),
                autonomy_level="A2",
                location="",
            ),
        )
        post_stats = _post_json(f"{sim_url}/admin/stats", {})
        apply_before = int(pre_stats.get("route_counts", {}).get("/plant/apply_cad_patch", 0))
        apply_after = int(post_stats.get("route_counts", {}).get("/plant/apply_cad_patch", 0))
        report["tests"]["INT-006"] = {
            "pass": (
                denied.get("decision") == "denied"
                and str(denied.get("denial_rule_id", "")).startswith("prohibit_")
                and apply_after == apply_before
            ),
            "detail": {
                "decision": denied.get("decision"),
                "apply_calls_before": apply_before,
                "apply_calls_after": apply_after,
                "denial_rule_id": denied.get("denial_rule_id"),
            },
        }

        report["summary"] = {
            "total": len(report["tests"]),
            "passed": sum(1 for v in report["tests"].values() if v.get("pass")),
            "failed": sum(1 for v in report["tests"].values() if not v.get("pass")),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"phase3-int report: {output_path}")
        return 0 if report["summary"]["failed"] == 0 else 1
    finally:
        gov_proc.terminate()
        sim_proc.terminate()
        try:
            gov_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            gov_proc.kill()
        try:
            sim_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            sim_proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase 3 INT-001..006 integration tests.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="tests/results/phase3_int_report.json")
    args = parser.parse_args()
    return run(root=Path(args.root).resolve(), output_path=Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
