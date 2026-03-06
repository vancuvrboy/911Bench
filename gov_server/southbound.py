"""Southbound simulation server integration adapters (Section 2.3.1.2)."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .shims import CheckpointResponse
from .types import StaleConflict

JSONObject = dict[str, Any]


class SouthboundHTTPError(RuntimeError):
    pass


class SouthboundTransientError(SouthboundHTTPError):
    pass


class SouthboundPermanentError(SouthboundHTTPError):
    pass


class SouthboundCircuitOpenError(SouthboundHTTPError):
    pass


def _post_json(
    url: str,
    payload: JSONObject,
    timeout_sec: float,
    tls_context: ssl.SSLContext | None = None,
) -> JSONObject:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec, context=tls_context) as resp:
        return json.loads(resp.read().decode("utf-8"))


class SimulationSouthboundClient:
    """HTTP client for governance southbound privileged tools."""

    def __init__(
        self,
        base_url: str,
        timeout_sec: float = 10.0,
        require_mtls: bool = False,
        ca_file: str | None = None,
        client_cert_file: str | None = None,
        client_key_file: str | None = None,
        retry_attempts: int = 2,
        retry_backoff_sec: float = 0.1,
        circuit_fail_threshold: int = 3,
        circuit_open_sec: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise SouthboundHTTPError(f"unsupported_southbound_scheme:{parsed.scheme}")
        if require_mtls and parsed.scheme != "https":
            raise SouthboundHTTPError("southbound_mtls_requires_https")
        if client_key_file and not client_cert_file:
            raise SouthboundHTTPError("southbound_client_key_without_cert")
        if retry_attempts < 0:
            raise SouthboundHTTPError("southbound_invalid_retry_attempts")
        if circuit_fail_threshold < 1:
            raise SouthboundHTTPError("southbound_invalid_circuit_fail_threshold")

        self.retry_attempts = int(retry_attempts)
        self.retry_backoff_sec = float(retry_backoff_sec)
        self.circuit_fail_threshold = int(circuit_fail_threshold)
        self.circuit_open_sec = float(circuit_open_sec)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        self.tls_context: ssl.SSLContext | None = None
        if parsed.scheme == "https":
            self.tls_context = ssl.create_default_context(cafile=ca_file)
            if client_cert_file:
                self.tls_context.load_cert_chain(certfile=client_cert_file, keyfile=client_key_file)

    def get_state_snapshot(self, incident_id: str) -> JSONObject:
        return self._request_json(
            f"{self.base_url}/plant/get_state_snapshot",
            {"incident_id": incident_id},
        )

    def get_transcript_since(self, incident_id: str, cursor: int) -> JSONObject:
        return self._request_json(
            f"{self.base_url}/plant/get_transcript_since",
            {"incident_id": incident_id, "cursor": int(cursor)},
        )

    def request_checkpoint(self, payload: JSONObject) -> JSONObject:
        return self._request_json(
            f"{self.base_url}/checkpoint/request",
            payload,
        )

    def poll_checkpoint(self, request_id: str) -> JSONObject:
        return self._request_json(
            f"{self.base_url}/checkpoint/poll",
            {"request_id": request_id},
        )

    def apply_cad_patch(self, payload: JSONObject) -> JSONObject:
        return self._request_json(
            f"{self.base_url}/plant/apply_cad_patch",
            payload,
        )

    def emit_event(self, event: JSONObject) -> JSONObject:
        return self._request_json(
            f"{self.base_url}/plant/emit_event",
            {"event": event},
        )

    def _request_json(self, url: str, payload: JSONObject) -> JSONObject:
        now = time.time()
        if now < self._circuit_open_until:
            raise SouthboundCircuitOpenError("southbound_circuit_open")

        attempts = 1 + self.retry_attempts
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                result = _post_json(
                    url=url,
                    payload=payload,
                    timeout_sec=self.timeout_sec,
                    tls_context=self.tls_context,
                )
                self._consecutive_failures = 0
                self._circuit_open_until = 0.0
                return result
            except Exception as exc:
                last_exc = exc
                transient = self._is_transient(exc)
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.circuit_fail_threshold:
                    self._circuit_open_until = time.time() + self.circuit_open_sec

                if not transient:
                    raise SouthboundPermanentError(f"southbound_permanent_error:{type(exc).__name__}") from exc
                if attempt >= attempts - 1:
                    raise SouthboundTransientError(f"southbound_transient_error:{type(exc).__name__}") from exc

                if self.retry_backoff_sec > 0:
                    time.sleep(self.retry_backoff_sec * (2**attempt))

        raise SouthboundTransientError(f"southbound_transient_error:{type(last_exc).__name__ if last_exc else 'unknown'}")

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in {408, 429, 500, 502, 503, 504}
        if isinstance(exc, (TimeoutError, urllib.error.URLError)):
            return True
        return False


@dataclass
class SouthboundPlantAdapter:
    """Engine-compatible adapter around southbound client apply/snapshot tools."""

    client: SimulationSouthboundClient
    _incident_id: ContextVar[str] = ContextVar("sb_plant_incident_id", default="")
    _action_class: ContextVar[str] = ContextVar("sb_plant_action_class", default="")
    _action_id: ContextVar[str] = ContextVar("sb_plant_action_id", default="")

    def set_request_context(self, incident_id: str, action_class: str, action_id: str = "") -> None:
        self._incident_id.set(incident_id)
        self._action_class.set(action_class)
        self._action_id.set(action_id)

    def check_read_set(self, read_set: JSONObject) -> StaleConflict | None:
        snapshot = self.client.get_state_snapshot(self._incident_id.get())
        versions = snapshot.get("field_versions", {}) or snapshot.get("versions", {}).get("field_versions", {})
        record_version = snapshot.get("record_version", snapshot.get("versions", {}).get("record_version", 0))

        stale_fields: list[str] = []
        current_versions: dict[str, int] = {}
        if int(read_set.get("record_version", 0)) < int(record_version):
            stale_fields.append("record_version")
            current_versions["record_version"] = int(record_version)

        for field, expected in (read_set.get("field_versions", {}) or {}).items():
            current = int(versions.get(field, 0))
            if int(expected) < current:
                stale_fields.append(str(field))
                current_versions[str(field)] = current

        if stale_fields:
            return StaleConflict(stale_fields=stale_fields, current_versions=current_versions)
        return None

    def apply_cad_patch(
        self,
        payload: JSONObject,
        read_set: JSONObject,
        policy_id: str,
        checkpoint_ref: str | None = None,
        incident_id: str | None = None,
        action_class: str | None = None,
    ) -> JSONObject:
        request = {
            "incident_id": incident_id or self._incident_id.get(),
            "action_class": action_class or self._action_class.get(),
            "idempotency_key": self._action_id.get(),
            "payload": payload,
            "read_set": read_set,
            "policy_id": policy_id,
            "checkpoint_ref": checkpoint_ref,
        }
        result = self.client.apply_cad_patch(request)
        status = result.get("status")
        if status == "conflict":
            return {"success": False, "conflict": result.get("conflict_detail", {})}
        if status not in {"applied", "success"}:
            raise SouthboundHTTPError(f"unexpected_apply_status:{status}")
        return {
            "success": True,
            "new_record_version": int(result.get("new_record_version", 0)),
            "new_field_versions": result.get("new_field_versions", {}),
            "policy_id": policy_id,
            "checkpoint_ref": checkpoint_ref,
        }


@dataclass
class SouthboundCheckpointAdapter:
    """Engine-compatible adapter for checkpoint.request/poll southbound behavior."""

    client: SimulationSouthboundClient
    poll_interval_sec: float = 0.25
    _incident_id: ContextVar[str] = ContextVar("sb_chk_incident_id", default="")
    _action_class: ContextVar[str] = ContextVar("sb_chk_action_class", default="")
    _escalation_depth: ContextVar[int] = ContextVar("sb_chk_escalation_depth", default=0)
    _action_id: ContextVar[str] = ContextVar("sb_chk_action_id", default="")

    def set_request_context(
        self,
        incident_id: str,
        action_class: str,
        escalation_depth: int = 0,
        action_id: str = "",
    ) -> None:
        self._incident_id.set(incident_id)
        self._action_class.set(action_class)
        self._escalation_depth.set(int(escalation_depth))
        self._action_id.set(action_id)

    def request(self, payload: JSONObject, approver_role: str, source: str, timeout_ms: int = 30000) -> tuple[str, CheckpointResponse, int]:
        started = time.perf_counter()
        req = {
            "incident_id": self._incident_id.get(),
            "request": {
                "action_class": self._action_class.get(),
                "idempotency_key": self._action_id.get(),
                "proposed_payload": payload,
                "evidence_summary": "governance_request",
                "approver_role": approver_role,
                "source": source,
                "timeout_ms": int(timeout_ms),
                "escalation_context": {
                    "trigger": source,
                    "escalation_depth": self._escalation_depth.get(),
                }
                if source in {"escalation_reactive", "escalation_proactive"}
                else None,
            },
        }
        if req["request"]["escalation_context"] is None:
            req["request"].pop("escalation_context")

        requested = self.client.request_checkpoint(req)
        request_id = str(requested.get("request_id"))
        deadline = time.perf_counter() + (int(timeout_ms) / 1000.0)

        while time.perf_counter() < deadline:
            poll = self.client.poll_checkpoint(request_id)
            status = str(poll.get("status", "pending"))
            if status == "pending":
                time.sleep(self.poll_interval_sec)
                continue
            response = poll.get("response", {}) or {}
            latency_ms = int(response.get("latency_ms", int((time.perf_counter() - started) * 1000)))
            return (
                request_id,
                CheckpointResponse(
                    response=status,
                    edited_payload=response.get("edited_payload"),
                    denial_reason=response.get("rationale"),
                    deferred_to=response.get("deferred_to"),
                    re_escalate_to=response.get("re_escalate_to"),
                ),
                latency_ms,
            )

        return request_id, CheckpointResponse(response="timeout"), int((time.perf_counter() - started) * 1000)
