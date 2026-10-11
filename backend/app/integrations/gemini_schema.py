from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$anchor",
        "$defs",
        "$id",
        "$ref",
        "additionalProperties",
        "anyOf",
        "description",
        "enum",
        "format",
        "items",
        "maximum",
        "maxItems",
        "minimum",
        "minItems",
        "oneOf",
        "prefixItems",
        "properties",
        "propertyOrdering",
        "required",
        "title",
        "type",
    }
)
_SCHEMA_MAP_KEYS = frozenset({"$defs", "properties"})


def gemini_response_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Build Gemini's supported JSON Schema subset from a Pydantic model."""

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if key not in _SUPPORTED_SCHEMA_KEYS:
                    continue
                if key in _SCHEMA_MAP_KEYS and isinstance(item, dict):
                    result[key] = {name: sanitize(schema) for name, schema in item.items()}
                elif isinstance(item, (dict, list)):
                    result[key] = sanitize(item)
                else:
                    result[key] = item
            return result
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return sanitize(model.model_json_schema())


__all__ = ["gemini_response_json_schema"]
