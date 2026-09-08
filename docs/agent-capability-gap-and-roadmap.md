# Coding Agent 能力差距与需求导向完善路线

## 1. 文档目的

本文基于 `F:\MyCodingAgent` 当前源码、测试和 README，并参考 `F:\pico-learn` 中 Pico 的实现，回答两个问题：

1. 当前 Coding Agent 已经具备哪些能力？
2. 下一步应该优先解决哪些真实用户问题？

建设原则不是简单复制 Pico 的模块，而是先通过可重复任务发现失败，再决定需要补充上下文管理、记忆、恢复还是模型编排能力。

## 2. 当前已经实现的能力

当前项目已经完成了一个受控操作 MVP：

- CLI 入口和 `CodingAgent` 公共 facade。
- 仓库根目录解析和工作区边界检查。
- `listfiles`、`readfile`、`search`、`find_files` 文件浏览工具。
- `write_file`、`patch_file` 文件修改工具。
- 受控 `shell` 工具。
- 写入和 shell 执行审批。
- 原子文件写入。
- JSON Schema 工具参数校验。
- 工具调用次数限制。
- OpenAI-compatible 模型接入。
- 每次运行的 `trace.json` 和 `report.json`。
- 基础多轮 `AgentLoop`。
- 运行链路检查脚本 `scripts/inspect_run.py`。

当前测试基线为：`164 passed, 4 skipped`。

这些能力已经覆盖了“在本地仓库内安全地浏览、修改和执行有限命令”的基础闭环。

## 3. 与 Pico 的主要差距

| 能力 | 当前 Coding Agent | Pico | 优先级 |
|---|---|---|---|
| 工具执行与安全 | 已有，边界清晰 | 已有 | 已完成 |
| 运行审计 | 有 trace/report | 有 trace/report/task state | 需要增强 |
| 上下文管理 | 每轮发送完整 history | 分层上下文、预算裁剪、重复读取折叠 | 高 |
| Working Memory | 没有 | 有 session 工作记忆 | 高 |
| Durable Memory | 没有 | 可跨 session 保存和召回 | 中 |
| Session 持久化 | 每次 `ask()` 独立 | 一个 session 可包含多个 run | 高 |
| Checkpoint / Resume | 没有 | 有 checkpoint、文件 freshness 和运行环境校验 | 高 |
| Retry / 错误恢复 | 主要依赖模型自行处理 | 有 retry、失败状态和恢复流程 | 中 |
| Provider | 主要是 OpenAI-compatible | 支持多种 provider | 中 |
| 多 Agent / 委托 | 没有 | 有相应运行时扩展 | 低 |
| Benchmark | 目前只有 PRD 草案 | 有固定任务、verifier、指标和实验结果 | 最高 |

注意：`docs/superpowers/specs/2026-09-08-coding-agent-benchmark-prd.md` 当前仍是“待评审”文档，不能当作已经落地的 Benchmark 实现。部分 shell 文档状态也需要与实际源码同步。

## 4. 需求导向，而不是模块导向

不要把目标写成“补上 Memory”“补上 Checkpoint”。应该先从用户问题出发：

### 问题 A：Agent 能否可靠回答仓库问题？

示例需求：

> 请解释这个仓库的启动流程，并指出测试命令。

验收标准：

- 读取了正确的文件。
- 回答包含文件或行号依据。
- 没有修改文件。
- 没有访问工作区外路径。
- 工具失败后能解释原因并调整策略。
- trace 能还原完整调用链。

### 问题 B：Agent 能否可靠完成小型修改？

示例需求：

> 把 README 中的占位文本替换成指定内容，并运行对应测试。

期望链路：

```text
readfile
  -> patch_file
  -> 用户审批
  -> 文件内容验证
  -> shell 执行测试
  -> 最终报告
```

### 问题 C：工具失败后能否恢复？

示例场景：第一次读取 `../secret.txt` 被拒绝，模型随后改为读取仓库内的 README。

需要观察：

- 错误类型是否正确。
- 模型是否重新选择了合适工具。
- 任务是否最终成功。
- 失败后增加了多少轮和工具调用。

### 问题 D：长任务中断后能否继续？

示例需求：

> 昨天分析到一半的任务，今天继续，不要重复已经完成的工作。

这个问题出现后，才需要引入 session、task state、checkpoint 和 resume。

## 5. 推荐完善路线

### 第一阶段：落地最小 Benchmark 闭环

这是当前最高优先级。先实现 3～5 个固定任务，而不是一开始做大规模评测：

```text
read_repository_question
read_and_explain_entrypoint
safe_patch_file
path_escape_recovery
shell_test_and_report
```

每个任务至少声明：

```json
{
  "id": "path_escape_recovery",
  "prompt": "...",
  "fixture_repo": "...",
  "allowed_tools": ["readfile"],
  "max_rounds": 4,
  "verifier": "...",
  "category": "security_recovery"
}
```

第一版先用 Fake LLM 建立确定性回归，再接入真实模型。

建议记录：

```text
pass_rate
attempts
tool_steps
rounds
failure_type
prompt_chars
path_rejection_count
approval_rejection_count
repeated_read_count
```

### 第二阶段：根据数据解决上下文问题

当 Benchmark 显示 prompt 变长、文件被重复读取、旧工具输出挤占当前请求时，再实现：

1. 记录 prompt、history 和 tool result 的字符数。
2. 压缩旧 history。
3. 折叠重复文件读取。
4. 引入分区预算，优先保留当前请求和最近结果。

可以逐步形成：

```text
prefix
  + current request
  + working memory
  + relevant memory
  + compressed history
```

### 第三阶段：解决跨 run 的任务连续性

当用户需要跨天或跨命令继续任务时，再增加：

```text
Session
  -> 多个 Run
  -> 共享任务状态
  -> 共享工作记忆
```

当前项目的模型是：

```text
一次 ask()
  -> 新的 ConversationContext
  -> 独立 run
```

而 Pico 的模型是：

```text
一个 session
  -> 多次 run
  -> 持久化状态和记忆
```

### 第四阶段：解决中断和恢复

建议按以下顺序实现：

```text
持久化 task_state
  -> 创建 checkpoint
  -> 校验关键文件 freshness
  -> 校验运行环境 identity
  -> 支持 resume
```

核心验收问题是：

> 程序中断后，能否安全地从上次完成的步骤继续，而不是重复操作或误用旧结果？

### 第五阶段：扩展 Provider

只有出现本地 Ollama、Anthropic 接口、模型对照实验或故障切换需求时，再抽象：

```text
ModelClient Protocol
  -> OpenAICompatibleClient
  -> OllamaClient
  -> AnthropicClient
```

`AgentLoop` 不应直接依赖具体供应商 SDK。

## 6. 暂不优先的能力

以下能力有价值，但不应排在核心可靠性之前：

- MCP。
- HTTP 服务。
- IDE 插件。
- 多 Agent 委托。
- 复杂数据库记忆。
- 自动规划系统。
- 大规模 Benchmark Dashboard。

当前项目定位为本地代码 Agent，CLI 加本地文件存储是合理的第一阶段形态。

## 7. 最终建设目标

把项目目标从：

> 继续增加 Agent 功能。

转成：

> 用固定任务证明 Agent 在哪些问题上可靠、在哪些问题上失败，并根据证据选择下一项工程投入。

推荐执行顺序：

```text
1. 落地 3～5 个 Benchmark
2. 加入自动 verifier
3. 运行 Fake LLM baseline
4. 记录失败分类和工具链
5. 根据失败数据决定做 context、memory 还是 resume
```

这样项目会从“功能集合”发展为一个有证据、可回放、可持续演进的 Agent Harness。
