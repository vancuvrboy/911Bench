"""Standalone governance enforcement validation harness."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gov_server.enforcement import Engine
from gov_server.policy_loader import PolicyLoader
from gov_server.predicates import PredicateEngine
from gov_server.shims import CheckpointResponse, CheckpointShim, PlantStateShim
from tests.harness.artifacts import write_case_artifacts
from tests.harness.assertions import AssertionMismatch, assert_outcome, normalize_for_determinism


@dataclass
class CaseResult:
    test_id: str
    category: str
    passed: bool
    duration_ms: int
    discrepancy: str
    policy_file: str
    decision: str


class Harness:
    def __init__(self, root: Path, output_dir: Path) -> None:
        self.root = root
        self.output_dir = output_dir
        self.run_id = dt.datetime.now().strftime("run_%Y%m%d_%H%M%S")
        self.run_dir = self.output_dir / self.run_id

    def run(self, cases_dir: Path, include_categories: set[str] | None = None) -> list[CaseResult]:
        case_files = sorted(p for p in cases_dir.glob("*.json") if not p.name.endswith(".schema.json"))
        results: list[CaseResult] = []

        self.run_dir.mkdir(parents=True, exist_ok=True)
        for case_file in case_files:
            data = json.loads(case_file.read_text(encoding="utf-8"))
            case_list = data if isinstance(data, list) else [data]
            for case in case_list:
                if not self._is_governance_case(case):
                    # Ignore non-governance manifests (e.g., SIM experiment configs)
                    # that may coexist under tests/cases.
                    continue
                category = case.get("category", "unknown")
                if include_categories and category not in include_categories:
                    continue
                results.append(self._run_case(case))

        self._write_outputs(results)
        return results

    @staticmethod
    def _is_governance_case(case: Any) -> bool:
        if not isinstance(case, dict):
            return False
        mode = case.get("mode")
        if mode == "batch_policy_comparison":
            return isinstance(case.get("batch"), dict) and "registry_file" in case
        required = {"policy_file", "registry_file", "action_proposal"}
        return required.issubset(case.keys())

    def _run_case(self, case: dict[str, Any]) -> CaseResult:
        start = time.perf_counter()
        discrepancy = ""
        passed = False
        outcome: dict[str, Any] | None = None
        decision = "error"
        audit_entry: dict[str, Any] | None = None

        try:
            if case.get("mode") == "batch_policy_comparison":
                outcome = self._execute_batch_policy_comparison(case)
                checkpoint = CheckpointShim(mode="auto_approve")
                engine = None
            else:
                outcome, checkpoint, engine = self._execute_case_once(case)
            decision = outcome.get("decision", "unknown")
            assert_outcome(
                outcome=outcome,
                expected=case.get("expected", {}),
                checkpoint_invoked=bool([inv for inv in checkpoint.invocations if inv.get("source") == "checkpoint"]),
                escalation_invoked=outcome.get("escalation") is not None,
            )
            passed = True

            if case.get("deterministic", False) and case.get("mode") != "batch_policy_comparison":
                replay, _, _ = self._execute_case_once(case)
                if json.dumps(normalize_for_determinism(outcome), sort_keys=True) != json.dumps(
                    normalize_for_determinism(replay), sort_keys=True
                ):
                    passed = False
                    discrepancy = "determinism_failed"

            if engine is not None and outcome is not None and outcome.get("audit_ref"):
                audit_entry = engine.audit_log.get(outcome["audit_ref"])

        except AssertionMismatch as exc:
            discrepancy = str(exc)
            passed = False
        except Exception as exc:  # pragma: no cover - harness should keep running
            expected_error = case.get("expected", {}).get("policy_error")
            if expected_error and expected_error in str(exc):
                passed = True
                decision = "policy_validation_error_expected"
            else:
                discrepancy = f"error:{exc}"
                passed = False

        duration_ms = int((time.perf_counter() - start) * 1000)
        test_id = case.get("test_id", "unknown")

        write_case_artifacts(
            run_dir=self.run_dir,
            test_id=test_id,
            case=case,
            outcome=outcome,
            discrepancy=discrepancy,
            audit_entry=audit_entry,
        )

        return CaseResult(
            test_id=test_id,
            category=case.get("category", "unknown"),
            passed=passed,
            duration_ms=duration_ms,
            discrepancy=discrepancy,
            policy_file=case.get("policy_file", ""),
            decision=decision,
        )

    def _execute_case_once(self, case: dict[str, Any]) -> tuple[dict[str, Any], CheckpointShim, Engine]:
        case = json.loads(json.dumps(case))
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

        custom_predicates = {
            "contains_keyword_urgent": lambda proposal: "urgent" in json.dumps(proposal.get("proposed_payload", {})).lower()
        }
        predicate_engine = PredicateEngine(custom_predicates=custom_predicates)
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
        return outcome, checkpoint, engine

    def _execute_batch_policy_comparison(self, case: dict[str, Any]) -> dict[str, Any]:
        case = json.loads(json.dumps(case))
        batch = case.get("batch", {})
        proposals = batch.get("proposals", [])
        policies = batch.get("policies", [])
        registry_file = case["registry_file"]
        evidence_cfg = case.get("evidence_config_file", "policies/domain_evidence_config.yaml")
        context_snapshot = case.get("context_snapshot", {"transcript_turns": [1, 2, 3, 4], "sop_ids": ["fire-res-v2"]})
        outcomes = ["executed", "denied", "needs_retry_conflict"]

        matrix: dict[str, dict[str, int]] = {policy: {o: 0 for o in outcomes} for policy in policies}
        for policy in policies:
            for idx, proposal in enumerate(proposals):
                proposal_obj = json.loads(json.dumps(proposal))
                proposal_obj["action_id"] = f"{proposal_obj.get('action_id', 'batch')}-{idx}-{Path(policy).stem}"

                checkpoint = CheckpointShim(mode=case.get("checkpoint_shim", "auto_approve"))
                plant = PlantStateShim(
                    cad_state=case.get("plant_state", {}).get("cad_state", {}),
                    record_version=case.get("plant_state", {}).get("record_version", 0),
                    field_versions=case.get("plant_state", {}).get("field_versions", {}),
                )
                predicate_engine = PredicateEngine(
                    custom_predicates={
                        "contains_keyword_urgent": lambda p: "urgent" in json.dumps(p.get("proposed_payload", {})).lower()
                    }
                )
                loader = PolicyLoader(predicate_engine)
                bundle = loader.load_bundle(self.root / policy, self.root / registry_file, self.root / evidence_cfg)
                engine = Engine(bundle, plant=plant, checkpoint=checkpoint, predicate_engine=predicate_engine)
                outcome = engine.propose_action(proposal_obj, context_snapshot=context_snapshot)
                decision = outcome.get("decision")
                if decision in matrix[policy]:
                    matrix[policy][decision] += 1

        chi2, df = chi_square_for_policy_matrix(matrix, outcomes)
        critical = chi_square_critical_05(df)
        significant = chi2 >= critical
        return {
            "action_id": case.get("test_id", "COMP-004"),
            "decision": "executed",
            "denial_reason": None,
            "denial_rule_id": None,
            "degraded_mode_available": False,
            "policy_id": "batch_policy_comparison",
            "policy_hash": "batch_policy_comparison",
            "checkpoint": None,
            "escalation": None,
            "execution": None,
            "conflict_detail": None,
            "audit_ref": "batch_policy_comparison",
            "sim_event_ref": None,
            "enforcement_trace": [
                {
                    "step": "audit",
                    "result": "pass",
                    "detail": {
                        "chi_square": chi2,
                        "df": df,
                        "critical_0_05": critical,
                        "significant": significant,
                        "matrix": matrix,
                    },
                    "duration_ms": 0,
                }
            ],
        }

    def _write_outputs(self, results: list[CaseResult]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        result_json = self.output_dir / "governance_harness_results.json"
        summary_csv = self.output_dir / "governance_harness_summary.csv"
        report_json = self.output_dir / "governance_harness_report.json"

        payload = {
            "run_id": self.run_id,
            "artifacts_dir": str(self.run_dir),
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
            "results": [r.__dict__ for r in results],
        }
        result_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        with summary_csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["test_id", "category", "policy_file", "decision", "passed", "duration_ms", "discrepancy"],
            )
            writer.writeheader()
            for row in results:
                writer.writerow(row.__dict__)

        report = build_report(results)
        report_json.write_text(json.dumps(report, indent=2), encoding="utf-8")


def percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(ordered[int(k)])
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def build_report(results: list[CaseResult]) -> dict[str, Any]:
    by_category: dict[str, dict[str, int]] = {}
    durations = [r.duration_ms for r in results]

    for item in results:
        row = by_category.setdefault(item.category, {"total": 0, "passed": 0, "failed": 0})
        row["total"] += 1
        if item.passed:
            row["passed"] += 1
        else:
            row["failed"] += 1

    policy_decisions: dict[str, dict[str, int]] = {}
    for item in results:
        bucket = policy_decisions.setdefault(item.policy_file, {"executed": 0, "denied": 0, "escalated": 0, "needs_retry_conflict": 0})
        if item.decision in bucket:
            bucket[item.decision] += 1

    return {
        "summary_by_category": by_category,
        "latency_ms": {
            "p50": percentile(durations, 0.50),
            "p95": percentile(durations, 0.95),
            "p99": percentile(durations, 0.99),
            "max": max(durations) if durations else 0,
        },
        "policy_decision_distributions": policy_decisions,
        "deterministic_replay": {
            "tests_marked_deterministic": sum(1 for r in results if r.test_id.startswith(("PREC", "OBLIG", "EVID", "PRED", "CHKPT", "OCC"))),
            "failures": [r.test_id for r in results if r.discrepancy == "determinism_failed"],
        },
    }


def chi_square_for_policy_matrix(matrix: dict[str, dict[str, int]], outcomes: list[str]) -> tuple[float, int]:
    policies = list(matrix.keys())
    rows = len(policies)
    cols = len(outcomes)
    if rows < 2 or cols < 2:
        return 0.0, 0

    row_totals = [sum(matrix[p][o] for o in outcomes) for p in policies]
    col_totals = [sum(matrix[p][o] for p in policies) for o in outcomes]
    grand_total = sum(row_totals)
    if grand_total == 0:
        return 0.0, (rows - 1) * (cols - 1)

    chi2 = 0.0
    for i, policy in enumerate(policies):
        for j, outcome in enumerate(outcomes):
            expected = row_totals[i] * col_totals[j] / grand_total
            if expected <= 0:
                continue
            observed = matrix[policy][outcome]
            chi2 += ((observed - expected) ** 2) / expected
    return chi2, (rows - 1) * (cols - 1)


def chi_square_critical_05(df: int) -> float:
    critical = {
        1: 3.841,
        2: 5.991,
        3: 7.815,
        4: 9.488,
        5: 11.070,
        6: 12.592,
        7: 14.067,
        8: 15.507,
        9: 16.919,
        10: 18.307,
    }
    if df <= 0:
        return 0.0
    return critical.get(df, 18.307)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run governance server standalone validation harness")
    parser.add_argument("--cases-dir", default="tests/cases")
    parser.add_argument("--output-dir", default="tests/results")
    parser.add_argument("--root", default=".")
    parser.add_argument("--category", action="append", default=[])
    args = parser.parse_args()

    harness = Harness(Path(args.root).resolve(), Path(args.output_dir).resolve())
    include_categories = set(args.category) if args.category else None
    results = harness.run(Path(args.cases_dir), include_categories=include_categories)
    failed = [item for item in results if not item.passed]

    print(f"Ran {len(results)} tests: {len(results) - len(failed)} passed, {len(failed)} failed")
    if failed:
        for item in failed:
            print(f"- {item.test_id}: {item.discrepancy}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
