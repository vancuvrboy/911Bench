"""Phase 3 smoke integration: governance southbound -> real SIM HTTP adapter."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

JSONObject = dict[str, Any]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _post_json(url: str, payload: JSONObject, timeout: float = 5.0) -> JSONObject:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, timeout: float = 5.0) -> JSONObject:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_for(url: str, timeout_sec: float = 10.0) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            _get_json(url, timeout=1.0)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"timeout_waiting_for:{url}")


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
    try:
        _wait_for(f"{sim_url}/healthz")
        _wait_for(f"{gov_url}/mcp/capabilities")

        loaded = _post_json(
            f"{sim_url}/admin/load_start",
            {
                "scenario_id": "phase3_smoke",
                "caller_fixture": "fixtures/caller_cooperative_calm.json",
                "incident_fixture": "fixtures/incident_fire_residential.json",
                "qa_fixture": "fixtures/qaTemplate_003.json",
            },
        )
        incident_id = str(loaded["loaded"]["incident_id"])

        proposal = {
            "action_id": "phase3-smoke-action-1",
            "incident_id": incident_id,
            "action_class": "cad_update.address",
            "proposed_payload": {"location": "2421 Main St", "city": "Vancouver"},
            "evidence_refs": [
                {
                    "type": "transcript_span",
                    "category": "human_communication",
                    "source": "turn:1",
                    "content": "There is a fire at 2421 Main St",
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
        outcome = _post_json(f"{gov_url}/mcp/propose_action", proposal)
        state = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})

        report = {
            "sim_url": sim_url,
            "gov_url": gov_url,
            "incident_id": incident_id,
            "decision": outcome.get("decision"),
            "audit_ref": outcome.get("audit_ref"),
            "sim_record_version": state.get("record_version"),
            "sim_cad_state": state.get("cad_state", {}),
            "pass": bool(outcome.get("decision") == "executed" and state.get("cad_state", {}).get("location")),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"phase3-smoke report: {output_path}")
        return 0 if report["pass"] else 1
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
    parser = argparse.ArgumentParser(description="Run Phase 3 governance+sim smoke integration.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="tests/results/phase3_smoke_report.json")
    args = parser.parse_args()
    return run(root=Path(args.root).resolve(), output_path=Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
