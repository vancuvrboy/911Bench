"""Standalone governance enforcement validation harness."""

from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gov_server.enforcement import Engine
from gov_server.policy_loader import PolicyLoader
from gov_server.predicates import PredicateEngine
from gov_server.shims import CheckpointResponse, CheckpointShim, PlantStateShim


@dataclass
class CaseResult:
    test_id: str
    category: str
    passed: bool
    duration_ms: int
    discrepancy: str


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root

    def run(self, cases_dir: Path, output_dir: Path) -> list[CaseResult]:
        case_files = sorted(cases_dir.glob("*.json"))
        results: list[CaseResult] = []

        for case_file in case_files:
            data = json.loads(case_file.read_text(encoding="utf-8"))
            case_list = data if isinstance(data, list) else [data]
            for case in case_list:
                results.append(self._run_case(case))

        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_outputs(results, output_dir)
        return results

    def _run_case(self, case: dict[str, Any]) -> CaseResult:
        start = time.perf_counter()
        discrepancy = ""
        passed = False

        try:
            outcome, checkpoint = self._execute_case_once(case)
            mismatch = self._compare_expected(outcome, case.get("expected", {}), checkpoint)
            if mismatch:
                discrepancy = mismatch
            else:
                passed = True

            if passed and case.get("deterministic", False):
                replay, _ = self._execute_case_once(case)
                if json.dumps(self._normalize_outcome(outcome), sort_keys=True) != json.dumps(
                    self._normalize_outcome(replay), sort_keys=True
                ):
                    passed = False
                    discrepancy = "determinism_failed"

        except Exception as exc:  # pragma: no cover - harness should keep running
            expected_error = case.get("expected", {}).get("policy_error")
            if expected_error and expected_error in str(exc):
                passed = True
            else:
                discrepancy = f"error:{exc}"

        duration_ms = int((time.perf_counter() - start) * 1000)
        return CaseResult(
            test_id=case.get("test_id", "unknown"),
            category=case.get("category", "unknown"),
            passed=passed,
            duration_ms=duration_ms,
            discrepancy=discrepancy,
        )

    def _execute_case_once(self, case: dict[str, Any]) -> tuple[dict[str, Any], CheckpointShim]:
        mode = case.get("checkpoint_shim", "auto_approve")
        shim_args = case.get("checkpoint_shim_args", {})
        scripted = [CheckpointResponse(**item) for item in shim_args.get("scripted", [])]
        checkpoint = CheckpointShim(
            mode=mode,
            denial_reason=shim_args.get("denial_reason", "denied_by_shim"),
            edit_fn=(lambda p: shim_args.get("edited_payload", p)),
            scripted=scripted,
        )

        plant_cfg = case.get("plant_state", {})
        plant = PlantStateShim(
            cad_state=plant_cfg.get("cad_state", {}),
            record_version=plant_cfg.get("record_version", 0),
            field_versions=plant_cfg.get("field_versions", {}),
        )

        predicate_engine = PredicateEngine()
        loader = PolicyLoader(predicate_engine)
        bundle = loader.load_bundle(
            self.root / case["policy_file"],
            self.root / case["registry_file"],
            self.root / case.get("evidence_config_file", "policies/domain_evidence_config.yaml"),
        )
        engine = Engine(bundle, plant=plant, checkpoint=checkpoint, predicate_engine=predicate_engine)

        outcome = engine.propose_action(
            case["action_proposal"],
            context_snapshot=case.get("context_snapshot", {"transcript_turns": [1, 2, 3, 4], "sop_ids": ["fire-res-v2"]}),
        )
        return outcome, checkpoint

    @staticmethod
    def _compare_expected(outcome: dict[str, Any], expected: dict[str, Any], checkpoint: CheckpointShim) -> str:
        if expected.get("decision") and outcome.get("decision") != expected["decision"]:
            return f"decision_mismatch:{outcome.get('decision')}!= {expected['decision']}"

        if expected.get("denial_rule_id") and outcome.get("denial_rule_id") != expected["denial_rule_id"]:
            return "denial_rule_id_mismatch"

        if "audit_emitted" in expected:
            trace_steps = [step["step"] for step in outcome.get("enforcement_trace", [])]
            has_audit = "audit" in trace_steps
            if bool(expected["audit_emitted"]) != has_audit:
                return "audit_mismatch"

        if "checkpoint_invoked" in expected:
            invoked = bool(checkpoint.invocations)
            if bool(expected["checkpoint_invoked"]) != invoked:
                return "checkpoint_invocation_mismatch"

        if "escalation_invoked" in expected:
            invoked = outcome.get("escalation") is not None
            if bool(expected["escalation_invoked"]) != invoked:
                return "escalation_invocation_mismatch"

        required_trace = expected.get("enforcement_trace_steps", [])
        if required_trace:
            actual = [(s.get("step"), s.get("result")) for s in outcome.get("enforcement_trace", [])]
            cursor = 0
            for item in required_trace:
                target = (item.get("step"), item.get("result"))
                while cursor < len(actual) and actual[cursor] != target:
                    cursor += 1
                if cursor >= len(actual):
                    return f"missing_trace_step:{target[0]}:{target[1]}"
                cursor += 1

        return ""

    @staticmethod
    def _write_outputs(results: list[CaseResult], output_dir: Path) -> None:
        result_json = output_dir / "governance_harness_results.json"
        summary_csv = output_dir / "governance_harness_summary.csv"

        payload = {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
            "results": [r.__dict__ for r in results],
        }
        result_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        with summary_csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["test_id", "category", "passed", "duration_ms", "discrepancy"])
            writer.writeheader()
            for row in results:
                writer.writerow(row.__dict__)

    @staticmethod
    def _normalize_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
        obj = json.loads(json.dumps(outcome))
        obj["audit_ref"] = "<normalized>"
        for step in obj.get("enforcement_trace", []):
            step["duration_ms"] = 0
            detail = step.get("detail")
            if isinstance(detail, dict) and "latency_ms" in detail:
                detail["latency_ms"] = 0
        if isinstance(obj.get("checkpoint"), dict):
            obj["checkpoint"]["request_id"] = "<normalized>"
            obj["checkpoint"]["latency_ms"] = 0
        if isinstance(obj.get("escalation"), dict):
            obj["escalation"]["latency_ms"] = 0
        return obj


def main() -> int:
    parser = argparse.ArgumentParser(description="Run governance server standalone validation harness")
    parser.add_argument("--cases-dir", default="tests/cases")
    parser.add_argument("--output-dir", default="tests/results")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    harness = Harness(Path(args.root).resolve())
    results = harness.run(Path(args.cases_dir), Path(args.output_dir))
    failed = [item for item in results if not item.passed]

    print(f"Ran {len(results)} tests: {len(results) - len(failed)} passed, {len(failed)} failed")
    if failed:
        for item in failed:
            print(f"- {item.test_id}: {item.discrepancy}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
