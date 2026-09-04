from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, SchemaError


class ToolSchemaError(ValueError):
    """Raised when a tool definition or call arguments violate its schema."""


class ToolSchemaValidator:
    @staticmethod
    def validate_definition(schema: dict[str, Any]) -> None:
        if not isinstance(schema, dict):
            raise ToolSchemaError("Tool schema must be an object")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ToolSchemaError("Tool schema is invalid") from exc
        if schema.get("type") != "object":
            raise ToolSchemaError("Tool schema top-level type must be object")

    @staticmethod
    def validate_arguments(schema: dict[str, Any], arguments: Any) -> None:
        ToolSchemaValidator.validate_definition(schema)
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda error: list(error.absolute_path))
        if errors:
            raise ToolSchemaError("Tool arguments do not match the registered schema")


def copy_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(schema)
