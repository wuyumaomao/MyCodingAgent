# ConversationContext 上下文管理实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 将 Agent 的 system prompt、workspace 上下文和对话 history 从 `AgentLoop` 中分离到独立的 `ConversationContext`，保证每轮发送完整且顺序稳定的消息。

**架构：** 扩展现有 `context.py`。`ConversationContext` 在创建时构造不变的 `system_messages`，并维护可变的 `history`；用户请求、assistant tool call 和 tool result 都按顺序追加到 history。`AgentLoop` 只负责循环、模型调用和工具执行，通过 `context.messages()` 获取待发送消息。

**技术栈：** Python 3.11+、现有 `Workspace`、`AssistantTurn`、`ToolCall`、标准库 `copy`（如需要）和 `pytest`；不增加外部依赖。

**规格文档：** `docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

## 全局约束

- system messages 在一次 run 内只构造一次，不因工具调用重复扫描仓库。
- history 只追加，不修改已经发送的消息顺序。
- `messages()` 返回 `system_messages + history` 的完整列表。
- 用户 query 只加入一次 history，不在每轮重复添加。
- 保持现有 4 轮限制、工具行为、debug trace 和超时行为不变。

---

### 任务 1：实现 ConversationContext

**文件：**
- 修改：`src/coding_agent/context.py`
- 新增：`tests/test_context.py`（在现有测试基础上扩展）

**接口：**
- `ConversationContext(workspace: Workspace, repository_context_builder: Callable[[Workspace], str] = build_repository_context)`
- `add_user_request(query: str) -> None`
- `add_assistant_turn(turn: AssistantTurn) -> None`
- `add_tool_result(call: ToolCall, result: dict[str, Any]) -> None`
- `messages() -> list[dict[str, Any]]`
- `system_messages: list[dict[str, Any]]`
- `history: list[dict[str, Any]]`

- [ ] **步骤 1：编写失败测试**

```python
def test_context_keeps_static_system_messages_and_appends_history(sample_git_repo):
    context = ConversationContext(Workspace(sample_git_repo))
    context.add_user_request("Explain scripts")
    context.add_assistant_turn(
        AssistantTurn(None, [ToolCall("c1", "readfile", {"path": "README.md"})])
    )
    context.add_tool_result(ToolCall("c1", "readfile", {"path": "README.md"}), {"ok": True})

    messages = context.messages()
    assert [message["role"] for message in messages[-3:]] == ["user", "assistant", "tool"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "system"
    assert len(context.system_messages) == 2
    assert len(context.history) == 3
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_context.py::test_context_keeps_static_system_messages_and_appends_history -q`
预期：失败，因为 `ConversationContext` 尚不存在。

- [ ] **步骤 3：实现最小上下文管理**

将当前 `AgentLoop` 中的 Agent 规则移动为常量 system message；构造第二条 system message 时调用现有 `build_repository_context(workspace)`。`add_assistant_turn()` 复用 `_assistant_tool_message()` 的消息格式；`add_tool_result()` 复用 `_tool_result_message()` 的 JSON 序列化格式。`messages()` 返回新的列表，避免调用方改变内部 system/history 容器。

- [ ] **步骤 4：运行上下文测试**

运行：`uv run pytest tests/test_context.py -q`
预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add src/coding_agent/context.py tests/test_context.py
git commit -m "refactor: add conversation context manager"
```

### 任务 2：迁移 AgentLoop 使用 ConversationContext

**文件：**
- 修改：`src/coding_agent/agent.py`
- 修改：`tests/test_agent.py`

**接口：**
- `AgentLoop.run()` 创建 `ConversationContext`，先调用 `add_user_request(query)`。
- 每轮调用 `llm_client.complete(context.messages(), tool_definitions)`。
- tool call 和 tool result 通过 context 追加，现有 trace 事件继续记录。

- [ ] **步骤 1：编写失败测试**

```python
def test_agent_loop_uses_context_for_each_complete_request(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("read", sample_git_repo, tmp_path / "runs", debug=True)
    llm = FakeLLM([
        AssistantTurn(None, [ToolCall("c1", "readfile", {"path": "README.md"})]),
        AssistantTurn("done", []),
    ])
    answer = AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
        "read", Workspace(sample_git_repo)
    )
    assert answer == "done"
    assert len(llm.messages[0]) == 3
    assert len(llm.messages[1]) == 5
    assert llm.messages[1][-2]["role"] == "assistant"
    assert llm.messages[1][-1]["role"] == "tool"
```

- [ ] **步骤 2：运行测试确认失败或暴露回归**

运行：`uv run pytest tests/test_agent.py::test_agent_loop_uses_context_for_each_complete_request -q`
预期：在重构前该测试可能通过消息数量断言，但实现仍直接拼接；先确认现有行为，再通过后续迁移保证消息来源改为 `ConversationContext`。

- [ ] **步骤 3：迁移 AgentLoop**

删除 `AgentLoop.run()` 内部的 system/user 字典初始化和 `_assistant_tool_message`/`_tool_result_message` 的直接调用；创建 `ConversationContext` 后使用其追加接口。保留每轮 debug trace 的 `messages` 快照、耗时记录、超时处理、4 轮限制和最终答案逻辑。消息快照在传给 LLM 前生成，避免后续追加影响已记录内容。

- [ ] **步骤 4：运行 Agent、trace 和 CLI 测试**

运行：`uv run pytest tests/test_agent.py tests/test_trace_integration.py tests/test_cli.py -q`
预期：PASS，消息轮次、工具调用、debug trace 和 CLI 行为保持一致。

- [ ] **步骤 5：提交**

```bash
git add src/coding_agent/agent.py tests/test_agent.py
git commit -m "refactor: route agent messages through context"
```

### 任务 3：文档和最终验证

**文件：**
- 修改：`README.md`
- 修改：`docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`
- 修改：`tests/test_context.py`

- [ ] **步骤 1：补充上下文顺序和不可变 system 测试**

断言添加 history 后 `system_messages` 长度和内容不变；第二次 `messages()` 调用仍返回相同顺序，且外部对返回列表追加元素不会污染内部 history。

- [ ] **步骤 2：更新 README**

在架构说明中补充 `ConversationContext`，说明 system messages 任务内固定、history 按轮次追加、每次请求发送完整消息历史。

- [ ] **步骤 3：运行完整验证**

运行：`uv run pytest -q`、`uv run python -m compileall -q src`、`git diff --check`。
预期：所有测试通过，编译和 diff 检查成功。

- [ ] **步骤 4：提交文档和最终测试**

```bash
git add README.md docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md tests/test_context.py
git commit -m "docs: describe conversation context flow"
```
