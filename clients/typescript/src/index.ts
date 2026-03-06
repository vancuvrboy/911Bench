export type JsonObject = Record<string, unknown>;

export class GovernanceMCPError extends Error {
  readonly status?: number;
  readonly payload: JsonObject;

  constructor(message: string, status?: number, payload: JsonObject = {}) {
    super(message);
    this.name = "GovernanceMCPError";
    this.status = status;
    this.payload = payload;
  }
}

export class GovernanceMCPClient {
  private readonly baseUrl: string;
  private readonly bearerToken?: string;

  constructor(baseUrl: string, bearerToken?: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.bearerToken = bearerToken;
  }

  capabilities(): Promise<JsonObject> {
    return this.getJson("/mcp/capabilities");
  }

  descriptor(): Promise<JsonObject> {
    return this.getJson("/mcp/descriptor");
  }

  toolsList(): Promise<JsonObject> {
    return this.getJson("/mcp/tools/list");
  }

  toolsCall(tool: string, argumentsPayload: JsonObject): Promise<JsonObject> {
    return this.postJson("/mcp/tools/call", { tool, arguments: argumentsPayload });
  }

  rpc(method: string, params: JsonObject, requestId = "req-1"): Promise<JsonObject> {
    return this.postJson("/mcp/rpc", { id: requestId, method, params });
  }

  proposeAction(proposal: JsonObject): Promise<JsonObject> {
    return this.postJson("/mcp/propose_action", proposal);
  }

  getContextSnapshot(incidentId: string, agentId: string, agentSecret?: string): Promise<JsonObject> {
    const payload: JsonObject = { incident_id: incidentId, agent_id: agentId };
    if (agentSecret) payload.agent_secret = agentSecret;
    return this.postJson("/mcp/get_context_snapshot", payload);
  }

  getContextSince(incidentId: string, agentId: string, cursor: number, agentSecret?: string): Promise<JsonObject> {
    const payload: JsonObject = { incident_id: incidentId, agent_id: agentId, cursor };
    if (agentSecret) payload.agent_secret = agentSecret;
    return this.postJson("/mcp/get_context_since", payload);
  }

  listActionClasses(agentId?: string, agentSecret?: string): Promise<JsonObject> {
    const query: Record<string, string> = {};
    if (agentId) query.agent_id = agentId;
    if (agentSecret) query.agent_secret = agentSecret;
    return this.getJson("/mcp/list_action_classes", query);
  }

  getActionSchema(actionClass: string): Promise<JsonObject> {
    return this.getJson("/mcp/get_action_schema", { action_class: actionClass });
  }

  getAuditRef(actionId: string): Promise<JsonObject> {
    return this.getJson("/mcp/get_audit_ref", { action_id: actionId });
  }

  swapPolicy(policyFile: string): Promise<JsonObject> {
    return this.postJson("/mcp/swap_policy", { policy_file: policyFile });
  }

  seedContext(
    incidentId: string,
    transcript: JsonObject[] = [],
    cadView: JsonObject = {},
    location: JsonObject = {},
    sopRefs: string[] = []
  ): Promise<JsonObject> {
    return this.postJson("/mcp/admin/seed_context", {
      incident_id: incidentId,
      transcript,
      cad_view: cadView,
      location,
      sop_refs: sopRefs
    });
  }

  private async getJson(path: string, query?: Record<string, string>): Promise<JsonObject> {
    const url = new URL(this.baseUrl + path);
    if (query) {
      Object.entries(query).forEach(([k, v]) => url.searchParams.set(k, v));
    }
    return this.send(url.toString(), { method: "GET" });
  }

  private async postJson(path: string, payload: JsonObject): Promise<JsonObject> {
    return this.send(this.baseUrl + path, {
      method: "POST",
      headers: this.withAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload)
    });
  }

  private withAuthHeaders(headers: Record<string, string>): Record<string, string> {
    if (!this.bearerToken) return headers;
    return { ...headers, Authorization: `Bearer ${this.bearerToken}` };
  }

  private async send(url: string, init: RequestInit): Promise<JsonObject> {
    const response = await fetch(url, { ...init, headers: this.withAuthHeaders((init.headers as Record<string, string>) || {}) });
    const bodyText = await response.text();
    let payload: JsonObject = {};
    try {
      payload = bodyText ? (JSON.parse(bodyText) as JsonObject) : {};
    } catch {
      payload = { raw: bodyText };
    }
    if (!response.ok) {
      const msg = typeof payload.error === "string" ? payload.error : `http_${response.status}`;
      throw new GovernanceMCPError(msg, response.status, payload);
    }
    return payload;
  }
}
