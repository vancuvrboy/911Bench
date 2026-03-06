"""Authentication, authorization, and rate-control helpers (Section 3.10)."""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import threading
import time
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JSONObject = dict[str, Any]


class AuthError(Exception):
    pass


class ForbiddenError(Exception):
    pass


class RateLimitedError(Exception):
    pass


class ConflictError(Exception):
    pass


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    secret: str
    role: str
    allowed_action_classes: list[str]


class AgentAuthManager:
    """Loads and validates agent credentials and action allow-lists."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._profiles: dict[str, AgentProfile] = {}
        self._auth_mode = "dev_secret"
        self._jwt_issuer: str | None = None
        self._jwt_audience: str | None = None
        self._jwt_hs256_secret: str | None = None
        self._jwt_clock_skew_sec: int = 60
        if config_path:
            self.load(config_path)

    def load(self, config_path: str | Path) -> None:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"auth_config_not_found:{path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self._auth_mode = str(data.get("auth_mode", "dev_secret"))
        jwt = data.get("jwt", {}) or {}
        self._jwt_issuer = jwt.get("issuer")
        self._jwt_audience = jwt.get("audience")
        self._jwt_hs256_secret = jwt.get("hs256_secret")
        self._jwt_clock_skew_sec = int(jwt.get("clock_skew_sec", 60))
        profiles: dict[str, AgentProfile] = {}
        for entry in data.get("agents", []):
            agent_id = str(entry.get("agent_id", "")).strip()
            if not agent_id:
                continue
            profiles[agent_id] = AgentProfile(
                agent_id=agent_id,
                secret=str(entry.get("secret", "")),
                role=str(entry.get("role", "unknown")),
                allowed_action_classes=list(entry.get("allowed_action_classes", [])),
            )
        self._profiles = profiles

    @property
    def mode(self) -> str:
        return self._auth_mode

    def has_profiles(self) -> bool:
        return self._auth_mode == "jwt_hs256" or bool(self._profiles)

    def authenticate(self, agent_id: str, agent_secret: str | None, bearer_token: str | None = None) -> AgentProfile:
        if self._auth_mode == "jwt_hs256":
            if not bearer_token:
                raise AuthError("missing_bearer_token")
            return self._authenticate_jwt_hs256(bearer_token=bearer_token, expected_agent_id=agent_id)
        return self._authenticate_dev_secret(agent_id=agent_id, agent_secret=agent_secret)

    def _authenticate_dev_secret(self, agent_id: str, agent_secret: str | None) -> AgentProfile:
        profile = self._profiles.get(agent_id)
        if profile is None:
            raise AuthError("unauthorized_agent")
        if profile.secret != str(agent_secret or ""):
            raise AuthError("invalid_agent_secret")
        return profile

    def _authenticate_jwt_hs256(self, bearer_token: str, expected_agent_id: str) -> AgentProfile:
        if not self._jwt_hs256_secret:
            raise AuthError("jwt_secret_not_configured")
        claims = self._verify_jwt_hs256(bearer_token)
        token_agent_id = str(claims.get("agent_id", claims.get("sub", "")))
        if expected_agent_id and token_agent_id and token_agent_id != expected_agent_id:
            raise AuthError("agent_id_token_mismatch")
        if not token_agent_id:
            raise AuthError("missing_agent_id_claim")
        return AgentProfile(
            agent_id=token_agent_id,
            secret="",
            role=str(claims.get("role", "unknown")),
            allowed_action_classes=[str(x) for x in claims.get("allowed_action_classes", []) if isinstance(x, str)],
        )

    def _verify_jwt_hs256(self, token: str) -> JSONObject:
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("invalid_jwt_format")
        head_b64, payload_b64, sig_b64 = parts
        signing_input = f"{head_b64}.{payload_b64}".encode("utf-8")
        expected_sig = hmac.new(
            self._jwt_hs256_secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        actual_sig = self._b64url_decode(sig_b64)
        if not hmac.compare_digest(actual_sig, expected_sig):
            raise AuthError("invalid_jwt_signature")

        header = json.loads(self._b64url_decode(head_b64).decode("utf-8"))
        if header.get("alg") != "HS256":
            raise AuthError("unsupported_jwt_alg")
        claims = json.loads(self._b64url_decode(payload_b64).decode("utf-8"))

        now = int(time.time())
        if self._jwt_issuer and claims.get("iss") != self._jwt_issuer:
            raise AuthError("invalid_jwt_issuer")
        if self._jwt_audience:
            aud = claims.get("aud")
            aud_ok = aud == self._jwt_audience or (isinstance(aud, list) and self._jwt_audience in aud)
            if not aud_ok:
                raise AuthError("invalid_jwt_audience")
        if "exp" in claims and int(claims["exp"]) + self._jwt_clock_skew_sec < now:
            raise AuthError("expired_jwt")
        if "nbf" in claims and int(claims["nbf"]) - self._jwt_clock_skew_sec > now:
            raise AuthError("jwt_not_yet_valid")
        return claims

    @staticmethod
    def _b64url_decode(value: str) -> bytes:
        pad = "=" * ((4 - len(value) % 4) % 4)
        return base64.urlsafe_b64decode((value + pad).encode("utf-8"))

    def authorize_action(self, profile: AgentProfile, action_class: str) -> None:
        if profile.allowed_action_classes and action_class not in profile.allowed_action_classes:
            raise ForbiddenError("action_class_forbidden")

    def role_for(self, agent_id: str) -> str:
        profile = self._profiles.get(agent_id)
        return profile.role if profile else "unknown"


class ProposalRateLimiter:
    """Sliding-window per-agent limiter for propose_action calls."""

    def __init__(self, proposals_per_sec: int = 10) -> None:
        self.proposals_per_sec = proposals_per_sec
        self._timestamps: dict[str, collections.deque[float]] = {}
        self._lock = threading.RLock()

    def check_and_record(self, agent_id: str) -> None:
        now = time.time()
        with self._lock:
            dq = self._timestamps.setdefault(agent_id, collections.deque())
            while dq and now - dq[0] > 1.0:
                dq.popleft()
            if len(dq) >= self.proposals_per_sec:
                raise RateLimitedError("rate_limited")
            dq.append(now)


class QueueCaps:
    """Basic queue cap tracking for checkpoint/escalation per incident."""

    def __init__(self, checkpoint_cap: int = 20, escalation_cap: int = 5) -> None:
        self.checkpoint_cap = checkpoint_cap
        self.escalation_cap = escalation_cap
        self._checkpoint_counts: dict[str, int] = {}
        self._escalation_counts: dict[str, int] = {}
        self._lock = threading.RLock()

    def reserve_checkpoint(self, incident_id: str) -> None:
        with self._lock:
            count = self._checkpoint_counts.get(incident_id, 0)
            if count >= self.checkpoint_cap:
                raise RateLimitedError("rate_limited")
            self._checkpoint_counts[incident_id] = count + 1

    def release_checkpoint(self, incident_id: str) -> None:
        with self._lock:
            count = self._checkpoint_counts.get(incident_id, 0)
            self._checkpoint_counts[incident_id] = max(0, count - 1)

    def reserve_escalation(self, incident_id: str) -> None:
        with self._lock:
            count = self._escalation_counts.get(incident_id, 0)
            if count >= self.escalation_cap:
                raise RateLimitedError("rate_limited")
            self._escalation_counts[incident_id] = count + 1

    def release_escalation(self, incident_id: str) -> None:
        with self._lock:
            count = self._escalation_counts.get(incident_id, 0)
            self._escalation_counts[incident_id] = max(0, count - 1)
