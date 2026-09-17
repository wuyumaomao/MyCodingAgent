# 会话记忆与上下文裁剪实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 为 Coding Agent 增加显式 session、可持久化的分层 memory、最多三条 episodic note 召回、受预算约束的上下文裁剪，以及 `readfile` 的按行重读能力。

**架构：** 新增独立的 `session.py` 管理版本化 JSON session 的加载和原子保存，新增 `memory.py` 负责确定性记忆提炼、freshness 和检索。`ConversationContext` 保留完整 session history，但每轮通过 reducer 构造受预算限制的模型 messages。`AgentLoop` 在工具执行后通知 memory，再持久化 session；工具本身不依赖 session。

**技术栈：** Python 3.11+、标准库 `dataclasses` / `hashlib` / `json` / `tempfile`、现有 OpenAI-compatible tool calling、现有 `AtomicWriter`、pytest。

**PRD：** `docs/superpowers/specs/2026-09-13-session-memory-prd.md`

## 全局约束

- 仅实现 session 级记忆，存储在目标仓库 `.coding-agent/sessions/<session_id>.json`；不实现 cross-session durable memory、checkpoint 或 `--resume latest`。
- 未传 `--session` 必须创建新 session，不能自动复用旧 session；传入 `--session` 时必须核验 canonical `repo_root`。
- 工具定义继续通过 `tools=` API 参数发送，绝不写入 session memory。
- session history 永久保存完整 OpenAI-compatible消息；只裁剪发送给模型的 transcript。
- 不允许发送没有对应 preceding assistant tool-call 的 `role: tool` 消息。
- 记忆提炼、关键词检索和文件摘要全部使用确定性本地规则，不新增 LLM、embedding 或数据库调用。
- `episodic_notes` 最多 12 条；相关召回最多 3 条；`recent_files` 最多 10 条。
- `write_file` / `patch_file` 成功后由 memory 层失效摘要，工具代码不得直接导入 session/memory。
- 维持既有工作区安全、JSON Schema、审批、trace/report 和 API retry 行为。
- 首轮 repository context 使用导航地图而非完整 manifest：只显示重要文件、少量顶层候选目录、固定忽略目录和按需工具提示。它和根目录 search 都必须隐藏 `.git`、`.coding-agent`、`.codex`、`.venv`、`venv`、`__pycache__`、`.pytest_cache`、`node_modules`、`dist`、`build`；已知范围时优先搜索更小目录。
- 不执行 Git commit，除非用户另行明确要求。

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `src/coding_agent/session.py` | `SessionState` 数据模型、加载、验证、敏感字段清理和原子持久化。 |
| `src/coding_agent/memory.py` | Working memory、file summaries、episodic notes、关键词检索、摘要失效和 prompt section 渲染。 |
| `src/coding_agent/context.py` | 将 memory 和 reduced session history 组装成每轮 provider messages。 |
| `src/coding_agent/agent.py` | 将 session/memory 生命周期接入每轮工具执行和结束持久化。 |
| `src/coding_agent/coding_agent.py` | 创建或加载 session，并将其传给 `AgentLoop`。 |
| `src/coding_agent/cli.py` | 增加 `--session`，打印 session ID，映射稳定 session 错误。 |
| `src/coding_agent/tools/readfile.py` | 支持 `start/end` 按行读取并返回范围元数据和行号内容。 |
| `tests/test_session.py` | session 创建、加载、原子保存、版本和仓库归属验证。 |
| `tests/test_memory.py` | 文件摘要、freshness、失效、notes 和确定性召回。 |
| `tests/test_context.py` | prompt 分区、预算裁剪和 tool-call/result 配对。 |
| `tests/test_readfile.py` | `start/end` Schema、边界和带行号读取。 |
| `tests/test_agent.py`、`tests/test_cli.py` | session 生命周期、观测事件和 CLI 行为。 |

---

### 任务 1：增加按行读取的 `readfile`

**文件：**
- 修改：`src/coding_agent/tools/readfile.py`
- 测试：`tests/test_readfile.py`
- 关联：`tests/test_registry.py`

**接口：**

```python
ReadFileTool.execute({
    "path": str,
    "start": int | None,
    "end": int | None,
}) -> dict[str, Any]
```

成功结果至少含 `ok`、`path`、`content`、`start`、`end`、`line_count`。`content` 每行以原始一开始的行号前缀展示。

- [ ] **步骤 1：写失败测试**

在 `tests/test_readfile.py` 加入：

```python
def test_readfile_reads_one_based_inclusive_range_with_line_numbers(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("zero\\none\\ntwo\\nthree\\n", encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo)).execute(
        {"path": "README.md", "start": 2, "end": 3}
    )

    assert result == {
        "ok": True,
        "path": "README.md",
        "content": "2: one\\n3: two",
        "start": 2,
        "end": 3,
        "line_count": 4,
    }


@pytest.mark.parametrize("arguments", [
    {"path": "README.md", "start": 0},
    {"path": "README.md", "end": 0},
    {"path": "README.md", "start": 4, "end": 3},
])
def test_readfile_rejects_invalid_line_range(sample_git_repo, arguments):
    result = ReadFileTool(Workspace(sample_git_repo)).execute(arguments)
    assert result["error"]["type"] == "invalid_arguments"
```

扩展 schema 断言，验证 `start`/`end` 都是 minimum 为 1 的 integer，且仍不允许额外字段。

- [ ] **步骤 2：确认测试失败**

运行：`uv run pytest tests/test_readfile.py -q`

预期：范围读取测试因当前 schema 拒绝 `start/end` 而失败。

- [ ] **步骤 3：实现最小按行读取**

在 decode 成功后用 `splitlines()` 获取行列表；验证 `start/end` 是非布尔整数，默认分别为 `1` 和总行数。实现范围计算：

```python
lines = content.splitlines()
line_count = len(lines)
start = arguments.get("start", 1)
end = arguments.get("end", line_count)
if not _valid_line_number(start) or not _valid_line_number(end) or start > end:
    return _error("invalid_arguments", "Invalid readfile line range")
if start > line_count:
    return _error("invalid_arguments", "Read range starts after the end of the file")
actual_end = min(end, line_count)
numbered = "\\n".join(f"{number}: {line}" for number, line in enumerate(lines[start - 1:actual_end], start))
return {"ok": True, "path": path, "content": numbered, "start": start, "end": actual_end, "line_count": line_count}
```

保持现有工作区、文件、UTF-8 和最大字节检查在范围逻辑之前；更新工具 description，说明 `start/end` 是一开始的行号、`end` 包含在结果内。

- [ ] **步骤 4：运行工具与注册表测试**

运行：`uv run pytest tests/test_readfile.py tests/test_registry.py -q`

预期：通过；完整读取也返回范围和行数元数据，现有 content 断言仍成立。

### 任务 2：实现版本化 session 存储

**文件：**
- 创建：`src/coding_agent/session.py`
- 测试：`tests/test_session.py`

**接口：**

```python
class SessionError(RuntimeError): ...

@dataclass
class SessionState:
    session_id: str
    repo_root: Path
    history: list[dict[str, Any]]
    memory: dict[str, Any]

class SessionStore:
    def __init__(self, workspace: Workspace) -> None: ...
    def create(self) -> SessionState: ...
    def load(self, session_id: str) -> SessionState: ...
    def save(self, session: SessionState) -> None: ...
    def path_for(self, session_id: str) -> Path: ...
```

`SessionStore` 的根目录是 `<workspace.root>/.coding-agent/sessions`；`save()` 使用临时文件、flush、fsync 和 replace。

- [ ] **步骤 1：写失败测试**

创建 `tests/test_session.py`：

```python
def test_session_store_creates_and_round_trips_state(sample_git_repo):
    workspace = Workspace(sample_git_repo)
    store = SessionStore(workspace)
    created = store.create()
    created.history.append({"role": "user", "content": "inspect README"})
    created.memory["working_memory"]["task_summary"] = "inspect README"
    store.save(created)

    loaded = store.load(created.session_id)

    assert loaded.session_id == created.session_id
    assert loaded.repo_root == workspace.root
    assert loaded.history == [{"role": "user", "content": "inspect README"}]
    assert loaded.memory["working_memory"]["task_summary"] == "inspect README"
    assert store.path_for(created.session_id).is_file()


def test_session_store_rejects_repository_mismatch(sample_git_repo, tmp_path):
    store = SessionStore(Workspace(sample_git_repo))
    session = store.create()
    store.save(session)
    payload = json.loads(store.path_for(session.session_id).read_text(encoding="utf-8"))
    payload["repo_root"] = str(tmp_path / "other")
    store.path_for(session.session_id).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionError, match="different repository"):
        store.load(session.session_id)
```

再覆盖 JSON 损坏、未知 `schema_version` 和 session ID 中路径分隔符时的稳定 `SessionError`。

- [ ] **步骤 2：确认测试失败**

运行：`uv run pytest tests/test_session.py -q`

预期：测试收集失败，因为模块尚不存在。

- [ ] **步骤 3：实现 SessionState 和 SessionStore**

实现固定 `SCHEMA_VERSION = 1` 和默认 memory：

```python
def _empty_memory() -> dict[str, Any]:
    return {
        "working_memory": {
            "task_summary": "",
            "constraints": [],
            "recent_files": [],
            "latest_tool_error": None,
        },
        "file_summaries": {},
        "episodic_notes": [],
    }
```

`create()` 生成时间戳加六位随机十六进制 ID。`load()` 限制 ID 为安全文件名、读取 UTF-8 JSON、校验版本和 canonical repo root；`save()` 写出 `schema_version`、时间戳、完整 history/memory，再原子 replace。持久化前复用 trace 的敏感键规则或提取共享 sanitizer，确保 api key/token/secret 字段变为 `[REDACTED]`。

- [ ] **步骤 4：运行 session 测试**

运行：`uv run pytest tests/test_session.py -q`

预期：通过；临时文件不遗留在 session 目录。

### 任务 3：实现确定性 MemoryManager

**文件：**
- 创建：`src/coding_agent/memory.py`
- 测试：`tests/test_memory.py`

**接口：**

```python
class MemoryManager:
    @property
    def data(self) -> dict[str, Any]: ...
    def begin_run(self, query: str) -> None: ...
    def observe_tool_result(self, call: ToolCall, result: dict[str, Any], *, run_id: str, round_number: int) -> None: ...
    def render_memory(self, *, max_chars: int) -> str: ...
    def retrieve_relevant(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]: ...
    def render_relevant_memory(self, query: str, *, max_chars: int) -> str: ...
```

`MemoryManager` 直接修改 `SessionState.memory`，所以 `SessionStore.save()` 可保存同一对象。

- [ ] **步骤 1：写失败测试**

创建 `tests/test_memory.py`，先覆盖完整读取、失效和召回：

```python
def make_memory() -> MemoryManager:
    return MemoryManager(SessionState(
        session_id="session-1",
        repo_root=Path("C:/repo"),
        history=[],
        memory={
            "working_memory": {
                "task_summary": "", "constraints": [], "recent_files": [],
                "latest_tool_error": None,
            },
            "file_summaries": {},
            "episodic_notes": [],
        },
    ))


def test_full_read_creates_fresh_file_summary_and_recent_file():
    memory = make_memory()
    call = ToolCall("read-1", "readfile", {"path": "httpstat.py"})
    result = {"ok": True, "path": "httpstat.py", "content": "1: def main():\\n2: pass", "start": 1, "end": 2, "line_count": 2}

    memory.observe_tool_result(call, result, run_id="run-1", round_number=1)

    summary = memory.data["file_summaries"]["httpstat.py"]
    assert summary["line_count"] == 2
    assert len(summary["freshness"]) == 64
    assert memory.data["working_memory"]["recent_files"] == ["httpstat.py"]


def test_successful_patch_invalidates_matching_file_summary_and_note():
    memory = make_memory()
    memory.data["file_summaries"]["README.md"] = {
        "summary": "summary", "line_count": 1, "freshness": "a" * 64,
        "source_range": {"start": 1, "end": 1}, "updated_at": "now",
    }
    memory.data["episodic_notes"].append({
        "id": "note-1", "content": "README was inspected", "keywords": ["readme"],
        "source": {"tool": "readfile", "run_id": "run-1", "round": 1},
        "path": "README.md", "freshness": "a" * 64, "created_at": "now",
    })

    memory.observe_tool_result(
        ToolCall("patch-1", "patch_file", {"path": "README.md"}),
        {"ok": True, "path": "README.md", "operation": "patch"},
        run_id="run-1", round_number=2,
    )

    assert "README.md" not in memory.data["file_summaries"]
    assert memory.data["episodic_notes"] == []


def test_retrieve_relevant_returns_at_most_three_keyword_matches_newest_first_on_tie():
    memory = make_memory()
    for number, content in enumerate(("pytest tests passed", "pytest test failed", "pytest output inspected", "unrelated shell output"), 1):
        memory.data["episodic_notes"].append({
            "id": f"note-{number}", "content": content, "keywords": content.split(),
            "source": {"tool": "shell", "run_id": "run-1", "round": number},
            "path": None, "freshness": None, "created_at": f"2026-01-01T00:00:0{number}Z",
        })

    notes = memory.retrieve_relevant("run pytest test", limit=3)

    assert [note["content"] for note in notes] == ["pytest output inspected", "pytest test failed", "pytest tests passed"]
```

另加 partial read 不生成完整文件摘要、零匹配 note 不召回、笔记数量超过 12 时丢弃最旧项、`begin_run()` 提取 “do not”/“不要”类限制的测试。

- [ ] **步骤 2：确认测试失败**

运行：`uv run pytest tests/test_memory.py -q`

预期：测试收集失败，因为模块尚不存在。

- [ ] **步骤 3：实现摘要、freshness、notes 和检索**

实现纯本地辅助函数：

```python
def _keywords(text: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z0-9_]+", text) if len(word) >= 2}

def _freshness(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
```

完整 read 的判定是 `result["start"] == 1 and result["end"] == result["line_count"]`。摘要从带行号文本提取最多 6 个非空行、每行限制 120 字符、总长度限制 600 字符；不要调用模型。将最新路径移到 `recent_files` 顶部并截断为 10 条。

对 notes 的关键词交集做降序排序，随后按保存顺序倒序。为支持测试和 context 层，`data` 属性返回 session 中的可变 `memory` 字典；它不是第二份副本。`render_memory()` 和 `render_relevant_memory()` 生成明确的 `[Memory]` / `[Relevant Memory]` 标签，并严格裁剪各自到传入的最大字符数。

- [ ] **步骤 4：运行 memory 测试**

运行：`uv run pytest tests/test_memory.py -q`

预期：通过；所有行为不依赖网络或真实 LLM。

### 任务 4：实现配对安全的 transcript reducer 并接入 ConversationContext

**文件：**
- 修改：`src/coding_agent/context.py`
- 测试：`tests/test_context.py`

**接口：**

```python
ConversationContext(
    workspace: Workspace,
    memory: MemoryManager,
    repository_context_builder: Callable[[Workspace], str] | None = None,
    transcript_budget_chars: int = 12_000,
)

ConversationContext.messages(query: str) -> list[dict[str, Any]]
ConversationContext.prompt_metrics(query: str) -> dict[str, int]
```

`history` 引用 session 的完整 history 列表；`messages()` 不再简单复制 `system_messages + history`。

- [ ] **步骤 1：写失败测试**

在 `tests/test_context.py` 增加：

```python
def test_context_renders_memory_relevant_memory_and_current_request_last(sample_git_repo):
    memory = MemoryManager(SessionStore(Workspace(sample_git_repo)).create())
    memory.begin_run("inspect pytest tests")
    memory.data["episodic_notes"].append({
        "id": "note-1", "content": "pytest passed", "keywords": ["pytest", "tests"],
        "source": {"tool": "shell", "run_id": "run-1", "round": 1},
        "path": None, "freshness": None, "created_at": "2026-01-01T00:00:00Z",
    })
    context = ConversationContext(Workspace(sample_git_repo), memory, transcript_budget_chars=1_000)

    messages = context.messages("inspect pytest tests")

    assert "[Memory]" in messages[2]["content"]
    assert "[Relevant Memory]" in messages[3]["content"]
    assert messages[-1] == {"role": "user", "content": "inspect pytest tests"}


def test_reducer_never_leaves_tool_message_without_its_assistant_call(sample_git_repo):
    memory = MemoryManager(SessionStore(Workspace(sample_git_repo)).create())
    context = ConversationContext(Workspace(sample_git_repo), memory, transcript_budget_chars=500)
    turn = AssistantTurn(None, [ToolCall("call-1", "readfile", {"path": "README.md"})])
    context.add_assistant_turn(turn)
    context.add_tool_result(turn.tool_calls[0], {"ok": True, "path": "README.md", "content": "x" * 10_000})

    messages = context.messages("continue")

    tool_ids = {message["tool_call_id"] for message in messages if message["role"] == "tool"}
    assistant_ids = {
        call["id"]
        for message in messages if message["role"] == "assistant"
        for call in message.get("tool_calls", [])
    }
    assert tool_ids <= assistant_ids
```

再加测试验证旧 readfile 结果缩成带 path/range 的 marker、完整 history 未改变、最近组保留在 transcript 内以及 budget 计数上限。

- [ ] **步骤 2：确认测试失败**

运行：`uv run pytest tests/test_context.py -q`

预期：失败，因为 `ConversationContext` 尚不接收 memory，也没有 reducer。

- [ ] **步骤 3：实现 group reducer 和 message 组装**

提取工具组时使用 assistant 消息的 `tool_calls[].id` 收集其后连续的匹配 `tool_call_id` tool messages。将 user/final-assistant 消息作为独立组。以 JSON 序列化字符数估算组大小；从 newest 到 oldest 放入预算，超出预算时先调用 `_compact_group()`，再整个跳过。

`_compact_group()` 仅替换成功旧工具结果的 `content`：

```python
{
    "ok": True,
    "path": "httpstat.py",
    "context_status": "compressed; call readfile with start/end for details",
}
```

保留原 assistant message、tool call ID、工具名和 arguments。每轮返回：两个 static system messages、memory system message、relevant-memory system message、reduced groups、最终 current user message。`add_user_request()` 改为只写 session history，不把当前 query重复放进 reduced transcript。

- [ ] **步骤 4：运行上下文测试**

运行：`uv run pytest tests/test_context.py -q`

预期：通过；旧 history 未被改写，所有发送的 tool message 都有对应 assistant tool call。

### 任务 5：把 session 和 memory 接入 AgentLoop、Facade 与 CLI

**文件：**
- 修改：`src/coding_agent/agent.py`
- 修改：`src/coding_agent/coding_agent.py`
- 修改：`src/coding_agent/cli.py`
- 测试：`tests/test_agent.py`
- 测试：`tests/test_cli.py`

**接口：**

```python
CodingAgent.from_settings(..., session: SessionState | None = None) -> CodingAgent
CodingAgent.ask(query: str, *, recorder: RunRecorder | None = None, session: SessionState | None = None) -> str

AgentLoop.run(query: str, workspace: Workspace, *, session: SessionState, event_sink: Any | None = None) -> str
```

CLI 新增：`--session SESSION_ID`。每次 run 都保存 session，无论最终回答、round limit 或 `AgentError`；未成功启动前的 CLI/config/repository 错误不创建 session。

- [ ] **步骤 1：写失败测试**

在 `tests/test_cli.py` 加入一组 fake `CodingAgent` 或 fake LLM 测试：

```python
def test_cli_creates_session_then_reuses_it_for_second_invocation(monkeypatch, sample_git_repo, capsys):
    first_code = main(["inspect README", "--repo", str(sample_git_repo)])
    first_output = capsys.readouterr().err
    session_id = re.search(r"Session: ([^\\s]+)", first_output).group(1)

    second_code = main(["continue", "--repo", str(sample_git_repo), "--session", session_id])

    assert first_code == 0
    assert second_code == 0
    session = SessionStore(Workspace(sample_git_repo)).load(session_id)
    assert [item["content"] for item in session.history if item["role"] == "user"] == ["inspect README", "continue"]
```

在 `tests/test_agent.py` 断言 successful read 之后 session summary 出现、successful patch 后其 summary 被移除，且 `llm_request` 事件含 `session_id`、`memory_chars`、`relevant_memory_chars` 和 `transcript_chars`。

- [ ] **步骤 2：确认测试失败**

运行：`uv run pytest tests/test_agent.py tests/test_cli.py -q`

预期：失败，因为 CLI 没有 `--session`，AgentLoop 不接收 session。

- [ ] **步骤 3：接入生命周期和事件**

`CodingAgent.from_settings()` 接受或创建 session 后构造 `MemoryManager(session)`。`AgentLoop.run()` 在开始时调用：

```python
memory.begin_run(query)
context.add_user_request(query)
```

每次 `ToolExecutor.execute(call)` 后按顺序调用 `memory.observe_tool_result(...)`、`context.add_tool_result(...)`、`store.save(session)`。最终答案和 round limit 也在返回前保存；`AgentError` 在 CLI 的 `finally` 中保存已存在 session。将 `context.prompt_metrics(query)` 添加到 `llm_request` 的 trace payload，且把完整 `messages` 放入 report payload。

CLI 先解析 repo 和 settings，再基于 `args.session` 执行 `SessionStore.load()` 或 `create()`；在标准错误输出：

```text
Session: <session_id>
```

增加 `SessionError` 到 `_error_type()`，稳定值为 `session_error`；打印错误时不泄露 session JSON 内容。

- [ ] **步骤 4：运行 Agent 和 CLI 测试**

运行：`uv run pytest tests/test_agent.py tests/test_cli.py -q`

预期：通过；两次 invocation 共享同一 session file，但仍创建独立 run trace/report。

### 任务 6：补齐集成回归、文档和检查

**文件：**
- 修改：`tests/test_trace_integration.py`
- 修改：`README.md`
- 修改：`学习文档.md`
- 修改：`docs/agent-capability-gap-and-roadmap.md`

- [ ] **步骤 1：写集成失败测试**

在 `tests/test_trace_integration.py` 创建一个两轮 Fake LLM：第一轮读取足够大的文件，第二轮再次决策。断言：

```python
assert report_request["messages"][-1] == {"role": "user", "content": "continue"}
assert any(event["type"] == "llm_request" and event["session_id"] == session.session_id for event in trace["events"])
assert "content" in original_report_tool_result
assert trace_request["prompt_chars"] >= trace_request["transcript_chars"]
```

再增加 end-to-end `readfile -> patch_file -> readfile` 测试，确认 patch 后旧摘要不被 prompt 使用，第二次成功 full read 生成新的 freshness。

- [ ] **步骤 2：确认集成测试失败**

运行：`uv run pytest tests/test_trace_integration.py -q`

预期：初始失败，因为当前 trace 没有 session/memory metrics。

- [ ] **步骤 3：更新用户文档**

在 README 增加两次调用示例：

```powershell
uv run coding-agent "先检查 README" --repo F:\AgentLabs\httpstat
# 输出 Session: 20260913T120000Z-a1b2c3
uv run coding-agent "继续检查测试" --repo F:\AgentLabs\httpstat --session 20260913T120000Z-a1b2c3
```

说明 session 文件位置、自动新 session、`--session` 的显式继续、memory/relevant memory 的区别、摘要过期后通过 `readfile(start, end)` 重新读取。学习文档说明“完整 session history 与缩减 prompt transcript 的区别”“文件 freshness”“确定性关键词召回”和“工具调用配对约束”。路线图中将上下文管理和 session memory 的当前状态改为已实现，checkpoint/resume 保持未实现。

- [ ] **步骤 4：运行专项和完整验证**

运行：

```powershell
uv run pytest tests/test_readfile.py tests/test_session.py tests/test_memory.py tests/test_context.py tests/test_agent.py tests/test_cli.py tests/test_trace_integration.py -q
uv run pytest -q
uv run python -m compileall -q src
git diff --check
```

预期：所有测试通过，编译成功，差异检查无输出。确认没有新批量读取/补丁工具、没有向 provider prompt 写入工具 schema、没有为 memory 额外发起模型请求、没有 session 外自动召回。
