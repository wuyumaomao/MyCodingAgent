# Debug Trace 与步骤耗时实现计划

> **状态：已被 `2026-09-02-trace-report.md` 取代。** 当前实现不再提供 `--debug` 或 span；详细消息统一写入 `report.json`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 在现有 run trace 上增加可选的完整消息调试信息和模型/工具步骤耗时，使一次真实 API 运行能够简洁复盘中间数据和性能。

**架构：** `RunRecorder` 继续是唯一 trace 持久化组件，新增 `debug` 开关；`AgentLoop` 直接在 `llm_response`、`tool_result` 和失败事件中写入 `duration_ms`。debug 只控制是否附加完整 messages、工具定义和规范化响应，不改变 Agent Loop 决策逻辑。

**技术栈：** Python 3.11+、标准库 `contextlib`、`time`、`uuid`、`json`、`pytest`；不增加外部依赖。

**规格文档：** `docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

## 设计修订（用户确认）

为保持 debug trace 简洁，取消嵌套 span、`span_id`、`parent_span_id` 以及步骤级开始/结束时间。最终只在 `llm_response`、`tool_result`、失败事件和 run 顶层记录 `duration_ms`；本节修订优先于下方原始 span 任务描述。

## 全局约束

- 默认 trace 行为和现有 CLI 保持兼容，`--debug` 默认关闭。
- 所有事件仍按递增 `seq` 写入同一个 `trace.json`，并采用原子替换持久化。
- debug 消息必须经过现有 `_sanitize()`；不得记录 API Key 或完整 SDK 对象。
- 只记录模型请求和工具执行等语义步骤的耗时，不为普通辅助函数创建独立事件。
- 不访问真实网络的单元测试必须继续通过；真实 API 只作为手动集成验证。

---

### 任务 1：扩展 RunRecorder 的 debug 和耗时能力

**文件：**
- 修改：`src/coding_agent/trace.py`
- 修改：`tests/test_trace.py`

**接口：**
- `RunRecorder.create(..., debug: bool = False) -> RunRecorder`
- `RunRecorder.debug: bool`
- `RunRecorder.record_debug(event_type: str, **payload: object) -> None`

- [ ] **步骤 1：编写失败测试**

```python
def test_debug_event_contains_payload_only_when_enabled(tmp_path):
    recorder = RunRecorder.create("q", tmp_path / "repo", tmp_path / "runs", debug=True)
    recorder.record_debug("llm_request", messages=[{"role": "user", "content": "q"}])
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert document["events"][-1]["messages"][0]["content"] == "q"


def test_span_events_include_parent_and_duration(tmp_path):
    recorder = RunRecorder.create("q", tmp_path / "repo", tmp_path / "runs")
    with recorder.span("AgentLoop", "run") as parent:
        with recorder.span("LLMClient", "complete") as child:
            assert child.parent_span_id == parent.span_id
    events = json.loads(recorder.trace_path.read_text(encoding="utf-8"))["events"]
    assert [event["type"] for event in events[-4:]] == ["span_start", "span_start", "span_end", "span_end"]
    assert events[-1]["status"] == "ok"
    assert "duration_ms" in events[-1]
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_trace.py::test_debug_event_contains_payload_only_when_enabled tests/test_trace.py::test_span_events_include_parent_and_duration -q`
预期：失败，因为 recorder 尚未支持 `debug`、`record_debug` 或 `span`。

- [ ] **步骤 3：实现最小功能**

增加 `debug` 字段；`record_debug()` 在 debug 关闭时不写详细 payload，开启时写入经过 `_sanitize()` 的 payload。`complete()` 和 `fail()` 为 run 写入总 `duration_ms`，保持原子持久化。

- [ ] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/test_trace.py -q`
预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add src/coding_agent/trace.py tests/test_trace.py
git commit -m "feat: add debug trace spans"
```

### 任务 2：记录 AgentLoop 和 LLMClient 的真实消息边界

**文件：**
- 修改：`src/coding_agent/agent.py`
- 修改：`tests/test_agent.py`

- [ ] **步骤 1：编写失败测试**

```python
def test_agent_debug_trace_contains_message_history_and_tool_decision(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("read", sample_git_repo, tmp_path / "runs", debug=True)
    llm = FakeLLM([
        AssistantTurn(None, [ToolCall("c1", "readfile", {"path": "README.md"})]),
        AssistantTurn("done", []),
    ])
    AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run("read", Workspace(sample_git_repo))
    events = json.loads(recorder.trace_path.read_text(encoding="utf-8"))["events"]
    requests = [event for event in events if event["type"] == "llm_request"]
    responses = [event for event in events if event["type"] == "llm_response"]
    assert requests[0]["messages"][-1]["content"] == "read"
    assert responses[0]["tool_calls"][0]["name"] == "readfile"
    assert any(event["type"] == "span_start" and event["component"] == "AgentLoop" for event in events)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_agent.py::test_agent_debug_trace_contains_message_history_and_tool_decision -q`
预期：失败，因为当前 trace 只保存消息数量，且没有 span 字段。

- [ ] **步骤 3：实现最小记录逻辑**

每轮 LLM 调用前记录开始时间，成功时将耗时写入 `llm_response`，失败时写入 `run_failed`；每次工具执行后将耗时写入 `tool_result`。调用前写 debug 请求（messages、工具定义），收到 `AssistantTurn` 后写规范化响应（content、tool call 名称和参数）。

- [ ] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/test_agent.py -q`
预期：PASS，未启用 recorder/debug 的既有行为不变。

- [ ] **步骤 5：提交**

```bash
git add src/coding_agent/agent.py tests/test_agent.py
git commit -m "feat: trace real llm message flow"
```

### 任务 3：增加 CLI `--debug` 开关并补充文档

**文件：**
- 修改：`src/coding_agent/cli.py`
- 修改：`tests/test_cli.py`
- 修改：`README.md`

- [ ] **步骤 1：编写失败测试**

```python
def test_cli_debug_flag_persists_full_llm_request(monkeypatch, sample_git_repo, tmp_path):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("coding_agent.cli.LLMClient", lambda **kwargs: FakeClient([AssistantTurn("ok", [])]))
    assert main(["Explain", "--debug", "--repo", str(sample_git_repo)]) == 0
    trace = next((tmp_path / "runs").glob("*/trace.json"))
    document = json.loads(trace.read_text(encoding="utf-8"))
    assert "messages" in next(event for event in document["events"] if event["type"] == "llm_request")
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_cli.py::test_cli_debug_flag_persists_full_llm_request -q`
预期：失败，因为 CLI 尚未解析或传递 `--debug`。

- [ ] **步骤 3：实现 CLI 开关**

在 argparse 中加入 `action="store_true"` 的 `--debug`，创建 recorder 时传入该值；默认不改变现有摘要 trace。README 说明 debug trace 的用途、路径和敏感信息风险。

- [ ] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/test_cli.py -q`
预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add src/coding_agent/cli.py tests/test_cli.py README.md
git commit -m "feat: add cli debug trace mode"
```

### 任务 4：完整验证和真实 API 手动检查

**文件：**
- 修改：`tests/test_trace_integration.py`

- [ ] **步骤 1：补充步骤耗时集成断言**

验证成功链路中的 `llm_response` 和 `tool_result` 都包含非负 `duration_ms`，事件 `seq` 严格递增。

- [ ] **步骤 2：运行完整自动化验证**

运行：`uv run pytest -q` 和 `uv run python -m compileall -q src`。
预期：所有测试通过，自动化测试不发起真实网络请求。

- [ ] **步骤 3：手动运行真实 API debug trace**

```powershell
uv run coding-agent "请读取 README.md 和 pyproject.toml，说明运行和测试方式" --repo .\demo-repo --debug
```

检查对应 `trace.json`：请求事件包含完整 messages，响应事件包含规范化 tool calls 和 `duration_ms`，工具结果包含 `duration_ms`，且 JSON 可解析。

- [ ] **步骤 4：提交最终验证**

```bash
git add tests/test_trace_integration.py
git commit -m "test: verify debug trace call hierarchy"
```
