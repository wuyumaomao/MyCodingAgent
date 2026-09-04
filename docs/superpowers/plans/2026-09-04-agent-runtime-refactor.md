# Agent Runtime 轻量化架构优化实施计划

> **给 Agent 开发者：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 作为执行子技能，按任务逐项实现。步骤使用复选框（`- [x]`）跟踪。

**目标：** 在保持现有工具、审批、trace/report 和 CLI 行为兼容的前提下，新增可复用的 `CodingAgent` 入口，并将 AgentLoop 的模型、工具和事件职责收敛到清晰边界。

**架构：** 新增 `CodingAgent` 负责静态依赖装配和一次 query 的公共调用；`AgentLoop` 负责轮次与上下文推进；`ModelGateway`、`ToolExecutor` 和 `EventSink` 分别负责模型错误归一化、工具执行边界和运行事件。`RunRecorder` 通过事件适配器接入，写工具继续由自身的审批门与原子写入器负责。

**技术栈：** Python 3.11+、现有 `pathlib`/`dataclasses`/`typing.Protocol`、OpenAI-compatible 客户端、pytest、uv。

**规格文档：** `docs/superpowers/specs/2026-09-04-agent-runtime-refactor-prd.md`

## 全局约束

- `CodingAgent` 实例可顺序复用，但 `ConversationContext`、工具调用计数和 recorder 必须按每次 `ask()` 隔离。
- 不改变现有 `AgentLoop` 直接调用的返回值、异常和消息顺序兼容性。
- 不改变只读工具、写工具、JSON Schema、逐调用审批、Workspace 边界和 AtomicWriter 行为。
- 工具失败继续作为结构化 `role: tool` 结果回传模型；模型请求失败继续抛出 `AgentError`。
- trace/report 继续记录现有事件名称、顺序、脱敏规则和摘要/详细字段。
- 无 recorder 时不写磁盘；不得在库 API 中隐式创建运行目录。
- 每项任务先写失败测试，再实现最小代码，再运行聚焦测试；每项任务完成后独立提交。

---

### 任务 1：定义运行事件和空记录适配器

**文件：**
- 新建：`src/coding_agent/events.py`
- 修改：`src/coding_agent/trace.py`
- 测试：`tests/test_events.py`
- 测试：`tests/test_trace.py`

**接口：**
- `EventSink` Protocol：`emit(event_type, **payload)`、`complete(answer)`、`fail(error_type, message, *, duration_ms=None)`。
- `NullEventSink`：所有方法为空操作，不写文件。
- `RecorderEventSink`：将 `emit/complete/fail` 转发到 `RunRecorder`，保留现有 `report_payload` 能力。

- [x] **步骤 1：编写失败测试**

覆盖空 sink 不产生副作用、record sink 保持事件类型和终态行为、同一 sink 不重复结束。测试使用 fake recorder，避免依赖真实磁盘。

- [x] **步骤 2：运行专项测试确认失败**

运行：`uv run pytest tests/test_events.py -q`

- [x] **步骤 3：实现最小事件接口**

使用 `Protocol` 定义窄契约；`NullEventSink` 不保存状态；`RecorderEventSink` 只做转发，不复制 trace/report 逻辑。

- [x] **步骤 4：运行专项测试确认通过**

运行：`uv run pytest tests/test_events.py tests/test_trace.py -q`

- [x] **步骤 5：提交**

```powershell
git add src/coding_agent/events.py src/coding_agent/trace.py tests/test_events.py tests/test_trace.py
git commit -m "refactor: add agent event sink boundary"
```

### 任务 2：提取 ModelGateway

**文件：**
- 新建：`src/coding_agent/model_gateway.py`
- 修改：`src/coding_agent/agent.py`
- 测试：`tests/test_model_gateway.py`

**接口：**
- `ModelGateway(llm_client).complete(messages, tools) -> AssistantTurn`。
- `ModelGatewayError(error_type, public_message)`：保留原始异常作为 cause。

**职责边界：** `ResponseParser` 继续负责将 Provider 原始响应解析为 `ParsedResponse`/`AssistantTurn`；`ModelGateway` 只负责调用 `LLMClient` 并统一 timeout、解析失败、无效工具参数和 provider 异常，不新增第二套响应解析逻辑。

- [x] **步骤 1：编写失败测试**

为 timeout、无效工具参数、无效响应、未知 provider 异常分别建立 fake LLM，断言稳定 `error_type` 和用户安全消息；成功路径断言返回原始 `AssistantTurn`。

- [x] **步骤 2：运行专项测试确认失败**

运行：`uv run pytest tests/test_model_gateway.py -q`

- [x] **步骤 3：实现异常归一化**

把当前 `AgentLoop.run()` 第 66-100 行的异常分类移入 gateway；不要在 gateway 写 recorder，不要吞掉原始异常 cause。

- [x] **步骤 4：将 AgentLoop 接到 gateway**

保留旧的 `llm_client` 构造兼容方式；内部优先使用 `ModelGateway`。模型错误仍在 loop 边界记录一次并抛出 `AgentError`。

- [x] **步骤 5：运行 AgentLoop 与专项测试**

运行：`uv run pytest tests/test_model_gateway.py tests/test_agent.py tests/test_trace_integration.py -q`

- [x] **步骤 6：提交**

```powershell
git add src/coding_agent/model_gateway.py src/coding_agent/agent.py tests/test_model_gateway.py
git commit -m "refactor: isolate model error mapping"
```

### 任务 3：提取 ToolExecutor 并隔离每次 run 的计数

**文件：**
- 新建：`src/coding_agent/tool_executor.py`
- 修改：`src/coding_agent/agent.py`
- 测试：`tests/test_tool_executor.py`
- 测试：`tests/test_agent.py`
- 测试：`tests/test_trace_integration.py`

**接口：**
- `ToolExecutor(registry, limits, event_sink).execute(call) -> ToolExecution`。
- `ToolExecution.result` 保存结构化工具结果，`duration_ms` 保存执行耗时。
- 工具计数器由 executor 实例持有；每次 `ask()` 创建新的 executor，或显式调用 `reset()`。

- [x] **步骤 1：编写失败测试**

覆盖已注册工具成功、未知工具、Schema 无效参数、执行器异常、调用上限和耗时；增加复用同一 `CodingAgent` 两次 query 的测试，确认第二次调用计数从零开始。

- [x] **步骤 2：运行专项测试确认失败**

运行：`uv run pytest tests/test_tool_executor.py tests/test_agent.py -q`

- [x] **步骤 3：实现通用工具边界**

把 `_execute()`、调用计数和工具耗时从 `AgentLoop` 移入 executor；继续调用 `ToolRegistry.validate()` 后再 `execute()`。写工具内部的审批、路径安全和原子写入不迁移。

- [x] **步骤 4：迁移 trace 事件**

executor 发出 `tool_call`、`tool_call_limit`、`tool_result` 所需的语义事件；保持当前 trace 摘要和 report 详细 payload，写工具大 payload 继续使用现有摘要逻辑。

- [x] **步骤 5：运行工具、AgentLoop 和 trace 测试**

运行：`uv run pytest tests/test_tool_executor.py tests/test_agent.py tests/test_trace_integration.py -q`

- [x] **步骤 6：提交**

```powershell
git add src/coding_agent/tool_executor.py src/coding_agent/agent.py tests/test_tool_executor.py tests/test_agent.py tests/test_trace_integration.py
git commit -m "refactor: isolate tool execution boundary"
```

### 任务 4：收敛 AgentLoop 主循环和生命周期

**文件：**
- 修改：`src/coding_agent/agent.py`
- 修改：`src/coding_agent/context.py`（仅在需要时）
- 测试：`tests/test_agent.py`
- 测试：`tests/test_trace_integration.py`

**接口：**
- `AgentLoop.run(query, workspace, *, event_sink=None) -> str`。
- 兼容旧的 `recorder` 参数，迁移到 `EventSink` 的过程不能破坏现有测试。

- [x] **步骤 1：编写生命周期测试**

断言每次 run 创建新上下文；同一 loop 连续运行时工具计数不串任务；成功和失败路径只产生一个终态事件；无 recorder 路径与当前回答一致。

- [x] **步骤 2：实现事件注入**

在 `run()` 开始时选择 `NullEventSink` 或传入 sink，并让主循环通过 `emit/complete/fail` 记录事件，移除重复的 `if self.recorder` 分支。

- [x] **步骤 3：实现简化后的轮次流程**

主循环只保留：获取消息和工具定义、调用 gateway、处理最终回答、追加 assistant turn、委托 executor、追加 tool result、处理轮次上限。删除已迁移的 provider 异常和工具执行细节。

- [x] **步骤 4：运行回归测试**

运行：`uv run pytest tests/test_agent.py tests/test_trace_integration.py tests/test_context.py -q`

- [x] **步骤 5：提交**

```powershell
git add src/coding_agent/agent.py src/coding_agent/context.py tests/test_agent.py tests/test_trace_integration.py
git commit -m "refactor: simplify agent loop lifecycle"
```

### 任务 5：新增 CodingAgent facade 和工厂装配

**文件：**
- 新建：`src/coding_agent/coding_agent.py`
- 修改：`src/coding_agent/__init__.py`
- 测试：`tests/test_coding_agent.py`

**接口：**
- `CodingAgent.from_settings(repo, settings, *, approval_ask=None, limits=None, repository_context_builder=build_repository_context) -> CodingAgent`。
- `CodingAgent.ask(query, *, recorder=None) -> str`。
- `CodingAgent.workspace` 为只读或约定不可变引用。

- [x] **步骤 1：编写失败测试**

测试工厂注册四个现有工具；注入 fake LLM、approval callback 和 context builder；测试 `ask()` 能完成读工具和写工具流程；测试不传 recorder 时不会创建 trace 文件。

- [x] **步骤 2：实现工厂装配**

将当前 `cli.py` 第 42-93 行的装配逻辑迁移为可测试构造函数：解析 repository、创建 workspace、注册 list/read/write/patch 工具、创建审批门、创建 LLM client 和 loop。`RunRecorder` 不在工厂中创建。

- [x] **步骤 3：实现 ask 生命周期**

`ask()` 为每次调用选择 event sink，并把 query 和 workspace 交给 loop；确保 executor、context 和终态状态按调用隔离。

- [x] **步骤 4：运行 facade 专项测试**

运行：`uv run pytest tests/test_coding_agent.py tests/test_agent.py -q`

- [x] **步骤 5：提交**

```powershell
git add src/coding_agent/coding_agent.py src/coding_agent/__init__.py tests/test_coding_agent.py
git commit -m "feat: add reusable coding agent facade"
```

### 任务 6：迁移 CLI 到 CodingAgent

**文件：**
- 修改：`src/coding_agent/cli.py`
- 修改：`tests/test_cli.py`
- 修改：`README.md`

- [x] **步骤 1：编写 CLI 迁移测试**

保留现有参数、成功输出、配置错误、仓库错误、模型错误、审批批准/拒绝和 trace/report 路径断言；新增断言 CLI 调用 `CodingAgent.from_settings()` 和 `ask()`，不再直接注册工具。

- [x] **步骤 2：实现 CLI 最小迁移**

CLI 继续创建一次运行的 `RunRecorder`，根据 stdin 是否交互创建 approval callback，然后调用 `CodingAgent.from_settings()`；将 recorder 传给 `ask()`。保留错误码和 stderr 文本兼容。

- [x] **步骤 3：处理 AgentService 兼容层**

将 `AgentService` 标记为迁移兼容入口，或让其内部委托 `CodingAgent`；不得在本任务删除已有符号，除非现有测试和文档都已迁移。

- [x] **步骤 4：更新 README 架构说明**

说明 `CodingAgent.from_settings()` 负责装配静态组件，`ask()` 代表一次独立运行，`RunRecorder` 属于调用方传入的一次 run；说明 CLI、库调用和未来客户端共享同一 facade。

- [x] **步骤 5：运行 CLI 与完整回归测试**

运行：`uv run pytest tests/test_cli.py tests/test_coding_agent.py tests/test_agent.py tests/test_trace_integration.py -q`，随后运行 `uv run pytest -q`。

- [x] **步骤 6：提交**

```powershell
git add src/coding_agent/cli.py tests/test_cli.py README.md
git commit -m "refactor: route cli through coding agent facade"
```

### 任务 7：最终验证与文档一致性检查

**文件：**
- 修改：`docs/superpowers/specs/2026-09-04-agent-runtime-refactor-prd.md`（仅修正文档自检发现的问题）
- 修改：`docs/superpowers/plans/2026-09-04-agent-runtime-refactor.md`（仅同步已确认接口）

- [x] **步骤 1：运行最终验证**

```powershell
uv run pytest -q
uv run python -m compileall -q src
git diff --check
```

- [x] **步骤 2：检查公共接口和错误类型**

确认 PRD、plan、README、现有测试中的 `CodingAgent.from_settings()`、`ask()`、`RunRecorder` 生命周期、工具错误和 trace/report 事件名称一致；确认没有 `TODO`、`TBD` 或未决定的占位接口。

- [x] **步骤 3：检查范围**

确认没有偷偷引入多线程、自动重试、事件总线、依赖注入容器或工具业务逻辑重写。

- [x] **步骤 4：提交文档**

```powershell
git add docs/superpowers/specs/2026-09-04-agent-runtime-refactor-prd.md docs/superpowers/plans/2026-09-04-agent-runtime-refactor.md
git commit -m "docs: specify lightweight agent runtime architecture"
```

