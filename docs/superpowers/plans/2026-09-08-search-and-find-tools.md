# Search 与 Find Files 工具实施计划

> **For agentic workers:** 本计划用于当前工作树的实现与复核。由于仓库约定由用户明确决定提交时机，执行过程中不自动创建 Git commit。

**目标：** 为 Coding Agent 增加内容搜索 `search` 和文件名/glob 搜索 `find_files`，并补充中文使用文档。

**架构：** 两个工具均实现独立的 `name`、`description`、JSON Schema 和 `execute()` 接口，通过 `CodingAgent.from_settings()` 注册到同一个 `ToolRegistry`。`search` 优先调用 `rg`，失败时使用 Python fallback；`find_files` 使用 pathlib 遍历和 glob 匹配。两者复用 `Workspace` 的边界校验和仓库忽略规则。

**技术栈：** Python 3.11+、pathlib、re、subprocess、pytest、现有 `Workspace` 与 `ToolRegistry`。

**规格：** `docs/superpowers/specs/2026-09-08-search-and-find-tools-prd.md`

## 全局约束

- 两个工具均为只读工具，不触发审批。
- 所有输入路径必须通过 `Workspace.resolve_relative()` 校验。
- 默认最多返回 200 条结果，并准确设置 `truncated`。
- 跳过 `.git`、`.codex`、`.venv`、`node_modules` 并遵循 `.gitignore`。
- `search.pattern` 是正则表达式；`find_files.pattern` 是文件名或 glob 模式。
- 不自动提交 Git，等待用户明确要求。

---

### 任务 1：为 `search` 建立行为测试

**文件：**

- 创建：`tests/test_search.py`

**接口：**

- 消费：`Workspace`、`SearchTool.execute(arguments)`。
- 产出：匹配行结构 `{path, line, text}`、空结果、越界错误、fallback 行为和截断行为的测试约束。

- [x] 测试普通内容匹配返回仓库相对路径、行号和文本。
- [x] 测试无匹配返回空列表。
- [x] 测试 workspace 外路径返回 `workspace_violation`。
- [x] 测试屏蔽 `rg` 后 Python fallback 仍能匹配。

### 任务 2：实现 `search`

**文件：**

- 创建：`src/coding_agent/tools/search.py`

**接口：**

- `SearchTool(workspace: Workspace, max_results: int = 200)`。
- `execute(arguments: dict[str, Any]) -> dict[str, Any]`。

- [x] 定义 `search` 名称、描述和严格 JSON Schema。
- [x] 校验 pattern、path、max_results 类型和正则表达式。
- [x] 用 `rg --line-number --no-heading --color never` 搜索，并处理无匹配退出码 1。
- [x] `rg` 不可用或启动失败时遍历 UTF-8 文件作为 fallback。
- [x] 过滤忽略目录、符号链接和无法解码的文件，返回统一结果。

### 任务 3：为 `find_files` 建立行为测试并实现

**文件：**

- 创建：`tests/test_find_files.py`
- 创建：`src/coding_agent/tools/find_files.py`

**接口：**

- `FindFilesTool(workspace: Workspace, max_results: int = 200)`。
- `execute(arguments: dict[str, Any]) -> dict[str, Any]`。

- [x] 测试精确文件名匹配。
- [x] 测试 glob 匹配和相对路径输出。
- [x] 测试 workspace 越界和结果截断。
- [x] 使用 pathlib 遍历普通文件，同时匹配文件名和仓库相对路径。
- [x] 过滤忽略目录、符号链接并返回排序后的文件列表。

### 任务 4：注册工具并更新 Agent 测试

**文件：**

- 修改：`src/coding_agent/coding_agent.py`
- 修改：`tests/test_coding_agent.py`

- [x] 在工厂函数创建 `SearchTool(workspace)` 和 `FindFilesTool(workspace)`。
- [x] 将两个工具注册到 `ToolRegistry`。
- [x] 更新可复用 Agent 的工具名称断言。

### 任务 5：更新 README 使用说明

**文件：**

- 修改：`README.md`

- [x] 将工具列表从“4 个文件工具和 1 个命令工具”更新为包含 `search`、`find_files` 的完整列表。
- [x] 说明 `search` 搜索文件内容，`find_files` 搜索文件名/glob。
- [x] 增加“自然语言描述功能 -> search -> readfile”和“已知文件模式 -> find_files”的示例。
- [x] 明确二者均为只读，不需要 Shell 审批。

### 任务 6：全量验证

**文件：** 无新增文件。

- [x] 运行 `uv run pytest -q`，确认所有测试通过。
- [x] 运行 `uv run python -m compileall -q src`。
- [x] 运行 `git diff --check`。
- [x] 检查工作树，确认未产生非预期文件或提交。
