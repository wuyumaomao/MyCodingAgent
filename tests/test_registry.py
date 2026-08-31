from __future__ import annotations

import pytest

from coding_agent.tools.registry import ToolRegistry


def fake_tool(arguments):
    return {"ok": True, "arguments": arguments}


def test_registry_exports_native_tool_schema():
    registry = ToolRegistry()
    registry.register(
        "readfile",
        fake_tool,
        {"type": "object", "properties": {"path": {"type": "string"}}},
        description="Read a file",
    )
    definition = registry.definitions()[0]
    assert definition["type"] == "function"
    assert definition["function"]["name"] == "readfile"
    assert definition["function"]["description"] == "Read a file"


def test_registry_executes_and_rejects_unknown_tools():
    registry = ToolRegistry()
    registry.register("readfile", fake_tool, {"type": "object"})
    assert registry.execute("readfile", {"path": "README.md"})["ok"] is True
    with pytest.raises(KeyError):
        registry.execute("missing", {})
