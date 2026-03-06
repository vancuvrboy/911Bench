"""Standalone harness shims for checkpoint and plant state."""

from __future__ import annotations

import copy
import itertools
import time
from dataclasses import dataclass
from typing import Callable

from .types import JSONObject, StaleConflict


@dataclass
class CheckpointResponse:
    response: str
    edited_payload: JSONObject | None = None
    denial_reason: str | None = None
    deferred_to: str | None = None
    re_escalate_to: str | None = None


class CheckpointShim:
    def __init__(
        self,
        mode: str = "auto_approve",
        denial_reason: str = "denied_by_shim",
        edit_fn: Callable[[JSONObject], JSONObject] | None = None,
        scripted: list[CheckpointResponse] | None = None,
    ) -> None:
        self.mode = mode
        self.denial_reason = denial_reason
        self.edit_fn = edit_fn or (lambda payload: payload)
        self.scripted = scripted or []
        self._id_counter = itertools.count(1)
        self._script_idx = 0
        self.invocations: list[JSONObject] = []

    def request(self, payload: JSONObject, approver_role: str, source: str, timeout_ms: int = 30000) -> tuple[str, CheckpointResponse, int]:
        request_id = f"chk-{next(self._id_counter)}"
        self.invocations.append({
            "request_id": request_id,
            "approver_role": approver_role,
            "source": source,
            "payload": copy.deepcopy(payload),
            "timeout_ms": timeout_ms,
        })

        start = time.perf_counter()
        response = self._resolve(payload, approver_role)
        latency_ms = int((time.perf_counter() - start) * 1000)
        return request_id, response, latency_ms

    def _resolve(self, payload: JSONObject, approver_role: str) -> CheckpointResponse:
        if self.mode == "auto_approve":
            return CheckpointResponse(response="approved")
        if self.mode == "auto_deny":
            return CheckpointResponse(response="denied", denial_reason=self.denial_reason)
        if self.mode == "auto_edit":
            return CheckpointResponse(response="edited_approved", edited_payload=self.edit_fn(copy.deepcopy(payload)))
        if self.mode == "auto_defer":
            return CheckpointResponse(response="deferred_escalated", deferred_to="supervisor")
        if self.mode == "auto_re_escalate":
            return CheckpointResponse(response="re_escalated", re_escalate_to="manager")
        if self.mode == "timeout":
            return CheckpointResponse(response="timeout")
        if self.mode == "scripted":
            if self._script_idx >= len(self.scripted):
                return CheckpointResponse(response="timeout")
            item = self.scripted[self._script_idx]
            self._script_idx += 1
            return item
        return CheckpointResponse(response="approved")


class PlantStateShim:
    def __init__(
        self,
        cad_state: JSONObject | None = None,
        record_version: int = 0,
        field_versions: dict[str, int] | None = None,
    ) -> None:
        self.cad_state = cad_state or {}
        self.record_version = record_version
        self.field_versions = field_versions or {}

    def get_state_snapshot(self) -> JSONObject:
        return {
            "cad_state": copy.deepcopy(self.cad_state),
            "versions": {
                "record_version": self.record_version,
                "field_versions": copy.deepcopy(self.field_versions),
            },
        }

    def check_read_set(self, read_set: JSONObject) -> StaleConflict | None:
        stale_fields: list[str] = []
        current_versions: dict[str, int] = {}

        requested_record = int(read_set.get("record_version", 0))
        if requested_record < self.record_version:
            stale_fields.append("record_version")
            current_versions["record_version"] = self.record_version

        requested_fields = read_set.get("field_versions", {})
        for field, expected_version in requested_fields.items():
            current = self.field_versions.get(field, 0)
            if int(expected_version) < current:
                stale_fields.append(field)
                current_versions[field] = current

        if stale_fields:
            return StaleConflict(stale_fields=stale_fields, current_versions=current_versions)
        return None

    def apply_cad_patch(self, payload: JSONObject, read_set: JSONObject, policy_id: str, checkpoint_ref: str | None = None) -> JSONObject:
        conflict = self.check_read_set(read_set)
        if conflict is not None:
            return {
                "success": False,
                "conflict": {
                    "stale_fields": conflict.stale_fields,
                    "current_versions": conflict.current_versions,
                },
            }

        for field, value in payload.items():
            self.cad_state[field] = value
            self.field_versions[field] = self.field_versions.get(field, 0) + 1
        self.record_version += 1

        return {
            "success": True,
            "new_record_version": self.record_version,
            "new_field_versions": copy.deepcopy(self.field_versions),
            "policy_id": policy_id,
            "checkpoint_ref": checkpoint_ref,
        }
