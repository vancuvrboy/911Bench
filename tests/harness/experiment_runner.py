"""Manifest-driven headless SIM experiment runner with external results targeting."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any

from harness.sim_episode_runner import SimEpisodeRunner


def _resolve_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return (root / path).resolve()


def _load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    if "scenarios" not in data or not isinstance(data["scenarios"], list) or not data["scenarios"]:
        raise ValueError("manifest.scenarios must be a non-empty array")
    return data


def _build_run_id(prefix: str) -> str:
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    clean_prefix = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in prefix).strip("_") or "experiment"
    return f"{clean_prefix}_{stamp}"


def run_experiment(
    *,
    root: Path,
    manifest: dict[str, Any],
    results_root: Path,
    run_id: str | None = None,
) -> dict[str, Any]:
    experiment_id = str(manifest.get("experiment_id", "sim_experiment"))
    resolved_run_id = run_id or _build_run_id(experiment_id)
    run_root = (results_root / resolved_run_id).resolve()
    run_root.mkdir(parents=True, exist_ok=True)

    mode = str(manifest.get("mode", "live"))
    replay_dir = None
    if manifest.get("replay_dir"):
        replay_dir = _resolve_path(root, str(manifest["replay_dir"]))

    runner = SimEpisodeRunner(
        root=root,
        output_dir=run_root,
        mode=mode,
        replay_dir=replay_dir,
        run_subdir="episodes",
    )

    rows: list[dict[str, Any]] = []
    for scenario in manifest["scenarios"]:
        if not isinstance(scenario, dict):
            raise ValueError("each scenario entry must be an object")
        scenario_name = str(scenario["scenario_name"])
        repetitions = int(scenario.get("repetitions", 1))
        if repetitions < 1:
            raise ValueError(f"{scenario_name}: repetitions must be >= 1")

        for rep in range(1, repetitions + 1):
            run_name = f"{scenario_name}-r{rep}" if repetitions > 1 else scenario_name
            outcome = runner.run_episode(
                run_name,
                str(scenario["caller_file"]),
                str(scenario["incident_file"]),
                str(scenario["qa_file"]),
                max_turns=int(scenario.get("max_turns", 20)),
                calltaker_config=scenario.get("calltaker_config"),
                qa_config=scenario.get("qa_config"),
            )
            qa_score = outcome.get("qa_score") or {}
            rows.append(
                {
                    "scenario": run_name,
                    "incident_type": outcome.get("incident_type"),
                    "termination_reason": outcome.get("termination_reason"),
                    "turn_count": int(outcome.get("turn_count", 0)),
                    "dispatch_turn": outcome.get("dispatch_turn"),
                    "schema_valid": bool(outcome.get("schema_valid", False)),
                    "qa_score": qa_score.get("normalized_score"),
                    "mode": mode,
                }
            )

    summary = {
        "experiment_id": experiment_id,
        "run_id": resolved_run_id,
        "mode": mode,
        "results_root": str(results_root),
        "run_root": str(run_root),
        "episodes_dir": str(run_root / "episodes"),
        "total_runs": len(rows),
        "schema_valid_runs": sum(1 for r in rows if r["schema_valid"]),
        "rows": rows,
    }

    (run_root / "manifest.resolved.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (run_root / "summary.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "scenario",
                "incident_type",
                "termination_reason",
                "turn_count",
                "dispatch_turn",
                "schema_valid",
                "qa_score",
                "mode",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run headless SIM experiments from a JSON manifest.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--manifest", required=True, help="Path to experiment manifest JSON")
    parser.add_argument(
        "--results-root",
        required=True,
        help="Output root for experiment results (can be outside repo, e.g., sibling experiments repo).",
    )
    parser.add_argument("--run-id", default="", help="Optional run id. If omitted, generated from experiment_id + UTC timestamp.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    manifest_path = _resolve_path(root, args.manifest)
    results_root = _resolve_path(root, args.results_root)
    manifest = _load_manifest(manifest_path)
    summary = run_experiment(
        root=root,
        manifest=manifest,
        results_root=results_root,
        run_id=(str(args.run_id).strip() or None),
    )
    print(
        "Experiment complete:",
        f"run_id={summary['run_id']}",
        f"total_runs={summary['total_runs']}",
        f"run_root={summary['run_root']}",
    )


if __name__ == "__main__":
    main()
