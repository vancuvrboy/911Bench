"""Phase 3 checkpoint integration runner for INT-010..INT-015."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

JSONObject = dict[str, Any]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _post_json(url: str, payload: JSONObject, timeout: float = 10.0) -> JSONObject:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, timeout: float = 10.0) -> JSONObject:
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
    field_versions: dict[str, int],
    *,
    location: str = "2421 Main St",
    p_correct: float = 0.95,
    conflict: bool = False,
    autonomy_level: str = "A3",
) -> JSONObject:
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
        "uncertainty": {"p_correct": float(p_correct), "conflict": bool(conflict)},
        "read_set": {"record_version": int(read_record_version), "field_versions": field_versions},
        "proposer": {
            "agent_id": "911buddy",
            "agent_secret": "dev-911buddy-secret",
            "agent_role": "dsa",
            "autonomy_level": autonomy_level,
        },
        "dsa": {"apply_suggested_payload": False},
    }


def _run_proposal_async(gov_url: str, proposal: JSONObject) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["outcome"] = _post_json(f"{gov_url}/mcp/propose_action", proposal, timeout=45.0)
        except Exception as exc:  # pragma: no cover
            box["error"] = str(exc)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t, box


def _wait_pending(sim_url: str, incident_id: str, role: str | None = None, timeout_sec: float = 8.0) -> JSONObject:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        payload = {"incident_id": incident_id, "status_filter": "pending"}
        if role is not None:
            payload["role_filter"] = role
        listing = _post_json(f"{sim_url}/checkpoint/list", payload)
        rows = listing.get("requests", [])
        if rows:
            return rows[0]
        time.sleep(0.1)
    raise RuntimeError(f"timeout_waiting_checkpoint_pending:{role or 'any'}")


def _drain_pending_for_role(
    sim_url: str,
    incident_id: str,
    role: str,
    rationale: str,
    seen: set[str] | None = None,
) -> set[str]:
    seen_ids = seen or set()
    listing = _post_json(
        f"{sim_url}/checkpoint/list",
        {"incident_id": incident_id, "status_filter": "pending", "role_filter": role},
    )
    for row in listing.get("requests", []):
        req_id = str(row.get("request_id", ""))
        if not req_id or req_id in seen_ids:
            continue
        _post_json(
            f"{sim_url}/checkpoint/submit",
            {"request_id": req_id, "decision": "approved", "rationale": rationale},
        )
        seen_ids.add(req_id)
    return seen_ids


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
        "--no-auto-approve-checkpoints",
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
                "scenario_id": "phase3_checkpoint",
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
                "call_taker": "911, what is your emergency?",
                "cad_updates": {"narrative": "initial"},
            },
        )

        def current_state() -> JSONObject:
            return _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})

        # INT-010
        _post_json(f"{gov_url}/mcp/swap_policy", {"policy_file": "policies/test_checkpoint_conditional.yaml"})
        st = current_state()
        p10 = _proposal(
            incident_id,
            "int010",
            read_record_version=int(st.get("record_version", 0)),
            field_versions=st.get("field_versions", {}),
            p_correct=0.95,
            conflict=False,
        )
        th, box = _run_proposal_async(gov_url, p10)
        req = _wait_pending(sim_url, incident_id, role="call_taker")
        _post_json(
            f"{sim_url}/checkpoint/submit",
            {"request_id": req["request_id"], "decision": "approved", "rationale": "approved_by_call_taker"},
        )
        th.join(timeout=70)
        out10 = box.get("outcome", {})
        events = _post_json(f"{sim_url}/admin/events", {"incident_id": incident_id}).get("events", [])
        report["tests"]["INT-010"] = {
            "pass": out10.get("decision") == "executed"
            and any(e.get("event_type") == "checkpoint_decision" for e in events)
            and any(e.get("event_type") == "cad_patch_applied" for e in events),
            "detail": {"decision": out10.get("decision"), "checkpoint_response": out10.get("checkpoint", {}).get("response")},
        }

        # INT-011
        st = current_state()
        p11 = _proposal(
            incident_id,
            "int011",
            read_record_version=int(st.get("record_version", 0)),
            field_versions=st.get("field_versions", {}),
            location="111 Original Ave",
        )
        th, box = _run_proposal_async(gov_url, p11)
        req = _wait_pending(sim_url, incident_id, role="call_taker")
        edited_payload = {"location": "111 Edited Ave", "city": "Vancouver"}
        _post_json(
            f"{sim_url}/checkpoint/submit",
            {
                "request_id": req["request_id"],
                "decision": "edited_approved",
                "edited_payload": edited_payload,
                "rationale": "edited_by_call_taker",
            },
        )
        th.join(timeout=70)
        out11 = box.get("outcome", {})
        st11 = current_state()
        report["tests"]["INT-011"] = {
            "pass": out11.get("decision") == "executed"
            and out11.get("checkpoint", {}).get("response") == "edited_approved"
            and st11.get("cad_state", {}).get("location") == "111 Edited Ave",
            "detail": {
                "decision": out11.get("decision"),
                "checkpoint_response": out11.get("checkpoint", {}).get("response"),
                "final_location": st11.get("cad_state", {}).get("location"),
            },
        }

        # INT-012
        st = current_state()
        p12 = _proposal(
            incident_id,
            "int012",
            read_record_version=int(st.get("record_version", 0)),
            field_versions=st.get("field_versions", {}),
            location="122 Deferred Ave",
            conflict=False,
        )
        th, box = _run_proposal_async(gov_url, p12)
        req = _wait_pending(sim_url, incident_id, role="call_taker")
        _post_json(
            f"{sim_url}/checkpoint/submit",
            {"request_id": req["request_id"], "decision": "deferred_escalated", "rationale": "need_supervisor"},
        )
        seen_sup: set[str] = set()
        deadline = time.time() + 35.0
        while th.is_alive() and time.time() < deadline:
            seen_sup = _drain_pending_for_role(
                sim_url,
                incident_id,
                role="supervisor",
                rationale="supervisor_approved",
                seen=seen_sup,
            )
            time.sleep(0.1)
        th.join(timeout=10)
        out12 = box.get("outcome", {})
        events12 = _post_json(f"{sim_url}/admin/events", {"incident_id": incident_id}).get("events", [])
        chk_decisions = [e for e in events12 if e.get("event_type") == "checkpoint_decision"]
        report["tests"]["INT-012"] = {
            "pass": out12.get("decision") == "executed" and len(chk_decisions) >= 2,
            "detail": {"decision": out12.get("decision"), "checkpoint_decision_events": len(chk_decisions)},
        }

        # INT-013
        _post_json(f"{sim_url}/admin/config", {"checkpoint_poll_mode": "force_timeout"})
        st = current_state()
        p13 = _proposal(
            incident_id,
            "int013",
            read_record_version=int(st.get("record_version", 0)),
            field_versions=st.get("field_versions", {}),
            location="133 Timeout Ave",
        )
        th, box = _run_proposal_async(gov_url, p13)
        th.join(timeout=70)
        out13 = box.get("outcome", {})
        _post_json(f"{sim_url}/admin/config", {"checkpoint_poll_mode": "normal"})
        report["tests"]["INT-013"] = {
            "pass": out13.get("decision") == "denied" and out13.get("denial_reason") == "checkpoint_timeout",
            "detail": {"decision": out13.get("decision"), "denial_reason": out13.get("denial_reason")},
        }

        # INT-014
        _post_json(f"{gov_url}/mcp/swap_policy", {"policy_file": "policies/test_bound_escalate.yaml"})
        st = current_state()
        p14 = _proposal(
            incident_id,
            "int014",
            read_record_version=int(st.get("record_version", 0)),
            field_versions=st.get("field_versions", {}),
            location="144 Reactive Ave",
            p_correct=0.4,
        )
        th, box = _run_proposal_async(gov_url, p14)
        seen_sup = set()
        deadline = time.time() + 35.0
        while th.is_alive() and time.time() < deadline:
            seen_sup = _drain_pending_for_role(
                sim_url,
                incident_id,
                role="supervisor",
                rationale="approved_reactive",
                seen=seen_sup,
            )
            time.sleep(0.1)
        th.join(timeout=10)
        out14 = box.get("outcome", {})
        all_reqs = _post_json(
            f"{sim_url}/checkpoint/list",
            {"incident_id": incident_id, "status_filter": None},
        ).get("requests", [])
        reactive = [r for r in all_reqs if r.get("source") == "escalation_reactive" and r.get("escalation_context")]
        report["tests"]["INT-014"] = {
            "pass": out14.get("decision") == "executed" and bool(reactive),
            "detail": {"decision": out14.get("decision"), "reactive_requests": len(reactive)},
        }

        # INT-015
        _post_json(f"{gov_url}/mcp/swap_policy", {"policy_file": "policies/test_escalate_proactive.yaml"})
        st = current_state()
        p15 = _proposal(
            incident_id,
            "int015",
            read_record_version=int(st.get("record_version", 0)),
            field_versions=st.get("field_versions", {}),
            location="155 Proactive Ave",
            p_correct=0.3,
        )
        th, box = _run_proposal_async(gov_url, p15)
        seen_sup = set()
        deadline = time.time() + 35.0
        while th.is_alive() and time.time() < deadline:
            seen_sup = _drain_pending_for_role(
                sim_url,
                incident_id,
                role="supervisor",
                rationale="approved_proactive",
                seen=seen_sup,
            )
            time.sleep(0.1)
        th.join(timeout=10)
        out15 = box.get("outcome", {})
        all_reqs = _post_json(
            f"{sim_url}/checkpoint/list",
            {"incident_id": incident_id, "status_filter": None},
        ).get("requests", [])
        proactive = [r for r in all_reqs if r.get("source") == "escalation_proactive" and r.get("escalation_context")]
        report["tests"]["INT-015"] = {
            "pass": out15.get("decision") == "executed" and bool(proactive),
            "detail": {"decision": out15.get("decision"), "proactive_requests": len(proactive)},
        }

        report["summary"] = {
            "total": len(report["tests"]),
            "passed": sum(1 for v in report["tests"].values() if v.get("pass")),
            "failed": sum(1 for v in report["tests"].values() if not v.get("pass")),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"phase3-checkpoint-int report: {output_path}")
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
    parser = argparse.ArgumentParser(description="Run Phase 3 checkpoint block INT-010..INT-015.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="tests/results/phase3_checkpoint_int_report.json")
    args = parser.parse_args()
    return run(root=Path(args.root).resolve(), output_path=Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
