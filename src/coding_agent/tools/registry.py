from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self._definitions: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        executor: Callable[[dict[str, Any]], dict[str, Any]],
        parameters: dict[str, Any],
        *,
        description: str = "",
    ) -> None:
        self._tools[name] = executor
        self._definitions[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }

    def definitions(self) -> list[dict[str, Any]]:
        return list(self._definitions.values())

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tools[name](arguments)
