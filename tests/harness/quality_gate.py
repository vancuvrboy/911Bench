"""Quality gate: enforce latency and deterministic replay requirements."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate governance harness quality gates.")
    parser.add_argument("--report", default="tests/results/governance_harness_report.json")
    parser.add_argument("--max-p95-ms", type=float, default=200.0)
    args = parser.parse_args()

    report_path = Path(args.report).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    failures: list[str] = []
    p95 = float(report.get("latency_ms", {}).get("p95", 0.0))
    if p95 > args.max_p95_ms:
        failures.append(f"p95_exceeded:{p95}>{args.max_p95_ms}")

    det_failures = list(report.get("deterministic_replay", {}).get("failures", []))
    if det_failures:
        failures.append(f"deterministic_replay_failures:{','.join(det_failures)}")

    payload = {
        "ok": len(failures) == 0,
        "p95_ms": p95,
        "max_p95_ms": args.max_p95_ms,
        "deterministic_failures": det_failures,
        "failures": failures,
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
