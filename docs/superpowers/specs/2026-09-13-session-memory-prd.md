# 会话记忆与上下文裁剪 PRD

## 文档状态

- 状态：已确认，待实施
- 版本：v0.1
- 日期：2026-09-13
- 范围：session 级持久化、prompt memory、历史裁剪与按需读取文件片段

## 1. 背景与问题

当前 Coding Agent 的每次 CLI 调用都会创建新的 `ConversationContext`。在单次 run 内，每个 assistant tool call 和完整 tool result 都会追加到 `history`，并在后续每轮重新发给模型。虽然 report 保留了有用的证据，但 prompt 会从一个简短请求膨胀到数万字符，已经在真实仓库探测中造成响应变慢与 API 超时。

Agent 需要保留后续有价值的证据，但不能把完整过程记录直接当作每轮 prompt；对于相关的多次 CLI 调用，也需要延续精选知识，同时避免把不相关任务混入同一上下文。

## 2. 产品目标

### 2.1 必须实现

- 引入通过 `--session <session_id>` 显式选择的 session。
- 未传 `--session` 时创建新 session，并在 run 开始后输出其 ID。
- 在目标仓库 `.coding-agent/sessions/<session_id>.json` 中原子持久化 session 状态。
- session 保存完整会话 history 与 memory；prompt 裁剪绝不删除已持久化 history。
- 在静态 system/repository context 后维持两个 prompt 区块：
  - `[Memory]`：当前任务、显式约束、最近文件与有效文件摘要。
  - `[Relevant Memory]`：最多三条被召回的 episodic notes。
- 根据当前 query、当前任务摘要和最新工具错误，确定性地召回 episodic notes；先按关键词重合度排序，再按新近程度排序。
- 仅为成功的 `readfile` 结果维护 `file_summaries`。每条摘要保存确定性的短摘要、行数和 SHA-256 freshness 哈希。
- `write_file` 或 `patch_file` 成功后，记录该文件为最近文件，且使其旧摘要与绑定旧文件哈希的 notes 失效。
- `readfile` 增加一开始、含终点的可选 `start` / `end` 行范围读取；旧全文被裁剪后，模型可以重新读取所需片段。
- 在固定字符预算内构造模型 transcript。保留当前用户请求、最近完整工具调用/结果组、memory 区块，并只保留较旧结果的短形式。
- Prompt 中必须保持 tool-call/result 配对。不得发送缺少其前置 assistant `tool_calls` 的 `role: tool` 消息。
- 保持现有 `trace.json` / `report.json` 行为。即使 prompt 中采用压缩表示，report 仍保存原始完整工具输出。
- 在 trace/report 中记录 session ID 和 prompt-memory 指标，以便诊断，但不向简洁 trace 写入私密正文。

### 2.2 不在本次范围内

- 不实现跨 session 的 durable-memory 目录或提升流程。
- 不实现 `--resume latest`、checkpoint，或中断工具操作的恢复。
- 不引入向量数据库、embedding、LLM 生成摘要，或仅为记忆检索额外调用模型。
- 不增加批量读取工具或重复的 patch 工具。
- 不改变写入和 shell 的审批行为。
- 未传 `--session` 时不自动复用旧 session。

## 3. Session 模型

### 3.1 标识与存储位置

一个 session 只属于一个规范化后的仓库根目录。session ID 使用与 run 相同的“时间戳 + 随机后缀”风格生成。CLI 在加载 session 时，必须比较其保存的 `repo_root` 与 `--repo` 规范化后的路径；不同则拒绝。

```text
目标仓库/
  .coding-agent/
    sessions/
      20260913T120000Z-a1b2c3.json

Agent 项目/
  .coding-agent/
    runs/
      20260913T120010Z-d4e5f6/
        trace.json
        report.json
```

run 记录仍归 Agent 项目管理；session 状态保存在它所描述的目标仓库旁。

### 3.2 持久化数据形状

session 文档具有版本号，且仅保存 JSON 安全的数据：

```json
{
  "schema_version": 1,
  "session_id": "20260913T120000Z-a1b2c3",
  "repo_root": "F:/AgentLabs/httpstat",
  "created_at": "2026-09-13T12:00:00Z",
  "updated_at": "2026-09-13T12:05:00Z",
  "history": [],
  "memory": {
    "working_memory": {
      "task_summary": "",
      "constraints": [],
      "recent_files": [],
      "latest_tool_error": null
    },
    "file_summaries": {},
    "episodic_notes": []
  }
}
```

`history` 按顺序保存完整的 OpenAI-compatible 消息；它是 session history，不等同于下一轮直接发送给模型的 prompt。

## 4. Memory 模型

### 4.1 Memory 区块

`[Memory]` 是确定性、受限长度的状态：

- `task_summary`：当前用户 query 的受长度限制的规范化摘要；每个新 run 都更新它。
- `constraints`：从当前 query 确定性提取的负向或安全约束，例如“不要修改已有源码”。
- `recent_files`：最近读取或成功修改的仓库相对路径，最多 10 个去重路径。
- `file_summaries`：最近相关且仍有效的文件摘要，同时受数量和区块字符预算限制。

每个 `file_summaries[path]` 条目：

```json
{
  "summary": "从读取行中确定性提取的短摘要。",
  "line_count": 563,
  "freshness": "sha256 hex digest",
  "source_range": {"start": 1, "end": 563},
  "updated_at": "2026-09-13T12:00:00Z"
}
```

仅当读取覆盖完整文件时，partial read 才可更新文件摘要；局部读取仍会留在最近 history 中，但不得声称它代表完整文件。

### 4.2 Relevant Memory 区块

`[Relevant Memory]` 至多包含三条 `episodic_notes`。每条 note 包含 ID、短内容、规范化关键词、来源元数据、可选的关联路径/freshness，以及创建顺序/时间。

```json
{
  "id": "note-12",
  "content": "pytest -q 已成功完成：54 passed。",
  "keywords": ["pytest", "test"],
  "source": {"tool": "shell", "run_id": "...", "round": 6},
  "path": null,
  "freshness": null,
  "created_at": "2026-09-13T12:01:00Z"
}
```

检索器从当前 query、当前 `task_summary` 和 `latest_tool_error` 中提取小写字母数字关键词。note 得分为相同唯一关键词的数量；得分为零的 note 不召回；并列时优先较新的 note。Memory Manager 最多保存 12 条 note，超出时淘汰最旧条目；失效 note 在检索前被移除。

### 4.3 不同工具对 Memory 的更新

| 工具结果 | Working memory | File summaries | Episodic notes |
| --- | --- | --- | --- |
| 成功 `readfile` | 记录路径 | 仅完整读取时创建/刷新 | 新增一条指出已生成摘要的短 note |
| 成功 `write_file` / `patch_file` | 记录路径 | 使该路径失效 | 删除路径与旧 freshness 匹配的 note |
| 工具失败 | 保存稳定错误类型/消息为 latest error | 不变 | 仅 shell 失败、审批拒绝或工具执行错误新增一条受限失败 note |
| 成功 `search`、`find_files`、`listfiles` | 除直接相关路径外不自动更新 | 不变 | 不自动生成 note |

## 5. Prompt 构造与裁剪

工具定义继续通过 Provider API 的 `tools=` 参数发送，不复制到 `[Memory]` 或 session history。

发送给模型的消息顺序：

```text
静态 system prompt
+ repository context
+ [Memory]
+ [Relevant Memory]
+ 已裁剪 session transcript
+ 当前用户请求
```

当前用户请求始终作为最后一条 user message 发送，并在 run 开始时仅追加一次到持久化 session history。

Reducer 把 history 组织为两类 group：独立 user/final assistant message，或者 assistant tool-call message 加所有后续匹配 tool result。不得拆分 assistant tool-call group 与它的 tool results。在固定 transcript 预算内，它：

1. 先保留最新完整 group；
2. 将较旧成功 `readfile` 的 result 内容替换为紧凑标记，包含 path、完整/局部范围，以及最新摘要是否存在；
3. 将其他较旧工具输出替换为受限结果元数据；
4. 若仍超预算，则整体省略最旧 group。

Reducer 不伪造 assistant content，不修改工具 arguments，也不会把工具错误改写成成功。System prompt 应说明：压缩结果不是完整证据，模型需要细节时必须再次调用工具。

## 6. `readfile` 行范围接口

`readfile` 保持已有名称和必填的 `path`。增加可选整数参数：

```json
{
  "path": "httpstat.py",
  "start": 300,
  "end": 340
}
```

- 行号从 1 开始。
- `start` 默认 1，`end` 默认最后一行。
- 必须满足 `start <= end`。
- 与文件有重叠的越界范围可裁剪到文件边界；`start` 大于最后一行时返回 `invalid_arguments`。
- 成功结果包含 `path`、`content`、`start`、`end` 和 `line_count`。
- 内容保留原始一开始行号，便于模型精确引用与 patch。
- 现有最大字节、UTF-8、工作区边界、目录、符号链接和错误行为保持不变。

## 7. 可观测性与安全

- 每轮请求记录 `session_id`、`memory_chars`、`relevant_memory_chars`、`transcript_chars` 和 `prompt_chars`。
- `report.json` 如当前一样记录每轮完整模型请求，包含渲染后的 memory 区块与裁剪 transcript。
- 简洁 trace 仅记录计数、预算、ID 和稳定元数据，绝不记录完整文件内容或 note 内容。
- session 写入使用与 Run Recorder 相同的“临时文件 + replace”原子持久化规则。
- 持久化前使用现有敏感字段清理策略，`api_key`、`token`、`secret` 等键对应的值写为 `[REDACTED]`。
- 损坏、不可读、不支持版本，或仓库不匹配的 session，必须在模型请求前产生稳定的 CLI/session 错误。

## 8. 验收标准

- 首次 CLI 调用创建并报告 session ID；后续 `--session` 调用加载同一状态。
- session 不能在不同仓库根目录下打开。
- 下一次 run 能召回最多三条匹配的持久化 note，不包含关键词零匹配的 note。
- 成功完整 `readfile` 创建 SHA-256 freshness 摘要；通过任一写工具改动该文件后，摘要失效。
- `readfile(path, start, end)` 返回指定的一开始、含终点行号内容，且拒绝非法范围。
- 长多轮 run 的完整原始工具数据留在 `report.json`；下一次 LLM 请求使用受预算限制的 transcript。
- 任何 prompt 都不会包含缺少所属 assistant tool-call message 的 tool result。
- 现有 Schema 校验、工作区安全、审批、响应解析、trace 与 benchmark 行为不回归。
- Fake LLM 测试覆盖确定性的 session 持久化、召回、裁剪、freshness 失效和范围读取，不发起网络调用。
