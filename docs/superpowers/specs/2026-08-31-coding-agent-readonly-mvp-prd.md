# Coding Agent 只读 MVP PRD

## 文档状态

- 状态：已实现（统一工具 JSON Schema 校验）
- 最近修订：2026-09-03，增加模型工具参数的统一 Schema 校验层
- 方案：A，单进程分层架构
- 范围：面向本地 Git 仓库的只读 Coding Agent

## 1. 产品概述

Coding Agent 是一个以 CLI 为入口的本地代码仓库 Agent Harness。用户用自然语言提出问题，Agent 读取仓库结构和脚本，通过原生 tool calling 请求只读工具，并返回对仓库的解释。每次用户提问都创建一个可复盘的 run，并持续保存执行 trace。

MVP 的目标不是实现通用自主编程，而是验证一条稳定、可观测、受边界保护的长链路：

```text
用户问题 → 模型决策 → 工具调用 → 工具结果 → 模型总结
```

## 2. 目标

### 必须实现

- 提供一个 CLI 命令接收自然语言问题
- 自动定位 Git 仓库根目录
- 在首次模型请求前生成仓库上下文前缀
- 支持原生 tool calling
- 提供 `listfiles` 和 `readfile` 两个只读工具
- 为每个工具注册参数 JSON Schema，并在执行前统一校验模型参数
- 严格限制工具只能访问仓库目录内的内容
- 最多执行 10 轮工具调用
- 每个工具默认最多调用 3 次，超限时返回结构化错误
- 返回清晰的仓库结构、脚本用途和验证结果解释
- 使用固定测试夹具验证主链路
- 每次提问创建独立 run，并将关键步骤持久化到 `trace.json`
- 每次 run 同时生成简洁的 `trace.json` 和详细的 `report.json`
- 在模型响应和工具结果事件中记录各自耗时
- 为模型请求提供默认 60 秒超时，并允许 CLI/环境变量覆盖
- 为 run 和各模块调用记录开始时间、结束时间与耗时

### 不在 MVP 范围内

- 写文件、执行 Shell、运行测试命令
- 多 Agent 协作
- Web UI 或本地 HTTP 服务
- 跨任务记忆和断点恢复
- 多模型编排
- 插件市场和远程仓库访问
- 跨 run 的记忆、自动重放和 trace 可视化界面

## 3. 用户场景与验收标准

### 主要用户场景

用户在一个本地 Git 仓库中运行：

```bash
coding-agent "解释这个仓库的启动和测试脚本"
```

Agent 应该能够：

1. 识别当前仓库根目录
2. 向模型提供初始文件清单
3. 根据模型的 tool call 调用 `listfiles` 或 `readfile`
4. 将工具结果回传模型
5. 在 10 轮内输出基于实际文件内容的解释

### 验收标准

- 在 Git 仓库根目录或任意子目录启动，结果使用同一个仓库根目录
- 请求仓库外路径时，工具调用被拒绝且不会读取文件
- 文件不存在、不可读或超过大小限制时，模型收到结构化错误
- 模型返回结构合法但不符合工具 Schema 的参数时，不执行工具，并将稳定的参数错误作为 tool result 回传模型
- 模型没有新的 tool call 时，Agent 正常结束并输出最终文本
- 达到 10 轮工具调用时，Agent 停止并明确说明原因
- 在 `tests/fixtures/sample-repo` 上可以完成一次端到端测试
- 每次 CLI 提问都会生成唯一 run ID 和对应的 `trace.json`
- LLM 请求、工具调用、工具结果、错误和最终回答会按顺序写入 trace
- 中途失败时仍保留已写入的 trace，并标记 run 状态

## 4. CLI 设计

### 命令

```bash
coding-agent "<query>" [--repo <path>]
```

- `query`：必填的自然语言任务
- `--repo`：可选的仓库起始目录，默认使用当前工作目录

### 输出

- 默认输出最终解释
- 工具调用失败和循环终止原因输出到标准错误
- 成功或失败时输出 run ID 和 trace 文件路径
- 每次运行自动生成详细 report，不再提供 debug 开关
- `--timeout`：模型请求超时秒数，默认 `60`

## 5. 方案 A 架构

```text
CLI
└── AgentService
    ├── RepositoryResolver
    ├── ConversationContext
    ├── AgentLoop
    │   ├── LLMClient
    │   └── ToolRegistry
    │       ├── ToolSchemaValidator
    │       ├── listfiles
    │       └── readfile
    ├── RunRecorder
    └── OutputRenderer
```

### 组件职责

#### CLI

解析命令行参数，调用 `AgentService`，渲染最终结果并设置退出码。

#### RepositoryResolver

从当前目录或 `--repo` 起始目录向上查找 Git 根目录。找不到 Git 根目录时直接报错退出。

#### ConversationContext

负责构造和维护发送给 LLM 的上下文。上下文分为不变的 system messages（Agent 规则和一次 run 开始时生成的仓库稳定上下文）以及可变的 history（用户请求、assistant tool call、tool result）。每轮向 LLM 提供 system messages 加完整 history；仓库上下文只在任务开始时生成一次。

仓库稳定上下文包括：

- 仓库根目录下的文件清单
- 根目录下存在的 `README.md`、`pyproject.toml`、`package.json`、`AGENTS.md` 和 `.env.example` 内容
- 扫描到的常见入口文件（如 `main.py`、`app.py`、`run.py`、`cli.py`、`__main__.py`）内容
- `git status --short` 的启动时快照

重要文件单个最多读取 12KB；超过限制时只保留前 12KB 并附带截断标记。文件不存在、不可读或 Git 状态获取失败时，写入简短状态说明，不阻断整个 run。重要文件内容和 Git 状态均按普通 system message 注入，不作为工具调用结果追加到 history。

#### AgentLoop

维护 Agent 循环和工具分发；通过 `ConversationContext` 获取消息，不直接拼接 system、user、assistant 和 tool 字典。

#### LLMClient

封装单一 OpenAI-compatible provider。API Key、模型名和 Base URL 通过配置注入，Agent Core 不依赖具体供应商 SDK。

#### ToolRegistry

注册工具定义及其参数 JSON Schema，并提供按工具名称查询和校验参数的接口。Schema 校验通过后才执行具体工具；工具自身仍负责路径边界、文件存在性、权限和其他运行时安全检查。Schema 校验失败时返回稳定的 `invalid_tool_arguments` 结果，不执行工具。

#### ToolSchemaValidator

使用 `jsonschema` 对工具注册时的参数 Schema 执行 Draft 2020-12 兼容校验。它只负责工具协议层的结构和声明级约束，不读取文件、不访问仓库状态，也不替代工具实现中的业务和安全校验。

#### OutputRenderer

输出最终回答和错误信息，不暴露 API Key，不把不必要的内部调试信息混入最终解释。

#### RunRecorder

为每次用户 query 创建唯一 run 目录和 `trace.json`、`report.json`。trace 按事件顺序保存简洁摘要；report 保存每轮完整消息、工具定义、规范化模型响应、工具参数和工具结果。每个关键事件写入后立即持久化，确保失败任务也能复盘。模型响应、工具结果和 run 顶层记录耗时。

## 6. 消息与数据流

`ConversationContext` 管理三类数据：

- `system_messages`：Agent 规则、仓库文件清单、重要项目文件内容和 Git 状态快照，任务内不变
- `history`：用户请求、assistant tool call、tool result，按发生顺序追加
- `current_request`：开始任务时加入 history 的本次用户 query

首次请求的消息顺序：

1. `system_messages`（Agent 规则和仓库稳定上下文）
2. `history` 中的 `user` query

后续每轮：

1. LLM 返回最终文本或一个或多个 tool call
2. Agent 确认工具已注册，并使用该工具的 JSON Schema 校验 arguments
3. Schema 校验通过后，Agent 调用工具；工具继续执行自身的业务和安全校验
4. Schema 或工具校验失败时，Agent 不执行危险操作，追加结构化 tool result
5. Agent 追加 assistant tool call 和 tool result
6. ConversationContext 将完整消息历史发送给 LLM

LLM API 无状态时，客户端负责在每次请求中重新发送消息历史。仓库稳定上下文前缀在一次任务内保持不变；只读 MVP 不需要在每轮重新扫描仓库或重新读取重要文件。

RunRecorder 与消息历史并行工作：消息历史用于下一轮模型请求，trace 用于任务复盘；trace 不替代消息历史。

## 7. Run 与 Trace

### 存储位置

默认存储在 Coding Agent 项目目录：

```text
.coding-agent/runs/<run-id>/trace.json
```

目标仓库不会因为 trace/report 产生额外文件。未来可通过配置覆盖 runs 根目录。

### Trace 结构

```json
{
  "run_id": "2026-09-01T153000-abc123",
  "query": "解释这个仓库",
  "repo_root": "F:/MyCodingAgent/demo-repo",
  "status": "completed",
  "started_at": "2026-09-01T15:30:00Z",
  "ended_at": "2026-09-01T15:30:04Z",
  "events": [
    {"seq": 1, "type": "run_started"},
    {"seq": 2, "type": "llm_request", "round": 1},
    {"seq": 3, "type": "tool_call", "name": "readfile", "arguments": {"path": "README.md"}},
    {"seq": 4, "type": "tool_result", "name": "readfile", "result": {}},
    {"seq": 5, "type": "final_answer", "content": "..."}
  ]
}
```

### 持久化规则

- run 创建时立即写入 `run_started`
- 每次 LLM 请求、模型响应、工具调用和工具结果完成后立即写入
- 任务结束时写入 `final_answer` 或 `run_failed`，并更新 `status`
- 写入采用临时文件替换，避免进程中断留下半截 JSON
- trace 不包含 API Key；工具结果和消息内容按配置的敏感信息规则处理
- `trace.json` 仅保存消息数量、工具数量等摘要；`report.json` 固定保存完整消息和工具定义
- 模型响应、工具结果和 run 顶层记录 `duration_ms`；不记录步骤开始时间和结束时间
- 模型超时记录为稳定的 `timeout` 错误类型
- report 内容沿用递归脱敏规则，且不记录完整 OpenAI SDK 对象

## 8. 工具契约

### 8.1 统一参数 Schema 校验

每个注册工具必须提供一个顶层 `type: object` 的 JSON Schema。Schema 至少声明工具参数的属性、类型、必填字段和额外字段策略；可按工具需要声明 `minimum`、`maximum`、`minLength`、`enum` 等约束。

Schema 校验发生在 `ResponseParser` 将 provider 响应转换为内部 `ToolCall` 之后、工具实现执行之前。校验顺序固定为：

```text
解析响应 → 查找工具 → 校验 JSON Schema → 工具业务/安全校验 → 执行工具
```

Schema 校验覆盖：

- arguments 顶层类型和字段结构
- required 字段
- 字段类型、数值范围、字符串长度和枚举值
- `additionalProperties` 规则

Schema 校验不覆盖：

- 文件是否存在或可读取
- 路径是否逃逸仓库
- 符号链接是否指向仓库外部
- 文件内容编码和大小
- 需要访问运行时状态才能判断的业务语义

上述内容必须继续由具体工具和 `Workspace` 执行安全检查。

当 arguments 是合法 JSON object 但不符合工具 Schema 时，返回：

```json
{
  "ok": false,
  "error": {
    "type": "invalid_tool_arguments",
    "message": "Tool arguments do not match the registered schema"
  }
}
```

该结果作为 `role: tool` 消息回传模型，允许模型修正参数；工具实现不得在 Schema 校验失败时被调用。未知工具仍返回 `unknown_tool`，不尝试执行或推断 Schema。

Provider 侧的 `strict` tool schema（如果 provider 支持）只能作为额外约束，不能替代客户端 Schema 校验和工具安全检查。

### `listfiles`

用途：列出仓库内指定目录的文件和子目录。

输入：

```json
{
  "path": "src",
  "max_depth": 2,
  "max_entries": 200
}
```

- `path` 默认为仓库根目录
- 接受相对路径和绝对路径；路径解析后的真实位置必须位于仓库根目录内
- 默认遵守 `.gitignore`
- 超过深度或条目限制时返回截断信息

输出包含相对路径、文件类型和必要的大小信息，不读取文件内容。

### `readfile`

用途：读取仓库内的文本文件。

输入：

```json
{
  "path": "package.json"
}
```

- 接受相对路径和绝对路径；路径解析后的真实位置必须位于仓库根目录内
- 解析后的真实路径必须位于仓库根目录内
- 允许解析后仍位于仓库内的 `..` 路径
- 拒绝解析后逃逸仓库的路径和符号链接
- 默认按 UTF-8 读取
- 单次读取设置最大字节数，超限时返回结构化错误

## 9. Agent Loop 规则

- 单个任务最多 10 轮工具调用
- 模型返回无 tool call 的普通文本时结束
- 达到上限时停止并返回明确的终止说明
- 工具错误作为结构化 tool result 回传，由模型决定重试、换路径或结束
- 工具调用必须先通过注册 Schema 校验；Schema 不匹配时返回 `invalid_tool_arguments`，不执行工具
- 单个工具达到调用上限时不再执行该工具，将 `tool_call_limit` 错误回传模型
- 模型请求设置超时；超时和网络错误由 Agent 转换为用户可读错误
- MVP 不自动重试模型请求，避免重复工具调用；重试策略留待后续版本

## 10. 配置

首个 provider 使用 OpenAI-compatible 接口，建议配置项为：

- `CODING_AGENT_API_KEY`
- `CODING_AGENT_MODEL`
- `CODING_AGENT_BASE_URL`
- `CODING_AGENT_TIMEOUT`（默认 `60` 秒）

配置优先级固定为：CLI 参数 > 环境变量 > 默认值。API Key 不提供硬编码默认值，必须来自环境变量或运行环境注入；任何情况下都不得把密钥写入仓库或日志。

## 11. 安全与错误处理

- 所有工具调用在执行前进行路径规范化和仓库边界校验
- 所有工具调用在工具执行前进行统一 JSON Schema 校验
- 工具层按解析后的真实路径执行边界校验，允许仓库内绝对路径
- 不访问仓库外文件、环境变量内容或用户主目录
- 错误结果包含稳定的错误类型和简短消息，不包含敏感路径细节
- CLI 对参数错误、仓库定位失败、模型失败和工具失败返回非零退出码

## 12. 测试计划

### 测试夹具

固定目录：`tests/fixtures/sample-repo`

```text
sample-repo/
├── README.md
├── package.json
├── src/index.js
└── tests/index.test.js
```

### 必须覆盖

- 仓库根目录解析：根目录启动和子目录启动
- `listfiles` 的深度、条目和 `.gitignore` 行为
- `readfile` 的正常读取、文件不存在和大小超限
- 路径穿越和符号链接逃逸被拒绝
- 原生 tool call 到工具结果再到最终回答的循环
- 工具 Schema 校验通过、缺少必填字段、类型错误、数值越界和额外字段时的行为
- Schema 校验失败时工具实现不会被调用，并将 `invalid_tool_arguments` 作为 tool result 回传
- 未知工具名返回 `unknown_tool`，不会执行任何工具
- 10 轮上限和模型请求错误
- 稳定仓库上下文包含文件清单、重要项目文件内容和 `git status --short`
- 重要文件包括根目录下的 `README.md`、`pyproject.toml`、`package.json`、`AGENTS.md`、`.env.example`，以及扫描到的常见入口文件（如 `main.py`、`app.py`、`run.py`、`cli.py`、`__main__.py`）
- 重要文件缺失、不可读和超过 12KB 时不会阻断 run，并生成明确的状态说明
- report 包含完整请求、工具调用和规范化模型响应，trace 只保留摘要
- 超时配置和每个模型/工具步骤的耗时可以在 trace 中复盘

## 13. 后续演进

在只读 MVP 稳定后，按以下顺序考虑扩展：

1. `writefile` 和变更预览
2. Shell/测试工具及审批机制
3. 运行状态持久化和断点恢复
4. 事件日志与可观测性
5. 本地 HTTP 服务和 IDE 客户端
