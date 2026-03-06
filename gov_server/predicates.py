"""Deterministic predicate evaluation for governance rules."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from .types import JSONObject

PredicateFn = Callable[[JSONObject], bool]
TransformFn = Callable[[JSONObject], JSONObject]


class AttrDict(dict):
    """dict with attribute access for expression evaluation."""

    def __getattr__(self, name: str) -> Any:
        value = self.get(name)
        if isinstance(value, dict):
            return AttrDict(value)
        return value


@dataclass
class PredicateContext:
    proposal: JSONObject
    payload: JSONObject
    action_registry_entry: JSONObject
    bound_violation: bool = False

    def as_eval_context(self) -> JSONObject:
        evidence_refs = self.proposal.get("evidence_refs", [])
        uncertainty = self.proposal.get("uncertainty", {})
        critical_fields = set(self.action_registry_entry.get("critical_fields", []))

        low_conf_on_critical = False
        for ref in evidence_refs:
            entity_type = ref.get("entity_type")
            confidence = ref.get("confidence")
            if entity_type in critical_fields and confidence is not None and confidence < 0.7:
                low_conf_on_critical = True
                break

        by_entity: dict[str, set[str]] = {}
        for ref in evidence_refs:
            entity = ref.get("entity_type")
            content = ref.get("content")
            if entity and isinstance(content, str):
                by_entity.setdefault(entity, set()).add(content.strip())
        conflict_by_content = any(len(values) > 1 for values in by_entity.values())

        return {
            "payload": AttrDict(self.payload),
            "uncertainty": AttrDict(uncertainty),
            "evidence_refs": evidence_refs,
            "evidence_count": len(evidence_refs),
            "bound_violation": self.bound_violation,
            "conflicting_evidence_detected": bool(uncertainty.get("conflict")) or conflict_by_content,
            "uncertainty_critical_field": low_conf_on_critical,
        }


class PredicateEngine:
    """Evaluates trigger/condition strings and custom predicates."""

    def __init__(self, custom_predicates: dict[str, PredicateFn] | None = None) -> None:
        self.custom_predicates = custom_predicates or {}
        self.transforms: dict[str, TransformFn] = {
            "normalize_address": self._normalize_address,
        }

    def has_custom_predicate(self, name: str) -> bool:
        return name in self.custom_predicates

    def evaluate(self, expression: str, context: PredicateContext) -> bool:
        expr = (expression or "").strip()
        if not expr:
            return False
        if expr == "always":
            return True
        if expr.startswith("custom:"):
            fn = self.custom_predicates.get(expr.split(":", 1)[1])
            if fn is None:
                return False
            return bool(fn(context.proposal))
        if expr in self.custom_predicates:
            return bool(self.custom_predicates[expr](context.proposal))

        # Simple symbolic references used in the architecture/test plan.
        eval_ctx = context.as_eval_context()
        if expr in eval_ctx:
            return bool(eval_ctx[expr])

        safe_expr = expr.replace(" true", " True").replace(" false", " False")
        safe_expr = safe_expr.replace("== true", "== True").replace("== false", "== False")
        safe_expr = safe_expr.replace("&&", " and ").replace("||", " or ")
        try:
            result = eval(safe_expr, {"__builtins__": {}}, eval_ctx)
        except Exception:
            return False
        return bool(result)

    def apply_transform(self, transform_name: str, payload: JSONObject) -> JSONObject:
        fn = self.transforms.get(transform_name)
        if fn is None:
            return copy.deepcopy(payload)
        return fn(copy.deepcopy(payload))

    @staticmethod
    def _normalize_address(payload: JSONObject) -> JSONObject:
        for key in ("location", "address", "address_line"):
            value = payload.get(key)
            if isinstance(value, str):
                payload[key] = " ".join(value.strip().split()).title()
        return payload
