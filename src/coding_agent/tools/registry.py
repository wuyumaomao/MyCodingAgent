from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from .schema import ToolSchemaError, ToolSchemaValidator


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self._definitions: dict[str, dict[str, Any]] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(#注册器需要工具名，执行函数，参数，描述
        self,
        name: str,
        executor: Callable[[dict[str, Any]], dict[str, Any]],
        parameters: dict[str, Any],
        *,
        description: str = "",
    ) -> None:
        ToolSchemaValidator.validate_definition(parameters)
        self._tools[name] = executor
        self._schemas[name] = deepcopy(parameters)
        self._definitions[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }

    def definitions(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._definitions.values()))

    def has(self, name: str) -> bool:
        return name in self._tools

    def schema(self, name: str) -> dict[str, Any]:
        return deepcopy(self._schemas[name])

    def validate(self, name: str, arguments: Any) -> None:
        ToolSchemaValidator.validate_arguments(self._schemas[name], arguments)

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tools[name](arguments)
