"""Policy and registry loading/validation for governance engine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PolicyValidationError
from .predicates import PredicateEngine
from .schema_utils import validate_required
from .types import JSONObject


def _yaml_or_json_load(path: Path) -> JSONObject:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    except ModuleNotFoundError:
        # JSON is valid YAML; this keeps the loader functional in lean envs.
        data = json.loads(text)
    if not isinstance(data, dict):
        raise PolicyValidationError(f"invalid_document_root:{path}")
    return data


def _autonomy_rank(level: str) -> int:
    if not level.startswith("A"):
        return -1
    try:
        return int(level[1:])
    except ValueError:
        return -1


@dataclass(frozen=True)
class PolicyBundle:
    policy: JSONObject
    registry: JSONObject
    evidence_config: JSONObject
    policy_hash: str
    policy_by_action_class: dict[str, JSONObject]
    registry_by_action_class: dict[str, JSONObject]


class PolicyLoader:
    def __init__(self, predicate_engine: PredicateEngine) -> None:
        self.predicate_engine = predicate_engine

    def load_bundle(
        self,
        policy_file: str | Path,
        registry_file: str | Path,
        evidence_config_file: str | Path,
    ) -> PolicyBundle:
        policy = _yaml_or_json_load(Path(policy_file))
        registry = _yaml_or_json_load(Path(registry_file))
        evidence_config = _yaml_or_json_load(Path(evidence_config_file))

        self._validate_policy(policy, registry)
        policy_hash = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        policy_by_action = {item["name"]: item for item in policy.get("action_classes", [])}
        registry_by_action = {item["name"]: item for item in registry.get("action_classes", [])}

        return PolicyBundle(
            policy=policy,
            registry=registry,
            evidence_config=evidence_config,
            policy_hash=policy_hash,
            policy_by_action_class=policy_by_action,
            registry_by_action_class=registry_by_action,
        )

    def _validate_policy(self, policy: JSONObject, registry: JSONObject) -> None:
        validate_required(policy, ["policy_id", "policy_version", "description", "action_classes"])
        validate_required(registry, ["action_classes"])
        if not isinstance(policy["action_classes"], list) or not policy["action_classes"]:
            raise PolicyValidationError("policy_missing_action_classes")

        registry_classes = {entry["name"]: entry for entry in registry["action_classes"]}
        seen_rule_ids: set[str] = set()

        for action_policy in policy["action_classes"]:
            validate_required(
                action_policy,
                ["name", "controlling_entity", "autonomy_level", "operators", "evidence_requirements"],
            )
            action_name = action_policy["name"]
            if action_name not in registry_classes:
                raise PolicyValidationError(f"unknown_action_class:{action_name}")

            controller = action_policy["controlling_entity"]
            validate_required(controller, ["role", "escalation_target"], f"action_classes.{action_name}.controlling_entity")

            operators = action_policy["operators"]
            if "audit" not in operators:
                raise PolicyValidationError("missing_audit_operator")

            for op_name in ("prohibit", "bound", "checkpoint", "escalate"):
                for rule in operators.get(op_name, []):
                    if "rule_id" in rule:
                        rid = rule["rule_id"]
                        if rid in seen_rule_ids:
                            raise PolicyValidationError(f"duplicate_rule_id:{rid}")
                        seen_rule_ids.add(rid)

                    predicate_key = "trigger" if op_name in ("prohibit", "checkpoint") else "condition"
                    if op_name == "bound":
                        predicate_key = "condition"
                    if predicate_key in rule and isinstance(rule[predicate_key], str):
                        self._validate_predicate_reference(rule[predicate_key])

                if op_name == "escalate":
                    for rule in operators.get("escalate", []):
                        validate_required(rule, ["rule_id", "condition", "target", "fallback"])

            level = action_policy["autonomy_level"]
            level_num = _autonomy_rank(level)
            reg_entry = registry_classes[action_name]
            has_checkpoint = bool(operators.get("checkpoint"))
            has_escalate = bool(operators.get("escalate"))
            rollback = reg_entry.get("rollback_semantics") or {}
            has_rollback = bool(rollback.get("reversible"))

            if level_num >= 3 and not (has_rollback or has_checkpoint):
                raise PolicyValidationError("missing_rollback_or_checkpoint_for_A3")
            if level_num >= 4 and not has_escalate:
                raise PolicyValidationError("missing_escalation_for_A4")

    def _validate_predicate_reference(self, expression: str) -> None:
        expr = expression.strip()
        if expr.startswith("custom:"):
            name = expr.split(":", 1)[1]
            if not self.predicate_engine.has_custom_predicate(name):
                raise PolicyValidationError(f"unknown_custom_predicate:{name}")
