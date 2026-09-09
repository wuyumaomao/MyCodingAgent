# Windows Shell 命令执行 PRD

## 文档状态

- 状态：实现中（运行时环境扩展已确认）
- 版本：v0.2
- 日期：2026-09-09
- 基础能力：CodingAgent、ToolExecutor、逐调用审批、trace/report
- 平台：Windows 10/11，Python 3.11+

## 1. 背景与目标

当前 Agent 可以读取和修改仓库文件，但不能运行测试、脚本或只读 Git 查询。新增一个受控的 `shell` 工具，使 Agent 能根据自然语言任务生成结构化命令请求，经系统校验和用户批准后在 Windows 仓库工作区内执行。

本功能的目标是让用户可以说“运行刚才创建的脚本”或直接提供“执行 `pytest tests/test_cli.py -q`”，Agent 将两者都转换为结构化命令；用户始终在真正执行前看到完整命令并逐次批准。

## 2. 产品范围

### 2.1 必须实现

- 新增 `shell` 工具，输入是 `program`、`args`、`cwd` 和可选 `timeout`，不接受任意命令字符串。
- 第一版程序白名单为 `python`、`pytest`、`git`、`npm` 和受控的 `uv`。
- `uv` 第一阶段只允许 `uv sync --dev` 或 `uv run <工作区内的 .py 脚本> [参数]`，每次调用都必须经过用户审批；其他 `uv` 子命令继续拒绝。
- `python` 和 `pytest` 动态优先使用目标仓库 `.venv` 的解释器；不存在时才回退 Agent 进程解释器。
- 目标 `.venv` 的 `Scripts`/`bin` 放在子进程 `PATH` 首位并设置 `VIRTUAL_ENV`，不切换 Agent 主进程环境。
- 每次 shell 调用单独审批；没有审批回调时返回 `approval_required`，不得启动进程。
- 默认使用 `shell=False` 的 Windows 进程启动方式，不通过 `cmd /c` 或 `powershell -Command` 拼接字符串。
- 工作目录、Python 脚本路径、pytest 路径和 Git 路径参数解析后必须位于 `Workspace.root` 内。
- 拒绝命令连接符、管道、重定向、反引号、换行和嵌套 Shell 参数。
- `git` 第一版只允许只读子命令：`status`、`diff`、`log`、`show`、`branch`、`rev-parse`。
- `npm` 第一版只允许 `test` 和 `run <script-name>`，拒绝 `install`、`exec`、`publish` 等命令。
- `python` 只允许执行仓库内脚本路径，拒绝 `-c`、`-m` 和脚本路径外的解释器入口。
- `pytest` 允许测试选择参数，但测试路径参数必须位于工作区；禁止通过参数修改工作区外的缓存或输出目录。
- 默认单次执行超时 60 秒，硬上限 300 秒；超时后终止完整 Windows 进程树。
- stdout 和 stderr 分别最多保留 64 KiB；超出后继续排空管道但只保留截断摘要。
- 执行结果返回退出码、stdout、stderr、是否超时、是否截断和耗时，供模型决定继续或结束。
- trace/report 记录命令摘要、工作目录、审批结果、退出码、超时、截断和输出摘要。
- 运行环境移除 API Key、Authorization、Token、Secret 等敏感变量，避免脚本直接继承模型凭据。
- CLI 在审批时显示程序、参数、仓库相对工作目录和超时，并保持现有错误码和 run 路径输出。

### 2.2 不在本次范围内

- 不支持任意 PowerShell/CMD 字符串、命令链、管道、重定向或后台任务。
- 不支持 `cmd.exe`、`powershell.exe`、`pwsh.exe` 作为白名单程序。
- 不支持除 `uv sync --dev` 外的安装依赖、网络下载、发布包、自动提交、推送或创建 Pull Request。
- 不支持跨多个命令的事务回滚。
- 不支持跨 ask 生命周期的 shell 进程、会话或终端状态持久化。
- 不实现命令白名单的配置文件热加载；白名单由代码中的 `ShellPolicy` 固定定义。

## 3. 用户交互与调用契约

用户既可以使用自然语言：

```text
运行刚才创建的 tools/repo_stats.py，告诉我 Python 文件总数。
```

也可以提供明确命令：

```text
执行 pytest tests/test_cli.py -q。
```

两种请求都由模型生成同一种结构化工具调用，系统不会把自然语言直接当作系统命令执行。

```json
{
  "program": "python",
  "args": ["tools/repo_stats.py"],
  "cwd": ".",
  "timeout": 60
}
```

工具结果使用稳定结构：

```json
{
  "ok": true,
  "program": "python",
  "args": ["tools/repo_stats.py"],
  "cwd": ".",
  "exit_code": 0,
  "stdout": "Python files: 12",
  "stderr": "",
  "timed_out": false,
  "output_truncated": false,
  "duration_ms": 42.1
}
```

失败结果仍作为 `role: tool` 回传模型：

```json
{
  "ok": false,
  "error": {
    "type": "command_not_allowed",
    "message": "The requested command is not allowed"
  }
}
```

## 4. 架构设计

```text
CodingAgent.from_settings()
    └── ShellPolicy + ShellTool + WindowsProcessRunner + TargetPythonResolver

AgentLoop.run()
    └── ToolExecutor
          └── ShellTool.execute(arguments)
                ├── ShellPolicy.validate()
                ├── ShellApprovalGate.approve()
                └── WindowsProcessRunner.run()
```

### 4.1 ShellPolicy

`ShellPolicy` 是纯校验组件，负责程序、子命令、参数、路径、超时和危险字符检查，不启动进程，也不记录事件。建议接口：

```python
class ShellPolicy:
    allowed_programs: frozenset[str]
    default_timeout: float = 60.0
    max_timeout: float = 300.0
    max_output_bytes: int = 64 * 1024

    def validate(self, request: ShellRequest, workspace: Workspace) -> ValidatedShellRequest:
        raise NotImplementedError
```

### 4.2 WindowsProcessRunner

`WindowsProcessRunner` 只负责启动和回收进程。使用参数列表调用 `subprocess.Popen(command, shell=False)`，通过后台读取线程限制 stdout/stderr 内存占用；超时时调用 `taskkill /PID <pid> /T /F`，再回收子进程和读取线程。测试中通过 fake Popen/runner 隔离真实系统进程。

### 4.3 ShellTool 与审批

`ShellTool` 遵循现有 ToolRegistry 工具契约，先做参数 Schema 校验，再调用 `ShellPolicy`。校验通过后构造 `ShellPreview`，通过当前运行的 EventSink 发出 `approval_request`，由 CLI 的回调展示并等待用户输入；批准后才调用 runner。拒绝、无审批和策略拒绝均不启动进程。

### 4.4 Agent 与 CLI

`CodingAgent.from_settings()` 装配一个可复用的 `ShellTool`。每次 `ask()` 仍由 `ToolExecutor` 创建本次调用计数和事件边界；Shell 审批事件必须写入当前 ask 的 recorder。CLI 保留一次运行的 recorder 和审批回调，只增加 shell 审批预览，不把 recorder 绑定到静态工具。

### 4.5 目标项目运行时

执行 `python` 或 `pytest` 前，runner 根据当前 `cwd` 所在目标 workspace 动态检查 `.venv`：Windows 使用 `.venv\\Scripts\\python.exe`，Unix 使用 `.venv/bin/python`。检查不到时暂时使用 Agent 的 `sys.executable`，并在上下文中提示可通过审批执行 `uv sync --dev`。同步命令在目标 workspace 中运行；命令完成后后续调用会重新发现新创建的解释器。目标环境依赖不完整导致 Python/pytest 失败时，错误结果回传模型，由模型请求批准同步后再重试。

## 5. 错误类型

Shell 工具使用以下稳定错误类型：

- `invalid_arguments`
- `command_not_allowed`
- `subcommand_not_allowed`
- `unsafe_argument`
- `workspace_violation`
- `cwd_not_found`
- `script_not_found`
- `approval_required`
- `approval_denied`
- `executable_not_found`
- `timeout`
- `output_limit`
- `process_error`

模型异常仍由 `ModelGateway` 处理，Shell 错误不抛出到模型循环外。

## 6. 可观测性与脱敏

事件顺序保持：

```text
tool_call
approval_request
approval_result
tool_result
```

trace 只保存程序、参数摘要、相对工作目录、退出码、状态和截断输出；report 保存完整的非敏感参数和受限输出。绝对仓库路径在用户界面可显示，但工具结果和 trace 统一使用仓库相对路径。输出中的常见 API Key、Bearer、Token 和 Secret 模式必须替换为 `[REDACTED]`。

## 7. 验收标准

- Agent 可通过自然语言生成并执行 `python tools/repo_stats.py`。
- Agent 可按用户明确给出的 `pytest tests/test_cli.py -q` 生成同等结构化调用。
- `cmd.exe`、PowerShell、命令连接符、管道、重定向和工作区外路径均被拒绝。
- `git status`、`git diff` 等只读命令可以执行，`git commit`、`git push`、`git reset` 被拒绝。
- `npm test` 和 `npm run <name>` 可以执行，`npm install` 被拒绝。
- 未批准、拒绝批准和非交互环境不会产生子进程。
- 超时能终止子进程树并返回 `timeout`，不会残留脚本进程。
- 大输出不会超过配置的内存和返回大小限制，并设置 `output_truncated=true`。
- 同一 `CodingAgent` 连续两次 `ask()` 的 shell 调用计数、审批事件和 recorder 彼此隔离。
- trace/report 的审批和工具结果事件顺序与现有写工具事件语义一致。
- 现有全部测试继续通过。
