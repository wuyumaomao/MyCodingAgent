# LLM API 层重试实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 在同一 AgentLoop 轮次内为 LLM 请求增加最多一次项目级重试，严格记录 `finish_reason` 来源，并避免 OpenAI SDK 内部重试叠加。

**架构：** `LLMClient` 保持“一次 Provider 请求 + 一次响应解析”；`ModelGateway` 接收同一组 `messages/tools` 并负责有限 API retry；`AgentLoop` 保持 round 和上下文不变，只记录 retry/response 事件。`ResponseParser` 负责区分 Provider 元数据和兼容推断。

**技术栈：** Python 3.11+、OpenAI Python SDK、dataclasses、现有 `ModelGateway`/`ResponseParser`/`EventSink`/`RunRecorder`/pytest。

**Spec：** `docs/superpowers/specs/2026-09-09-llm-api-retry-prd.md`

## 全局约束

- 默认 `max_llm_retries=1`，最多两次 API attempt。
- API retry 使用完全相同的 `messages` 和 `tools`，不重置 AgentLoop round。
- SDK 初始化显式使用 `max_retries=0`。
- 工具错误仍作为 `role: tool` 回传模型，不进入 API retry。
- 缺少 `finish_reason` 且无 tool calls 必须视为 `invalid_response`。
- 不记录 API Key、Authorization、Token 或完整原始异常。
- 不执行 Git commit，除非用户另外明确要求。

---

### 任务 1：扩展 ParsedResponse 和严格 finish_reason 解析

**文件：**
- 修改：`src/coding_agent/response_parser.py`
- 测试：`tests/test_llm.py`

**接口：**
- `ParsedResponse.finish_reason: str`
- 新增 `ParsedResponse.finish_reason_source: Literal["provider", "fallback"]`
- `LLMResponseError(message, *, retryable: bool = True)` 保存解析失败是否允许项目级 API retry。
- `ResponseParser.parse()` 继续返回 `ParsedResponse`。

- [x] **步骤 1：写失败测试**

增加三个测试：

```python
def test_parser_records_provider_finish_reason():
    response = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop",
        message=SimpleNamespace(content="done", tool_calls=[]),
    )])
    parsed = ResponseParser().parse(response)
    assert parsed.finish_reason == "stop"
    assert parsed.finish_reason_source == "provider"


def test_parser_rejects_missing_finish_reason_for_final_response():
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="done", tool_calls=[]),
    )])
    with pytest.raises(LLMResponseError):
        ResponseParser().parse(response)


def test_parser_marks_missing_finish_reason_with_tool_calls_as_fallback():
    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="readfile", arguments='{"path":"README.md"}'),
    )
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=None, tool_calls=[call]),
    )])
    parsed = ResponseParser().parse(response)
    assert parsed.finish_reason == "tool_calls"
    assert parsed.finish_reason_source == "fallback"
```

- [x] **步骤 2：运行失败测试**

运行：`uv run pytest tests/test_llm.py -q`

预期：新增测试因没有 `finish_reason_source` 且缺失字段仍被 fallback 为 `stop` 而失败。

- [x] **步骤 3：实现最小解析变更**

在 `ResponseParser.parse()` 中保存 `raw_finish_reason = getattr(choice, "finish_reason", None)`：

```python
raw_finish_reason = getattr(choice, "finish_reason", None)
if raw_finish_reason is None:
    if tool_calls:
        finish_reason = "tool_calls"
        finish_reason_source = "fallback"
    else:
        raise LLMResponseError("finish_reason is required when no tool calls are returned")
else:
    finish_reason = str(raw_finish_reason)
    finish_reason_source = "provider"
```

保留现有 `stop + tool_calls`、`tool_calls + no tool_calls`、`length`、`content_filter` 和未知值检查。将 `length`、`content_filter` 的错误构造为 `LLMResponseError("model response is not retryable", retryable=False)`；其他解析错误保留默认 `retryable=True`。

- [x] **步骤 4：运行测试**

运行：`uv run pytest tests/test_llm.py -q`

预期：全部通过。

### 任务 2：关闭 SDK 默认重试并增加 ModelGateway retry

**文件：**
- 修改：`src/coding_agent/llm.py`
- 修改：`src/coding_agent/model_gateway.py`
- 测试：`tests/test_llm.py`
- 测试：`tests/test_model_gateway.py`

**接口：**
- `ModelGateway(llm_client, max_retries: int = 1)`
- `ModelGateway.complete(messages, tools, on_retry: Callable[[dict[str, object]], None] | None = None) -> AssistantTurn`

- [x] **步骤 1：写失败测试**

增加以下行为测试：

```python
def test_gateway_retries_invalid_response_once():
    class Client:
        def __init__(self):
            self.calls = []
        def complete(self, messages, tools):
            self.calls.append((messages, tools))
            if len(self.calls) == 1:
                raise LLMResponseError("missing finish_reason")
            return AssistantTurn("done", [])

    retries = []
    client = Client()
    result = ModelGateway(client, max_retries=1).complete(
        [{"role": "user", "content": "x"}], [], on_retry=retries.append
    )
    assert result.content == "done"
    assert len(client.calls) == 2
    assert client.calls[0] == client.calls[1]
    assert retries[0]["attempt"] == 1
    assert retries[0]["error_type"] == "invalid_response"


def test_gateway_stops_after_retry_budget():
    class Client:
        def complete(self, messages, tools):
            raise LLMResponseError("bad response")

    with pytest.raises(ModelGatewayError) as raised:
        ModelGateway(Client(), max_retries=1).complete([], [])
    assert raised.value.error_type == "invalid_response"
```

同时扩展 `tests/test_llm.py`，注入 fake `OpenAI` 构造器，断言 `max_retries == 0` 被传入；不发真实网络请求。

- [x] **步骤 2：运行失败测试**

运行：`uv run pytest tests/test_model_gateway.py tests/test_llm.py -q`

预期：retry 测试失败，因为当前 gateway 只调用一次，且 SDK 构造参数没有 `max_retries`。

- [x] **步骤 3：实现最小 retry**

在 `LLMClient.__init__` 的 OpenAI 参数中加入 `max_retries=0`。

在 `ModelGateway.complete()` 中使用 `for attempt in range(max_retries + 1)`，并在每个 attempt 开始前保存 `attempt_started = time.perf_counter()`：

```python
for attempt in range(self.max_retries + 1):
    try:
        return self.llm_client.complete(messages, tools)
    except (LLMTimeoutError, InvalidToolArguments, LLMResponseError) as exc:
        can_retry = not isinstance(exc, LLMResponseError) or exc.retryable
        if attempt >= self.max_retries or not can_retry:
            raise self._normalize(exc) from exc
        if on_retry is not None:
            on_retry({
                "attempt": attempt + 1,
                "error_type": self._error_type(exc),
                "duration_ms": _duration_ms(attempt_started),
            })
raise AssertionError("unreachable")
```

`messages` 和 `tools` 只读传递，不在 gateway 修改。将现有异常映射提取为私有 `_normalize()`，保持稳定错误类型和 cause。

网关需要实现以下私有辅助函数，避免异常分类分散：`_normalize(exc: Exception) -> ModelGatewayError`、`_error_type(exc: Exception) -> str` 和 `_duration_ms(started: float) -> float`。`_normalize()` 沿用当前 timeout、invalid tool arguments、invalid response 和 provider error 的稳定消息；`_error_type()` 只返回这些稳定类型；`_duration_ms()` 使用 `time.perf_counter()` 计算毫秒数。

- [x] **步骤 4：运行专项测试**

运行：`uv run pytest tests/test_model_gateway.py tests/test_llm.py -q`

预期：全部通过。

### 任务 3：接入 AgentLoop 事件和响应记录

**文件：**
- 修改：`src/coding_agent/agent.py`
- 测试：`tests/test_agent.py`
- 测试：`tests/test_trace_integration.py`

**接口：**
- AgentLoop 通过 `on_retry` 将 retry 事件发给当前 `EventSink`。

- [x] **步骤 1：写失败测试**

增加 Fake LLM 测试：第一次抛出 `LLMResponseError`，第二次返回 `AssistantTurn("done", [])`；断言：

- `AgentLoop.run()` 返回 `done`；
- `llm_retry` 事件只有一条；
- `llm_response` 记录 `finish_reason`、`finish_reason_source` 和 `api_attempts=2`；
- `round` 仍为 1；
- 失败两次时只产生一个最终 `run_failed`。

- [x] **步骤 2：运行失败测试**

运行：`uv run pytest tests/test_agent.py tests/test_trace_integration.py -q`

预期：失败，因为 AgentLoop 没有传入 retry callback，也没有记录这些字段。

- [x] **步骤 3：实现 AgentLoop 接入**

在每轮调用 gateway 时传入 callback。callback 递增当前轮的 `retry_count`，并在 retry 发生时立即记录事件：

```python
retry_count = 0
def record_retry(payload: dict[str, object]) -> None:
    nonlocal retry_count
    retry_count += 1
    sink.emit("llm_retry", round=round_number, **payload)

turn = self.model_gateway.complete(
    messages,
    tool_definitions,
    on_retry=record_retry,
)
```

在 `llm_response` 中记录 `finish_reason=getattr(turn, "finish_reason", None)`、`finish_reason_source=getattr(turn, "finish_reason_source", None)` 和 `api_attempts=retry_count + 1`。`llm_response.duration_ms` 保持为从 `request_started` 到最终成功响应的总耗时；工具调用和最终回答分支保持原有逻辑。

- [x] **步骤 4：运行专项测试**

运行：`uv run pytest tests/test_agent.py tests/test_trace_integration.py -q`

预期：全部通过。

### 任务 4：文档与完整验证

**文件：**
- 修改：`README.md`
- 修改：`学习文档.md`
- 修改：`docs/superpowers/specs/2026-09-09-llm-api-retry-prd.md`（仅修正文档一致性）

- [x] **步骤 1：更新 README 和学习文档**

说明 API retry 与工具错误恢复的区别、同一 round 重试、SDK `max_retries=0` 和 trace 中的 `llm_retry`/`finish_reason_source` 字段。

- [x] **步骤 2：运行完整验证**

```powershell
uv run pytest -q
uv run python -m compileall -q src
git diff --check
```

预期：全部测试通过，源码编译成功，差异检查无错误。

- [x] **步骤 3：检查范围**

确认没有把工具错误改成 API retry，没有重置 AgentLoop round，没有增加持久化 checkpoint，也没有让 prompt 负责生成 `finish_reason`。
