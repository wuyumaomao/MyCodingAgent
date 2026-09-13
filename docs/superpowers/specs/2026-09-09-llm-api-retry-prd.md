# LLM API 层重试与响应元数据 PRD

## 文档状态

- 状态：已确认，已实现
- 版本：v0.1
- 日期：2026-09-09
- 范围：同一 AgentLoop 轮次内的模型请求重试、`finish_reason` 严格记录

## 1. 背景与问题

当前 Agent 已经能够处理工具执行错误：工具返回结构化的 `role: tool` 结果，模型在下一轮决定是否修正参数或再次调用工具。但模型 API 层还没有由项目自身控制的重试机制：

- `invalid_response` 在第一次出现时就会让当前 run 失败；
- `finish_reason` 缺失时，解析器可能静默推断为 `stop`；
- OpenAI SDK 可能默认进行内部重试，项目 trace 看不到实际请求尝试次数；
- `llm_response` 没有记录 `finish_reason` 及其来源，无法判断它是 Provider 返回还是解析器推断。

这会把临时网络错误、响应元数据缺失和模型输出不符合协议混在一起，也会让调试记录不完整。

## 2. 产品目标

### 2.1 必须实现

- 在同一 AgentLoop 轮次内，对可重试的 LLM 请求失败最多重试 1 次。
- 重试发送完全相同的 `messages` 和 `tools`，不重置 `ConversationContext`、AgentLoop 轮次或工具调用计数。
- 第二次请求成功后，继续原轮次的工具调用或最终回答流程。
- 重试次数耗尽后，才将稳定的 `ModelGatewayError` 交给 AgentLoop，使 run 失败。
- 严格区分工具错误恢复和 API 层重试：工具错误仍作为 `role: tool` 回传模型，不进入 API retry。
- 显式关闭 OpenAI SDK 内置重试，由项目统一控制 API 尝试次数。
- 在 `llm_response` 中记录规范化的 `finish_reason`、来源和实际 API 尝试次数。
- Provider 返回 `finish_reason` 时记录来源 `provider`；工具调用存在但字段缺失时可以推断 `tool_calls`，来源记为 `fallback`。
- 无工具调用且 `finish_reason` 缺失时，判定为 `invalid_response`，而不是静默当作正常结束。

### 2.2 不在本次范围内

- 不把 `invalid_response` 包装成 `role: tool` 消息。
- 不自动重试工具本身；工具调用次数限制和模型驱动的工具恢复保持现状。
- 不从磁盘恢复中断的 Agent run，不增加 checkpoint、resume 或 durable memory。
- 不改变最终回答阶段允许 `content + 无 tool_calls + finish_reason=stop` 的行为。
- 不通过 prompt 伪造或要求模型自行输出 `finish_reason`；该字段属于 Provider `choice` 元数据。

## 3. 术语与边界

```text
API attempt：同一轮 messages/tools 的一次模型请求
Agent round：工具结果写入上下文后，下一次模型决策
Tool retry/recovery：工具错误回传模型后，模型再次调用工具
```

示例：

```text
round 4 / API attempt 1 -> invalid_response
round 4 / API attempt 2 -> finish_reason=tool_calls
                            -> 执行工具
round 5                 -> 使用工具结果继续决策
```

API retry 不会把轮次重置为 0，也不会新增一条用户消息。

## 4. 响应协议规则

OpenAI Chat Completions 的 `finish_reason` 位于 `choice`，`tool_calls` 位于 `choice.message`。解析器按以下规则处理：

| Provider 响应 | 解析结果 |
| --- | --- |
| `finish_reason=stop` 且无 tool calls | 正常最终回答，来源 `provider` |
| `finish_reason=tool_calls` 且有 tool calls | 继续执行工具，来源 `provider` |
| `finish_reason=stop` 且有 tool calls | `invalid_response` |
| 缺少 `finish_reason` 且有 tool calls | 兼容推断 `tool_calls`，来源 `fallback` |
| 缺少 `finish_reason` 且无 tool calls | `invalid_response`，进入 API retry |
| `finish_reason=length/content_filter` | `invalid_response`，按不可恢复响应处理 |
| 未知 `finish_reason` | `invalid_response`，进入 API retry |

成功解析的结果应保存：

```json
{
  "finish_reason": "stop",
  "finish_reason_source": "provider",
  "tool_call_count": 0,
  "api_attempts": 1
}
```

若字段缺失但根据 tool calls 推断：

```json
{
  "finish_reason": "tool_calls",
  "finish_reason_source": "fallback",
  "tool_call_count": 1,
  "api_attempts": 1
}
```

## 5. 重试策略

### 5.1 重试位置

`ModelGateway` 负责一次 AgentLoop 轮次内的有限重试。`LLMClient` 负责一次 Provider 请求和响应解析，不保存跨轮次上下文。

```text
AgentLoop
  -> ModelGateway.complete(messages, tools, on_retry)
       -> LLMClient.complete(messages, tools)
       -> ResponseParser.parse(response)
```

### 5.2 默认策略

- `max_llm_retries=1`，即最多 2 次 API attempt。
- 重试使用同一份 `messages` 和 `tools` 快照。
- 默认重试 `LLMTimeoutError`、`LLMResponseError` 和 `InvalidToolArguments`。
- 第一次失败产生 `llm_retry` 事件，包含尝试序号、稳定错误类型和耗时；不把原始异常或 API Key 写入报告。
- 第二次仍失败时产生一次最终 `run_failed`，错误类型为稳定的 `timeout`、`invalid_response` 或 `invalid_tool_arguments`。
- SDK 创建时设置 `max_retries=0`，避免 SDK 内部重试与项目重试叠加。

### 5.3 不重试的情况

- 工具执行失败、审批拒绝、工作区越界和 `tool_call_limit`：这些是工具层结果，直接回传模型。
- `finish_reason=length` 或 `content_filter`：属于模型响应已明确结束但不可用，不做无意义的同请求重试。

`LLMResponseError` 需要携带仅供网关使用的 `retryable: bool` 标记。缺失 `finish_reason`、未知结束原因、无效工具 JSON 和工具调用元数据不一致默认可重试；`length` 与 `content_filter` 创建为 `retryable=False`，网关立即转换为最终 `invalid_response`。

## 6. 可观测性

`trace.json` 保留简洁摘要：

```json
{
  "type": "llm_retry",
  "round": 4,
  "attempt": 1,
  "error_type": "invalid_response",
  "duration_ms": 120.3
}
```

`report.json` 的 `llm_response` 增加：

- `finish_reason`
- `finish_reason_source`
- `api_attempts`
- 每轮最终响应的 `content` 和 `tool_calls`

单次 API 重试不新增 Agent round；`llm_request.round` 仍保持原轮次。

## 7. 验收标准

- 正常 `stop` 响应记录 `finish_reason_source=provider`。
- 缺少 `finish_reason` 且无工具调用时，第一次解析失败并触发一次 API retry。
- retry 成功返回 tool call 时，仍在原 Agent round 执行工具。
- 两次响应都无效时，run 失败且只产生一个最终失败事件。
- `messages` 和 `tools` 在两次 attempt 中完全一致。
- OpenAI 客户端配置 `max_retries=0`。
- 工具返回 `not_a_directory`、Schema 错误和 Shell 超时仍走 `role: tool`，不触发 API retry。
- trace/report 不泄露 API Key、Authorization、Token 或原始敏感异常内容。
- 现有测试及新增重试测试全部通过。
