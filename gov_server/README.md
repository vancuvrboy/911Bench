# Governance Server (WP0)

Implemented modules:
- `gov_server/enforcement.py`: deterministic enforcement pipeline (`validate -> prohibit -> bound -> freshness -> escalate -> checkpoint -> execute -> audit`).
- `gov_server/policy_loader.py`: policy/registry/evidence config loading and obligation validation.
- `gov_server/evidence.py`: evidence subtype/category/source/confidence validation.
- `gov_server/shims.py`: standalone checkpoint + plant state shims for harness testing.
- `gov_server/predicates.py`: built-in predicate evaluation and deterministic transforms.

Use the standalone harness:

```bash
python3 -m tests.harness.runner --root . --cases-dir tests/cases --output-dir tests/results
```
