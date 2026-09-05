# Windows Shell 命令执行实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 CodingAgent 增加一个只执行白名单程序、逐次审批、受工作区和资源限制的 Windows Shell 工具。

**Architecture:** 使用 `ShellPolicy` 做纯命令和路径校验，使用 `WindowsProcessRunner` 管理 `shell=False` 的 Windows 子进程，使用 `ShellTool` 连接 Schema、审批、runner 和结构化工具结果。`CodingAgent` 只装配静态 shell 依赖，`ToolExecutor` 和当前 `EventSink` 继续负责每次 ask 的计数与事件隔离。

**Tech Stack:** Python 3.11+、`subprocess`、`threading`、`pathlib`、`dataclasses`、现有 `ToolRegistry`/`ToolExecutor`/`RunRecorder`/pytest；目标运行平台为 Windows 10/11。

**Spec:** `docs/superpowers/specs/2026-09-05-shell-command-execution-prd.md`

## Global Constraints

- 只允许程序白名单：`python`、`pytest`、`git`、`npm`。
- 不接受任意命令字符串；工具输入必须是 `program`、`args`、`cwd`、`timeout` 的结构化对象。
- 使用 `subprocess.Popen(command, shell=False)`，不得通过 `cmd /c`、`powershell -Command` 或 `pwsh -Command` 拼接执行。
- 每次调用必须经过 `ShellApprovalGate`；未批准不得启动进程。
- 所有工作目录、脚本路径和路径参数解析后必须位于 `Workspace.root` 内。
- 默认超时 60 秒，最大超时 300 秒；stdout/stderr 各最多保留 64 KiB。
- 超时必须终止完整 Windows 进程树；输出超限时继续排空管道但只保留截断内容。
- 不继承 `CODING_AGENT_API_KEY`、`OPENAI_API_KEY`、Authorization/Token/Secret 类敏感环境变量。
- 现有读写工具、AgentLoop、trace/report 和 `AgentService` 兼容行为不能回归。

---

### Task 1: 定义 Shell 请求、策略和稳定错误

**Files:**
- Create: `src/coding_agent/tools/shell_policy.py`
- Test: `tests/test_shell_policy.py`

**Interfaces:**
- `ShellRequest(program: str, args: list[str], cwd: str = ".", timeout: float | None = None)`。
- `ValidatedShellRequest` 保存规范化程序名、参数、仓库相对 cwd、实际超时和执行文件名。
- `ShellPolicy.validate(request, workspace) -> ValidatedShellRequest`。
- `ShellPolicy` 默认 `allowed_programs=frozenset({"python", "pytest", "git", "npm"})`、`default_timeout=60.0`、`max_timeout=300.0`、`max_output_bytes=64 * 1024`。

- [ ] **Step 1: 编写失败测试**

覆盖以下具体断言：

测试函数必须明确覆盖：`test_allows_python_script_inside_workspace`、`test_rejects_unknown_program`、`test_rejects_shell_metacharacters`、`test_rejects_python_c_and_m`、`test_allows_read_only_git_commands_and_rejects_commit`、`test_allows_npm_test_and_run_but_rejects_install`、`test_rejects_path_outside_workspace`、`test_rejects_timeout_above_maximum`。每个测试创建 `ShellRequest`，调用 `ShellPolicy.validate(request, Workspace(sample_git_repo))`，并断言返回的规范化字段或具体异常错误类型。

每个拒绝断言检查稳定错误类型，例如 `command_not_allowed`、`subcommand_not_allowed`、`unsafe_argument` 或 `workspace_violation`。

- [ ] **Step 2: 运行失败测试**

运行：`uv run pytest tests/test_shell_policy.py -q`

预期：因 `shell_policy.py` 和接口尚未存在而失败。

- [ ] **Step 3: 实现最小策略层**

实现 `ShellPolicy` 的规范化和校验：

1. `program.lower()` 后只接受四个白名单值。
2. 所有 token 拒绝 `&`、`|`、`;`、`<`、`>`、反引号、``、`\n` 以及 `cmd.exe`/`powershell.exe`/`pwsh.exe`。
3. 使用 `Workspace.resolve_relative()` 校验 cwd 和脚本/路径参数；拒绝符号链接组件、仓库外路径和不存在的 cwd。
4. `python` 只接受仓库内 `.py` 脚本作为第一个位置参数，拒绝 `-c`、`-m`。
5. `git` 只接受 `status`、`diff`、`log`、`show`、`branch`、`rev-parse`；拒绝 `commit`、`push`、`reset`、`clean` 等写操作。
6. `npm` 只接受 `test` 或 `run` 后跟一个脚本名，拒绝 `install`、`exec`、`publish`。
7. timeout 缺省使用 60 秒，非正数或超过 300 秒直接返回 `invalid_arguments`。

- [ ] **Step 4: 运行策略测试**

运行：`uv run pytest tests/test_shell_policy.py -q`

预期：全部通过。

- [ ] **Step 5: 提交**

```powershell
git add src/coding_agent/tools/shell_policy.py tests/test_shell_policy.py
git commit -m "feat: add shell command policy"
```

### Task 2: 实现 Windows 进程 runner

**Files:**
- Create: `src/coding_agent/tools/shell_runner.py`
- Test: `tests/test_shell_runner.py`

**Interfaces:**
- `ShellRunResult(exit_code: int | None, stdout: str, stderr: str, timed_out: bool, output_truncated: bool, duration_ms: float)`。
- `WindowsProcessRunner.run(request: ValidatedShellRequest, workspace: Workspace, max_output_bytes: int) -> ShellRunResult`。

- [ ] **Step 1: 编写失败测试**

使用 fake Popen 注入，不启动真实长时间进程，覆盖：

测试函数必须明确覆盖：`test_runner_uses_argument_list_and_shell_false`、`test_runner_captures_stdout_and_stderr`、`test_runner_truncates_each_stream_without_deadlock`、`test_runner_terminates_process_tree_on_timeout`、`test_runner_returns_nonzero_exit_code`、`test_runner_removes_sensitive_environment_variables`。每个测试向 runner 注入 fake Popen，设置明确的 pid、communicate/read 行为和退出码，再检查 runner 的调用参数及 `ShellRunResult` 字段。

断言 `Popen` 收到的是列表参数、`shell=False`、仓库内 `cwd`、受限 `env`；超时断言调用 `taskkill /PID <pid> /T /F`，随后调用 fake process 的回收方法。

- [ ] **Step 2: 运行失败测试**

运行：`uv run pytest tests/test_shell_runner.py -q`

预期：因 runner 尚未存在而失败。

- [ ] **Step 3: 实现 runner**

实现细节：

1. 将程序别名解析为 Windows 可执行文件：`python` 使用当前解释器路径，`pytest` 使用当前解释器的 `-m pytest` 入口，`git` 查找 `git.exe`，`npm` 查找 `npm.cmd`。
2. 使用 `subprocess.Popen(command, cwd=workspace.root / request.cwd, stdin=subprocess.DEVNULL, stdout=PIPE, stderr=PIPE, shell=False, creationflags=CREATE_NEW_PROCESS_GROUP)`。
3. 为 stdout/stderr 各启动一个读取线程，单独维护最多 `max_output_bytes` 的字节缓冲；超过上限只设置截断标记，继续读取直到进程结束。
4. 主线程等待 request.timeout；超时先执行 `taskkill /PID str(pid) /T /F`，失败时回退到 `process.kill()`，再等待并回收读取线程。
5. 使用 `errors="replace"` 解码 UTF-8；记录进程退出码、超时、截断状态和耗时。
6. 从 `os.environ` 复制环境并删除 `CODING_AGENT_API_KEY`、`OPENAI_API_KEY`、包含 `AUTHORIZATION`、`TOKEN`、`SECRET` 的变量。

- [ ] **Step 4: 运行 runner 测试**

运行：`uv run pytest tests/test_shell_runner.py -q`

预期：全部通过。

- [ ] **Step 5: 提交**

```powershell
git add src/coding_agent/tools/shell_runner.py tests/test_shell_runner.py
git commit -m "feat: add bounded Windows shell runner"
```

### Task 3: 实现 ShellTool 和逐调用审批

**Files:**
- Create: `src/coding_agent/tools/shell.py`
- Modify: `src/coding_agent/tools/approval.py`
- Test: `tests/test_shell_tool.py`

**Interfaces:**
- `ShellPreview` 保存 program、args、cwd、timeout 和可读命令摘要，并提供 `as_dict()`。
- `EventRecorder` 协议定义 `__call__(event_type: str, **payload: object) -> None`；`ShellApprovalGate(ask: Callable[[ShellPreview], bool] | None = None, record: EventRecorder | None = None)`。
- `ShellTool(workspace, policy, approval_gate, runner).execute(arguments) -> dict[str, Any]`。
- 工具 Schema 为 `program: string`、`args: array[string]`、`cwd: string`、`timeout: number`，`max_output_bytes` 不暴露给模型。

- [ ] **Step 1: 编写失败测试**

覆盖：

测试函数必须明确覆盖：`test_shell_tool_requires_approval_without_starting_runner`、`test_shell_tool_denial_returns_structured_error`、`test_shell_tool_approves_and_returns_process_result`、`test_shell_tool_policy_error_does_not_request_approval`、`test_shell_tool_emits_approval_events_to_current_sink`、`test_shell_tool_reports_timeout_and_truncated_output`。每个测试传入固定的 `program`、`args`、`cwd` 和 `timeout`，并断言 fake runner 调用次数、稳定错误类型和事件顺序。

审批测试断言 runner 调用次数为零或一；事件测试断言顺序为 `approval_request`、`approval_result`、`tool_result`，且 report payload 包含完整非敏感预览。

- [ ] **Step 2: 运行失败测试**

运行：`uv run pytest tests/test_shell_tool.py -q`

预期：因 ShellTool 和 ShellApprovalGate 尚未存在而失败。

- [ ] **Step 3: 实现 ShellTool**

执行顺序固定为：解析 Schema 参数 -> `ShellPolicy.validate()` -> 构造 `ShellPreview` -> 通过当前 EventSink 记录审批请求 -> 调用 approval callback -> 记录批准/拒绝 -> 批准后调用 runner -> 返回稳定结果。策略拒绝不产生审批询问；审批拒绝和无 callback 分别返回 `approval_denied`、`approval_required`。

在 `approval.py` 中抽取现有事件 sink 上下文的通用小适配，保持 `WriteApprovalGate` 的行为兼容；ShellTool 使用同一当前 run sink，不在工厂绑定某一次 recorder。

- [ ] **Step 4: 运行工具测试**

运行：`uv run pytest tests/test_shell_tool.py tests/test_write_primitives.py tests/test_trace_integration.py -q`

预期：全部通过，现有写工具审批行为不回归。

- [ ] **Step 5: 提交**

```powershell
git add src/coding_agent/tools/shell.py src/coding_agent/tools/approval.py tests/test_shell_tool.py
git commit -m "feat: add approved shell tool"
```

### Task 4: 接入 CodingAgent、ToolExecutor 和生命周期

**Files:**
- Modify: `src/coding_agent/coding_agent.py`
- Modify: `src/coding_agent/tool_executor.py`
- Modify: `tests/test_coding_agent.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- `CodingAgent.from_settings(repo, settings, *, shell_policy: ShellPolicy | None = None, shell_runner: WindowsProcessRunner | None = None)`。
- 同一 `CodingAgent` 顺序两次 `ask()` 时，每次 shell 调用使用当前 EventSink，工具调用计数不跨任务共享。

- [ ] **Step 1: 编写失败测试**

增加以下测试：

测试函数必须明确覆盖：`test_factory_registers_shell_tool`、`test_agent_uses_current_recorder_for_shell_approval_events`、`test_reused_agent_resets_shell_call_limit_between_asks`、`test_shell_result_is_returned_to_model_as_role_tool`。测试使用 fake runner 和 fake LLM，不启动真实进程；断言工具定义中出现 `shell`，第二次 ask 的 recorder 不包含第一次的审批事件，并检查模型收到的最后一条消息 `role == "tool"`。

使用 fake runner 和 fake LLM，不启动真实进程；断言工具定义中出现 `shell`，第二次 ask 的 recorder 不包含第一次的审批事件。

- [ ] **Step 2: 运行失败测试**

运行：`uv run pytest tests/test_coding_agent.py tests/test_agent.py -q`

预期：因工厂未注册 shell 工具而失败。

- [ ] **Step 3: 接入静态装配**

在 `CodingAgent.from_settings()` 创建 workspace 后构造 `ShellPolicy`、`WindowsProcessRunner`、`ShellApprovalGate` 和 `ShellTool`，注册其 Schema、描述和执行函数。runner/policy 通过可选参数注入，供测试替换；默认实例只在工厂创建一次，不保存运行级计数或 recorder。

确认 `ToolExecutor` 继续在每次 `AgentLoop.run()` 内实例化，ShellTool 的审批事件通过 `event_sink_context` 指向当前 run。

- [ ] **Step 4: 运行 Agent 生命周期测试**

运行：`uv run pytest tests/test_coding_agent.py tests/test_agent.py tests/test_tool_executor.py -q`

预期：全部通过，读写工具和现有工具调用限制不回归。

- [ ] **Step 5: 提交**

```powershell
git add src/coding_agent/coding_agent.py src/coding_agent/tool_executor.py tests/test_coding_agent.py tests/test_agent.py
git commit -m "feat: register shell tool in coding agent"
```

### Task 5: 迁移 CLI 审批交互和配置

**Files:**
- Modify: `src/coding_agent/cli.py`
- Modify: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- 新增 `_ask_shell_approval(preview: ShellPreview) -> bool`，展示程序、参数、相对 cwd 和 timeout，只接受 `y`/`yes`。
- CLI 默认把 `_ask_shell_approval` 传给 `CodingAgent.from_settings()`；非交互环境传 `None`。
- CLI 增加 `--shell-timeout`，默认 60，允许范围 1-300；输出上限保持策略固定为 64 KiB。

- [ ] **Step 1: 编写失败测试**

覆盖：

测试函数必须明确覆盖：`test_cli_shell_approval_accepts_only_yes`、`test_cli_shell_command_runs_after_approval`、`test_cli_shell_command_is_rejected_without_tty`、`test_cli_passes_shell_timeout_to_policy`、`test_cli_reports_shell_trace_and_report_paths`。fake LLM 返回固定的 `shell` 工具调用，fake runner 返回固定结果；断言拒绝时 runner 调用次数为零，批准时工具结果回到模型。

fake LLM 返回 `shell` 工具调用，fake runner 返回固定结果，断言审批拒绝时 runner 不执行，批准时模型能收到 `role: tool` 结果。

- [ ] **Step 2: 运行失败测试**

运行：`uv run pytest tests/test_cli.py -q`

预期：新增 shell CLI 测试失败。

- [ ] **Step 3: 实现 CLI 迁移**

复用现有 `_ask_write_approval` 的异常处理和 stderr 输出风格；shell 预览显示规范化命令但不显示绝对路径。`--shell-timeout` 先由 argparse 解析，再交给 `ShellPolicy` 做 1-300 的最终校验。移除任何静态 recorder 绑定，确保审批事件写入当前 ask 的 recorder。

- [ ] **Step 4: 运行 CLI 回归测试**

运行：`uv run pytest tests/test_cli.py tests/test_coding_agent.py tests/test_agent.py tests/test_trace_integration.py -q`

预期：全部通过。

- [ ] **Step 5: 更新 README**

加入自然语言和明确命令两个示例，明确说明 Agent 会生成结构化命令、系统会展示完整命令并逐次审批；列出四个程序白名单、Git 只读子命令、超时和输出限制，以及不支持任意 CMD/PowerShell 字符串。

- [ ] **Step 6: 提交**

```powershell
git add src/coding_agent/cli.py tests/test_cli.py README.md
git commit -m "feat: add shell approval flow to cli"
```

### Task 6: 端到端验证、文档一致性和安全回归

**Files:**
- Modify: `docs/superpowers/specs/2026-09-05-shell-command-execution-prd.md`（仅在验证发现契约不一致时修改）
- Modify: `docs/superpowers/plans/2026-09-05-shell-command-execution.md`（同步已确认接口）
- Test: `tests/test_shell_policy.py`
- Test: `tests/test_shell_runner.py`
- Test: `tests/test_shell_tool.py`

**Interfaces:**
- 不新增公共运行时接口；本任务只验证前述契约在 Windows 和现有运行链路中一致。

- [ ] **Step 1: 运行安全回归测试**

运行：`uv run pytest tests/test_shell_policy.py tests/test_shell_runner.py tests/test_shell_tool.py tests/test_cli.py -q`

必须覆盖：命令注入字符、工作区逃逸、符号链接、只读 Git 白名单、npm 安装拒绝、非交互拒绝、超时树终止、输出截断和敏感环境变量移除。

- [ ] **Step 2: 运行完整验证**

运行：

```powershell
uv run pytest -q
uv run python -m compileall -q src
git diff --check
```

预期：完整测试无失败，源码编译成功，diff 检查无错误。

- [ ] **Step 3: 做契约检查**

逐项核对 PRD、plan、README、`CodingAgent.from_settings()`、ShellTool Schema、稳定错误类型和 trace/report 事件名称；确认没有引入任意 shell 字符串、自动批准、后台进程、网络安装或静态 recorder 绑定。

- [ ] **Step 4: 提交最终文档同步**

```powershell
git add docs/superpowers/specs/2026-09-05-shell-command-execution-prd.md docs/superpowers/plans/2026-09-05-shell-command-execution.md
git commit -m "docs: plan Windows shell command execution"
```
