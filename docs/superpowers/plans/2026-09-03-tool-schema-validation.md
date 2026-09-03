# Tool JSON Schema Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a client-side JSON Schema validation layer that validates every parsed tool call before tool-specific execution and returns recoverable errors to the model.

**Architecture:** `ToolRegistry` remains the source of truth for tool definitions and parameter schemas. A focused `ToolSchemaValidator` uses `jsonschema` to validate schema definitions at registration time and tool arguments at execution time. `AgentLoop` performs registry lookup and Schema validation before invoking a tool; runtime, repository-boundary, and business checks remain inside the concrete tool implementation.

**Tech Stack:** Python 3.11+, `jsonschema` Draft 2020-12 validator, existing OpenAI-compatible Chat Completions client, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

## Global Constraints

- Every registered tool must provide a top-level `type: object` parameter JSON Schema.
- Schema validation occurs after `ResponseParser` creates an internal `ToolCall` and before the concrete tool executes.
- Schema validation covers structure and declared constraints; it does not replace repository, filesystem, permission, or business checks.
- A parsed call with invalid arguments returns `invalid_tool_arguments` as a structured `role: tool` result and must not invoke the tool executor.
- An unknown tool returns `unknown_tool` and must not be executed or assigned an inferred schema.
- Provider-side `strict` support is optional defense-in-depth and never replaces local validation.
- Existing read-only behavior, tool limits, trace/report behavior, and response parsing must remain compatible.

---

### Task 1: Build the Schema Validator and Registry Contract

**Files:**
- Create: `src/coding_agent/tools/schema.py`
- Modify: `src/coding_agent/tools/registry.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_registry.py`

**Interfaces:**
- `ToolSchemaError(ValueError)`: internal exception for invalid tool definitions or arguments.
- `ToolSchemaValidator.validate_definition(schema: dict[str, Any]) -> None`: verifies a registered schema is a valid Draft 2020-12 object schema with top-level `type: object`.
- `ToolSchemaValidator.validate_arguments(schema: dict[str, Any], arguments: Any) -> None`: raises `ToolSchemaError` with a stable message when arguments do not match the schema.
- `ToolRegistry.register(...) -> None`: validates `parameters` before storing the tool.
- `ToolRegistry.has(name: str) -> bool`.
- `ToolRegistry.schema(name: str) -> dict[str, Any]`: returns the registered schema or raises `KeyError`.
- `ToolRegistry.validate(name: str, arguments: Any) -> None`: resolves the schema and validates arguments, preserving `KeyError` for unknown tools.

- [ ] **Step 1: Write failing registry and validator tests**

Add tests covering:

```python
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
```

Also assert that a malformed definition such as `{"type": "array"}` is rejected during `register`, valid definitions remain available through `definitions()`, and an unknown tool still raises `KeyError`.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
uv run pytest tests/test_registry.py -q
```

Expected: FAIL because `ToolSchemaValidator`, `ToolSchemaError`, and the registry validation methods do not yet exist.

- [ ] **Step 3: Add the dependency and implement the validator**

Add `jsonschema>=4.0` to the main project dependencies and refresh the lock file with `uv lock`. In `schema.py`, use `Draft202012Validator.check_schema(schema)` for developer-supplied definitions and `Draft202012Validator(schema).iter_errors(arguments)` for calls. Sort validation errors deterministically by their JSON path, but expose only the stable public message `Tool arguments do not match the registered schema` to callers.

- [ ] **Step 4: Integrate validation into ToolRegistry**

Store a private `_schemas` mapping alongside `_tools` and `_definitions`. Validate the schema before mutating registry state in `register`; make `schema()` return a defensive copy; make `validate()` resolve the registered schema and delegate to `ToolSchemaValidator.validate_arguments()`.

- [ ] **Step 5: Run the focused tests and verify success**

Run:

```powershell
uv run pytest tests/test_registry.py -q
```

Expected: PASS, including existing registration and unknown-tool behavior.

- [ ] **Step 6: Commit the registry layer**

```powershell
git add src/coding_agent/tools/schema.py src/coding_agent/tools/registry.py pyproject.toml uv.lock tests/test_registry.py
git commit -m "feat: add tool schema validation contract"
```

### Task 2: Validate Parsed Calls Before Execution

**Files:**
- Modify: `src/coding_agent/agent.py`
- Test: `tests/test_agent.py`
- Test: `tests/test_trace_integration.py`

**Interfaces:**
- `AgentLoop._execute(call: ToolCall) -> dict[str, Any]` remains the single execution boundary.
- `_execute()` returns `unknown_tool` for an unregistered name, `invalid_tool_arguments` for Schema failures, and delegates to the concrete executor only after validation passes.

- [ ] **Step 1: Write failing AgentLoop tests**

Add a spy executor and a two-turn fake model. The first model turn should call `readfile` with `{"path": 123}`; the second should return a final answer. Assert the final answer is returned, the spy executor call count is zero, and the second model request contains a tool message with `"invalid_tool_arguments"`. Add a separate test asserting an unknown tool produces `"unknown_tool"` without raising. Preserve the existing workspace-violation test to prove valid Schema arguments still reach tool-level safety checks.

- [ ] **Step 2: Run the focused AgentLoop tests and verify failure**

Run:

```powershell
uv run pytest tests/test_agent.py -q
```

Expected: FAIL because `_execute()` currently dispatches directly to the executor and does not call `ToolRegistry.validate()`.

- [ ] **Step 3: Add the validation gate in `_execute()`**

Before invoking `registry.execute()`, call `registry.validate(call.name, call.arguments)`. Convert `ToolSchemaError` to:

```python
{
    "ok": False,
    "error": {
        "type": "invalid_tool_arguments",
        "message": "Tool arguments do not match the registered schema",
    },
}
```

Keep the existing `KeyError` mapping for unknown tools and the generic `tool_error` mapping for unexpected executor failures. Do not change the `ConversationContext` message ordering or per-tool call limit behavior.

- [ ] **Step 4: Add trace assertions for rejected calls**

Assert that a Schema-rejected call still records `tool_call` and `tool_result`, that `tool_result.ok` is false, that the result payload contains `invalid_tool_arguments`, and that the concrete executor was not called. The tool result must be available to the next LLM request so the model can recover.

- [ ] **Step 5: Run AgentLoop and trace tests**

Run:

```powershell
uv run pytest tests/test_agent.py tests/test_trace_integration.py -q
```

Expected: PASS, with existing successful calls, workspace errors, tool limits, and trace sequencing unchanged.

- [ ] **Step 6: Commit the execution gate**

```powershell
git add src/coding_agent/agent.py tests/test_agent.py tests/test_trace_integration.py
git commit -m "feat: validate tool calls before execution"
```

### Task 3: Align Built-in Tool Schemas and End-to-End Coverage

**Files:**
- Modify: `src/coding_agent/tools/readfile.py`
- Modify: `src/coding_agent/tools/listfiles.py`
- Test: `tests/test_readfile.py`
- Test: `tests/test_listfiles.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- `ReadFileTool.parameters` and `ListFilesTool.parameters` remain the provider-facing schemas registered by the CLI.
- Concrete tool `execute()` methods continue to retain runtime checks as defense-in-depth.

- [ ] **Step 1: Write schema and compatibility tests**

Assert that built-in schemas expose `type: object` and `additionalProperties: False`; `readfile` requires `path`; `listfiles` declares non-negative `max_depth` and positive `max_entries`. Add execution tests proving malformed values are rejected consistently whether reached through the registry validation path or through direct tool invocation.

- [ ] **Step 2: Run the built-in tool tests and verify any failures**

Run:

```powershell
uv run pytest tests/test_readfile.py tests/test_listfiles.py tests/test_llm.py -q
```

Expected: existing direct-tool behavior remains green; any schema mismatch identifies the exact built-in definition to correct.

- [ ] **Step 3: Tighten only the declared constraints required by the PRD**

Keep optional fields optional for `listfiles`, keep direct tool error types stable, and add only constraints that are already enforced by the implementations. Do not put filesystem existence, repository-boundary, permission, encoding, or file-size rules into JSON Schema.

- [ ] **Step 4: Run the complete test suite**

Run:

```powershell
uv run pytest -q
```

Expected: all existing tests and the new schema tests pass.

- [ ] **Step 5: Commit built-in schema coverage**

```powershell
git add src/coding_agent/tools/readfile.py src/coding_agent/tools/listfiles.py tests/test_readfile.py tests/test_listfiles.py tests/test_llm.py
git commit -m "test: cover built-in tool schemas"
```

### Task 4: Document and Verify the Feature

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

**Interfaces:**
- User-facing documentation names the validation order and explains recoverable Schema errors without promising that Schema replaces runtime safety checks.

- [ ] **Step 1: Update README architecture and error behavior**

Document that the agent validates parsed tool arguments against the registered JSON Schema before executing `listfiles` or `readfile`; explain that invalid arguments are returned to the model as a structured tool result and that path and filesystem safety checks remain in the tools.

- [ ] **Step 2: Self-review the PRD and README**

Check that the documents consistently describe the order `ResponseParser -> tool lookup -> JSON Schema -> tool safety -> execution`, the exact error type `invalid_tool_arguments`, and the optional nature of provider-side `strict` mode.

- [ ] **Step 3: Run final verification commands**

Run:

```powershell
uv run pytest -q
uv run python -m compileall -q src
git diff --check
```

Expected: tests pass, compilation succeeds, and no whitespace errors are reported.

- [ ] **Step 4: Commit documentation and final verification**

```powershell
git add README.md docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md
git commit -m "docs: specify tool schema validation flow"
```
