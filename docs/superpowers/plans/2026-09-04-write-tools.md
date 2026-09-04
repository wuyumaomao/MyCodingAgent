# 写工具实现计划

> **给 Agent 开发者：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 作为执行子技能，按任务逐项实现。步骤使用复选框（`- [ ]`）跟踪。

**目标：** 增加需要用户批准的 `write_file` 和 `patch_file` 工具，使 Agent 能安全修改仓库工作区内的文件。

**架构：** 工具继续通过 `ToolRegistry` 执行。新增 `WriteApprovalGate` 负责逐调用审批，新增 `AtomicWriter` 负责同目录临时文件和原子替换。CLI 注入交互式审批回调，测试注入确定性回调。`AgentLoop` 继续控制工具调用顺序，并把结构化结果回传模型。

**技术栈：** Python 3.11+、`pathlib`、`tempfile`、`os.replace`、现有 `ToolRegistry`、`Workspace`、`RunRecorder` 和 pytest。

**规格文档：** `docs/superpowers/specs/2026-09-04-write-tools-prd.md`

## 全局约束

- 两个工具都是高风险工具，每个工具调用都必须单独审批。
- `write_file` 可以创建或覆盖仓库文件，但审批前绝不写入。
- `patch_file` 只在旧文本恰好出现一次时进行精确替换。
- 所有路径必须解析到 `Workspace.root` 内；解析后仍在仓库内的绝对路径和 `..` 路径允许使用。
- 拒绝符号链接目标，写入时不能跟随符号链接。
- 写入内容和 patch 后文件限制为 UTF-8 编码后的 64 KiB。
- 父目录不存在时返回错误；工具不自动创建目录。
- 使用同目录临时文件和原子替换；写入失败时保留原文件。
- 审批、执行和工具结果写入 `trace.json` 与 `report.json`，不得记录 API Key。
- 保持现有只读工具和 10 轮 AgentLoop 行为兼容。

---

### 任务 1：审批和原子写入基础设施

**文件：**
- 新建：`src/coding_agent/tools/approval.py`
- 新建：`src/coding_agent/tools/atomic_writer.py`
- 测试：`tests/test_write_primitives.py`

**接口：**
- `WritePreview(operation: str, path: str, content: str | None = None, old_text: str | None = None, new_text: str | None = None, existed: bool = False)`：不可变预览对象。
- `ApprovalDecision`：`"approved" | "denied" | "required"`。
- `WriteApprovalGate(ask: Callable[[WritePreview], bool] | None = None, record: Callable[..., None] | None = None)`：`approve(preview) -> ApprovalDecision`；无回调返回 `"required"`，回调返回 `False` 返回 `"denied"`。
- `AtomicWriter.write(target: Path, content: str) -> int`：UTF-8 写入同目录临时文件，flush/fsync 后用 `os.replace` 替换目标。
- `WriteError`：原子写入失败异常。

- [x] **步骤 1：先写失败测试**

```python
def test_approval_gate_calls_callback_once():
    previews = []
    gate = WriteApprovalGate(ask=lambda preview: previews.append(preview) or True)
    assert gate.approve(WritePreview("create", "new.py", content="x")) == "approved"
    assert len(previews) == 1


def test_approval_gate_without_callback_requires_approval():
    assert WriteApprovalGate().approve(WritePreview("overwrite", "a.py")) == "required"


def test_atomic_writer_preserves_original_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr("coding_agent.tools.atomic_writer.os.replace", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(WriteError):
        AtomicWriter().write(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
```

- [x] **步骤 2：运行失败测试**

运行：`uv run pytest tests/test_write_primitives.py -q`

预期：因基础设施尚不存在而失败。

- [x] **步骤 3：实现基础设施**

审批门只调用一次回调，并在询问前后记录 `approval_request`/`approval_result`。缺少回调或回调异常返回 `"required"`。原子写入器只清理自己创建的临时文件。

- [x] **步骤 4：运行专项测试**

运行：`uv run pytest tests/test_write_primitives.py -q`，预期通过。

- [x] **步骤 5：提交**

```bash
git add src/coding_agent/tools/approval.py src/coding_agent/tools/atomic_writer.py tests/test_write_primitives.py
git commit -m "feat: add write approval and atomic writer primitives"
```

### 任务 2：实现 `write_file`

**文件：**
- 新建：`src/coding_agent/tools/writefile.py`
- 测试：`tests/test_writefile.py`

**接口：** `WriteFileTool(workspace: Workspace, approval_gate: WriteApprovalGate | None = None, max_bytes: int = 64 * 1024)`，名称为 `write_file`，`execute(arguments: dict[str, Any]) -> dict[str, Any]`。

- [x] **步骤 1：先写失败测试**

覆盖新建、覆盖、审批拒绝、参数错误、父目录不存在、越界路径、符号链接、目录目标和超 64 KiB；每个拒绝场景断言目标未改变。

```python
def test_write_file_creates_after_approval(sample_git_repo):
    tool = WriteFileTool(Workspace(sample_git_repo), WriteApprovalGate(ask=lambda _: True))
    result = tool.execute({"path": "new.py", "content": "print('ok')\n"})
    assert result["ok"] is True
    assert (sample_git_repo / "new.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_write_file_overwrites_after_approval(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("old", encoding="utf-8")
    result = WriteFileTool(Workspace(sample_git_repo), WriteApprovalGate(ask=lambda _: True)).execute({"path": "README.md", "content": "new"})
    assert result["operation"] == "overwrite"
    assert target.read_text(encoding="utf-8") == "new"


def test_write_file_does_not_modify_when_denied(sample_git_repo):
    target = sample_git_repo / "README.md"
    before = target.read_bytes()
    result = WriteFileTool(Workspace(sample_git_repo), WriteApprovalGate(ask=lambda _: False)).execute({"path": "README.md", "content": "new"})
    assert result["error"]["type"] == "approval_denied"
    assert target.read_bytes() == before
```

- [x] **步骤 2：运行测试确认失败**：`uv run pytest tests/test_writefile.py -q`。
- [x] **步骤 3：实现工具**：先校验参数、UTF-8 字节数、workspace 边界和符号链接；判断 `create`/`overwrite`，要求父目录存在；审批通过后才调用 `AtomicWriter`；映射 `invalid_arguments`、`workspace_violation`、`parent_not_found`、`permission_denied`、`write_error`、`file_too_large`、`approval_denied`、`approval_required`。
- [x] **步骤 4：运行测试确认通过**：`uv run pytest tests/test_writefile.py -q`。
- [x] **步骤 5：提交**：`git add src/coding_agent/tools/writefile.py tests/test_writefile.py; git commit -m "feat: add approved write_file tool"`。

### 任务 3：实现 `patch_file`

**文件：**
- 新建：`src/coding_agent/tools/patchfile.py`
- 测试：`tests/test_patchfile.py`

**接口：** `PatchFileTool(workspace: Workspace, approval_gate: WriteApprovalGate | None = None, max_bytes: int = 64 * 1024)`，名称为 `patch_file`，`execute(arguments: dict[str, Any]) -> dict[str, Any]`。

- [x] **步骤 1：先写失败测试**

```python
def test_patch_file_replaces_one_match_after_approval(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("before\nvalue\nafter\n", encoding="utf-8")
    result = PatchFileTool(Workspace(sample_git_repo), WriteApprovalGate(ask=lambda _: True)).execute({"path": "README.md", "old_text": "value", "new_text": "updated"})
    assert result["ok"] is True
    assert result["replacements"] == 1
    assert "updated" in target.read_text(encoding="utf-8")


def test_patch_file_rejects_missing_and_duplicate_text(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("same\nsame\n", encoding="utf-8")
    tool = PatchFileTool(Workspace(sample_git_repo), WriteApprovalGate(ask=lambda _: True))
    assert tool.execute({"path": "README.md", "old_text": "absent", "new_text": "x"})["error"]["type"] == "text_not_found"
    assert tool.execute({"path": "README.md", "old_text": "same", "new_text": "x"})["error"]["type"] == "text_not_unique"
```

还要覆盖审批拒绝、目标不存在/不是文件、非 UTF-8、符号链接、越界路径和超大 patch；校验失败时不得请求审批。

- [x] **步骤 2：运行测试确认失败**：`uv run pytest tests/test_patchfile.py -q`。
- [x] **步骤 3：实现工具**：路径检查后按 UTF-8 读取，使用 `str.count` 严格计数；0 次或多次匹配在审批前拒绝；构造并限制新内容，审批通过后使用 `AtomicWriter` 写入；任何失败不修改文件。
- [x] **步骤 4：运行测试确认通过**：`uv run pytest tests/test_patchfile.py -q`。
- [x] **步骤 5：提交**：`git add src/coding_agent/tools/patchfile.py tests/test_patchfile.py; git commit -m "feat: add approved patch_file tool"`。

### 任务 4：CLI 注册和交互审批

**文件：** 修改 `src/coding_agent/cli.py`，测试 `tests/test_cli.py`。

**接口：** `_ask_write_approval(preview: WritePreview) -> bool`。输出操作类型、仓库相对路径和受限预览；覆盖明确警告；输入 `y/yes` 才批准；非交互 stdin 返回 `approval_required`。

- [x] **步骤 1：写失败测试**：用 monkeypatch `input` 测试 yes/no，并用 FakeLLM 验证批准时写入、拒绝时不写入。
- [x] **步骤 2：运行 `uv run pytest tests/test_cli.py -q`，确认因审批函数和注册不存在而失败。**
- [x] **步骤 3：实现**：在 `RunRecorder.create()` 后创建共享审批门，注册两个工具，保留读工具；不得显示绝对路径或真实 `.env` 内容。
- [x] **步骤 4：运行 `uv run pytest tests/test_cli.py -q`，确认通过。**
- [x] **步骤 5：提交**：`git add src/coding_agent/cli.py tests/test_cli.py; git commit -m "feat: register approved write tools in cli"`。

### 任务 5：AgentLoop、Trace/Report 和文档集成

**文件：** 按需修改 `src/coding_agent/agent.py`、`src/coding_agent/trace.py`、`tests/test_agent.py`、`tests/test_trace_integration.py`、`README.md` 和 PRD 状态。

- [x] **步骤 1：写失败集成测试**：验证审批拒绝后工具结果作为 `role: tool` 回传，模型可以继续；成功写入事件顺序为 `approval_request`、`approval_result`、`tool_result`；同一响应中两个写调用触发两次审批；report 有预览，trace 只有摘要。
- [x] **步骤 2：运行 `uv run pytest tests/test_agent.py tests/test_trace_integration.py -q`，确认失败。**
- [x] **步骤 3：实现最小集成**：注册写工具并将审批事件连接到 `RunRecorder`，不增加自动重试、多文件事务，不改变读工具语义。
- [x] **步骤 4：运行完整测试 `uv run pytest -q`，预期所有测试通过，仅保留已有平台相关 skip。**
- [x] **步骤 5：更新 `README.md` 和扩展 PRD，说明两个工具、逐调用审批、覆盖警告、精确 patch 和非交互行为。**
- [x] **步骤 6：提交**：`git add src/coding_agent/agent.py src/coding_agent/trace.py tests/test_agent.py tests/test_trace_integration.py README.md docs/superpowers/specs/2026-09-04-write-tools-prd.md; git commit -m "feat: integrate approved file write tools"`。
