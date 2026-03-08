# 911Bench Phase 4 Pipeline Checkpoint

Date: 2026-03-07
Repo: 911Bench

## Scope
Executed the Phase 4 end-to-end pipeline harness for the planned 19 PIPE IDs in the `PIPE-001..PIPE-044` namespace (per `911Bench_Integration_Test_Plan_v2_1.docx`), with deterministic normalization rules from Section 1.4.

## Command

```bash
python3 -m tests.harness.phase4_pipeline --root . --output-dir tests/results
```

## Result

- Harness report:
  - `tests/results/phase4_pipeline_bundle_20260307_165158/phase4_report.json`
- Summary:
  - Total checks: `19`
  - Passed: `19`
  - Failed: `0`

## Artifact Bundle

Bundle root:
- `tests/results/phase4_pipeline_bundle_20260307_165158`

Contained artifacts include:
- Per-run directories with:
  - `_events.ndjson`
  - `governance_audit.ndjson`
  - `qa_score.json`
  - `meta.json`
- Aggregate files:
  - `summary.json`
  - `summary.csv`
  - `phase4_report.json`

## Notes

- SIM southbound adapter now supports:
  - `POST /admin/end_call`
  - optional `max_turns` in `POST /admin/load_start`
- Phase 4 reproducibility normalization excludes non-semantic volatile fields (`ts`, ids/correlation fields, and timing metrics such as `duration_ms`/`latency_ms`).
