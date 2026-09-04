# Agent Runtime 轻量化架构优化 PRD

## 文档状态

- 状态：已实现
- 版本：v0.1
- 日期：2026-09-04
- 基础能力：当前 Coding Agent MVP、工具 Schema 校验计划、已实现的写工具和审批流程
- 范围：收敛 AgentLoop 职责，增加可复用的 `CodingAgent` 公共入口

## 1. 背景与问题

当前 `AgentLoop` 同时负责以下职责：

- 创建并推进 `ConversationContext`
- 调用 LLM，并把 provider 异常转换为 AgentError
- 遍历模型返回的工具调用
- 执行工具 Schema 校验、工具限流和工具分发
- 计算模型与工具耗时
- 生成 trace/report 事件及结束状态

CLI 还需要手工创建 `Workspace`、四个内置工具、`ToolRegistry`、审批门、`LLMClient`、`AgentLoop` 和 `AgentService`。这使启动路径很长，调用方必须知道过多内部组件；同时 `AgentService` 当前只是对 `AgentLoop.run()` 的透传，无法形成稳定的公共 API。

本次优化的目标不是重写 Agent 的决策逻辑，而是把装配和横切职责放到明确边界中，使主循环可以直接表达“请求模型、处理工具、继续或结束”。

## 2. 产品目标

### 2.1 必须实现

- 提供 `CodingAgent` 作为面向 CLI、未来 HTTP/IDE 客户端的轻量公共入口。
- 提供 `CodingAgent.from_settings(...)` 工厂方法，集中装配 `Workspace`、工具注册表、审批门、LLM 客户端、上下文构造器和 `AgentLoop`。
- 提供 `CodingAgent.ask(query, ...)` 作为一次任务的调用入口。
- `CodingAgent` 实例可以复用；每次 `ask()` 都创建全新的 `ConversationContext`、运行状态和工具调用计数。
- 将 LLM 异常映射、工具执行/限流和事件记录从 `AgentLoop.run()` 的主流程中收敛到可独立测试的组件或适配器。
- 保持现有读工具、写工具、JSON Schema 校验、逐调用审批、工作区边界检查、原子写入和 trace/report 语义兼容。
- CLI 只负责参数解析、配置读取、一次运行的输出和错误码，不再手工编排全部底层组件。

### 2.2 预期收益

- 新调用方只需理解 `CodingAgent` 和 `ask()`，不必了解内部装配细节。
- `AgentLoop` 的核心循环更短、更容易阅读和测试。
- 记录逻辑可替换为禁用记录、内存记录或文件记录，而不改变 Agent 决策逻辑。
- provider、工具执行器和 CLI 的错误边界清晰，异常类型不在模块间扩散。

## 3. 非目标与范围边界

本次不实现以下能力：

- 不改变模型供应商协议，不引入新的 Agent 编排框架。
- 不增加多轮会话持久化、断点恢复、后台任务或并发执行。
- 不改变工具的业务校验、审批提示、文件写入规则和安全策略。
- 不改变现有 `trace.json` 与 `report.json` 的事件名称和字段兼容性；双文件原子一致性可单独作为后续议题处理。
- 不在本次重构中引入完整依赖注入容器、事件总线或插件系统。
- 不要求立即删除旧的 `AgentLoop`、`AgentService` 公共符号；迁移期保留兼容入口。

## 4. 目标架构

```text
CLI / HTTP / IDE
       |
       v
CodingAgent.ask(query, run_options)
       |
       v
AgentLoop.run(query, workspace, event_sink)
       |
       +--> ConversationContext
       +--> ModelGateway ------> LLMClient
       +--> ToolExecutor ------> ToolRegistry --> read/write tools
       +--> EventSink ----------> RunRecorder / NullEventSink
```

### 4.1 CodingAgent

`CodingAgent` 是外部调用边界，持有可复用的静态依赖：

- `Workspace`
- `AgentLoop` 或其所需的模型、工具和上下文依赖
- 默认的运行限制和上下文构造器

它不持有某一次运行的 `ConversationContext`、工具调用计数或 `RunRecorder`。

建议接口：

```python
class CodingAgent:
    @classmethod
    def from_settings(
        cls,
        repo: Path,
        settings: Settings,
        *,
        approval_ask: Callable[[WritePreview], bool] | None = None,
        limits: AgentLimits | None = None,
        repository_context_builder: Callable[[Workspace], str] = build_repository_context,
    ) -> "CodingAgent": ...

    def ask(
        self,
        query: str,
        *,
        recorder: RunRecorder | None = None,
    ) -> str: ...
```

`from_settings()` 负责解析仓库、创建 `Workspace`、注册内置工具、创建审批门、创建 `LLMClient`，并把依赖装配到 `AgentLoop`。`RunRecorder` 不在工厂中创建，因为它属于一次具体的 query。

`ask()` 接收本次运行的 recorder；如果调用方不传 recorder，则使用空事件接收器。是否自动创建文件 recorder 由 CLI 或上层应用决定，避免库 API 隐式写磁盘。

### 4.2 AgentLoop

`AgentLoop` 只负责：

- 为本次运行创建 `ConversationContext`
- 按轮次请求模型
- 把模型的最终回答或工具调用推进到下一状态
- 在达到最终回答或轮次上限时结束

它通过窄接口依赖 `ModelGateway`、`ToolExecutor` 和 `EventSink`，不直接拼装 provider 级异常分类和详细报告 payload。

兼容期可保留现有构造参数和 `run(query, workspace)` 形态，但新实现应支持通过依赖注入传入适配器。旧调用方的行为、返回值和异常类型必须保持不变。

### 4.3 ModelGateway

`ModelGateway` 封装一次模型请求及其稳定错误映射：

> **职责边界说明：** `ResponseParser` 负责把 OpenAI 或兼容 Provider 的原始响应解析为统一的 `ParsedResponse`/`AssistantTurn`；`ModelGateway` 不重复解析响应，而是负责调用 `LLMClient`，统一 provider 异常、超时和错误消息。这样 `AgentLoop` 只需要处理统一的 turn 或稳定的模型错误。

```python
class ModelGateway:
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn: ...
```

它把 `LLMTimeoutError`、`InvalidToolArguments`、`LLMResponseError` 和未知 provider 异常映射为带稳定 `error_type` 的 Agent 层错误。底层异常作为 cause 保留，用户可见消息不得泄漏 API Key、完整 provider 响应或敏感请求内容。

### 4.4 ToolExecutor

`ToolExecutor` 是唯一的工具执行边界，负责：

- 检查工具是否注册
- 调用 `ToolRegistry.validate()` 完成 Schema 校验
- 按工具名称维护本次 run 的调用计数
- 处理调用上限
- 调用 `ToolRegistry.execute()`
- 将未知工具、无效参数和执行异常转换为稳定结构化结果

建议接口：

```python
class ToolExecutor:
    def execute(self, call: ToolCall) -> ToolExecution: ...

@dataclass(frozen=True)
class ToolExecution:
    result: dict[str, Any]
    duration_ms: float
```

计数器必须属于一次 `run`，不能让可复用的 `CodingAgent` 在不同 query 之间共享计数。写工具的审批仍由已存在的 `WriteApprovalGate` 负责，`ToolExecutor` 不重复实现审批和文件安全逻辑。

### 4.5 EventSink

`EventSink` 为 AgentLoop 提供最小事件接口：

```python
class EventSink(Protocol):
    def emit(self, event_type: str, **payload: object) -> None: ...
    def complete(self, answer: str) -> None: ...
    def fail(self, error_type: str, message: str, *, duration_ms: float | None = None) -> None: ...
```

`RunRecorder` 通过适配器实现该接口，`NullEventSink` 提供无记录运行。trace/report 的摘要与详细 payload 规则仍由 recorder 侧维护；AgentLoop 只发出语义事件，不再在每个分支判断 `if self.recorder`。

## 5. 一次 ask 的生命周期

```text
CodingAgent.from_settings()
  -> 静态依赖装配完成

agent.ask(query, recorder)
  -> 创建本次 ConversationContext
  -> 创建本次 ToolExecutor / 清空调用计数
  -> 循环请求 ModelGateway
  -> 无工具调用：EventSink.complete(answer)，返回
  -> 有工具调用：ToolExecutor.execute()，追加 tool result，继续
  -> 达到轮次上限：EventSink.fail("round_limit", ...)，返回兼容提示
```

每次 `ask()` 都必须满足：

- 不携带上一次 query 的消息历史。
- 不携带上一次 query 的工具调用计数。
- 不复用上一次 query 的 recorder 状态。
- 同一 `CodingAgent` 实例可以顺序执行多个 query。

## 6. 错误处理契约

- 参数、仓库和配置错误在 `CodingAgent.from_settings()` 或 CLI 装配阶段失败。
- 模型请求错误由 `ModelGateway` 归一化，`AgentLoop` 只负责记录并抛出 `AgentError`。
- 工具错误不抛到模型循环外，而是转换为 `role: tool` 的结构化结果，让模型可以修正或结束。
- 已存在的错误类型保持不变：`timeout`、`invalid_tool_arguments`、`invalid_response`、`provider_error`、`unknown_tool`、`tool_error`、`tool_call_limit` 及各工具定义的稳定错误类型。
- recorder 写入失败仍然应显式抛出 `TraceWriteError`，不能静默吞掉；是否让一次 Agent 运行失败由现有 recorder 语义和 CLI 策略决定。
- 任意错误路径最多完成一次 `complete()` 或 `fail()`，避免重复终态事件。

## 7. CLI 迁移要求

CLI 保留以下职责：

- 解析 query、repo、model、base URL、timeout 和工具调用限制。
- 调用 `Settings.from_args_and_env()`。
- 创建本次运行的 `RunRecorder`，决定是否启用交互审批。
- 调用 `CodingAgent.from_settings()` 和 `agent.ask()`。
- 输出最终回答、run ID、trace/report 路径和退出码。

CLI 不再直接注册每一个工具，也不直接构造 `AgentLoop`。为了保持测试和外部调用兼容，可以保留 `AgentService` 一段时间，但其实现应委托给 `CodingAgent` 或标记为迁移兼容层。

## 8. 可观测性要求

- 现有事件顺序保持：`run_started`、模型请求/响应、工具调用/审批/结果、`final_answer` 或 `run_failed`。
- AgentLoop 不直接依赖具体文件格式；`RunRecorder` 继续负责 trace/report 双层 payload 和脱敏。
- 无 recorder 时不写磁盘，也不改变模型请求和工具结果。
- 工具调用计数、模型/工具耗时必须属于当前 run。

## 9. 验收标准

- CLI 的正常查询、模型异常、工具异常、写入批准/拒绝和非交互行为回归测试全部通过。
- 可以通过 `CodingAgent.from_settings()` 创建 agent，并使用 `ask()` 完成读工具和写工具任务。
- 顺序调用同一个 agent 两次时，第二次不会看到第一次的消息、工具计数或终态事件。
- 无 recorder 调用不会产生 trace/report 文件。
- recorder 调用产生与现有 schema 兼容的 trace/report，事件序列和摘要/详细字段保持兼容。
- `AgentLoop` 主循环不再包含重复的 recorder 条件、provider 异常分类和工具执行细节。
- 旧的 `AgentLoop` 直接调用测试和 `AgentService` 兼容测试继续通过。

## 10. 风险与决策记录

- **风险：** 将 recorder 自动创建放入 `CodingAgent` 会让库 API 隐式写磁盘。**决策：** 工厂只装配静态依赖，recorder 由 `ask()` 调用方传入或由上层显式创建。
- **风险：** 将审批逻辑搬进通用 ToolExecutor 会造成写工具职责重复。**决策：** 保留 `WriteApprovalGate` 在写工具内部，ToolExecutor 只负责通用执行边界。
- **风险：** 一次性删除 `AgentService` 会影响现有测试和调用方。**决策：** 迁移期保留兼容层，待新入口稳定后再单独移除。
- **风险：** 追求完整事件总线会扩大 MVP 范围。**决策：** 先采用 `EventSink` 窄接口，暂不引入发布订阅系统。

