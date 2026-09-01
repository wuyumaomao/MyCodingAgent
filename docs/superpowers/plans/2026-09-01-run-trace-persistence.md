# Run Trace 持久化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 为每次 CLI 提问创建独立 run，并把 Agent 的关键执行步骤实时保存到 `.coding-agent/runs/<run-id>/trace.json`，支持成功和失败任务复盘。

**架构：** 新增 `RunRecorder` 作为独立的持久化组件，由 CLI 创建并注入 `AgentLoop`。AgentLoop 在请求模型、处理响应、调用工具和任务结束等边界记录事件；消息历史仍只负责驱动下一轮模型请求，trace 只负责审计和复盘。

**技术栈：** Python 3.11+、标准库 `dataclasses`、`datetime`、`json`、`pathlib`、`tempfile`、`secrets`，以及 `pytest`。不增加数据库或外部日志服务依赖。

**规格文档：** `docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

## 全局约束

- 每次 CLI 提问必须创建唯一 run ID 和独立 run 目录。
- 默认目录为当前 Coding Agent 项目目录下的 `.coding-agent/runs/<run-id>/trace.json`，不写入目标仓库。
- `trace.json` 顶层包含 `run_id`、`query`、`repo_root`、`status`、时间戳和有序 `events`。
- 关键事件写入后立即持久化；进程中断不能破坏上一次完整 JSON。
- 任务成功写入 `final_answer`，失败写入 `run_failed`，并更新最终 `status`。
- trace 不得包含 API Key；错误信息不得泄露密钥或不必要的敏感绝对路径。
- 记录工具参数和结果时保持 JSON 可序列化；不记录完整 OpenAI SDK 对象。
- 不引入数据库、异步后台写入或跨 run 共享状态。

---

### 任务 1：定义 trace 数据模型和原子写入记录器

**文件：**
- 新建：`src/coding_agent/trace.py`
- 新建：`tests/test_trace.py`

**接口：**
- `RunStatus = Literal["running", "completed", "failed"]`
- `RunRecorder.create(query: str, repo_root: Path, runs_root: Path | None = None) -> RunRecorder`
- `RunRecorder.record(event_type: str, **payload: object) -> None`
- `RunRecorder.complete(answer: str) -> None`
- `RunRecorder.fail(error_type: str, message: str) -> None`
- `RunRecorder.run_id: str`
- `RunRecorder.trace_path: Path`

- [ ] **步骤 1：编写失败的记录器测试**

```python
def test_recorder_creates_run_and_persists_started_event(tmp_path):
    recorder = RunRecorder.create("Explain repo", tmp_path / "repo", tmp_path / "runs")
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert recorder.trace_path.parent.parent == tmp_path / "runs"
    assert document["status"] == "running"
    assert document["query"] == "Explain repo"
    assert document["events"][0]["type"] == "run_started"


def test_recorder_appends_events_and_completes(tmp_path):
    recorder = RunRecorder.create("Explain repo", tmp_path / "repo", tmp_path / "runs")
    recorder.record("tool_call", name="readfile", arguments={"path": "README.md"})
    recorder.complete("Done")
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert document["status"] == "completed"
    assert document["events"][-1] == {"seq": 3, "type": "final_answer", "content": "Done"}


def test_recorder_failure_is_persisted_without_secret(tmp_path):
    recorder = RunRecorder.create("Explain repo", tmp_path / "repo", tmp_path / "runs")
    recorder.fail("provider_error", "Request failed")
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["events"][-1]["type"] == "run_failed"
    assert "api-key" not in json.dumps(document).lower()
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_trace.py -q`
预期：失败，因为 `RunRecorder` 尚不存在。

- [ ] **步骤 3：实现 run ID、目录和基础文档**

使用 UTC 时间戳加 6 位 `secrets.token_hex(3)` 生成 run ID，例如 `20260901T153000Z-a1b2c3`。`create()` 创建 `runs_root / run_id`，初始化文档：`status="running"`、`started_at`、`ended_at=null`、传入 query、`repo_root` 使用字符串保存，以及空 `events`；随后调用 `record("run_started")`。

- [ ] **步骤 4：实现事件追加和状态更新**

`record()` 为事件分配从 1 开始递增的 `seq`，写入 `type` 和 payload，并立即调用 `_persist()`。`complete()` 追加 `final_answer` 后设置 `status="completed"` 和 `ended_at`；`fail()` 追加 `run_failed`（只保存稳定错误类型和用户安全消息）后设置 `status="failed"` 和 `ended_at`。

- [ ] **步骤 5：实现原子 JSON 写入**

在同一 run 目录创建临时文件，使用 `json.dump(..., ensure_ascii=False, indent=2)` 写入并 flush，再用 `Path.replace()` 替换 `trace.json`。任何写入异常都抛出 `TraceWriteError`，不得覆盖已有完整 trace。

- [ ] **步骤 6：运行测试确认通过**

运行：`uv run pytest tests/test_trace.py -q`
预期：PASS，覆盖创建、事件顺序、完成、失败和 JSON 持久化。

- [ ] **步骤 7：提交**

```bash
git add src/coding_agent/trace.py tests/test_trace.py
git commit -m "feat: add atomic run trace recorder"
```

### 任务 2：为 AgentLoop 增加事件记录边界

**文件：**
- 修改：`src/coding_agent/agent.py`
- 修改：`tests/test_agent.py`

**接口：**
- `AgentLoop(..., recorder: RunRecorder | None = None, max_rounds: int = 4)`
- 当 `recorder` 为 `None` 时保持现有行为，便于现有单元测试复用。

- [ ] **步骤 1：编写失败的 Agent trace 测试**

```python
def test_agent_loop_records_model_and_tool_events(sample_git_repo, tmp_path):
    recorder = RunRecorder.create("Explain scripts", sample_git_repo, tmp_path / "runs")
    llm = FakeLLM([
        AssistantTurn(None, [ToolCall("call-1", "readfile", {"path": "package.json"})]),
        AssistantTurn("The repository uses npm scripts.", []),
    ])
    answer = AgentLoop(llm, make_registry(sample_git_repo), build_repository_context, recorder=recorder).run(
        "Explain scripts", Workspace(sample_git_repo)
    )
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    event_types = [event["type"] for event in document["events"]]
    assert answer == "The repository uses npm scripts."
    assert "llm_request" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert event_types[-1] == "final_answer"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_agent.py::test_agent_loop_records_model_and_tool_events -q`
预期：失败，因为 AgentLoop 尚未接收或使用 `RunRecorder`。

- [ ] **步骤 3：注入可选 recorder 并记录请求/响应**

在每轮调用 LLM 前记录 `llm_request`（round 和 message 数量，不记录 API Key）；收到 `AssistantTurn` 后记录 `llm_response`（round、是否有 tool calls、工具调用数量）。记录内容必须来自内部 dataclass，不保存 SDK 原始对象。

- [ ] **步骤 4：记录工具调用和工具结果**

执行每个 `ToolCall` 前记录 `tool_call`（id、name、arguments）；执行后记录 `tool_result`（id、name、result）。工具结果使用与消息相同的结构化 dict，并通过 recorder 的 JSON 序列化路径写入。

- [ ] **步骤 5：记录完成、上限和异常**

无 tool call 的最终文本调用 `recorder.complete(content)`；达到轮次上限调用 `recorder.fail("round_limit", "Reached the tool-call limit")` 并保留 Agent 当前返回语义；模型或其他 AgentError 调用 `recorder.fail(...)` 后继续向上抛出。

- [ ] **步骤 6：运行 Agent 测试确认通过**

运行：`uv run pytest tests/test_agent.py -q`
预期：PASS，且原有不传 recorder 的测试仍然通过。

- [ ] **步骤 7：提交**

```bash
git add src/coding_agent/agent.py tests/test_agent.py
git commit -m "feat: record agent loop trace events"
```

### 任务 3：将 RunRecorder 接入 CLI 并输出 run 信息

**文件：**
- 修改：`src/coding_agent/cli.py`
- 修改：`tests/test_cli.py`
- 修改：`README.md`
- 修改：`.gitignore`

**接口：**
- CLI 为每次调用创建 `RunRecorder`，并将 recorder 传给 `AgentLoop`。
- 成功和失败路径均输出 run ID 与 trace 路径；最终回答仍输出到 stdout。

- [ ] **步骤 1：编写失败的 CLI 持久化测试**

```python
def test_cli_creates_trace_for_success(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("coding_agent.cli.LLMClient", lambda **kwargs: FakeClient([
        AssistantTurn("ok", [])
    ]))
    assert main(["Explain", "--repo", str(sample_git_repo)]) == 0
    output = capsys.readouterr().out
    assert "ok" in output
    trace_files = list((tmp_path / "runs").glob("*/trace.json"))
    assert len(trace_files) == 1


def test_cli_creates_failed_trace(monkeypatch, sample_git_repo, tmp_path, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr("coding_agent.cli.LLMClient", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")))
    assert main(["Explain", "--repo", str(sample_git_repo)]) != 0
    trace_files = list((tmp_path / "runs").glob("*/trace.json"))
    assert json.loads(trace_files[0].read_text(encoding="utf-8"))["status"] == "failed"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/test_cli.py::test_cli_creates_trace_for_success tests/test_cli.py::test_cli_creates_failed_trace -q`
预期：失败，因为 CLI 尚未创建 recorder 或输出 run 信息。

- [ ] **步骤 3：实现 CLI run 生命周期**

在 `main()` 解析仓库并创建 `Workspace` 后立即调用 `RunRecorder.create(" ".join(args.query), repository, RUNS_ROOT)`。将 recorder 注入 `AgentLoop`。成功时完成 recorder；捕获配置、仓库、模型和 Agent 错误时调用 `recorder.fail(...)`，再输出错误并返回 1。创建 recorder 失败时输出错误且不伪造 trace 路径。

- [ ] **步骤 4：输出复盘位置并保护目标仓库**

成功和失败路径都向 stderr 输出：`Run: <run_id>` 和 `Trace: <absolute-trace-path>`；stdout 仅保留最终回答。确保 `RUNS_ROOT` 默认基于 Coding Agent 项目根目录，而不是 `--repo` 目标仓库；将 `.coding-agent/` 加入根项目 `.gitignore`。

- [ ] **步骤 5：更新 README**

说明每次命令都会创建 run，给出 trace 路径示例，并说明 trace 不会写入目标仓库且不包含 API Key。

- [ ] **步骤 6：运行 CLI 测试确认通过**

运行：`uv run pytest tests/test_cli.py -q`
预期：PASS。

- [ ] **步骤 7：提交**

```bash
git add src/coding_agent/cli.py tests/test_cli.py README.md .gitignore
git commit -m "feat: persist CLI runs and trace paths"
```

### 任务 4：补充 trace 集成验证和最终检查

**文件：**
- 新建：`tests/test_trace_integration.py`

- [ ] **步骤 1：添加完整成功链路测试**

使用 Fake LLM 对 `demo-repo` 或 `sample-repo` 执行一次读取任务，断言 trace 事件顺序至少为：`run_started → llm_request → llm_response → tool_call → tool_result → llm_request → llm_response → final_answer`，每个事件 `seq` 严格递增。

- [ ] **步骤 2：添加失败和轮次上限测试**

验证 provider 异常、工具异常和 4 轮上限都会生成 `status="failed"` 的 trace，且已有事件在失败后仍然存在。

- [ ] **步骤 3：验证原子写入和敏感信息保护**

模拟 `_persist()` 替换失败，确认旧 `trace.json` 仍可解析；将 API Key 放入 client 配置和异常文本，确认 trace 文件中不存在该值。

- [ ] **步骤 4：运行完整测试和编译检查**

运行：`uv run pytest -q` 和 `uv run python -m compileall -q src`。
预期：所有测试通过，编译退出码为 0，测试不发起真实网络请求。

- [ ] **步骤 5：手动验证真实 CLI 的 trace 输出**

在配置有效 API 的前提下运行：

```powershell
uv run coding-agent "读取 README.md 并解释如何运行" --repo .\demo-repo
```

确认 stdout 为最终回答，stderr 显示 run ID 和 trace 路径，并检查对应 `trace.json` 可以被 JSON 解析。

- [ ] **步骤 6：提交最终验证**

```bash
git add tests/test_trace_integration.py tests/test_agent.py
git commit -m "test: verify persisted run traces"
```
