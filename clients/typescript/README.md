# TypeScript SDK (`@acaf/governance-mcp-client`)

TypeScript client wrapper for governance MCP northbound endpoints.

## Example

```ts
import { GovernanceMCPClient } from "./src/index";

const client = new GovernanceMCPClient("http://127.0.0.1:8200");
const caps = await client.capabilities();
console.log(caps.protocol);
```

## Methods

- `capabilities()`
- `descriptor()`
- `toolsList()`
- `toolsCall(tool, argumentsPayload)`
- `rpc(method, params, requestId?)`
- `proposeAction(proposal)`
- `getContextSnapshot(...)`
- `getContextSince(...)`
- `listActionClasses(...)`
- `getActionSchema(actionClass)`
- `getAuditRef(actionId)`
- `swapPolicy(policyFile)`
- `seedContext(...)`
