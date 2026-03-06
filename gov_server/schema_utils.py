"""Lightweight JSON-schema-like validation used in sandbox without third-party deps."""

from __future__ import annotations

import re
from typing import Any

from .types import JSONObject


class SchemaError(ValueError):
    pass


def validate_required(obj: JSONObject, required: list[str], prefix: str = "") -> None:
    for key in required:
        if key not in obj:
            path = f"{prefix}.{key}" if prefix else key
            raise SchemaError(f"missing_required_field:{path}")


def validate_payload(payload: JSONObject, schema: JSONObject) -> None:
    schema_type = schema.get("type")
    if schema_type == "object" and not isinstance(payload, dict):
        raise SchemaError("payload_not_object")

    required = schema.get("required", [])
    validate_required(payload, required, "payload")

    properties = schema.get("properties", {})
    for key, subschema in properties.items():
        if key not in payload:
            continue
        value = payload[key]
        _validate_field(f"payload.{key}", value, subschema)


def _validate_field(path: str, value: Any, subschema: JSONObject) -> None:
    kind = subschema.get("type")
    if kind == "string" and not isinstance(value, str):
        raise SchemaError(f"type_error:{path}:string")
    if kind == "number" and not isinstance(value, (int, float)):
        raise SchemaError(f"type_error:{path}:number")
    if kind == "integer" and not isinstance(value, int):
        raise SchemaError(f"type_error:{path}:integer")
    if kind == "boolean" and not isinstance(value, bool):
        raise SchemaError(f"type_error:{path}:boolean")

    if "minimum" in subschema and isinstance(value, (int, float)) and value < subschema["minimum"]:
        raise SchemaError(f"minimum_violation:{path}")
    if "maximum" in subschema and isinstance(value, (int, float)) and value > subschema["maximum"]:
        raise SchemaError(f"maximum_violation:{path}")
    if "enum" in subschema and value not in subschema["enum"]:
        raise SchemaError(f"enum_violation:{path}")

    pattern = subschema.get("pattern")
    if pattern and isinstance(value, str) and not re.match(pattern, value):
        raise SchemaError(f"pattern_violation:{path}")
