# Trace 与 Report 双文件记录实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 移除 debug 开关，为每次 run 同时持久化简洁 `trace.json` 和详细 `report.json`。

**架构：** `RunRecorder` 为同一 run 管理两个文档和两个路径。`record()` 接收摘要字段，并可通过 `report_payload` 附加详细字段；摘要事件写入 trace，合并后的完整事件写入 report。AgentLoop 在请求/响应/工具边界提供两套字段，CLI 输出两个文件路径。

**技术栈：** Python 3.11+、标准库 `dataclasses`、`json`、`tempfile`、`pathlib`、`pytest`；不增加外部依赖。

**规格文档：** `docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

## 全局约束

- 每次 run 固定生成 `trace.json` 和 `report.json`。
- trace 只保存摘要；report 保存完整 messages、tools、模型响应和工具结果。
- 两个文档事件序号一致，所有写入仍采用临时文件替换。
- report 和 trace 都使用现有递归字段脱敏；不保存完整 SDK 对象。
- 删除 CLI `--debug`，不再需要 debug 条件分支。

---

### 任务 1：扩展 RunRecorder 双文档持久化

**文件：**
- 修改：`src/coding_agent/trace.py`
- 修改：`tests/test_trace.py`

**接口：**
- `RunRecorder.create(query, repo_root, runs_root=None) -> RunRecorder`
- `RunRecorder.trace_path: Path`
- `RunRecorder.report_path: Path`
- `RunRecorder.record(event_type: str, report_payload: dict[str, object] | None = None, **payload: object) -> None`

- [ ] **步骤 1：写失败测试**

```python
def test_recorder_persists_summary_trace_and_detailed_report(tmp_path):
    recorder = RunRecorder.create("q", tmp_path / "repo", tmp_path / "runs")
    recorder.record(
        "llm_request",
        round=1,
        message_count=3,
        report_payload={"messages": [{"role": "user", "content": "q"}]},
    )
    trace = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    report = json.loads(recorder.report_path.read_text(encoding="utf-8"))
    assert "messages" not in trace["events"][-1]
    assert report["events"][-1]["messages"][0]["content"] == "q"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_trace.py::test_recorder_persists_summary_trace_and_detailed_report -q`
预期：失败，因为 recorder 目前只有一个文档和一个路径。

- [ ] **步骤 3：实现双文档**

新增 `report_path` 和 report 文档；`record()` 将摘要 payload 写入 trace，将摘要与 `report_payload` 合并后写入 report。`complete()`、`fail()` 同步更新两个文档状态和耗时。`_persist()` 为两个文件分别创建临时文件并替换。

- [ ] **步骤 4：运行 trace 测试**

运行：`uv run pytest tests/test_trace.py -q`
预期：PASS。

### 任务 2：让 AgentLoop 记录双层事件

**文件：**
- 修改：`src/coding_agent/agent.py`
- 修改：`tests/test_agent.py`

- [ ] **步骤 1：写失败测试**

```python
def test_agent_report_contains_full_messages_without_debug_flag(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("read", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM([AssistantTurn("done", [])])
    AgentLoop(llm, make_registry(sample_git_repo), recorder=recorder).run(
        "read", Workspace(sample_git_repo)
    )
    report = json.loads(recorder.report_path.read_text(encoding="utf-8"))
    request = next(event for event in report["events"] if event["type"] == "llm_request")
    assert request["messages"][-1]["content"] == "read"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_agent.py::test_agent_report_contains_full_messages_without_debug_flag -q`
预期：失败，因为当前完整消息只在 debug 条件下记录。

- [ ] **步骤 3：实现事件分层**

请求事件 trace 保存 `round`/`message_count`，report 额外保存完整 `messages`/`tools`；响应事件 trace 保存布尔值和数量，report 额外保存 `content`/规范化 `tool_calls`；工具结果 trace 保存成功摘要和耗时，report 保存完整 result。删除 `recorder.debug` 分支。

- [ ] **步骤 4：运行 Agent 和集成测试**

运行：`uv run pytest tests/test_agent.py tests/test_trace_integration.py -q`
预期：PASS。

### 任务 3：移除 CLI debug 并输出两个路径

**文件：**
- 修改：`src/coding_agent/cli.py`
- 修改：`tests/test_cli.py`

- [ ] **步骤 1：写失败测试**

```python
def test_cli_outputs_report_path(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("coding_agent.cli.LLMClient", lambda **kwargs: FakeClient([AssistantTurn("ok", [])]))
    assert main(["Explain", "--repo", str(sample_git_repo)]) == 0
    assert "Report:" in capsys.readouterr().err
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_cli.py::test_cli_outputs_report_path -q`
预期：失败，因为 CLI 目前只输出 trace 路径且仍支持 `--debug`。

- [ ] **步骤 3：实现 CLI 输出**

删除 `--debug` 参数和 `args.debug` 传递；成功和失败路径都输出 `Run:`、`Trace:`、`Report:` 三行。

- [ ] **步骤 4：运行 CLI 测试**

运行：`uv run pytest tests/test_cli.py -q`
预期：PASS。

### 任务 4：完整验证和文档收尾

**文件：**
- 修改：`README.md`
- 修改：`docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`
- 修改：`tests/test_trace_integration.py`

- [ ] **步骤 1：补充双文件一致性测试**

断言成功和失败 run 的 trace/report 都存在，事件 `seq` 对齐，report 包含完整 messages，trace 不包含详细 messages。

- [ ] **步骤 2：运行完整验证**

运行：`uv run pytest -q`、`uv run python -m compileall -q src`、`git diff --check`。
预期：所有测试通过。

- [ ] **步骤 3：手动真实 API 验证**

```powershell
uv run coding-agent "请读取 README.md" --repo .\demo-repo
```

检查同一 run 目录同时存在 `trace.json` 和 `report.json`，report 含每轮完整 messages。
