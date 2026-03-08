"""Phase 1 SIM+agents validation runner for SIM-AGT-* integration tests."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import pstdev

from harness.sim_episode_runner import SimEpisodeRunner


def _conversation(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("event_type") == "conversation"]


def _system(events: list[dict], subtype: str | None = None) -> list[dict]:
    rows = [e for e in events if e.get("event_type") == "system"]
    if subtype is not None:
        rows = [e for e in rows if e.get("subtype") == subtype]
    return rows


def _contains_any(text: str, words: list[str]) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


def run(root: Path, output_dir: Path) -> dict:
    live_runner = SimEpisodeRunner(root=root, output_dir=output_dir, mode="live")

    fire = live_runner.run_episode(
        "phase1_fire",
        "fixtures/caller_cooperative_calm.json",
        "fixtures/incident_fire_residential.json",
        "fixtures/qaTemplate_003.json",
    )
    police = live_runner.run_episode(
        "phase1_police",
        "fixtures/caller_cooperative_calm.json",
        "fixtures/incident_police_domestic.json",
        "fixtures/qaTemplate_003.json",
    )
    ems = live_runner.run_episode(
        "phase1_ems",
        "fixtures/caller_cooperative_calm.json",
        "fixtures/incident_ems_cardiac.json",
        "fixtures/qaTemplate_003.json",
    )

    ask_name = live_runner.run_episode(
        "phase1_ask_name",
        "fixtures/caller_on_ask_once.json",
        "fixtures/incident_fire_residential.json",
        "fixtures/qaTemplate_003.json",
        calltaker_config={"ask_name_turn": 2},
    )
    no_ask_name = live_runner.run_episode(
        "phase1_no_ask_name",
        "fixtures/caller_on_ask_once.json",
        "fixtures/incident_fire_residential.json",
        "fixtures/qaTemplate_003.json",
    )
    parse_fail = live_runner.run_episode(
        "phase1_parse_fail",
        "fixtures/caller_cooperative_calm.json",
        "fixtures/incident_fire_residential.json",
        "fixtures/qaTemplate_003.json",
        calltaker_config={"parse_fail_turns": {3}},
    )
    no_dispatch = live_runner.run_episode(
        "phase1_no_dispatch",
        "fixtures/caller_cooperative_calm.json",
        "fixtures/incident_fire_residential.json",
        "fixtures/qaTemplate_003.json",
        calltaker_config={"dispatch_enabled": False, "resolve_no_dispatch_turn": 5},
    )
    max_turns = live_runner.run_episode(
        "phase1_max_turns",
        "fixtures/caller_cooperative_calm.json",
        "fixtures/incident_fire_residential.json",
        "fixtures/qaTemplate_003.json",
        max_turns=5,
        calltaker_config={"dispatch_enabled": False},
    )
    qa_retry = live_runner.run_episode(
        "phase1_qa_retry",
        "fixtures/caller_cooperative_calm.json",
        "fixtures/incident_fire_residential.json",
        "fixtures/qaTemplate_003.json",
        qa_config={"simulate_parse_fail_once": True, "parse_retry_max": 2},
    )

    record_runner = SimEpisodeRunner(root=root, output_dir=output_dir, mode="record")
    replay_runner = SimEpisodeRunner(root=root, output_dir=output_dir, mode="replay")
    rec_fire = record_runner.run_episode("phase1_fire", "fixtures/caller_cooperative_calm.json", "fixtures/incident_fire_residential.json", "fixtures/qaTemplate_003.json")
    rep_fire = replay_runner.run_episode("phase1_fire", "fixtures/caller_cooperative_calm.json", "fixtures/incident_fire_residential.json", "fixtures/qaTemplate_003.json")

    fire_conv = _conversation(fire["events"])
    ask_conv = _conversation(ask_name["events"])
    noask_conv = _conversation(no_ask_name["events"])

    reps = [
        live_runner.run_episode("phase1_fire", "fixtures/caller_cooperative_calm.json", "fixtures/incident_fire_residential.json", "fixtures/qaTemplate_003.json")["qa_score"]["normalized_score"]
        for _ in range(5)
    ]

    checks = {
        "SIM-AGT-001": bool(fire_conv and _contains_any(fire_conv[0].get("caller", ""), ["fire", "main"])),
        "SIM-AGT-002": (
            (lambda ask_turn: ask_turn is not None and any(
                int(c.get("turn", 0)) > ask_turn and "my name" in c.get("caller", "").lower() for c in ask_conv
            ))(
                next((int(c.get("turn", 0)) for c in ask_conv if "name" in c.get("call_taker", "").lower()), None)
            )
        ),
        "SIM-AGT-003": all("morgan reed" not in c.get("caller", "").lower() for c in noask_conv),
        "SIM-AGT-004": any("spreading" in c.get("caller", "").lower() for c in fire_conv),
        "SIM-AGT-005": any("um" in c.get("caller", "").lower() for c in fire_conv),
        "SIM-AGT-006": any("responders" in c.get("caller", "").lower() for c in fire_conv[-2:]),
        # Metadata schema currently allows sanitized provider metadata or null.
        # Validate contract-level presence/type, not semantic emotional-state extraction.
        "SIM-AGT-007": bool(fire_conv) and all(
            ("caller_metadata" in c) and (c.get("caller_metadata") is None or isinstance(c.get("caller_metadata"), dict))
            for c in fire_conv
        ),
        "SIM-AGT-010": bool(fire_conv and fire_conv[0].get("call_taker") == "911, what is your emergency?"),
        "SIM-AGT-011": fire.get("dispatch_turn") is not None and fire.get("dispatch_turn") >= 2,
        "SIM-AGT-012": (fire.get("dispatch_turn") or 999) <= 4,
        "SIM-AGT-013": any(_contains_any(c.get("call_taker", ""), ["occupants", "hazards", "exposures"]) for c in fire_conv),
        "SIM-AGT-014": len(_system(fire["events"], "parse_error")) == 0,
        "SIM-AGT-015": len(_system(parse_fail["events"], "parse_error")) >= 1 and len(_conversation(parse_fail["events"])) >= 4,
        "SIM-AGT-016": fire.get("termination_reason") == "responders_arrived",
        "SIM-AGT-017": fire.get("turn_count", 0) >= 3 and fire.get("dispatch_turn") is not None,
        "SIM-AGT-020": isinstance(fire.get("qa_score"), dict) and "normalized_score" in fire["qa_score"],
        "SIM-AGT-021": fire["qa_score"].get("sections_applied") == ["COMMON", "FIRE"],
        "SIM-AGT-022": len(fire["qa_score"].get("items", [])) >= 1,
        "SIM-AGT-023": abs(fire["qa_score"]["normalized_score"] - (fire["qa_score"]["total_points_awarded"] / fire["qa_score"]["total_points_possible"] * 100)) < 1e-9,
        "SIM-AGT-024": 0 < qa_retry["qa_score"].get("parse_retry_count", 0) <= 2,
        "SIM-AGT-025": fire["qa_score"] == replay_runner.run_episode("phase1_fire", "fixtures/caller_cooperative_calm.json", "fixtures/incident_fire_residential.json", "fixtures/qaTemplate_003.json")["qa_score"],
        "SIM-AGT-030": fire["schema_valid"],
        "SIM-AGT-031": police["schema_valid"],
        "SIM-AGT-032": ems["schema_valid"],
        "SIM-AGT-033": pstdev(reps) <= 5.0,
        "SIM-AGT-034": rec_fire["normalized_events_hash"] == rep_fire["normalized_events_hash"],
        "SIM-AGT-035": {
            fire.get("termination_reason"),
            no_dispatch.get("termination_reason"),
            max_turns.get("termination_reason"),
        } == {"responders_arrived", "resolved_no_dispatch", "max_turns"},
    }

    cases = json.loads((root / "tests" / "cases" / "phase1_agent_cases.json").read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        tid = case["test_id"]
        rows.append({
            "test_id": tid,
            "category": case["category"],
            "description": case["description"],
            "passed": bool(checks.get(tid, False)),
        })

    summary = {
        "total": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "failed": sum(1 for r in rows if not r["passed"]),
        "results": rows,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sim_phase1_validation.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (output_dir / "sim_phase1_validation.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["test_id", "category", "description", "passed"])
        writer.writeheader()
        writer.writerows(rows)

    return summary


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run SIM Phase 1 agent validation suite")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output-dir", default="tests/reports")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    out = (root / args.output_dir).resolve()
    summary = run(root, out)
    print(f"SIM Phase 1 validation: passed {summary['passed']}/{summary['total']}")
    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
