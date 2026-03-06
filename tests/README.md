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
