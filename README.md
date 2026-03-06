# Action-Class Authority Framework (ACAF) Verification Repository

This repository contains the reference implementation and standalone validation harness for the Action-Class Authority Framework (ACAF) as operationalized in 911Bench WP0.

The purpose of this repository is to provide a reproducible verification package for the governance framework contribution referenced in the paper's Section IX-A-a evaluation narrative.

## What This Repository Contains

- Governance enforcement engine implementation (`gov_server/`)
- Policy loading and obligation enforcement logic
- Evidence validation and predicate evaluation logic
- Standalone test harness with:
  - checkpoint shim
  - plant state shim (OCC/freshness)
  - assertions and deterministic replay checks
  - per-test artifact output
- Purpose-built policy set and fixtures used for governance validation
- Full category case suites and report outputs

## Validation Objective

The validation suite is designed to verify the compositional properties of ACAF in isolation from the full 911Bench simulation pipeline:

- deterministic operator sequencing and precedence
- obligation enforcement during policy ingestion
- evidence model/type/source/confidence validation
- predicate behavior across operators
- checkpoint and escalation resolution paths
- OCC freshness conflict handling
- behavioral sensitivity across policy configurations

## How Validation Was Performed

The governance engine was exercised directly through a Python harness (no MCP transport for WP0), using structured test cases organized by category:

- operator precedence
- obligation enforcement
- evidence validation
- evidence predicates
- checkpoint and escalation workflows
- freshness and OCC
- policy comparison

The harness compares actual outcomes against expected outcomes, verifies deterministic replay for deterministic tests, and writes detailed artifacts for independent review.

## Reproducibility: Step-by-Step

### 1. Clone the repository

```bash
git clone git@github.com:vancuvrboy/Actio-Class_Authority-Framework.git
cd Actio-Class_Authority-Framework
```

### 2. Run the standalone governance validation harness

```bash
python3 -m tests.harness.runner \
  --root "$(pwd)" \
  --cases-dir "$(pwd)/tests/cases" \
  --output-dir "$(pwd)/tests/results"
```

### 3. Confirm expected run summary

You should see a summary similar to:

- `Ran <N> tests: <N> passed, 0 failed`

### 4. Inspect generated outputs

Primary outputs:

- `tests/results/governance_harness_results.json`
- `tests/results/governance_harness_summary.csv`
- `tests/results/governance_harness_report.json`

Per-test artifacts (latest run directory):

- `tests/results/run_<timestamp>/<test_id>/input.json`
- `tests/results/run_<timestamp>/<test_id>/outcome.json`
- `tests/results/run_<timestamp>/<test_id>/trace.json`
- `tests/results/run_<timestamp>/<test_id>/audit_entry.json`
- `tests/results/run_<timestamp>/<test_id>/assertion.txt`

### 5. Verify key criteria

Check in `governance_harness_report.json`:

- category-level pass/fail totals
- latency percentiles (including p95)
- deterministic replay failure list (should be empty)
- policy-comparison statistics (including COMP-004 significance metadata)

## CI and Packaging Hardening

- Pinned dependency lockfile: `requirements.lock`
- CI gate workflow: `.github/workflows/ci.yml`
- Quality gate script: `tests/harness/quality_gate.py`
- SBOM manifest generator:

```bash
python3 scripts/generate_sbom.py --root . --output tests/results/sbom_manifest.json
```

## Notes on Scope

- This repository captures WP0 (standalone governance verification).
- MCP serverization work is planned for WP1.
- The harness is intentionally transport-agnostic so governance logic can be validated independently.
