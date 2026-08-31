# Coding Agent 只读 MVP 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 构建一个 Python CLI，通过 `listfiles` 和 `readfile` 读取本地 Git 仓库，驱动最多 4 轮的原生 tool calling，并返回基于实际文件内容的解释。

**架构：** 单进程 CLI 之上提供 `AgentService`。`AgentLoop` 管理消息历史和终止条件，`LLMClient` 封装 OpenAI-compatible SDK，`ToolRegistry` 执行受工作空间限制的工具，`ContextBuilder` 生成初始仓库清单。

**技术栈：** Python 3.11+、`argparse`、`pathlib`、`subprocess`、`openai` Python SDK、`pathspec`、`pytest` 和 `pyproject.toml`。安装可使用 `uv` 或 `pip`；下方命令统一使用 `python -m pip`，不依赖额外包管理器。

**规格文档：** `docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`

## 全局约束

- CLI 是唯一运行入口，不添加 HTTP 服务。
- 首个 provider 使用支持原生 tool calling 的 OpenAI-compatible endpoint。
- 工具只能访问解析出的 Git 仓库根目录及其子目录。
- 允许仓库内绝对路径和解析后仍位于仓库内的 `..` 路径；拒绝解析后逃逸出仓库的路径和符号链接。
- `listfiles` 默认最大深度为 4、最多返回 200 项；`readfile` 默认最多读取 64 KiB。
- 单个任务最多执行 4 轮工具调用；模型返回无 tool call 时结束。
- MVP 只提供 `listfiles` 和 `readfile`，不提供写文件或 Shell 工具。
- 配置优先级为 CLI 参数 > 环境变量 > 默认值；API Key 没有硬编码默认值。
- 不得打印 API Key，也不得将其放入仓库上下文、错误或日志。

---

### 任务 1：创建 Python 包和测试基线

**文件：**
- 新建：`pyproject.toml`
- 新建：`.gitignore`
- 新建：`src/coding_agent/__init__.py`
- 新建：`src/coding_agent/__main__.py`
- 新建：`tests/test_smoke.py`

**接口：**
- 产出名为 `coding-agent` 的可安装包，并提供 `coding-agent` console script。

- [ ] **步骤 1：编写失败的 smoke test**

```python
from coding_agent import __version__


def test_package_is_importable():
    assert __version__ == "0.1.0"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python -m pytest tests/test_smoke.py -q`
预期：失败，因为包和版本号尚不存在。

- [ ] **步骤 3：添加包元数据和导入入口**

设置 `project.scripts.coding-agent = "coding_agent.cli:main"`，设置 `requires-python = ">=3.11"`，运行依赖包含 `openai` 和 `pathspec`，`dev` extra 包含 `pytest`。添加 `.gitignore` 忽略 Python 缓存、构建产物、egg-info 和虚拟环境。在 `src/coding_agent/__init__.py` 定义 `__version__ = "0.1.0"`，在 `__main__.py` 中委托给 `coding_agent.cli.main`。

- [ ] **步骤 4：运行 smoke test 确认通过**

运行：`python -m pytest tests/test_smoke.py -q`
预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add pyproject.toml src/coding_agent tests/test_smoke.py
git commit -m "chore: scaffold coding agent package"
```

### 任务 2：解析仓库根目录并强制工作空间路径边界

**文件：**
- 新建：`src/coding_agent/repository.py`
- 新建：`tests/test_repository.py`

**接口：**
- `resolve_repository(start: Path) -> Path`：返回 Git 顶层目录，否则抛出 `RepositoryError`。
- `Workspace(root: Path)`，提供 `resolve_relative(path: str) -> Path`：返回位于 `root` 内的真实路径，否则抛出 `WorkspaceViolation`。

- [ ] **步骤 1：编写失败的仓库和路径安全测试**

```python
def test_resolve_repository_from_nested_directory(sample_git_repo):
    assert resolve_repository(sample_git_repo / "src") == sample_git_repo


def test_workspace_rejects_parent_traversal(sample_git_repo):
    with pytest.raises(WorkspaceViolation):
        Workspace(sample_git_repo).resolve_relative("../outside.txt")


def test_workspace_rejects_symlink_escape(sample_git_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (sample_git_repo / "link.txt").symlink_to(outside)
    with pytest.raises(WorkspaceViolation):
        Workspace(sample_git_repo).resolve_relative("link.txt")
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python -m pytest tests/test_repository.py -q`
预期：失败，因为解析器和工作空间类尚不存在。

- [ ] **步骤 3：实现根目录解析和真实路径包含校验**

使用 `subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True)`，将输出转换为 resolved `Path`。`Workspace.resolve_relative` 接受相对路径或绝对路径，规范化候选路径，调用 `Path.resolve(strict=False)`，再使用 `Path.is_relative_to(root)` 强制包含关系。定义稳定异常类型和用户安全的错误消息。

- [ ] **步骤 4：运行测试确认通过**

运行：`python -m pytest tests/test_repository.py -q`
预期：PASS，包括路径穿越和符号链接逃逸。

- [ ] **步骤 5：提交**

```bash
git add src/coding_agent/repository.py tests/test_repository.py
git commit -m "feat: enforce repository workspace boundary"
```

### 任务 3：实现共享文件扫描器和 `listfiles`

**文件：**
- 新建：`src/coding_agent/filesystem.py`
- 新建：`src/coding_agent/tools/listfiles.py`
- 新建：`tests/test_listfiles.py`
- 新建：`tests/conftest.py`

**接口：**
- `FileEntry(path: str, kind: Literal["file", "directory"], size: int | None)`。
- `scan_files(workspace: Workspace, path: str = ".", max_depth: int = 4, max_entries: int = 200) -> tuple[list[FileEntry], bool]`。
- `ListFilesTool(workspace).execute(arguments: dict[str, object]) -> dict[str, object]`。

- [ ] **步骤 1：创建夹具并编写失败的扫描测试**

创建 `tests/fixtures/sample-repo/README.md`、`package.json`、`src/index.js`、`tests/index.test.js`、`scripts/dev.sh` 和包含 `ignored.txt` 规则的 `.gitignore`。在 `tests/conftest.py` 中创建 `sample_git_repo` fixture：将夹具复制到 `tmp_path`，并在副本中执行 `git init`。

```python
def test_listfiles_honors_ignore_and_depth(sample_git_repo):
    entries, truncated = scan_files(Workspace(sample_git_repo), max_depth=1)
    paths = {entry.path for entry in entries}
    assert "README.md" in paths
    assert "ignored.txt" not in paths
    assert truncated is False
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python -m pytest tests/test_listfiles.py -q`
预期：失败，因为扫描器和工具尚不存在。

- [ ] **步骤 3：实现确定性的文件扫描**

使用 `Path.iterdir()` 按名称排序遍历目录。使用 `pathspec.PathSpec.from_lines("gitignore", lines)` 读取 `.gitignore`，并对规范化的仓库相对路径执行匹配。始终跳过 `.git`、被忽略路径，以及超过 `max_depth` 或 `max_entries` 的条目；达到限制时返回 `truncated=True`。请求目录通过 `Workspace.resolve_relative` 校验。

- [ ] **步骤 4：实现工具适配器**

当存在 `path` 时校验其为字符串；`max_depth` 校验为非负整数，默认 4；`max_entries` 校验为正整数，默认 200。返回：

```json
{
  "entries": [{"path": "src/index.js", "kind": "file", "size": 123}],
  "truncated": false
}
```

- [ ] **步骤 5：运行测试确认通过**

运行：`python -m pytest tests/test_listfiles.py -q`
预期：PASS，包括忽略规则和限制行为。

- [ ] **步骤 6：提交**

```bash
git add src/coding_agent/filesystem.py src/coding_agent/tools/listfiles.py tests/conftest.py tests/test_listfiles.py tests/fixtures/sample-repo
git commit -m "feat: add workspace-confined listfiles tool"
```

### 任务 4：实现 `readfile`

**文件：**
- 新建：`src/coding_agent/tools/readfile.py`
- 新建：`tests/test_readfile.py`

**接口：**
- `ReadFileTool(workspace, max_bytes: int = 64 * 1024).execute(arguments: dict[str, object]) -> dict[str, object]`。

- [ ] **步骤 1：编写失败的读取测试**

```python
def test_readfile_returns_utf8_text(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "README.md"})
    assert result["ok"] is True
    assert "sample-repo" in result["content"]


def test_readfile_returns_stable_errors(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "missing.txt"})
    assert result == {"ok": False, "error": {"type": "file_not_found", "message": "File not found"}}
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python -m pytest tests/test_readfile.py -q`
预期：失败，因为工具尚不存在。

- [ ] **步骤 3：实现受限的 UTF-8 读取**

要求 `path` 为非空路径，通过 `Workspace` 解析，允许仓库内绝对路径和解析后仍位于仓库内的 `..` 路径，拒绝目录，最多读取 `max_bytes + 1` 字节，超限返回 `file_too_large`。将文件不存在、权限错误和解码错误转换为稳定错误类型，错误中不暴露绝对路径。

- [ ] **步骤 4：运行测试确认通过**

运行：`python -m pytest tests/test_readfile.py -q`
预期：PASS，覆盖成功、文件不存在、目录、解码、大小、路径穿越和符号链接场景。

- [ ] **步骤 5：提交**

```bash
git add src/coding_agent/tools/readfile.py tests/test_readfile.py
git commit -m "feat: add bounded readfile tool"
```

### 任务 5：构建仓库上下文前缀

**文件：**
- 新建：`src/coding_agent/context.py`
- 新建：`tests/test_context.py`

**接口：**
- `build_repository_context(workspace: Workspace) -> str`。

- [ ] **步骤 1：编写失败的上下文测试**

```python
def test_context_contains_root_and_relative_manifest(sample_git_repo):
    context = build_repository_context(Workspace(sample_git_repo))
    assert "Repository Context" in context
    assert "README.md" in context
    assert str(sample_git_repo) not in context
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python -m pytest tests/test_context.py -q`
预期：失败，因为上下文构建器尚不存在。

- [ ] **步骤 3：实现稳定且有界的渲染器**

使用深度 4、最多 200 项调用共享扫描器。只渲染仓库标签、相对条目、类型、大小和截断状态，不包含绝对路径、文件内容、环境变量或密钥。保持扫描器顺序以确保结果确定性。

- [ ] **步骤 4：运行测试确认通过**

运行：`python -m pytest tests/test_context.py -q`
预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add src/coding_agent/context.py tests/test_context.py
git commit -m "feat: build repository context prefix"
```

### 任务 6：定义工具注册表和 OpenAI-compatible LLM Client

**文件：**
- 新建：`src/coding_agent/models.py`
- 新建：`src/coding_agent/llm.py`
- 新建：`src/coding_agent/tools/registry.py`
- 新建：`tests/test_llm.py`
- 新建：`tests/test_registry.py`

**接口：**
- `ToolDefinition(name: str, description: str, parameters: dict[str, object])`。
- `ToolRegistry.register(tool, definition)`、`definitions() -> list[dict[str, object]]` 和 `execute(name: str, arguments: dict[str, object]) -> dict[str, object]`。
- `LLMClient.complete(messages: list[dict[str, object]], tools: list[dict[str, object]]) -> AssistantTurn`。
- `AssistantTurn(content: str | None, tool_calls: list[ToolCall])`。
- `ToolCall(id: str, name: str, arguments: dict[str, object])`。

- [ ] **步骤 1：编写失败的注册表和模拟客户端测试**

```python
def test_registry_exports_native_tool_schema():
    registry = ToolRegistry()
    registry.register("readfile", fake_tool, {"type": "object", "properties": {"path": {"type": "string"}}})
    assert registry.definitions()[0]["type"] == "function"
    assert registry.definitions()[0]["function"]["name"] == "readfile"


def test_llm_client_normalizes_tool_calls(monkeypatch):
    client = LLMClient(api_key="key", model="model", base_url="https://example.test")
    monkeypatch.setattr(client, "_request", lambda messages, tools: fake_provider_response())
    turn = client.complete([], [])
    assert turn.tool_calls[0].name == "readfile"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python -m pytest tests/test_llm.py tests/test_registry.py -q`
预期：失败，因为类型、注册表和客户端尚不存在。

- [ ] **步骤 3：实现与 provider 无关的数据模型和注册表**

定义 `ToolCall` 和 `AssistantTurn` dataclass。按名称保存工具执行器，拒绝未知工具，校验参数为 JSON object，并暴露 `type="function"` 的 OpenAI Chat Completions 兼容工具定义。

- [ ] **步骤 4：实现 SDK 适配器**

构造 `openai.OpenAI(api_key=..., base_url=...)`，调用 `client.chat.completions.create(model=..., messages=..., tools=...)`。将 `message.content` 和 provider 的 tool calls 统一为 dataclass。解析 JSON arguments；解析失败时由循环转换为稳定的 `invalid_tool_arguments` 错误。

- [ ] **步骤 5：运行测试确认通过**

运行：`python -m pytest tests/test_llm.py tests/test_registry.py -q`
预期：PASS，且不发起网络请求。

- [ ] **步骤 6：提交**

```bash
git add src/coding_agent/models.py src/coding_agent/llm.py src/coding_agent/tools/registry.py tests/test_llm.py tests/test_registry.py
git commit -m "feat: add native tool calling client and registry"
```

### 任务 7：实现四轮 Agent Loop

**文件：**
- 新建：`src/coding_agent/agent.py`
- 新建：`tests/test_agent.py`

**接口：**
- `AgentService.run(query: str, workspace: Workspace) -> str`。
- `AgentLoop(llm_client, registry, context_builder, max_rounds=4).run(query: str, workspace: Workspace) -> str`。

- [ ] **步骤 1：使用 Fake LLM 编写失败的循环测试**

```python
def test_loop_executes_tool_and_returns_final_answer(sample_git_repo):
    llm = FakeLLM([
        ToolCallTurn("call-1", "readfile", {"path": "package.json"}),
        FinalTurn("The repository uses npm scripts.")
    ])
    answer = AgentLoop(llm, registry, build_repository_context, max_rounds=4).run("Explain scripts", Workspace(sample_git_repo))
    assert answer == "The repository uses npm scripts."
    assert llm.calls == 2


def test_loop_stops_after_four_tool_rounds(sample_git_repo):
    llm = FakeLLM([ToolCallTurn(f"call-{n}", "listfiles", {}) for n in range(5)])
    answer = AgentLoop(llm, registry, build_repository_context, max_rounds=4).run("Inspect", Workspace(sample_git_repo))
    assert "4" in answer
    assert llm.calls == 4
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python -m pytest tests/test_agent.py -q`
预期：失败，因为循环尚不存在。

- [ ] **步骤 3：实现消息历史和工具分发**

使用 developer 规则、仓库上下文和用户 query 初始化消息。每轮调用 `LLMClient.complete`；当 `tool_calls` 为空时返回 `content`；否则追加 assistant tool-call 消息，通过 `ToolRegistry` 执行每个调用，并追加包含 JSON 序列化结果的 tool 消息。计数单位是轮次，而不是单个调用数。

- [ ] **步骤 4：实现终止和错误处理**

达到 4 轮后停止并返回用户可读的未完成说明。将未知工具、参数错误和工具异常转换为结构化工具结果，让模型尝试恢复。将 provider 超时或网络错误转换为 `AgentError`，交给 CLI 处理。

- [ ] **步骤 5：运行测试确认通过**

运行：`python -m pytest tests/test_agent.py -q`
预期：PASS，覆盖最终响应、工具结果、错误和四轮上限。

- [ ] **步骤 6：提交**

```bash
git add src/coding_agent/agent.py tests/test_agent.py
git commit -m "feat: implement bounded read-only agent loop"
```

### 任务 8：添加 CLI 配置、渲染和端到端覆盖

**文件：**
- 新建：`src/coding_agent/config.py`
- 新建：`src/coding_agent/cli.py`
- 新建：`tests/test_cli.py`
- 修改：`README.md`

**接口：**
- `Settings.from_args_and_env(args: argparse.Namespace) -> Settings`。
- `main(argv: Sequence[str] | None = None) -> int`。

- [ ] **步骤 1：编写失败的 CLI 测试**

```python
def test_cli_requires_query():
    assert main([]) != 0


def test_cli_uses_repo_argument_and_renders_answer(monkeypatch, sample_git_repo, capsys):
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setattr("coding_agent.cli.AgentService.run", lambda self, query, workspace: "ok")
    assert main(["Explain scripts", "--repo", str(sample_git_repo)]) == 0
    assert capsys.readouterr().out.strip() == "ok"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python -m pytest tests/test_cli.py -q`
预期：失败，因为配置和 CLI 模块尚不存在。

- [ ] **步骤 3：实现配置优先级**

解析位置参数 `query`、可选 `--repo`、`--model` 和 `--base-url`。读取 `CODING_AGENT_API_KEY`、`CODING_AGENT_MODEL` 和 `CODING_AGENT_BASE_URL`；应用 CLI 覆盖、环境变量，再使用默认 Base URL。当 API Key 或模型缺失时返回清晰的非零错误。

- [ ] **步骤 4：实现 CLI 编排和输出**

解析仓库，构造 `Workspace`、`Settings`、`LLMClient`、包含两个工具的 `ToolRegistry`、`AgentService`，并将最终回答输出到 stdout。参数、仓库、配置、模型和工具错误输出到 stderr，并返回退出码 1。MVP 不启用 verbose 工具输出。

- [ ] **步骤 5：添加使用 Fake Client 的端到端测试**

用任务 7 的 Fake LLM 替换 CLI client factory。断言命令可从嵌套目录运行，上下文包含夹具清单，工具调用均在夹具内，provider 失败时返回非零退出码。

- [ ] **步骤 6：更新 README 的快速开始**

记录 Python 3.11+、`python -m pip install -e ".[dev]"` 安装方式、三个环境变量和 PRD 中的两个示例命令。明确 MVP 只读，并且仅支持 `listfiles` 和 `readfile`。

- [ ] **步骤 7：运行完整测试套件**

运行：`python -m pytest -q`
预期：PASS，且不发起网络请求。

- [ ] **步骤 8：提交**

```bash
git add src/coding_agent/config.py src/coding_agent/cli.py tests/test_cli.py README.md
git commit -m "feat: add coding agent CLI"
```

### 任务 9：执行最终验证和审查

**文件：**
- 仅当实现决策实质性偏离时修改：`docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`
- 除非验证失败需要修复，否则不新增源文件

- [ ] **步骤 1：运行静态和行为检查**

运行：`python -m pytest -q` 和 `python -m compileall -q src`。
预期：所有测试通过，编译命令退出码为 0。

- [ ] **步骤 2：针对夹具手动运行 CLI**

从 `tests/fixtures/sample-repo/src` 执行：

```bash
coding-agent "解释这个仓库的启动和测试脚本" --repo .
```

预期：输出基于夹具文件的最终解释，不包含绝对路径或密钥。

- [ ] **步骤 3：审查 diff 和安全边界**

运行：`git diff --check`、`git status --short`，并检查是否存在绝对路径访问、无界读取、测试中的意外网络请求或 API Key 日志。

- [ ] **步骤 4：提交最终验证修复**

```bash
git add src tests pyproject.toml README.md
git commit -m "test: verify read-only coding agent MVP"
```
