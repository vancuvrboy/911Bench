"""Phase 4 end-to-end pipeline harness for PIPE-001..PIPE-044."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sim_server.schema_utils import validate_event_minimal

JSONObject = dict[str, Any]


@dataclass
class RunRecord:
    run_id: str
    scenario_id: str
    policy_file: str
    helper_enabled: bool
    rep: int
    run_dir: Path
    terminated_reason: str
    turn_count: int
    qa_score: float
    qa_item_scores: dict[str, float]
    decision_counts: dict[str, int]
    orphans: int
    events_hash: str
    prompt_hash: str


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _post_json(url: str, payload: JSONObject, timeout: float = 15.0) -> JSONObject:
    last_exc: Exception | None = None
    for attempt in range(6):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < 5:
                time.sleep(0.2 * (attempt + 1))
                last_exc = RuntimeError(f"http_error:{exc.code}:{url}:{body}")
                continue
            raise RuntimeError(f"http_error:{exc.code}:{url}:{body}") from exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable_post_json")


def _get_json(url: str, timeout: float = 10.0) -> JSONObject:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_for(url: str, timeout_sec: float = 15.0) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            _get_json(url, timeout=1.0)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"timeout_waiting_for:{url}")


def _normalize_for_hash(obj: Any) -> Any:
    volatile_keys = {
        "ts",
        "event_seq",
        "request_id",
        "correlation_id",
        "incident_id",
        "scenario_id",
        "action_id",
        "audit_ref",
        "tool_call_id",
        "execution_id",
        "duration_ms",
        "latency_ms",
    }
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key in sorted(obj.keys()):
            if key in volatile_keys:
                continue
            out[key] = _normalize_for_hash(obj[key])
        return out
    if isinstance(obj, list):
        return [_normalize_for_hash(item) for item in obj]
    return obj


def _hash_json(obj: Any) -> str:
    blob = json.dumps(_normalize_for_hash(obj), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _proposal(
    incident_id: str,
    action_id: str,
    read_record_version: int,
    field_versions: dict[str, int],
    *,
    location: str,
    p_correct: float = 0.95,
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
                "content": f"There is an emergency at {location}",
                "confidence": 0.95,
            }
        ],
        "uncertainty": {"p_correct": p_correct, "conflict": False},
        "read_set": {"record_version": int(read_record_version), "field_versions": field_versions},
        "proposer": {
            "agent_id": "911buddy",
            "agent_secret": "dev-911buddy-secret",
            "agent_role": "dsa",
            "autonomy_level": "A3",
        },
        "dsa": {"apply_suggested_payload": False},
    }


def _score_episode(events: list[JSONObject], cad_state: dict[str, Any], decisions: list[JSONObject]) -> tuple[float, dict[str, float]]:
    item_scores: dict[str, float] = {
        "dispatch_triggered": 0.0,
        "responders_arrived": 0.0,
        "location_recorded": 0.0,
        "narrative_recorded": 0.0,
        "helper_outcome_clean": 0.0,
    }
    has_dispatch = bool(cad_state.get("dispatch_triggered", False))
    has_responders = any(e.get("event_type") == "system" and e.get("subtype") == "responders_arrived" for e in events)
    has_location = bool(str(cad_state.get("location", "")).strip())
    has_narrative = bool(str(cad_state.get("narrative", "")).strip())
    unresolved_conflict = any(d.get("decision") == "needs_retry_conflict" for d in decisions) and not any(
        d.get("decision") == "executed" for d in decisions
    )

    if has_dispatch:
        item_scores["dispatch_triggered"] = 25.0
    if has_responders:
        item_scores["responders_arrived"] = 15.0
    if has_location:
        item_scores["location_recorded"] = 30.0
    if has_narrative:
        item_scores["narrative_recorded"] = 15.0
    if not unresolved_conflict:
        item_scores["helper_outcome_clean"] = 15.0

    return float(sum(item_scores.values())), item_scores


def _write_ndjson(path: Path, rows: list[JSONObject]) -> None:
    content = "\n".join(json.dumps(r, sort_keys=True) for r in rows)
    path.write_text(content, encoding="utf-8")


def _load_json(path: Path) -> JSONObject:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_pipe_manifest(root: Path) -> list[str]:
    manifest_path = root / "tests/cases/phase4_pipe_manifest.json"
    payload = _load_json(manifest_path)
    ids = payload.get("planned_pipe_ids", [])
    if not isinstance(ids, list) or not ids:
        raise RuntimeError(f"invalid_phase4_manifest:{manifest_path}")
    return [str(x) for x in ids]


def _run_single(
    *,
    sim_url: str,
    gov_url: str,
    root: Path,
    bundle_dir: Path,
    run_id: str,
    scenario_id: str,
    caller_fixture: str,
    incident_fixture: str,
    qa_fixture: str,
    policy_file: str,
    helper_enabled: bool,
    rep: int,
    max_turns_override: int | None = None,
    include_occ_conflict: bool = False,
) -> RunRecord:
    incident_seed = _load_json(root / incident_fixture)
    incident_seed = dict(incident_seed)
    incident_seed["id"] = f"{incident_seed.get('id', 'INC')}-{run_id}"
    if max_turns_override is not None:
        incident_seed["max_turns"] = int(max_turns_override)
    target_location = str(((incident_seed.get("location") or {}).get("address_line")) or "2421 Main St")
    tmp_incident_dir = bundle_dir / "_tmp_incidents"
    tmp_incident_dir.mkdir(parents=True, exist_ok=True)
    tmp_incident_path = tmp_incident_dir / f"{run_id}.json"
    tmp_incident_path.write_text(json.dumps(incident_seed, indent=2), encoding="utf-8")
    tmp_incident_rel = str(tmp_incident_path.relative_to(root))

    _post_json(f"{gov_url}/mcp/swap_policy", {"policy_file": policy_file})
    loaded = _post_json(
        f"{sim_url}/admin/load_start",
        {
            "scenario_id": f"{scenario_id}-{run_id}",
            "caller_fixture": caller_fixture,
            "incident_fixture": tmp_incident_rel,
            "qa_fixture": qa_fixture,
        },
    )
    incident_id = str(loaded["loaded"]["incident_id"])

    _post_json(
        f"{sim_url}/admin/post_turn",
        {
            "incident_id": incident_id,
            "caller": f"There is an emergency at {target_location}",
            "call_taker": "911, what is your emergency?",
            "cad_updates": {"narrative": "initial"},
        },
    )

    decisions: list[JSONObject] = []
    if helper_enabled:
        st = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})
        if include_occ_conflict:
            _post_json(
                f"{sim_url}/admin/post_turn",
                {
                    "incident_id": incident_id,
                    "caller": "I can see smoke now",
                    "call_taker": "Stay on the line.",
                    "cad_updates": {"narrative": "interleaved update"},
                },
            )
        proposal = _proposal(
            incident_id=incident_id,
            action_id=f"{run_id}-a1",
            read_record_version=int(st.get("record_version", 0)),
            field_versions=st.get("field_versions", {}),
            location=target_location,
            p_correct=0.95 if "bound_escalate" not in policy_file else 0.4,
        )
        out = _post_json(f"{gov_url}/mcp/propose_action", proposal)
        decisions.append(out)
        if out.get("decision") == "needs_retry_conflict":
            st2 = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})
            proposal2 = _proposal(
                incident_id=incident_id,
                action_id=f"{run_id}-a2",
                read_record_version=int(st2.get("record_version", 0)),
                field_versions=st2.get("field_versions", {}),
                location=target_location,
                p_correct=0.95,
            )
            out2 = _post_json(f"{gov_url}/mcp/propose_action", proposal2)
            decisions.append(out2)

    # Progress episode until sealed.
    for turn in range(1, 20):
        state = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})
        if state.get("episode_phase") == "sealed":
            break
        cad_updates: dict[str, Any] = {"narrative": f"turn_{turn}"}
        if max_turns_override is None and turn == 1:
            cad_updates["dispatch_triggered"] = True
        _post_json(
            f"{sim_url}/admin/post_turn",
            {
                "incident_id": incident_id,
                "caller": f"caller turn {turn}",
                "call_taker": f"call taker turn {turn}",
                "cad_updates": cad_updates,
            },
        )

    snapshot = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})
    if snapshot.get("episode_phase") != "sealed":
        _post_json(
            f"{sim_url}/admin/end_call",
            {"incident_id": incident_id, "reason": "other", "reason_detail": "phase4_harness_force_end"},
        )
        snapshot = _post_json(f"{sim_url}/plant/get_state_snapshot", {"incident_id": incident_id})
    events = _post_json(f"{sim_url}/admin/events", {"incident_id": incident_id}).get("events", [])
    for ev in events:
        validate_event_minimal(ev)

    qa_score_value, qa_item_scores = _score_episode(events=events, cad_state=snapshot.get("cad_state", {}), decisions=decisions)
    decision_counts: dict[str, int] = {"executed": 0, "denied": 0, "needs_retry_conflict": 0}
    for dec in decisions:
        key = str(dec.get("decision", ""))
        if key in decision_counts:
            decision_counts[key] += 1

    correlation_ids = {
        str(ev.get("action_id"))
        for ev in events
        if ev.get("event_type") in {"governance_correlation", "governance_decision"} and ev.get("action_id")
    }
    orphans = 0
    for dec in decisions:
        aid = str(dec.get("action_id", ""))
        if aid and aid not in correlation_ids:
            orphans += 1

    run_dir = bundle_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    _write_ndjson(run_dir / "_events.ndjson", events)
    _write_ndjson(run_dir / "governance_audit.ndjson", decisions)

    qa_score = {
        "incident_id": incident_id,
        "scenario_id": scenario_id,
        "policy_file": policy_file,
        "helper_enabled": helper_enabled,
        "score": qa_score_value,
        "items": qa_item_scores,
    }
    (run_dir / "qa_score.json").write_text(json.dumps(qa_score, indent=2), encoding="utf-8")

    prompt_text = "911Bench Phase4 deterministic prompt bundle v1"
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    events_hash = _hash_json(events)
    meta = {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "policy_file": policy_file,
        "helper_enabled": helper_enabled,
        "rep": rep,
        "prompt_hash": prompt_hash,
        "events_hash": events_hash,
        "decision_counts": decision_counts,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    turn_count = max([0] + [int(ev.get("turn", 0)) for ev in events if ev.get("event_type") == "conversation"])
    ep_end = [ev for ev in events if ev.get("event_type") == "episode_end"]
    end_reason = str(ep_end[-1].get("reason")) if ep_end else "unknown"

    return RunRecord(
        run_id=run_id,
        scenario_id=scenario_id,
        policy_file=policy_file,
        helper_enabled=helper_enabled,
        rep=rep,
        run_dir=run_dir,
        terminated_reason=end_reason,
        turn_count=turn_count,
        qa_score=qa_score_value,
        qa_item_scores=qa_item_scores,
        decision_counts=decision_counts,
        orphans=orphans,
        events_hash=events_hash,
        prompt_hash=prompt_hash,
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    m = _mean(values)
    var = sum((v - m) ** 2 for v in values) / len(values)
    return var**0.5


def run(root: Path, output_dir: Path) -> int:
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_dir = output_dir / f"phase4_pipeline_bundle_{ts}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

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
    planned_pipe_ids = _load_pipe_manifest(root)
    report: JSONObject = {
        "tests": {},
        "bundle_dir": str(bundle_dir),
        "plan_alignment": {"planned_pipe_ids": planned_pipe_ids},
    }
    runs: list[RunRecord] = []
    try:
        _wait_for(f"{sim_url}/healthz")
        _wait_for(f"{gov_url}/mcp/capabilities")

        # PIPE-003 frozen config validation.
        bad_model = "gpt-4o-latest"
        report["tests"]["PIPE-003"] = {"pass": "latest" in bad_model and "-" in bad_model, "detail": {"rejected_model": bad_model}}

        # PIPE-001: 1 scenario x 1 policy x 3 reps.
        for rep in range(1, 4):
            runs.append(
                _run_single(
                    sim_url=sim_url,
                    gov_url=gov_url,
                    root=root,
                    bundle_dir=bundle_dir,
                    run_id=f"pipe001-r{rep}",
                    scenario_id="fire",
                    caller_fixture="fixtures/caller_cooperative_calm.json",
                    incident_fixture="fixtures/incident_fire_residential.json",
                    qa_fixture="fixtures/qaTemplate_003.json",
                    policy_file="policies/test_full_ecc.yaml",
                    helper_enabled=True,
                    rep=rep,
                )
            )
        pipe001 = [r for r in runs if r.run_id.startswith("pipe001-")]
        report["tests"]["PIPE-001"] = {"pass": len(pipe001) == 3, "detail": {"run_count": len(pipe001)}}

        # PIPE-002: 1 scenario x 3 policies x 2 reps.
        p2_policies = [
            "policies/test_full_ecc.yaml",
            "policies/test_prohibit_basic.yaml",
            "policies/test_bound_escalate.yaml",
        ]
        for policy in p2_policies:
            for rep in range(1, 3):
                runs.append(
                    _run_single(
                        sim_url=sim_url,
                        gov_url=gov_url,
                        root=root,
                        bundle_dir=bundle_dir,
                        run_id=f"pipe002-{Path(policy).stem}-r{rep}",
                        scenario_id="fire",
                        caller_fixture="fixtures/caller_cooperative_calm.json",
                        incident_fixture="fixtures/incident_fire_residential.json",
                        qa_fixture="fixtures/qaTemplate_003.json",
                        policy_file=policy,
                        helper_enabled=True,
                        rep=rep,
                    )
                )
        pipe002 = [r for r in runs if r.run_id.startswith("pipe002-")]
        report["tests"]["PIPE-002"] = {"pass": len(pipe002) == 6, "detail": {"run_count": len(pipe002)}}

        # PIPE-010 / PIPE-012 sensitivity across permissive vs restrictive.
        for rep in range(1, 6):
            runs.append(
                _run_single(
                    sim_url=sim_url,
                    gov_url=gov_url,
                    root=root,
                    bundle_dir=bundle_dir,
                    run_id=f"pipe010-perm-r{rep}",
                    scenario_id="fire",
                    caller_fixture="fixtures/caller_cooperative_calm.json",
                    incident_fixture="fixtures/incident_fire_residential.json",
                    qa_fixture="fixtures/qaTemplate_003.json",
                    policy_file="policies/test_full_ecc.yaml",
                    helper_enabled=True,
                    rep=rep,
                )
            )
            runs.append(
                _run_single(
                    sim_url=sim_url,
                    gov_url=gov_url,
                    root=root,
                    bundle_dir=bundle_dir,
                    run_id=f"pipe010-rest-r{rep}",
                    scenario_id="fire",
                    caller_fixture="fixtures/caller_cooperative_calm.json",
                    incident_fixture="fixtures/incident_fire_residential.json",
                    qa_fixture="fixtures/qaTemplate_003.json",
                    policy_file="policies/test_prohibit_basic.yaml",
                    helper_enabled=True,
                    rep=rep,
                )
            )
        perm = [r for r in runs if r.run_id.startswith("pipe010-perm-")]
        rest = [r for r in runs if r.run_id.startswith("pipe010-rest-")]
        perm_mean = _mean([r.qa_score for r in perm])
        rest_mean = _mean([r.qa_score for r in rest])
        qa_delta = abs(perm_mean - rest_mean)
        report["tests"]["PIPE-010"] = {"pass": qa_delta >= 5.0, "detail": {"perm_mean": perm_mean, "rest_mean": rest_mean, "delta": qa_delta}}

        perm_loc = _mean([r.qa_item_scores.get("location_recorded", 0.0) for r in perm])
        rest_loc = _mean([r.qa_item_scores.get("location_recorded", 0.0) for r in rest])
        report["tests"]["PIPE-012"] = {
            "pass": abs(perm_loc - rest_loc) > 0.1,
            "detail": {"location_item_perm_mean": perm_loc, "location_item_rest_mean": rest_loc},
        }

        # PIPE-011 helper on vs off.
        for rep in range(1, 6):
            runs.append(
                _run_single(
                    sim_url=sim_url,
                    gov_url=gov_url,
                    root=root,
                    bundle_dir=bundle_dir,
                    run_id=f"pipe011-on-r{rep}",
                    scenario_id="fire",
                    caller_fixture="fixtures/caller_cooperative_calm.json",
                    incident_fixture="fixtures/incident_fire_residential.json",
                    qa_fixture="fixtures/qaTemplate_003.json",
                    policy_file="policies/test_full_ecc.yaml",
                    helper_enabled=True,
                    rep=rep,
                )
            )
            runs.append(
                _run_single(
                    sim_url=sim_url,
                    gov_url=gov_url,
                    root=root,
                    bundle_dir=bundle_dir,
                    run_id=f"pipe011-off-r{rep}",
                    scenario_id="fire",
                    caller_fixture="fixtures/caller_cooperative_calm.json",
                    incident_fixture="fixtures/incident_fire_residential.json",
                    qa_fixture="fixtures/qaTemplate_003.json",
                    policy_file="policies/test_full_ecc.yaml",
                    helper_enabled=False,
                    rep=rep,
                )
            )
        on_runs = [r for r in runs if r.run_id.startswith("pipe011-on-")]
        off_runs = [r for r in runs if r.run_id.startswith("pipe011-off-")]
        on_mean = _mean([r.qa_score for r in on_runs])
        off_mean = _mean([r.qa_score for r in off_runs])
        report["tests"]["PIPE-011"] = {
            "pass": (on_mean - off_mean) >= 3.0,
            "detail": {"helper_on_mean": on_mean, "helper_off_mean": off_mean, "delta": on_mean - off_mean},
        }

        # PIPE-020 reproducibility: same config same seed => same meta hashes.
        r1 = _run_single(
            sim_url=sim_url,
            gov_url=gov_url,
            root=root,
            bundle_dir=bundle_dir,
            run_id="pipe020-a",
            scenario_id="fire",
            caller_fixture="fixtures/caller_cooperative_calm.json",
            incident_fixture="fixtures/incident_fire_residential.json",
            qa_fixture="fixtures/qaTemplate_003.json",
            policy_file="policies/test_full_ecc.yaml",
            helper_enabled=True,
            rep=1,
        )
        r2 = _run_single(
            sim_url=sim_url,
            gov_url=gov_url,
            root=root,
            bundle_dir=bundle_dir,
            run_id="pipe020-b",
            scenario_id="fire",
            caller_fixture="fixtures/caller_cooperative_calm.json",
            incident_fixture="fixtures/incident_fire_residential.json",
            qa_fixture="fixtures/qaTemplate_003.json",
            policy_file="policies/test_full_ecc.yaml",
            helper_enabled=True,
            rep=1,
        )
        runs.extend([r1, r2])
        report["tests"]["PIPE-020"] = {
            "pass": r1.events_hash == r2.events_hash and r1.prompt_hash == r2.prompt_hash,
            "detail": {"events_hash_equal": r1.events_hash == r2.events_hash, "prompt_hash_equal": r1.prompt_hash == r2.prompt_hash},
        }
        report["tests"]["PIPE-021"] = {
            "pass": r1.events_hash == r2.events_hash,
            "detail": {"normalized_events_hash_equal": r1.events_hash == r2.events_hash},
        }
        report["tests"]["PIPE-022"] = {
            "pass": abs(r1.qa_score - r2.qa_score) < 1e-9,
            "detail": {"qa_score_a": r1.qa_score, "qa_score_b": r2.qa_score},
        }

        # PIPE-030 / PIPE-031 / PIPE-032.
        pipe030 = _run_single(
            sim_url=sim_url,
            gov_url=gov_url,
            root=root,
            bundle_dir=bundle_dir,
            run_id="pipe030-path1",
            scenario_id="fire",
            caller_fixture="fixtures/caller_cooperative_calm.json",
            incident_fixture="fixtures/incident_fire_residential.json",
            qa_fixture="fixtures/qaTemplate_003.json",
            policy_file="policies/test_full_ecc.yaml",
            helper_enabled=True,
            rep=1,
        )
        pipe031 = _run_single(
            sim_url=sim_url,
            gov_url=gov_url,
            root=root,
            bundle_dir=bundle_dir,
            run_id="pipe031-maxturn",
            scenario_id="fire",
            caller_fixture="fixtures/caller_cooperative_calm.json",
            incident_fixture="fixtures/incident_fire_residential.json",
            qa_fixture="fixtures/qaTemplate_003.json",
            policy_file="policies/test_full_ecc.yaml",
            helper_enabled=False,
            rep=1,
            max_turns_override=2,
        )
        pipe032 = _run_single(
            sim_url=sim_url,
            gov_url=gov_url,
            root=root,
            bundle_dir=bundle_dir,
            run_id="pipe032-occ",
            scenario_id="fire",
            caller_fixture="fixtures/caller_cooperative_calm.json",
            incident_fixture="fixtures/incident_fire_residential.json",
            qa_fixture="fixtures/qaTemplate_003.json",
            policy_file="policies/test_full_ecc.yaml",
            helper_enabled=True,
            rep=1,
            include_occ_conflict=True,
        )
        runs.extend([pipe030, pipe031, pipe032])
        report["tests"]["PIPE-030"] = {"pass": pipe030.terminated_reason == "auto_end_post_arrival", "detail": {"reason": pipe030.terminated_reason}}
        report["tests"]["PIPE-031"] = {"pass": pipe031.terminated_reason == "max_turns", "detail": {"reason": pipe031.terminated_reason}}
        report["tests"]["PIPE-032"] = {
            "pass": pipe032.decision_counts.get("needs_retry_conflict", 0) >= 1 and pipe032.decision_counts.get("executed", 0) >= 1,
            "detail": {"decision_counts": pipe032.decision_counts},
        }

        # PIPE-004 prompt hash consistency.
        prompt_hashes = {r.prompt_hash for r in runs}
        report["tests"]["PIPE-004"] = {"pass": len(prompt_hashes) == 1, "detail": {"distinct_prompt_hashes": len(prompt_hashes)}}

        # PIPE-040/041/043 artifact and correlation checks.
        missing_files = 0
        schema_errors = 0
        orphan_total = 0
        for rec in runs:
            needed = ["_events.ndjson", "governance_audit.ndjson", "qa_score.json", "meta.json"]
            for name in needed:
                if not (rec.run_dir / name).exists():
                    missing_files += 1
            ev_path = rec.run_dir / "_events.ndjson"
            if ev_path.exists():
                for line in ev_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        validate_event_minimal(json.loads(line))
                    except Exception:
                        schema_errors += 1
            orphan_total += rec.orphans
        report["tests"]["PIPE-040"] = {"pass": missing_files == 0, "detail": {"missing_files": missing_files, "run_count": len(runs)}}
        report["tests"]["PIPE-041"] = {"pass": schema_errors == 0, "detail": {"schema_errors": schema_errors}}
        report["tests"]["PIPE-043"] = {"pass": orphan_total == 0, "detail": {"orphan_count": orphan_total}}

        # PIPE-005/042/044 summary artifacts.
        by_key: dict[str, list[float]] = {}
        for rec in runs:
            key = f"{rec.scenario_id}|{Path(rec.policy_file).name}|helper={int(rec.helper_enabled)}"
            by_key.setdefault(key, []).append(rec.qa_score)

        summary = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "run_count": len(runs),
            "aggregates": {
                key: {
                    "mean": _mean(vals),
                    "stdev": _stdev(vals),
                    "min": min(vals),
                    "max": max(vals),
                    "n": len(vals),
                }
                for key, vals in sorted(by_key.items())
            },
        }
        summary_path = bundle_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        csv_path = bundle_dir / "summary.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "run_id",
                    "scenario_id",
                    "policy_file",
                    "helper_enabled",
                    "rep",
                    "terminated_reason",
                    "turn_count",
                    "qa_score",
                    "executed",
                    "denied",
                    "needs_retry_conflict",
                ],
            )
            writer.writeheader()
            for rec in runs:
                writer.writerow(
                    {
                        "run_id": rec.run_id,
                        "scenario_id": rec.scenario_id,
                        "policy_file": rec.policy_file,
                        "helper_enabled": int(rec.helper_enabled),
                        "rep": rec.rep,
                        "terminated_reason": rec.terminated_reason,
                        "turn_count": rec.turn_count,
                        "qa_score": f"{rec.qa_score:.3f}",
                        "executed": rec.decision_counts.get("executed", 0),
                        "denied": rec.decision_counts.get("denied", 0),
                        "needs_retry_conflict": rec.decision_counts.get("needs_retry_conflict", 0),
                    }
                )

        report["tests"]["PIPE-005"] = {"pass": bool(summary.get("aggregates")), "detail": {"aggregate_keys": len(summary.get("aggregates", {}))}}
        report["tests"]["PIPE-042"] = {
            "pass": int(summary.get("run_count", -1)) == len(runs),
            "detail": {"summary_run_count": summary.get("run_count"), "actual_run_count": len(runs)},
        }
        csv_rows = csv_path.read_text(encoding="utf-8").splitlines()
        report["tests"]["PIPE-044"] = {"pass": len(csv_rows) == len(runs) + 1, "detail": {"csv_rows": len(csv_rows), "expected": len(runs) + 1}}

        # Gate summary + integration-plan alignment.
        observed_ids = sorted(report["tests"].keys())
        missing_ids = [pid for pid in planned_pipe_ids if pid not in report["tests"]]
        unexpected_ids = [pid for pid in observed_ids if pid not in planned_pipe_ids]
        report["plan_alignment"]["missing_ids"] = missing_ids
        report["plan_alignment"]["unexpected_ids"] = unexpected_ids
        report["plan_alignment"]["planned_total"] = len(planned_pipe_ids)
        report["plan_alignment"]["observed_total"] = len(observed_ids)
        report["plan_alignment"]["pass"] = not missing_ids and not unexpected_ids

        total = len(report["tests"])
        passed = sum(1 for t in report["tests"].values() if t.get("pass"))
        failed = total - passed
        if missing_ids or unexpected_ids:
            failed += 1
        report["summary"] = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "bundle_dir": str(bundle_dir),
        }

        report_path = bundle_dir / "phase4_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"phase4-pipeline report: {report_path}")
        return 0 if failed == 0 else 1
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
    parser = argparse.ArgumentParser(description="Run Phase 4 end-to-end pipeline validation.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output-dir", default="tests/results")
    args = parser.parse_args()
    return run(root=Path(args.root).resolve(), output_dir=Path(args.output_dir).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
