# ACAF Port Checklist

Purpose: prevent drift while maintaining three code lines:
- Canonical governance development: `ACAF-Research-Server`
- Production mirror: `ACAF-Production-Server`
- Integration mirror: `911Bench`

## Port Order

1. Implement and validate in `ACAF-Research-Server`.
2. Port to `ACAF-Production-Server` with production guardrails.
3. Port to `911Bench` (matching integration branch state).

## Per-Port Checklist

1. Code modules
- `gov_server/*.py` changed in source commit are mirrored.
- Any new CLI flags in `gov_server/mcp_server.py` are mirrored.

2. Policy/config schema
- `policies/dsa_profiles.yaml` keys and defaults are mirrored.
- New config keys are documented in `gov_server/README.md`.

3. Tests
- Unit tests for new behavior are mirrored:
  - `tests/test_mcp_server.py`
  - `tests/test_python_sdk.py`
  - `tests/test_runtime_adapters.py`
- Harness utilities are mirrored if behavior changed:
  - `tests/harness/*`

4. Artifacts/docs
- Update delta docs/checkpoint reports in `design_docs`.
- Record latest validation command set and pass/fail counts.

5. Verification
- Run minimum verification on target repo:
  - `python3 -m unittest tests.test_mcp_server`
  - `python3 -m unittest tests.test_python_sdk tests.test_runtime_adapters`
- Confirm no unexpected schema/config regression in existing harness reports.

6. Sync record
- Capture:
  - source commit SHA
  - target commit SHA
  - date
  - operator
  - any intentional divergence

## Intentional Divergence Rules

1. Production may be stricter than research (allowlists, safer defaults, tighter limits).
2. `911Bench` should match research behavior unless integration constraints require temporary divergence.
3. Every divergence must be documented in a delta or checkpoint report.
