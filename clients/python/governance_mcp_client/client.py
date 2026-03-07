"""Northbound MCP HTTP client for governance service."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError

JSONObject = dict[str, Any]


class GovernanceMCPError(RuntimeError):
    """Raised for transport/protocol errors from governance MCP server."""

    def __init__(self, message: str, status: int | None = None, payload: JSONObject | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


@dataclass
class GovernanceMCPClient:
    """Thin client for the governance MCP HTTP northbound interface."""

    base_url: str
    timeout_sec: float = 10.0
    bearer_token: str | None = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def capabilities(self) -> JSONObject:
        return self._get_json("/mcp/capabilities")

    def descriptor(self) -> JSONObject:
        return self._get_json("/mcp/descriptor")

    def tools_list(self) -> JSONObject:
        return self._get_json("/mcp/tools/list")

    def tools_call(self, tool: str, arguments: JSONObject) -> JSONObject:
        return self._post_json("/mcp/tools/call", {"tool": tool, "arguments": arguments})

    def rpc(self, method: str, params: JSONObject, request_id: str = "req-1") -> JSONObject:
        return self._post_json("/mcp/rpc", {"id": request_id, "method": method, "params": params})

    def propose_action(self, proposal: JSONObject) -> JSONObject:
        return self._post_json("/mcp/propose_action", proposal)

    def get_context_snapshot(self, incident_id: str, agent_id: str, agent_secret: str | None = None) -> JSONObject:
        payload: JSONObject = {"incident_id": incident_id, "agent_id": agent_id}
        if agent_secret is not None:
            payload["agent_secret"] = agent_secret
        return self._post_json("/mcp/get_context_snapshot", payload)

    def get_context_since(
        self,
        incident_id: str,
        agent_id: str,
        cursor: int,
        agent_secret: str | None = None,
    ) -> JSONObject:
        payload: JSONObject = {"incident_id": incident_id, "agent_id": agent_id, "cursor": int(cursor)}
        if agent_secret is not None:
            payload["agent_secret"] = agent_secret
        return self._post_json("/mcp/get_context_since", payload)

    def list_action_classes(self, agent_id: str | None = None, agent_secret: str | None = None) -> JSONObject:
        query: JSONObject = {}
        if agent_id is not None:
            query["agent_id"] = agent_id
        if agent_secret is not None:
            query["agent_secret"] = agent_secret
        return self._get_json("/mcp/list_action_classes", query=query)

    def list_dsa_profiles(
        self,
        action_class: str | None = None,
        requested_profile_id: str | None = None,
        include_disabled: bool = False,
    ) -> JSONObject:
        query: JSONObject = {"include_disabled": str(bool(include_disabled)).lower()}
        if action_class is not None:
            query["action_class"] = action_class
        if requested_profile_id is not None:
            query["requested_profile_id"] = requested_profile_id
        return self._get_json("/mcp/list_dsa_profiles", query=query)

    def get_action_schema(self, action_class: str) -> JSONObject:
        return self._get_json("/mcp/get_action_schema", query={"action_class": action_class})

    def get_audit_ref(self, action_id: str) -> JSONObject:
        return self._get_json("/mcp/get_audit_ref", query={"action_id": action_id})

    def swap_policy(self, policy_file: str) -> JSONObject:
        return self._post_json("/mcp/swap_policy", {"policy_file": policy_file})

    def seed_context(
        self,
        incident_id: str,
        transcript: list[JSONObject] | None = None,
        cad_view: JSONObject | None = None,
        location: JSONObject | None = None,
        sop_refs: list[str] | None = None,
    ) -> JSONObject:
        return self._post_json(
            "/mcp/admin/seed_context",
            {
                "incident_id": incident_id,
                "transcript": transcript or [],
                "cad_view": cad_view or {},
                "location": location or {},
                "sop_refs": sop_refs or [],
            },
        )

    def _url(self, path: str, query: JSONObject | None = None) -> str:
        q = f"?{urllib.parse.urlencode(query)}" if query else ""
        return f"{self.base_url}{path}{q}"

    def _get_json(self, path: str, query: JSONObject | None = None) -> JSONObject:
        req = urllib.request.Request(self._url(path, query=query), method="GET", headers=self._auth_headers())
        return self._send(req)

    def _post_json(self, path: str, payload: JSONObject) -> JSONObject:
        req = urllib.request.Request(
            self._url(path),
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **self._auth_headers()},
        )
        return self._send(req)

    def _auth_headers(self) -> dict[str, str]:
        if not self.bearer_token:
            return {}
        return {"Authorization": f"Bearer {self.bearer_token}"}

    def _send(self, req: urllib.request.Request) -> JSONObject:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {"raw": body}
            message = payload.get("error", f"http_{exc.code}")
            raise GovernanceMCPError(message, status=exc.code, payload=payload) from exc
