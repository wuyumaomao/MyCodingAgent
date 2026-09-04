# Task 2: ModelGateway

## Changes

- Added `src/coding_agent/model_gateway.py` with `ModelGateway.complete(messages, tools)` and `ModelGatewayError`.
- Normalized timeout, invalid tool arguments, invalid responses, and unknown provider errors to stable error types and safe public messages while preserving the original exception as `__cause__`.
- Updated `AgentLoop` to call `ModelGateway` while preserving direct `llm_client` construction, recorder failure events, duration fields, and `AgentError` behavior.
- Added gateway success and error mapping tests in `tests/test_model_gateway.py`.

## Verification

RED (before implementation):

```text
uv run pytest tests/test_model_gateway.py -q
ERROR ... ModuleNotFoundError: No module named 'coding_agent.model_gateway'
```

GREEN:

```text
uv run pytest tests/test_model_gateway.py tests/test_agent.py tests/test_trace_integration.py -q
...........................                                              [100%]
27 passed in 3.36s
```

`git diff --check` completed without whitespace errors.
