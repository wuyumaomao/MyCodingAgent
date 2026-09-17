# 记忆检索与工具结果压缩实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将长期文件摘要检索与临时工具结果压缩分离，并引入配额和可注入 LLM 摘要降级路径。

**Architecture:** `MemoryManager` 管理 working/file/episodic 三个分区，新增文件摘要检索和结构化摘要 provider 接口。`ConversationContext` 仅注入相关文件摘要，`_reduce_history` 负责 tool result 的本地配额压缩，并可调用独立 provider。原始 session history 和 report 不变。

**Tech Stack:** Python 3.11、标准库 JSON/正则、现有 session/memory/context、pytest。

**Spec:** `docs/superpowers/specs/2026-09-16-memory-retrieval-and-compression-prd.md`

## Global Constraints

- `file_summaries` 与 `episodic_notes` 保持独立字段和独立更新语义。
- 完整工具结果继续保存在 session history/report；只压缩发送给模型的 transcript。
- 摘要 provider 使用已配置的 OpenAI-compatible `LLMClient` 发起独立无工具请求，不能调用工具或修改工作区；失败时使用本地降级。
- 必须保持 assistant tool call 与 role=tool 结果配对。
- 不使用 embedding、向量数据库或额外持久化服务。
- 不执行 Git commit，除非用户另行明确要求。

### Task 1: 定义记忆结构与文件摘要检索

**Files:**
- Modify: `src/coding_agent/session.py`
- Modify: `src/coding_agent/memory.py`
- Test: `tests/test_memory.py`

- [x] 写测试：默认 working memory 使用 `recent_modified_files`；`retrieve_file_summaries(query)` 只返回相关摘要并按上限截断。
- [x] 运行 `uv run pytest tests/test_memory.py -q`，确认新测试失败。
- [x] 实现字段兼容、关键词评分和最多 5 条摘要召回。
- [x] 运行专项测试确认通过。

### Task 2: 让 ConversationContext 只注入相关文件摘要

**Files:**
- Modify: `src/coding_agent/context.py`
- Test: `tests/test_context.py`

- [x] 写测试：无关摘要不出现在 `[Relevant File Summaries]`，匹配 query 的摘要出现。
- [x] 运行专项测试确认失败。
- [x] 将 `[Memory]` 保持为工作记忆，将文件摘要移到独立的 `[Relevant File Summaries]` 区块。
- [x] 运行 context 测试确认通过。

### Task 3: 为 tool result 增加单项/总 transcript 配额

**Files:**
- Modify: `src/coding_agent/context.py`
- Test: `tests/test_context.py`

- [x] 写测试：超出单结果配额时本地压缩；超出总预算时保留 tool-call/result 配对并丢弃最旧 group。
- [x] 运行专项测试确认失败。
- [x] 增加 `tool_result_budget_chars` 参数，并在 `_compact_result` 后再次截断到安全元数据。
- [x] 运行专项测试确认通过。

### Task 4: 接入可注入 LLM 摘要 provider 与降级

**Files:**
- Modify: `src/coding_agent/context.py`
- Modify: `src/coding_agent/memory.py`
- Test: `tests/test_memory.py`, `tests/test_context.py`

- [x] 写测试：超大 file summary/tool result 调用 provider；provider 返回非法数据或抛错时使用本地摘要。
- [x] 运行专项测试确认失败。
- [x] 定义无工具 provider callable，校验其 JSON/evidence 形状，失败回退本地规则。
- [x] 运行专项测试确认通过。

### Task 4a: 将 FileSummary provider 连接到同一模型配置

**Files:**
- Modify: `src/coding_agent/llm.py`
- Modify: `src/coding_agent/agent.py`
- Modify: `src/coding_agent/coding_agent.py`
- Modify: `src/coding_agent/memory.py`
- Test: `tests/test_llm.py`, `tests/test_agent.py`, `tests/test_memory.py`

- [x] 写测试：无工具摘要请求使用同一模型配置并解析严格 JSON；非法响应回退本地摘要。
- [x] 运行专项测试确认失败。
- [x] 增加 `LLMClient.complete_text()` 和 FileSummary JSON provider；在完整 `readfile` 成功后传入磁盘原始内容。
- [x] 运行专项测试确认通过。

### Task 5: 文档与全量验证

**Files:**
- Modify: `学习文档.md`

- [x] 记录 file summary 与 transcript compression 的边界、配额和检索流程。
- [x] 运行 `uv run pytest -q`。
- [x] 运行 `uv run python -m compileall -q src` 和 `git diff --check`。
