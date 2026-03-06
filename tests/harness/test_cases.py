"""Pytest entrypoint for governance harness regression suite."""

from __future__ import annotations

import json
from pathlib import Path

from tests.harness.runner import Harness


ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = ROOT / "tests" / "cases"
OUTPUT_DIR = ROOT / "tests" / "results"


def test_harness_suite_passes() -> None:
    harness = Harness(ROOT, OUTPUT_DIR)
    results = harness.run(CASES_DIR, include_categories=None)
    failures = [r for r in results if not r.passed]
    assert not failures, json.dumps([f.__dict__ for f in failures], indent=2)
