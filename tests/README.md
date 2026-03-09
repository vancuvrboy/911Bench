# Governance Standalone Harness

Run:

```bash
python3 -m tests.harness.runner --root . --cases-dir tests/cases --output-dir tests/results
```

Outputs:
- `tests/results/governance_harness_results.json`
- `tests/results/governance_harness_summary.csv`

WP1 MCP validation bundle (integration tests + descriptor smoke):

```bash
python3 -m tests.harness.wp1_validation --root . --output-dir tests/results
```

Northbound conformance matrix (Python SDK vs raw HTTP client):

```bash
python3 -m tests.harness.conformance_matrix --root . --output-dir tests/results
```

Southbound transport hardening tests:

```bash
python3 -m unittest tests.test_southbound_security
```

Southbound resilience tests:

```bash
python3 -m unittest tests.test_southbound_resilience
```

Runtime persistence tests:

```bash
python3 -m unittest tests.test_state_store
```

Performance and deterministic replay quality gate:

```bash
python3 -m tests.harness.quality_gate --report tests/results/governance_harness_report.json --max-p95-ms 200
```

Phase 3 smoke (governance server wired to real SIM southbound adapter):

```bash
python3 -m tests.harness.phase3_smoke --root . --output tests/results/phase3_smoke_report.json
```

Phase 3 INT southbound block (`INT-001..INT-006`):

```bash
python3 -m tests.harness.phase3_int --root . --output tests/results/phase3_int_report.json
```

Phase 3 INT checkpoint block (`INT-010..INT-015`):

```bash
python3 -m tests.harness.phase3_checkpoint_int --root . --output tests/results/phase3_checkpoint_int_report.json
```

Phase 4 end-to-end pipeline block (planned 19 PIPE IDs from v2.1 manifest):

```bash
python3 -m tests.harness.phase4_pipeline --root . --output-dir tests/results
```

Headless SIM experiment runner (manifest-driven, external output root supported):

```bash
python3 -m tests.harness.experiment_runner \
  --root . \
  --manifest tests/cases/sim_experiment_manifest.sample.json \
  --results-root ../911Bench-Experiments/results
```

Notes:
- `--results-root` can point to a sibling experiments repo.
- Output bundle per run includes:
  - `summary.json`
  - `summary.csv`
  - `manifest.resolved.json`
  - `episodes/*_events.ndjson`
  - `episodes/*_qa_score.json`
  - `episodes/*_meta.json`
