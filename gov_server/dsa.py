"""Decision-support agent (DSA) profile registry and routing."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .types import JSONObject


@dataclass(frozen=True)
class DSAProfile:
    id: str
    provider: str
    model: str
    mode: str
    enabled: bool
    description: str
    action_classes: tuple[str, ...]
    runtime: JSONObject


@dataclass(frozen=True)
class DSARegistry:
    default_profile_id: str
    profiles: tuple[DSAProfile, ...]
    routing_by_action_class: dict[str, JSONObject]

    def list_profiles(self, include_disabled: bool = False) -> list[JSONObject]:
        out: list[JSONObject] = []
        for profile in self.profiles:
            if not include_disabled and not profile.enabled:
                continue
            row = asdict(profile)
            row["action_classes"] = list(profile.action_classes)
            out.append(row)
        return out

    def profile_by_id(self, profile_id: str) -> DSAProfile | None:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        return None

    def route_for_action_class(self, action_class: str) -> JSONObject:
        raw = self.routing_by_action_class.get(action_class)
        if isinstance(raw, dict):
            profile_ids = [str(v) for v in raw.get("profiles", []) if str(v)]
            strategy = str(raw.get("strategy", "fallback_chain") or "fallback_chain")
            return {"profiles": profile_ids, "strategy": strategy}
        if isinstance(raw, (list, tuple)):
            profile_ids = [str(v) for v in raw if str(v)]
            return {"profiles": profile_ids, "strategy": "fallback_chain"}
        return {"profiles": [], "strategy": "fallback_chain"}

    def allowed_profile_ids_for_action_class(self, action_class: str) -> list[str]:
        explicit = self.route_for_action_class(action_class).get("profiles", [])
        if explicit:
            return list(explicit)
        discovered: list[str] = []
        for profile in self.profiles:
            if profile.action_classes and action_class not in profile.action_classes:
                continue
            discovered.append(profile.id)
        return discovered

    def select_profile(self, action_class: str, requested_profile_id: str | None = None) -> DSAProfile | None:
        allowed_ids = self.allowed_profile_ids_for_action_class(action_class)
        allowed_profiles: list[DSAProfile] = []
        for profile_id in allowed_ids:
            profile = self.profile_by_id(profile_id)
            if profile and profile.enabled:
                allowed_profiles.append(profile)
        if not allowed_profiles:
            return None

        if requested_profile_id:
            for profile in allowed_profiles:
                if profile.id == requested_profile_id:
                    return profile

        for profile in allowed_profiles:
            if profile.id == self.default_profile_id:
                return profile
        return allowed_profiles[0]


def load_dsa_registry(path: str | Path | None) -> DSARegistry:
    if path is None:
        return _default_registry()
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(text)
    except ModuleNotFoundError:
        raw = json.loads(text)
    if not isinstance(raw, dict):
        return _default_registry()

    profiles_raw = raw.get("profiles", [])
    if not isinstance(profiles_raw, list) or not profiles_raw:
        return _default_registry()

    profiles: list[DSAProfile] = []
    for item in profiles_raw:
        if not isinstance(item, dict):
            continue
        profile_id = str(item.get("id", "")).strip()
        if not profile_id:
            continue
        profile = DSAProfile(
            id=profile_id,
            provider=str(item.get("provider", "builtin")),
            model=str(item.get("model", "rule-based")),
            mode=str(item.get("mode", "deterministic")),
            enabled=bool(item.get("enabled", True)),
            description=str(item.get("description", "")),
            action_classes=tuple(str(v) for v in (item.get("action_classes") or []) if str(v)),
            runtime=item.get("runtime", {}) if isinstance(item.get("runtime"), dict) else {},
        )
        profiles.append(profile)

    if not profiles:
        return _default_registry()

    routing_raw = raw.get("routing", {})
    routing_by_action_class: dict[str, JSONObject] = {}
    if isinstance(routing_raw, dict):
        for action_class, rule in routing_raw.items():
            key = str(action_class)
            if isinstance(rule, list):
                routing_by_action_class[key] = {
                    "profiles": [str(v) for v in rule if str(v)],
                    "strategy": "fallback_chain",
                }
                continue
            if isinstance(rule, dict):
                routing_by_action_class[key] = {
                    "profiles": [str(v) for v in rule.get("profiles", []) if str(v)],
                    "strategy": str(rule.get("strategy", "fallback_chain") or "fallback_chain"),
                }

    default_profile = str(raw.get("default_profile_id", "")).strip() or profiles[0].id
    if all(profile.id != default_profile for profile in profiles):
        default_profile = profiles[0].id

    return DSARegistry(
        default_profile_id=default_profile,
        profiles=tuple(profiles),
        routing_by_action_class=routing_by_action_class,
    )


def _default_registry() -> DSARegistry:
    profiles = (
        DSAProfile(
            id="deterministic_911buddy_v1",
            provider="builtin",
            model="rule-based",
            mode="deterministic",
            enabled=True,
            description="Deterministic 911Buddy baseline profile.",
            action_classes=(),
            runtime={},
        ),
    )
    return DSARegistry(default_profile_id=profiles[0].id, profiles=profiles, routing_by_action_class={})
