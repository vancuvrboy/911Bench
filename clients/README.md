# Northbound Clients

Current client interfaces:

- Python SDK: `clients/python/governance_mcp_client`
- TypeScript SDK scaffold: `clients/typescript`
- Runtime adapters (Python):
  - `OpenAIRuntimeAdapter`
  - `LangChainRuntimeAdapter`

Conformance runner:

```bash
python3 -m tests.harness.conformance_matrix --root . --output-dir tests/results
```
