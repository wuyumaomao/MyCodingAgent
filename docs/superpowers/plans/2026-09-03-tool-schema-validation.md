# 工具 JSON Schema 校验实施计划

> **对于 Agent 执行者：** REQUIRED SUB-SKILL：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐项执行本计划。步骤使用复选框（`- [ ]`）跟踪。

**目标：** 增加客户端 JSON Schema 校验层，在工具执行前校验每一个已解析的 tool call，并将可恢复的错误返回给模型。

**架构：** `ToolRegistry` 继续作为工具定义和参数 Schema 的唯一来源。新增独立的 `ToolSchemaValidator`，使用 `jsonschema` 在工具注册时校验 Schema 定义，在执行时校验模型参数。`AgentLoop` 先确认工具已注册并完成 Schema 校验，再调用具体工具；运行时、仓库边界和业务检查继续由具体工具负责。

**技术栈：** Python 3.11+、`jsonschema` Draft 2020-12 校验器、现有 OpenAI-compatible Chat Completions 客户端、pytest、uv。

**规格文档：** `docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

## 全局约束

- 每个注册工具必须提供顶层为 `type: object` 的参数 JSON Schema。
- Schema 校验发生在 `ResponseParser` 创建内部 `ToolCall` 之后、具体工具执行之前。
- Schema 校验负责结构和声明级约束，不能替代仓库、文件系统、权限或业务检查。
- 已解析但参数无效的调用返回结构化 `invalid_tool_arguments` tool result，不能调用工具执行器。
- 未知工具返回 `unknown_tool`，不能执行，也不能猜测或推导 Schema。
- Provider 侧的 `strict` 支持只能作为额外约束，不能替代本地校验。
- 现有只读行为、工具调用限制、trace/report 行为和响应解析行为必须保持兼容。

---

### 任务 1：实现 Schema 校验器和 Registry 契约

**文件：**
- 新建：`src/coding_agent/tools/schema.py`
- 修改：`src/coding_agent/tools/registry.py`
- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 测试：`tests/test_registry.py`

**接口：**
- `ToolSchemaError(ValueError)`：工具定义或工具参数无效时使用的内部异常。
- `ToolSchemaValidator.validate_definition(schema: dict[str, Any]) -> None`：确认注册的 Schema 是合法的 Draft 2020-12 object schema，且顶层 `type` 为 `object`。
- `ToolSchemaValidator.validate_arguments(schema: dict[str, Any], arguments: Any) -> None`：参数不符合 Schema 时抛出 `ToolSchemaError`，并使用稳定的错误消息。
- `ToolRegistry.register(...) -> None`：保存工具前先校验 `parameters`。
- `ToolRegistry.has(name: str) -> bool`。
- `ToolRegistry.schema(name: str) -> dict[str, Any]`：返回注册的 Schema；未知工具抛出 `KeyError`。
- `ToolRegistry.validate(name: str, arguments: Any) -> None`：查找工具 Schema 并校验参数；未知工具保留 `KeyError`。

- [ ] **步骤 1：编写 Registry 和校验器失败测试**

增加以下覆盖：

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

同时断言：`{"type": "array"}` 这样的错误定义会在 `register` 时被拒绝；合法定义仍可通过 `definitions()` 导出；未知工具仍抛出 `KeyError`。

- [ ] **步骤 2：运行聚焦测试并确认失败**

运行：

```powershell
uv run pytest tests/test_registry.py -q
```

预期：失败，因为 `ToolSchemaValidator`、`ToolSchemaError` 和 Registry 校验方法尚不存在。

- [ ] **步骤 3：增加依赖并实现校验器**

在项目主依赖中增加 `jsonschema>=4.0`，然后运行 `uv lock` 更新锁文件。在 `schema.py` 中使用 `Draft202012Validator.check_schema(schema)` 校验开发者提供的定义，使用 `Draft202012Validator(schema).iter_errors(arguments)` 校验模型调用参数。按 JSON path 对校验错误稳定排序，但对外只暴露统一消息：`Tool arguments do not match the registered schema`。

- [ ] **步骤 4：将校验接入 ToolRegistry**

在 `_tools` 和 `_definitions` 旁边增加私有 `_schemas` 映射。`register` 必须在修改 Registry 状态前完成 Schema 校验；`schema()` 返回防御性拷贝；`validate()` 查找对应 Schema，并委托给 `ToolSchemaValidator.validate_arguments()`。

- [ ] **步骤 5：运行聚焦测试并确认通过**

运行：

```powershell
uv run pytest tests/test_registry.py -q
```

预期：通过，包括现有工具注册和未知工具行为。

- [ ] **步骤 6：提交 Registry 层**

```powershell
git add src/coding_agent/tools/schema.py src/coding_agent/tools/registry.py pyproject.toml uv.lock tests/test_registry.py
git commit -m "feat: add tool schema validation contract"
```

### 任务 2：在工具执行前校验已解析调用

**文件：**
- 修改：`src/coding_agent/agent.py`
- 测试：`tests/test_agent.py`
- 测试：`tests/test_trace_integration.py`

**接口：**
- `AgentLoop._execute(call: ToolCall) -> dict[str, Any]` 继续作为唯一的工具执行边界。
- `_execute()` 对未注册工具返回 `unknown_tool`，对 Schema 失败返回 `invalid_tool_arguments`，只有校验通过后才委托具体执行器。

- [ ] **步骤 1：编写 AgentLoop 失败测试**

增加一个带调用计数的 spy executor 和两轮 fake model。第一轮模型调用 `readfile`，参数为 `{"path": 123}`；第二轮返回最终答案。断言最终答案正常返回、spy executor 调用次数为 0、第二次模型请求包含带有 `invalid_tool_arguments` 的 tool 消息。另加未知工具测试，确认返回 `unknown_tool` 而不是抛出异常。保留现有 workspace violation 测试，证明 Schema 合法的参数仍会进入工具级安全检查。

- [ ] **步骤 2：运行聚焦 AgentLoop 测试并确认失败**

运行：

```powershell
uv run pytest tests/test_agent.py -q
```

预期：失败，因为当前 `_execute()` 直接调用执行器，没有调用 `ToolRegistry.validate()`。

- [ ] **步骤 3：在 `_execute()` 中增加校验门**

在调用 `registry.execute()` 前调用 `registry.validate(call.name, call.arguments)`。将 `ToolSchemaError` 转换为：

```python
{
    "ok": False,
    "error": {
        "type": "invalid_tool_arguments",
        "message": "Tool arguments do not match the registered schema",
    },
}
```

保留现有 `KeyError` 到 `unknown_tool` 的映射，以及执行器未知异常到 `tool_error` 的映射。不要改变 `ConversationContext` 消息顺序或每个工具的调用限制行为。

- [ ] **步骤 4：增加被拒绝调用的 trace 断言**

断言 Schema 拒绝的调用仍会记录 `tool_call` 和 `tool_result`；`tool_result.ok` 为 false；结果中包含 `invalid_tool_arguments`；具体执行器没有被调用。该 tool result 必须进入下一次 LLM 请求，使模型能够修正参数并恢复。

- [ ] **步骤 5：运行 AgentLoop 和 trace 测试**

运行：

```powershell
uv run pytest tests/test_agent.py tests/test_trace_integration.py -q
```

预期：通过，已有成功调用、workspace 错误、工具调用限制和 trace 顺序保持不变。

- [ ] **步骤 6：提交执行校验门**

```powershell
git add src/coding_agent/agent.py tests/test_agent.py tests/test_trace_integration.py
git commit -m "feat: validate tool calls before execution"
```

### 任务 3：对齐内置工具 Schema 并补充端到端覆盖

**文件：**
- 修改：`src/coding_agent/tools/readfile.py`
- 修改：`src/coding_agent/tools/listfiles.py`
- 测试：`tests/test_readfile.py`
- 测试：`tests/test_listfiles.py`
- 测试：`tests/test_llm.py`

**接口：**
- `ReadFileTool.parameters` 和 `ListFilesTool.parameters` 继续作为 CLI 注册并发送给 provider 的 Schema。
- 具体工具的 `execute()` 方法继续保留运行时检查，作为纵深防御。

- [ ] **步骤 1：编写 Schema 和兼容性测试**

断言内置 Schema 的顶层 `type` 为 `object`，并声明 `additionalProperties: False`；`readfile` 必须要求 `path`；`listfiles` 必须声明非负的 `max_depth` 和正数的 `max_entries`。增加执行测试，证明无论通过 Registry 校验路径还是直接调用工具，错误参数都能得到一致处理。

- [ ] **步骤 2：运行内置工具测试并定位失败**

运行：

```powershell
uv run pytest tests/test_readfile.py tests/test_listfiles.py tests/test_llm.py -q
```

预期：现有直接调用工具的测试保持通过；如果 Schema 不一致，测试会指出需要修正的内置定义。

- [ ] **步骤 3：只收紧 PRD 要求的声明级约束**

保留 `listfiles` 的可选字段，保持直接工具调用的错误类型稳定，只增加实现已经执行的约束。不要把文件存在性、仓库边界、权限、编码或文件大小规则放入 JSON Schema。

- [ ] **步骤 4：运行完整测试套件**

运行：

```powershell
uv run pytest -q
```

预期：已有测试和新增 Schema 测试全部通过。

- [ ] **步骤 5：提交内置工具 Schema 覆盖**

```powershell
git add src/coding_agent/tools/readfile.py src/coding_agent/tools/listfiles.py tests/test_readfile.py tests/test_listfiles.py tests/test_llm.py
git commit -m "test: cover built-in tool schemas"
```

### 任务 4：更新文档并完成验证

**文件：**
- 修改：`README.md`
- 修改：`docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

**接口：**
- 面向用户的文档说明校验顺序和可恢复的 Schema 错误，不承诺 Schema 可以替代运行时安全检查。

- [ ] **步骤 1：更新 README 架构和错误行为说明**

说明 Agent 会在执行 `listfiles` 或 `readfile` 前，根据注册的 JSON Schema 校验已解析的 tool arguments；无效参数会作为结构化 tool result 返回模型；路径和文件系统安全检查仍由工具负责。

- [ ] **步骤 2：自检 PRD 和 README**

确认所有文档都一致描述以下顺序：`ResponseParser -> 工具查找 -> JSON Schema -> 工具安全检查 -> 执行`；确认错误类型统一为 `invalid_tool_arguments`；确认 provider 的 `strict` 是可选能力。

- [ ] **步骤 3：运行最终验证命令**

运行：

```powershell
uv run pytest -q
uv run python -m compileall -q src
git diff --check
```

预期：测试通过、编译成功且没有空白字符错误。

- [ ] **步骤 4：提交文档和最终验证**

```powershell
git add README.md docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md
git commit -m "docs: specify tool schema validation flow"
```
