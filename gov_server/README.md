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

## WP1 MCP Server

Run the Governance MCP HTTP/SSE server:

```bash
python3 -m gov_server \
  --root . \
  --host 127.0.0.1 \
  --port 8200 \
  --policy-file policies/test_full_ecc.yaml \
  --registry-file registries/test_registry.yaml \
  --evidence-config-file policies/domain_evidence_config.yaml \
  --auth-config-file policies/agent_auth_config.json \
  --dsa-config-file policies/dsa_profiles.yaml \
  --proposals-per-sec 10 \
  --sim-base-url http://127.0.0.1:8300 \
  --southbound-timeout-sec 10 \
  --checkpoint-poll-interval-sec 0.25 \
  --southbound-require-mtls \
  --southbound-ca-file certs/sim-ca.pem \
  --southbound-client-cert-file certs/gov-client.crt \
  --southbound-client-key-file certs/gov-client.key \
  --southbound-retry-attempts 2 \
  --southbound-retry-backoff-sec 0.1 \
  --southbound-circuit-fail-threshold 3 \
  --southbound-circuit-open-sec 5 \
  --state-db-file .runtime/governance_state.db
```

Southbound mode:
- Omit `--sim-base-url` to run with local harness shims (`CheckpointShim`, `PlantStateShim`).
- Set `--sim-base-url` to enable real simulation southbound integration for:
  - `plant.get_state_snapshot`
  - `plant.get_transcript_since`
  - `checkpoint.request`
  - `checkpoint.poll`
  - `plant.apply_cad_patch`
  - `plant.emit_event`
- In southbound mode, `propose_action` and context retrieval calls refresh state/transcript from simulation server before enforcement.
- For production transport hardening:
  - use `https://...` `--sim-base-url`
  - set `--southbound-require-mtls`
  - provide CA and client certificate flags
- Resilience controls:
  - transient failures use bounded retry/backoff
  - non-retryable failures fail fast
  - circuit breaker opens after repeated failures and temporarily short-circuits requests
- Persistence:
  - set `--state-db-file` to persist idempotency keys, action->audit refs, and southbound cursors across restarts
  - when enabled, governance decisions are hash-chained for tamper-evident verification

Northbound routes implemented (Section 3.2):
- `GET /mcp/capabilities` (MCP-style capability discovery envelope)
- `GET /mcp/descriptor` (client-oriented descriptor with schemas/examples)
- `GET /mcp/tools/list` (tool metadata discovery)
- `POST /mcp/tools/call` (tool invocation envelope: `tool` + `arguments`)
- `POST /mcp/rpc` (single endpoint method dispatch envelope)
- `POST /mcp/propose_action`
- `POST /mcp/get_context_snapshot`
- `POST /mcp/get_context_since`
- `GET /mcp/subscribe_context` (SSE, bounded stream duration)
- `GET /mcp/list_action_classes`
- `GET /mcp/list_dsa_profiles`
- `GET /mcp/get_action_schema`
- `GET /mcp/get_audit_ref`
- `POST /mcp/swap_policy` (hot-swap hook)
- `POST /mcp/admin/seed_context` (test/admin helper)
- `GET /mcp/admin/metrics` (observability counters/latency snapshot)
- `GET /mcp/admin/events` (event log since cursor; supports `verbosity=normal|debug`)
- `GET /mcp/admin/events/stream` (SSE event stream for live console)
- `GET /mcp/admin/ui` (live web console for setup/client activity/governance events)
- `GET /mcp/admin/verify_audit_chain` (tamper-evident audit chain verification; requires `--state-db-file`)
- `GET /mcp/admin/version_matrix` (server/protocol/policy compatibility matrix)

RPC envelope shape:

```json
{
  "id": "req-1",
  "method": "tools/call",
  "params": {
    "tool": "propose_action",
    "arguments": { "...": "..." }
  }
}
```

RPC result/error:
- Success: `{ "id": "...", "result": { ... } }`
- Error: `{ "id": "...", "error": { "code": <int>, "message": "...", "data": {...} } }`

## Section 3.10 Controls Implemented

- Agent authentication: `agent_id + agent_secret` against `policies/agent_auth_config.json`
- Optional JWT mode (HS256): `Authorization: Bearer <token>` against `policies/agent_auth_jwt_config.json`
- Agent/action allow-list: `(agent_id, action_class)` enforced before `propose_action`
- Per-agent rate limit: default 10 `propose_action` requests/sec
- Queue caps:
  - checkpoint queue cap default 20 per incident
  - escalation queue cap default 5 per incident
- Idempotency:
  - `proposer.idempotency_key` (or fallback `action_id`) deduplicates `propose_action`
  - same key + different payload returns `409 idempotency_key_payload_mismatch`
  - southbound calls include idempotency key in checkpoint/apply payloads
- Error responses:
  - `401` for auth failures
  - `403` for authorization failures
  - `429` with `{\"error\": \"rate_limited\"}` for rate/queue cap violations

Observability:
- Responses include `X-Correlation-Id` (echoing inbound header or generated UUID).
- Structured JSON events are emitted via server logger.
- In-process counters/latencies are exposed at `/mcp/admin/metrics`.
- Live event feeds support two verbosity modes:
  - `normal`: concise operational fields
  - `debug`: full event payloads

JWT mode notes:
- Set `--auth-config-file policies/agent_auth_jwt_config.json`
- Token claims expected:
  - `sub` or `agent_id`
  - `role`
  - `allowed_action_classes` (array)
  - `iss`, `aud`, `exp` validated against config

Version compatibility:
- Supported policy major versions are currently `1.x`.
- Policy swap to unsupported major versions is rejected with `422 incompatible_policy_version`.

DSA registry:
- DSA profile registry is configured via `policies/dsa_profiles.yaml` by default.
- `GET /mcp/list_dsa_profiles` returns default profile, available profiles, and optional selection for an `action_class`.
- `propose_action` accepts optional `dsa` block:
  - `profile_id`: request-level DSA profile override
  - `session_profile_id`: session-level profile override (used if `profile_id` absent)
  - `scenario_profile_id`: scenario-level profile override (used if request/session absent)
  - `strategy`: request-level strategy override
  - `session_strategy`: session-level strategy override
  - `scenario_strategy`: scenario-level strategy override
  - `apply_suggested_payload`: if `true`, apply selected DSA suggestion before enforcement
  - response includes `dsa` provenance and resolution metadata (`profile_id`, `context_hash`, `suggestions`, `chosen_payload_source`, selected source fields, and safe-fallback flags)
- `POST /mcp/admin/seed_context` also accepts optional seeded overrides:
  - `dsa_session_profile_id`, `dsa_scenario_profile_id`
  - `dsa_session_strategy`, `dsa_scenario_strategy`
- routing strategies:
  - `fallback_chain`: try profiles in order until one succeeds
  - `parallel_best` (research mode): evaluate multiple profiles and pick highest-score suggestion
