# 911Bench Integration Test Plan v2.1 Addendum (Phase 4)

Date: 2026-03-07  
Scope: Clarification of Phase 4 PIPE ID interpretation in `911Bench_Integration_Test_Plan_v2_1.docx`.

## Clarification

Phase 4 uses the PIPE namespace `PIPE-001..PIPE-044`, but the planned/required test count in v2.1 is 19, not 44.

Required IDs in v2.1:

- `PIPE-001`
- `PIPE-002`
- `PIPE-003`
- `PIPE-004`
- `PIPE-005`
- `PIPE-010`
- `PIPE-011`
- `PIPE-012`
- `PIPE-020`
- `PIPE-021`
- `PIPE-022`
- `PIPE-030`
- `PIPE-031`
- `PIPE-032`
- `PIPE-040`
- `PIPE-041`
- `PIPE-042`
- `PIPE-043`
- `PIPE-044`

## Implementation Alignment

- Canonical manifest: `tests/cases/phase4_pipe_manifest.json`
- Harness enforcement: `tests/harness/phase4_pipeline.py` now checks:
  - no missing planned IDs
  - no unexpected IDs outside the planned set
  - summary failure if manifest alignment fails

## Verification

Run:

```bash
python3 -m tests.harness.phase4_pipeline --root . --output-dir tests/results
```

Check in `phase4_report.json`:

- `summary.total == 19`
- `summary.failed == 0`
- `plan_alignment.pass == true`
- `plan_alignment.missing_ids == []`
- `plan_alignment.unexpected_ids == []`
