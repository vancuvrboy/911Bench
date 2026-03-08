# 911Bench Architecture v4 Parity Gap Log

Date: 2026-03-07  
Repo: `911Bench`  
Source Spec: `design_docs/911Bench_Architecture_v4.docx`

## Purpose

Track the remaining implementation work needed for full parity with Architecture v4, while allowing phased delivery.

Status scale:
- `Complete`: implemented and validated in repo/harness.
- `Partial`: implemented in baseline form; deeper spec parity still pending.
- `Not Started`: no meaningful implementation yet.

## Current Parity Snapshot

- Section 1 (System Overview): `Complete`
- Section 2 (ECC Simulation MCP Server): `Partial` (core complete; extended resources/stubs depth pending)
- Section 3 (Governance MCP Server): `Complete` (research baseline)
- Section 4 (Agent Specifications): `Partial`
- Section 5 (Governance Error Detection): `Partial`
- Section 6 (Experiment Harness): `Complete` (planned set; manifest-enforced)

## Open Work Items

### A. Agent Development Parity (Section 4)

1. Expand role-agent profile matrix beyond deterministic/manual/replay
- Scope:
  - production-grade model-backed caller/call-taker/QA variants
  - explicit profile contracts per role (I/O schema + capability flags)
- Deliverables:
  - profile registry/config files for all supported role agents
  - loader/selection validation with clear error modes
  - integration tests for mixed-profile episodes
- Acceptance:
  - each configured profile can be selected, initialized, and exercised in harness
  - deterministic fallback path remains available

2. Prompt/config lifecycle parity for agent specs
- Scope:
  - frozen prompt/config versioning and hash capture for each agent run
  - strict rejection of unpinned model aliases in agent configs
- Deliverables:
  - prompt/config manifest files
  - run artifact fields for prompt/config hashes by role
- Acceptance:
  - reproducibility harness confirms stable hashes for fixed config/seed

3. QA evaluator parity enhancements
- Scope:
  - complete rubric merge/calibration pipeline from architecture spec
  - calibration threshold reporting and drift checks
- Deliverables:
  - calibration job/script + artifacts
  - validation tests for rubric merge and calibration thresholds
- Acceptance:
  - calibration outputs deterministic under frozen inputs
  - threshold violations are surfaced in reports

4. Governance-facing operational agent expansion
- Scope:
  - additional DSA/action-class profiles beyond baseline 911Buddy
  - explicit capability mapping and safety constraints per profile
- Deliverables:
  - expanded DSA profile catalog
  - conformance tests per action_class/profile pairing
- Acceptance:
  - unsupported pairings are rejected
  - supported pairings show expected proposal provenance in audit

### B. Governance Error Detection Pipeline (Section 5)

1. Define explicit governance error taxonomy
- Scope:
  - classify enforcement/pathology categories (policy, evidence, OCC, checkpoint, escalation, auth/rate, transport, drift)
- Deliverables:
  - taxonomy markdown + machine-readable code map
  - normalization helper for report aggregation
- Acceptance:
  - every error/denial emitted by governance can map to taxonomy code

2. Implement error detection/aggregation module
- Scope:
  - aggregate structured error events across runs
  - severity levels and incident-level rollups
- Deliverables:
  - module under `gov_server/` (or `tests/harness/`) for aggregation
  - report output (`error_summary.json` + CSV)
- Acceptance:
  - harness emits deterministic error rollups for fixed test corpus

3. Add anomaly/drift detection checks over governance outputs
- Scope:
  - detect unexpected shifts in denial/escalation/latency patterns versus baseline
- Deliverables:
  - baseline profile file + comparator script
  - thresholded checks wired to quality gate
- Acceptance:
  - controlled regressions trigger deterministic gate failure

4. Cross-link error detection into experiment artifacts
- Scope:
  - ensure Phase 3/4 bundles include governance error rollup and references
- Deliverables:
  - artifact writer updates
  - documentation updates in `tests/README.md`
- Acceptance:
  - each bundle contains per-run and aggregate error-detection outputs

## Suggested Implementation Order

1. Governance error taxonomy + aggregation foundation (B1, B2)  
2. Agent profile matrix + config/prompt lifecycle hardening (A1, A2)  
3. QA calibration parity and DSA expansion (A3, A4)  
4. Drift/anomaly detection + quality-gate integration + artifact wiring (B3, B4)

## Verification Checklist (When Closing This Log)

- [ ] Section 4 marked `Complete` with tests demonstrating profile breadth, frozen config behavior, and QA calibration parity.
- [ ] Section 5 marked `Complete` with deterministic taxonomy-mapped error reporting and drift/anomaly gates.
- [ ] Integration harness artifacts include error-detection outputs and pass/fail gate evidence.
- [ ] Documentation references updated to remove parity caveats.
