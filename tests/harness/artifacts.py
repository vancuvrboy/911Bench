"""Per-test artifact persistence for governance harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_case_artifacts(
    run_dir: Path,
    test_id: str,
    case: dict[str, Any],
    outcome: dict[str, Any] | None,
    discrepancy: str,
    audit_entry: dict[str, Any] | None,
) -> None:
    case_dir = run_dir / test_id
    case_dir.mkdir(parents=True, exist_ok=True)

    (case_dir / "input.json").write_text(json.dumps(case, indent=2), encoding="utf-8")
    if outcome is not None:
        (case_dir / "outcome.json").write_text(json.dumps(outcome, indent=2), encoding="utf-8")
        (case_dir / "trace.json").write_text(
            json.dumps(outcome.get("enforcement_trace", []), indent=2),
            encoding="utf-8",
        )
    if audit_entry is not None:
        (case_dir / "audit_entry.json").write_text(json.dumps(audit_entry, indent=2), encoding="utf-8")

    (case_dir / "assertion.txt").write_text(discrepancy or "pass", encoding="utf-8")
