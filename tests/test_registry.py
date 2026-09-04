from __future__ import annotations

import pytest

from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.schema import ToolSchemaError


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


def test_registry_validates_required_type_and_extra_fields():
    registry = ToolRegistry()
    registry.register(
        "readfile",
        fake_tool,
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    registry.validate("readfile", {"path": "README.md"})
    with pytest.raises(ToolSchemaError):
        registry.validate("readfile", {})
    with pytest.raises(ToolSchemaError):
        registry.validate("readfile", {"path": 123})
    with pytest.raises(ToolSchemaError):
        registry.validate("readfile", {"path": "README.md", "extra": True})


def test_registry_rejects_invalid_definition_before_registering():
    registry = ToolRegistry()

    with pytest.raises(ToolSchemaError):
        registry.register("bad", fake_tool, {"type": "array"})
    assert registry.definitions() == []


def test_registry_exposes_schema_copy_and_unknown_validation_errors():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    registry = ToolRegistry()
    registry.register("readfile", fake_tool, schema)

    exported = registry.schema("readfile")
    exported["properties"]["path"]["type"] = "integer"
    assert registry.schema("readfile")["properties"]["path"]["type"] == "string"
    assert registry.has("readfile") is True
    assert registry.has("missing") is False
    with pytest.raises(KeyError):
        registry.schema("missing")
    with pytest.raises(KeyError):
        registry.validate("missing", {})
