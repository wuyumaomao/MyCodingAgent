# Coding Agent

一个用于探索和构建 Coding Agent 的项目。

## 项目状态

项目处于受控操作 MVP 阶段，当前支持通过 CLI 读取、解释、在用户批准后修改本地 Git 仓库，以及运行受限的测试和脚本命令。

## 项目目标

逐步构建一个能够理解开发任务、构造上下文、调用工具并交付结果的 Coding Agent。

计划关注以下能力：

- 任务理解与澄清
- 计划生成与执行
- 代码库浏览和修改
- 测试与结果验证
- 可追踪的执行过程

## 快速开始

环境要求：Python 3.11+。

安装开发依赖：

```bash
uv sync --extra dev
```

配置模型服务：

复制 `.env.example` 为 `.env`，然后填写真实配置：

```bash
copy .env.example .env
```

`.env` 会被自动读取且不会提交到 Git。也可以直接设置同名环境变量。

运行查询：

```bash
coding-agent "解释这个仓库的启动和测试脚本"
coding-agent "package.json 里有哪些可用命令" --repo .
```

每个工具默认最多调用 3 次；需要调整时可使用 `--max-tool-calls 5`。超过限制后，Agent 会收到结构化错误并决定结束或改用其他工具。

当前提供六个文件/搜索工具和一个受控命令工具：

- `listfiles`：列出仓库中的文件和目录。
- `readfile`：读取 UTF-8 文本文件。
- `search`：搜索文件内容中的文本或正则表达式，返回匹配文件、行号和文本。
- `find_files`：按文件名或 glob 模式查找文件，例如 `*.py`、`*shell*`。
- `write_file`：创建文件或整体覆盖已有文件。
- `patch_file`：精确替换已有文件中唯一匹配的一段文本。
- `shell`：在批准后运行白名单中的仓库命令。

查找代码时，可以根据目标信息选择工具：

- 只知道代码功能或关键词，例如“找出排序脚本”或“哪里使用了 `ShellPolicy`”，使用 `search` 搜索文件内容，再用 `readfile` 阅读候选文件。
- 知道文件名的一部分或文件模式，例如“找所有 shell 相关的 Python 文件”，使用 `find_files` 搜索文件名或路径。

`search` 和 `find_files` 都是只读工具，不需要用户审批；它们内部可以使用 `rg` 或 Python 遍历，但不会调用对外暴露的高风险 `shell` 工具。

`write_file` 和 `patch_file` 都是高风险工具。模型提出调用后，CLI 会逐次显示仓库相对路径和受限预览；仅输入 `y` 或 `yes` 才会写入。覆盖已有文件会显示明确警告。无交互输入时，写工具返回 `approval_required`，不会修改文件。

写入只允许发生在仓库工作区内。解析后仍在工作区内的绝对路径或 `..` 路径可以使用；逃逸工作区、经过符号链接、父目录不存在或超过 64 KiB 的写入会被拒绝。写入通过同目录临时文件和原子替换完成，替换失败时保留原文件。

`shell` 同样是高风险工具。可以用自然语言要求 Agent 运行脚本或测试，例如“运行 `tools/repo_stats.py`”或“执行 `pytest tests/test_cli.py -q`”；也可以在请求中写出完整命令。Agent 会生成结构化的 `program + args` 调用，CLI 显示实际命令、仓库相对工作目录和超时，只有输入 `y` 或 `yes` 才会启动进程。无交互输入时返回 `approval_required`。

第一版只允许 `python`、`pytest`、`git`、`npm` 和受控的 `uv`：Python 只能运行工作区内的 `.py` 脚本；Git 只允许 `status`、`diff`、`log`、`show`、`branch`、`rev-parse`；npm 只允许 `npm test` 与 `npm run <script>`；uv 只允许 `uv sync --dev` 或 `uv run <工作区内的 .py 脚本> [参数]`。目标仓库存在 `.venv` 时，Python/pytest 会优先使用目标解释器；环境缺失或依赖不完整时，Agent 可以请求批准后运行 `uv sync --dev`，完成后自动使用新环境。不支持任意 CMD/PowerShell 字符串、管道、重定向、命令连接、后台进程、其他 uv 命令、提交或推送。默认超时为 60 秒，可用 `--shell-timeout` 调整到最多 300 秒；stdout 与 stderr 各最多保留 64 KiB，超时会终止 Windows 进程树。

工具调用的校验顺序是：`ResponseParser -> 工具查找 -> JSON Schema -> 工具安全检查 -> 执行`。模型返回的 arguments 先按注册时提供的 JSON Schema 在客户端校验；缺少必填字段、类型错误、数值越界或包含不允许的额外字段时，不会调用工具，而是把 `invalid_tool_arguments` 作为结构化 `role: tool` 结果回传给模型。Schema 只负责参数结构，工作区边界、文件存在性、权限和编码等运行时安全检查仍由具体工具负责。Provider 支持的 `strict` schema 只是额外约束，不能替代客户端校验。

库调用可以通过 `CodingAgent.from_settings()` 创建一个可复用的 Agent，再使用 `ask()` 执行独立任务：

```python
agent = CodingAgent.from_settings(repo, settings)
answer = agent.ask("解释项目结构")
```

`from_settings()` 负责装配 `Workspace`、模型客户端、工具注册表和循环依赖；每次 `ask()` 都会创建新的对话上下文、工具调用计数和事件接收器。若需要保存本次运行的 trace/report，应为每次 `ask()` 创建新的 `RunRecorder` 并显式传入。CLI 也通过同一 facade 启动 Agent。

上下文由 `ConversationContext` 管理：system messages 在一次 run 内保持不变，history 按顺序追加用户请求、assistant tool call 和 tool result；每轮请求都会发送完整消息历史。

每次 CLI 提问都会创建一个独立的 run。运行结束后，CLI 会在标准错误中显示 run ID 和 trace 路径：

```text
.coding-agent/runs/<run-id>/trace.json
```

trace 会记录模型请求、工具调用、审批结果、工具结果、错误和最终回答，不会保存 API Key。

每次运行都会在同一个 run 目录生成两个文件：`trace.json` 保存简洁摘要，`report.json` 保存每轮完整 LLM 消息、工具定义、规范化模型响应和工具结果。report 仍会进行字段脱敏；如果读取的文件包含密钥等敏感文本，这些内容可能出现在报告中，请谨慎保存。

可以使用链路检查脚本查看一次 run 的关键过程：

```powershell
uv run python scripts/inspect_run.py
uv run python scripts/inspect_run.py .coding-agent/runs/<run-id>
```

脚本默认选择最新的完整 run，只输出事件顺序、工具、审批、成功/失败和耗时，不展开完整 prompt。每个 `llm_request` 事件还包含 `prompt_chars`，表示本轮发送给模型的 `messages` 与工具定义序列化后的字符数，可用来观察上下文长度随轮次的变化。

## 开发约定

- 先明确目标和边界，再开始实现
- 为重要行为补充自动化验证
- 保持提交范围小而清晰
- 在完成前运行与变更相匹配的检查

## 后续计划

1. 增加 Shell/测试工具及审批机制
2. 增加运行状态持久化和断点恢复
3. 增加本地 HTTP 服务和 IDE 客户端
