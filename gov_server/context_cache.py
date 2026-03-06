"""Incident-scoped context cache for governance northbound MCP tools."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any


JSONObject = dict[str, Any]


@dataclass
class IncidentContext:
    incident_id: str
    transcript: list[JSONObject] = field(default_factory=list)
    cad_view: JSONObject = field(default_factory=dict)
    location: JSONObject = field(default_factory=dict)
    sop_refs: list[str] = field(default_factory=list)
    cursor: int = 0
    deltas: list[JSONObject] = field(default_factory=list)


class ContextCache:
    """In-memory context state with cursor-based delta history."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._incidents: dict[str, IncidentContext] = {}

    def ensure_incident(self, incident_id: str) -> IncidentContext:
        with self._lock:
            if incident_id not in self._incidents:
                self._incidents[incident_id] = IncidentContext(incident_id=incident_id)
            return self._incidents[incident_id]

    def set_snapshot(
        self,
        incident_id: str,
        transcript: list[JSONObject],
        cad_view: JSONObject,
        location: JSONObject,
        sop_refs: list[str],
    ) -> None:
        with self._lock:
            ctx = self.ensure_incident(incident_id)
            ctx.transcript = copy.deepcopy(transcript)
            ctx.cad_view = copy.deepcopy(cad_view)
            ctx.location = copy.deepcopy(location)
            ctx.sop_refs = copy.deepcopy(sop_refs)
            self._append_delta_unlocked(
                ctx,
                {
                    "type": "snapshot_refresh",
                    "transcript_size": len(ctx.transcript),
                    "cad_keys": sorted(ctx.cad_view.keys()),
                },
            )

    def append_transcript_turn(self, incident_id: str, turn: int, text: str) -> None:
        with self._lock:
            ctx = self.ensure_incident(incident_id)
            entry = {"turn": int(turn), "text": str(text)}
            ctx.transcript.append(entry)
            self._append_delta_unlocked(ctx, {"type": "transcript_turn", "entry": entry})

    def update_cad_view(self, incident_id: str, patch: JSONObject) -> None:
        with self._lock:
            ctx = self.ensure_incident(incident_id)
            for key, value in patch.items():
                ctx.cad_view[key] = value
            self._append_delta_unlocked(ctx, {"type": "cad_patch", "patch": copy.deepcopy(patch)})

    def get_context_snapshot(self, incident_id: str, agent_id: str) -> JSONObject:
        with self._lock:
            ctx = self.ensure_incident(incident_id)
            return {
                "incident_id": incident_id,
                "agent_id": agent_id,
                "transcript": copy.deepcopy(ctx.transcript),
                "cad_view": copy.deepcopy(ctx.cad_view),
                "location": copy.deepcopy(ctx.location),
                "sop_refs": copy.deepcopy(ctx.sop_refs),
                "cursor": ctx.cursor,
            }

    def get_context_since(self, incident_id: str, agent_id: str, cursor: int) -> JSONObject:
        with self._lock:
            ctx = self.ensure_incident(incident_id)
            deltas = [copy.deepcopy(d) for d in ctx.deltas if int(d["cursor"]) > int(cursor)]
            return {
                "incident_id": incident_id,
                "agent_id": agent_id,
                "cursor": int(cursor),
                "deltas": deltas,
                "new_cursor": ctx.cursor,
            }

    def _append_delta_unlocked(self, ctx: IncidentContext, delta: JSONObject) -> None:
        ctx.cursor += 1
        payload = copy.deepcopy(delta)
        payload["cursor"] = ctx.cursor
        ctx.deltas.append(payload)

