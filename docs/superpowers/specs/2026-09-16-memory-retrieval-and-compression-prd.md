# 记忆检索与工具结果压缩 PRD

## 1. 背景

当前系统把文件摘要和工具历史都放在 memory/transcript 中，但两者目标不同：文件摘要用于长期理解代码仓库，工具结果压缩用于支撑当前轮次继续推理。若每轮发送全部文件摘要，仓库变大后会浪费上下文预算。

## 2. 目标

- 将 `file_summaries` 与 `episodic_notes` 物理分区保存、逻辑分开管理。
- `file_summaries` 保存结构化代码索引：文件职责、关键符号、证据行号和 freshness。
- 文件摘要按 query、最近文件和符号关键词检索，只把相关摘要放入 prompt。
- `episodic_notes` 继续按事件追加，并在读取时对 note 正文重新切词后按关键词召回，使旧 session 无需迁移即可受益于分词规则升级。召回只依据当前 query 与 run 的 `task_summary`；`latest_tool_error` 不参与召回。
- 将 tool result 压缩限定在 transcript 层，不修改完整 `session.history` 或 `report.json`。
- 为单个 tool result 和整个 transcript 设置字符配额；超出配额时先本地压缩，仍超限才通过可选 LLM summarizer 生成受限摘要。
- 所有 LLM 生成的摘要必须是结构化 JSON，并保留来源证据；摘要器不能调用工具或修改仓库。
- FileSummary 默认复用已配置的 `LLMClient`、模型、base URL 和 API key，但通过不带工具定义的独立请求生成摘要。

## 3. 非目标

- 不把 file summary 与 episodic note 合并为扁平数组。
- 不使用向量数据库、embedding 或额外持久化数据库。
- 不让摘要器替代原始 history；摘要失败时仍保留本地压缩结果。

## 4. 数据结构

`session.json` 中继续使用同一个 `memory` 对象，但保持两个分区：

```json
{
  "working_memory": {
    "task_summary": "...",
    "constraints": [],
    "recent_modified_files": [],
    "latest_tool_error": null
  },
  "file_summaries": {
    "src/app.py": {
      "summary": "一句话说明文件职责",
      "symbols": ["main"],
      "line_index": [{"lines": "10-40", "desc": "main 函数"}],
      "line_count": 120,
      "freshness": "sha256...",
      "source_range": {"start": 1, "end": 120},
      "updated_at": "..."
    }
  },
  "episodic_notes": []
}
```

文件摘要按路径覆盖更新；事件笔记按 ID 追加并限制数量。两者共用 session JSON 的原子写入和敏感字段清理。

字段名以本节的 `summary`、`symbols`、`line_index` 为准，provider 产出、检索打分和 prompt 渲染必须使用同一套命名，不得出现第二套别名。

## 5. File Summary 流程

成功读取完整文件后，程序保存路径、完整行数、freshness 和原始证据。默认由同一 `LLMClient` 的独立 FileSummary 请求生成结构化摘要；请求使用专用 system prompt，不传工具定义。provider 不可用、返回非法 JSON 或证据不合法时，使用确定性的本地降级摘要。摘要写入 `file_summaries`，局部读取不得覆盖完整文件摘要。

每轮构造 prompt 时，文件摘要检索器根据以下信号排序：query 中的路径、摘要中的符号/关键词、working memory 中的最近文件；最多返回固定数量，并受字符预算限制。

检索的匹配单位是关键词集合：ASCII 缩写符（文件名、符号名、技术术语）整体匹配，连续中文段切分为 bigram。因此：

- 中文提问可以召回中文 `summary`，不需要分词依赖；
- `symbols` 中的英文标识符仍然是最稳定的召回抓手；
- FileSummary 的 system prompt 必须要求 `summary` 保留源码中的英文标识符与技术术语，不得整体翻译，否则会显著降低召回率。

## 6. Transcript Tool Result 压缩

Reducer 仍按 assistant tool call 与 tool result 成组处理，保证配对。每个结果先执行本地压缩并应用单结果配额；较旧结果优先压缩，最新结果优先保留原文。压缩后若仍超出总 transcript 配额，则对最旧的一段调用 `compaction_summarizer` 生成交接摘要；失败时保留本地 stub，最后才丢弃最旧 group。

单结果压缩**只使用确定性本地规则，不调用模型**。理由是缓存与复现：本地规则是纯函数，同一份历史在任何一轮、任何一次运行都会算出逐字符相同的 stub；而 prompt cache 按精确前缀匹配，LLM 概括每次措辞不同，会让第一条 stub 之后的全部内容失效，也让同一份 session 无法复现出同一个 prompt。主流实现（Codex / Claude Code / DSH）对单个工具结果同样是确定性处理，LLM 只用在段落交接摘要这一层（本节的 `compaction_summarizer`）。

tool result 摘要只存在于本轮发送的 transcript，不写入 `file_summaries` 或 `episodic_notes`。完整工具输出仍保存在 session history/report。

## 6a. Prompt 构建次数

每轮只构建一次 prompt：`messages()` 的结果同时用于模型请求和 `prompt_metrics(messages)`。`prompt_metrics` 不得再次调用 `messages()`，否则启用 LLM 摘要时会产生重复的模型请求。

## 7. Prompt 结构

```text
静态 system prompt
+ repository navigation map
+ [Memory]（任务、约束、最近改动文件、最新错误）
+ [Relevant File Summaries]（检索结果）
+ [Relevant Episodic Notes]（最多 3 条）
+ reduced transcript
+ 当前用户请求
```

## 8. 验收标准

- 首轮 prompt 不包含全部 file summaries。
- query 相关的摘要可以被召回，不相关摘要不会全部注入。
- file summary 和 episodic note 仍在同一 session JSON 的独立字段中。
- history/report 保留完整工具结果。
- 单结果和总 transcript 配额可测试；超大结果能调用注入的 summarizer，失败有本地降级。
- tool-call/result 配对不被破坏；现有测试全部通过。
