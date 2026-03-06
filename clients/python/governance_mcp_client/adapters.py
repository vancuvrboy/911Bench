"""Runtime adapter helpers built on top of GovernanceMCPClient."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import GovernanceMCPClient

JSONObject = dict[str, Any]


@dataclass
class OpenAIRuntimeAdapter:
    """Adapter for OpenAI-style orchestrators that need governance gating."""

    client: GovernanceMCPClient
    agent_id: str
    agent_role: str
    autonomy_level: str
    agent_secret: str | None = None

    def evaluate_and_propose(
        self,
        *,
        action_id: str,
        incident_id: str,
        action_class: str,
        proposed_payload: JSONObject,
        evidence_refs: list[JSONObject],
        uncertainty: JSONObject,
        read_set: JSONObject,
        idempotency_key: str | None = None,
    ) -> JSONObject:
        proposal: JSONObject = {
            "action_id": action_id,
            "incident_id": incident_id,
            "action_class": action_class,
            "proposed_payload": proposed_payload,
            "evidence_refs": evidence_refs,
            "uncertainty": uncertainty,
            "read_set": read_set,
            "proposer": {
                "agent_id": self.agent_id,
                "agent_role": self.agent_role,
                "autonomy_level": self.autonomy_level,
            },
        }
        if self.agent_secret is not None:
            proposal["proposer"]["agent_secret"] = self.agent_secret
        if idempotency_key:
            proposal["proposer"]["idempotency_key"] = idempotency_key
        return self.client.propose_action(proposal)

    def fetch_incident_context(self, incident_id: str) -> JSONObject:
        return self.client.get_context_snapshot(
            incident_id=incident_id,
            agent_id=self.agent_id,
            agent_secret=self.agent_secret,
        )


@dataclass
class LangChainRuntimeAdapter:
    """Adapter for tool-oriented LangChain style runtime integration."""

    client: GovernanceMCPClient
    agent_id: str
    agent_role: str
    autonomy_level: str
    agent_secret: str | None = None

    def list_action_tools(self) -> list[JSONObject]:
        classes = self.client.list_action_classes(
            agent_id=self.agent_id,
            agent_secret=self.agent_secret,
        ).get("classes", [])
        tools: list[JSONObject] = []
        for item in classes:
            tools.append(
                {
                    "name": f"governance::{item.get('name')}",
                    "description": f"Governed proposal tool for {item.get('name')}",
                    "payload_schema": item.get("payload_schema", {}),
                }
            )
        return tools

    def run_tool(
        self,
        *,
        action_id: str,
        incident_id: str,
        action_class: str,
        payload: JSONObject,
        evidence_refs: list[JSONObject],
        uncertainty: JSONObject,
        read_set: JSONObject,
    ) -> JSONObject:
        proposal: JSONObject = {
            "action_id": action_id,
            "incident_id": incident_id,
            "action_class": action_class,
            "proposed_payload": payload,
            "evidence_refs": evidence_refs,
            "uncertainty": uncertainty,
            "read_set": read_set,
            "proposer": {
                "agent_id": self.agent_id,
                "agent_role": self.agent_role,
                "autonomy_level": self.autonomy_level,
            },
        }
        if self.agent_secret is not None:
            proposal["proposer"]["agent_secret"] = self.agent_secret
        return self.client.propose_action(proposal)
