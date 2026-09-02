# 工具调用次数限制实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 为每个工具增加独立的最大调用次数限制，默认每个工具最多调用 3 次，超限时返回结构化错误。

**架构：** 新增不可变 `AgentLimits` 配置对象保存 `max_calls_per_tool`，由 `AgentLoop` 注入并维护按工具名称计数。`ToolRegistry` 继续只负责注册和执行；AgentLoop 在执行前检查限制，超限不执行工具，而是把错误结果追加到上下文并继续让模型决策。trace/report 记录超限事件。

**技术栈：** Python 3.11+、标准库 `dataclasses`、`collections`、`pytest`；不增加外部依赖。

**规格文档：** `docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

## 全局约束

- 每个工具默认最多调用 3 次。
- 计数按工具名称分别维护，不同工具互不影响。
- 超限时不执行工具，返回 `tool_call_limit` 结构化错误。
- 超限结果仍追加到上下文，模型可以结束或选择其他工具。
- 现有 4 轮限制、trace/report、超时和工作区安全规则保持不变。

---

### 任务 1：新增 AgentLimits 和调用计数

**文件：**
- 修改：`src/coding_agent/agent.py`
- 修改：`tests/test_agent.py`

**接口：**
- `AgentLimits(max_rounds: int = 4, max_calls_per_tool: int = 3)`
- `AgentLoop(..., limits: AgentLimits | None = None)`

- [ ] **步骤 1：写失败测试**

```python
def test_loop_rejects_tool_after_per_tool_limit(sample_git_repo):
    llm = FakeLLM([
        AssistantTurn(None, [ToolCall(f"c{i}", "readfile", {"path": "README.md"})])
        for i in range(4)
    ])
    answer = AgentLoop(
        llm,
        make_registry(sample_git_repo),
        limits=AgentLimits(max_rounds=4, max_calls_per_tool=3),
    ).run("read", Workspace(sample_git_repo))
    assert llm.messages[-1][-1]["role"] == "tool"
    assert "tool_call_limit" in llm.messages[-1][-1]["content"]
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_agent.py::test_loop_rejects_tool_after_per_tool_limit -q`
预期：失败，因为当前没有 `AgentLimits` 和按工具计数逻辑。

- [ ] **步骤 3：实现限制策略**

新增 `AgentLimits` 并校验两个值为正数；AgentLoop 使用 `defaultdict(int)` 计数。每个 tool call 执行前检查 `counts[call.name] >= limits.max_calls_per_tool`，超限生成 `{"ok": false, "error": {"type": "tool_call_limit", ...}}`，不调用 registry；未超限则递增后执行。

- [ ] **步骤 4：运行 Agent 测试**

运行：`uv run pytest tests/test_agent.py -q`
预期：PASS。

### 任务 2：记录超限事件并接入 CLI

**文件：**
- 修改：`src/coding_agent/agent.py`
- 修改：`src/coding_agent/cli.py`
- 修改：`tests/test_trace_integration.py`
- 修改：`tests/test_cli.py`

- [ ] **步骤 1：写失败测试**

```python
def test_trace_records_tool_call_limit(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("read", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM([
        AssistantTurn(None, [ToolCall(f"c{i}", "readfile", {"path": "README.md"})])
        for i in range(4)
    ])
    AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
        "read", Workspace(sample_git_repo)
    )
    trace = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert any(event.get("error_type") == "tool_call_limit" for event in trace["events"])
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_trace_integration.py::test_trace_records_tool_call_limit -q`
预期：失败，因为超限事件尚未写入 trace。

- [ ] **步骤 3：实现记录和配置**

超限时调用 recorder 记录 `tool_call_limit` 事件（工具名、限制值、已调用次数），同时保留结构化 tool result。CLI 增加 `--max-tool-calls`，传入 `AgentLimits(max_calls_per_tool=...)`；默认值为 3。

- [ ] **步骤 4：运行 CLI、trace 测试**

运行：`uv run pytest tests/test_trace_integration.py tests/test_cli.py -q`
预期：PASS。

### 任务 3：文档和最终验证

**文件：**
- 修改：`README.md`
- 修改：`docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

- [ ] **步骤 1：更新 README**

说明默认每个工具最多调用 3 次，超限会作为错误结果返回模型；给出 `--max-tool-calls 5` 示例。

- [ ] **步骤 2：运行完整验证**

运行：`uv run pytest -q`、`uv run python -m compileall -q src`、`git diff --check`。
预期：所有测试通过。
