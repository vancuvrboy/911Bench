# Python SDK (`governance_mcp_client`)

This SDK provides typed convenience wrappers for the governance MCP northbound endpoints.

## Example

```python
from governance_mcp_client import GovernanceMCPClient

client = GovernanceMCPClient("http://127.0.0.1:8200")
caps = client.capabilities()
print(caps["protocol"])
```

## Supported methods

- `capabilities()`
- `descriptor()`
- `tools_list()`
- `tools_call(tool, arguments)`
- `rpc(method, params, request_id="req-1")`
- `propose_action(proposal)`
- `get_context_snapshot(incident_id, agent_id, agent_secret=None)`
- `get_context_since(incident_id, agent_id, cursor, agent_secret=None)`
- `list_action_classes(agent_id=None, agent_secret=None)`
- `get_action_schema(action_class)`
- `get_audit_ref(action_id)`
- `swap_policy(policy_file)`
- `seed_context(...)`

## Runtime adapters

- `OpenAIRuntimeAdapter`
  - `fetch_incident_context(incident_id)`
  - `evaluate_and_propose(...)`
- `LangChainRuntimeAdapter`
  - `list_action_tools()`
  - `run_tool(...)`
