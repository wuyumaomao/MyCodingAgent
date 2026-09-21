# Run 报告阅读器

`scripts/inspect_run.py` 把一次 run 落盘的两个文件渲染成人能直接读的格式，用来审计 **agent 调用了哪些工具、每轮发给模型的 prompt 和响应是否合理**。

## 1. 两个文件的分工

每次 run 在 `.coding-agent/runs/<run-id>/` 下产生两份文档：

| 文件 | 内容 | 用途 |
|---|---|---|
| `trace.json` | 简洁链路：事件顺序、工具名、审批决定、成功/失败、耗时、指标 | 快速定位「哪一步出问题」 |
| `report.json` | 完整内容：每轮的完整 `messages`、工具定义、规范化的模型响应、工具结果 | 审计「模型到底看到了什么、回了什么」 |

`report.json` 通常几十 KB 到 1 MB，直接翻很痛苦；本脚本把两者都渲染成可读文本。

## 2. 命令

```powershell
cd F:\MyCodingAgent

# 事件链路（默认，等价于不带参数）
uv run python scripts/inspect_run.py

# 逐轮展开完整 prompt 与响应
uv run python scripts/inspect_run.py --prompts

# 指定某一次 run（<run-id> 是占位符，要换成真实 id）
uv run python scripts/inspect_run.py .coding-agent/runs/<run-id> --prompts

# 也可以直接粘贴 CLI 结束后打印的 Trace / Report 路径（文件路径会被归一化成所在目录）
uv run python scripts/inspect_run.py .coding-agent/runs/<run-id>/trace.json --prompts

# run 不在默认目录时（例如 benchmark 的结果）
uv run python scripts/inspect_run.py --runs-root .coding-agent/benchmark-runs/fake --prompts
```

不带 `run_dir` 时自动选择 `<runs-root>` 下**修改时间最新**的完整 run（同时存在 `trace.json` 和 `report.json`）。找不到完整 run 时报错退出。

**怎么拿到 run id**：每次 CLI 结束会在标准错误打印

```text
Run:    20260917T102707Z-1a2782
Trace:  F:\MyCodingAgent\.coding-agent\runs\20260917T102707Z-1a2782\trace.json
Report: F:\MyCodingAgent\.coding-agent\runs\20260917T102707Z-1a2782\report.json
```

直接复制 `Trace:` 那一整行路径即可（脚本接受文件路径，也接受目录）。想看历史某次就列目录：

```powershell
Get-ChildItem .coding-agent\runs | Sort-Object Name -Descending | Select-Object -First 5 Name
```

进行中的 run 也能读：事件是逐条落盘的，所以 `Status: running` 的 run 同样会渲染出已发生的部分。

## 3. `--prompts` 的输出格式

```
round 1 request  prompt_chars=5606  stable_prefix=0 (0%)
  [0] system       837  'You are a coding agent. Use only the provided tools…'
  [1] system       898  '[Repository Context] root: <workspace> important_files:…'
  [2] user          15  'Explain scripts'
  [3] user         137  '[Memory] task_summary: Explain scripts constraints: none…'

round 1 response  finish=tool_calls  api_attempts=1
  content: '先看一下仓库结构。'
  call: readfile({"path": "package.json"})

tool_result  readfile  ok=True  3.76ms
```

逐字段含义：

**`request` 行**
- `prompt_chars`：本轮发送给模型的 `messages` 序列化字符数（**不含**工具定义）
- `transcript_chars`：**视图本身**（交接摘要 + 历史）的字符数——段落压缩阈值比较的就是这个数。它比 `prompt_chars` 小的部分就是静态 system 消息加上本轮的记忆快照
- `compaction_threshold_chars`：本轮的压缩触发线（默认是实际 `transcript_budget_chars × 0.8`；预算由模型 context window 推导，也可由 CLI 覆盖）。拿 `transcript_chars` 和它比，才知道"该压而没压"是否真的发生了
- `stable_prefix`：本轮 prompt 与**上一轮** prompt 的公共字符前缀长度，括号内是占本轮 prompt 的比例。第 1 轮恒为 0
- `covered`：有多少条历史已被压缩摘要覆盖（有压缩过才显示）
- `dropped_groups`：因超硬预算被丢弃的旧组数量（仅在发生丢弃时显示）

**消息行** `[序号] 角色 字符数 预览 [附加信息]`
- 角色为 `system` / `user` / `assistant` / `tool`
- `assistant` 带 `tool_calls=[…]`，`tool` 带 `-> <tool_call_id>` 指回它应答的调用

**`response` 行**
- `finish_reason`：`tool_calls`（要继续跑工具）/ `stop`（结束）/ 其它
- `api_attempts`：本轮模型请求的实际尝试次数（>1 表示发生过重试）
- `call:` 列出模型请求调用的工具与参数

**`tool_result` 行**：工具名、`ok`、错误类型（若有）、耗时。

**`context_compressed` 行**：压缩发生的位置与代价。
- `kind=tool_result`：单个工具结果被 **本地规则** stub 化（`tool` 说明是哪个工具，`before->after` 是字符数变化）
- `kind=file_summary`：完整读取后成功生成了文件摘要（`path`）
- `kind=file_summary_failed`：**摘要生成失败并降级了**（`path`、`reason`）——出现这个事件就说明该文件只剩"前几行拼接 + 正则抽出的符号"，没有 `line_index`，召回的 prompt 里会标 `[fallback summary]`
- `kind=span`：段落压缩，旧消息被换成交接摘要（`covered` 是累计覆盖条数）
- `kind=span_failed`：**段落压缩失败**（`reason`、`view_chars`、`retry_at_chars`）——出现这个事件说明 prompt 会继续增长；视图涨到 `retry_at_chars` 之前不会重试，之后自动再试一次
- `before->after`：压缩前后的字符数；`duration_ms` 是这次压缩的耗时

## 4. 指标速查

| 指标 | 在哪看 | 正常表现 | 异常含义 |
|---|---|---|---|
| `prompt_chars` | `request` 行 | 随轮次平缓增长，压缩后回落一次 | 每轮都回落 = 压缩抖动 |
| `transcript_chars` | `request` 行 | 长期低于 `compaction_threshold_chars`，越线后回落一次 | 持续高于阈值却不回落 = 该压没压，prompt 会一直涨 |
| `compaction_threshold_chars` | `request` 行 | 同一 run 内恒定 | 与预算配置不一致 = 配置没生效 |
| `stable_prefix` | `request` 行 | 第 2 轮起接近上一轮 prompt 的 85%+ | 长期偏低 = prompt 前缀每轮都在变，缓存失效 |
| `covered` | `request` 行 | 偶尔跳增一次，然后长期不变 | 每轮 +N 且 prompt 不增长 = 把最新工作压进了摘要 |
| `dropped_groups` | `request` 行 | 一直是 0 | 大于 0 表示硬预算兜底被触发，有历史被丢弃 |
| `api_attempts` | `response` 行 | 1 | 大于 1 表示发生过 API 重试 |
| `duration_ms` | `tool_result` 行 | — | 异常大表示工具慢（例如 shell 超时） |

## 5. 三个审计清单

### 5.1 工具调用是否正确

- 是否先 `readfile` / `search` 定位再下结论，而不是凭空猜
- 参数是否指向存在的路径（错误类型 `file_not_found` / `workspace_violation` 说明模型在试错）
- 一条响应里是否合并了多个独立的工具调用（效率）
- 是否触发了 `tool_call_repeat`（`reason=already_succeeded` 表示重复了已经成功过的只读调用；`reason=identical_failure` 表示同一个失败调用试了三次）
- 是否触发了 `tool_call_limit`（安全阀，默认 30 次；正常情况下不该出现）
- 失败后是否换了工具或参数，而不是重复同一个失败调用

### 5.2 每轮 prompt 是否合理

- `[Repository Instructions]` 是否出现（目标仓库有 `AGENTS.md` 时应该出现）
- `[Memory]` 快照是否在尾部（它每轮重算，放进前缀会破坏缓存）。注意它**不一定是最末尾**：段落压缩把原始提问吸收进摘要后，末尾会补回一条 `user` 提问，此时顺序是 `…历史… [Memory] 提问`
- 当前提问是否在 prompt 中（被裁剪掉时会在末尾补回）
- `tool` 消息与 `assistant` 的 `tool_calls` 是否成对（配对不能破）
- 压缩后的 `tool` 结果是否还保留内容：`readfile` 的 stub 应当有 `content`（首尾两段代码）、`omitted_lines`（被省略的行号区间）和 `next_action_hint`（可照着做的续读区间）。只剩 `path` / `summary` 而没有 `content` 就说明内容被整块丢掉了，模型将被迫重读
- `next_action_hint` 给的区间是不是"读得完"的：窗口写成固定行数（例如 `first + 99`）时，长行文件上提示本身就会引发第二次截断。现在窗口按该段内容的平均行长算，判断方法是——出现提示之后**紧接着又出现同一个小缺口的提示**，就说明窗口偏大；缺口在稳步收窄则正常
- 失败的调用是否还带着 `error_type` / `error_message`：只有 `ok: false` 的 stub 会让模型不知道失败原因，只能重发同一条命令

### 5.3 压缩是否按预期工作

- 阈值以下：相邻两轮的 prompt 应是**纯追加**关系，`stable_prefix` 高
- 跨过阈值（`transcript_chars` > `compaction_threshold_chars`）：`covered` 跳增一次，`transcript_chars` 回落，**然后恢复纯追加**
- `covered` 持续增长 = 压缩抖动，检查 `compaction_target_chars` 加交接摘要和台账上限是否低于当前触发线
- `dropped_groups` 大于 0 = 摘要器不可用或反复失败，触发了硬预算兜底
- 单结果压缩是否生效：`context_compressed kind=tool_result` 的 `before->after`。若某个 `readfile` 反复出现 `before` 很大而模型下一轮又读同一个文件的相邻区间，先看它的 stub 是不是只有元数据（见 5.2）

### 5.4 Session checkpoint 恢复

断点恢复的细节落在目标仓库 `.coding-agent/sessions/<session-id>.json`，而不是 run report。检查 `checkpoints.active` 是否只在工具执行窗口存在；正常完成后它应归档到 `recent` 并清空 active。若下一次 run 发生恢复，查看 `resume_state.status`：`reconciled` 表示文件证据足以确认写入已完成，`resume_required` 表示存在无法确认的调用，`runtime_mismatch` 表示运行模型或上下文身份发生变化。恢复不会自动重跑高风险工具；应在 history 中看到对应的 `execution_interrupted` tool result，并由模型决定下一步。

## 6. 常见现象与对应原因

| 现象 | 原因 |
|---|---|
| 第 2 轮 `stable_prefix` 就接近 0 | 每轮都在改动 system 前缀（例如把随轮次变化的内容放进了 `system_messages`） |
| `stable_prefix` 只有 30% 左右且不涨 | prompt 太短（只有静态前缀 + 1 轮历史），属于正常；历史变长后会自然升高 |
| `covered` 每轮 +2、`prompt_chars` 一条不涨 | 压缩抖动：target 比一个 group 还小，最新工具结果被卷进摘要 |
| `readfile` 被反复读、区间越读越窄 | 该结果的 stub 没保留 `content`（只剩元数据），模型看不到代码；检查单结果压缩是否把内容整块丢了 |
| 失败调用之后模型原样重发同一条命令 | stub 丢了 `error_type` / `error_message`，模型不知道失败原因 |
| `transcript_chars` 高于阈值却一直不回落 | 该压没压：看是否有 `span_failed`，或可吸收的旧组太少（最近两组永不吸收） |
| `(no messages recorded)` | 该 report 没有记录 `messages`（脚本的防御分支） |
| `finish=None` | 记录该 run 的客户端没提供 `finish_reason`（例如 benchmark 的脚本化 LLM） |

## 7. 相关文件

- 实现：`scripts/inspect_run.py`
- 测试：`tests/test_inspect_run.py`
- 事件与落盘：`src/coding_agent/trace.py`、`src/coding_agent/events.py`
- 事件产生处：`src/coding_agent/agent.py`（`llm_request` / `llm_response`）、`src/coding_agent/tool_executor.py`（`tool_call` / `tool_result`）
- 上下文与压缩：`src/coding_agent/context.py`
