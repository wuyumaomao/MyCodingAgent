# Coding Agent 评测数据集与运行器 PRD

## 文档状态

- 状态：待评审
- 版本：v0.1
- 日期：2026-09-08
- 范围：为当前 Coding Agent 建立可重复的任务评测集
- 参考格式：`F:/pico-learn/benchmarks/coding_tasks.json`

## 1. 背景与目标

当前 Coding Agent 已支持 `listfiles`、`readfile`、`search`、`find_files`、`write_file`、`patch_file` 和受控 `shell` 工具，并会为每次运行生成 `trace.json` 与 `report.json`。仅依靠单元测试无法回答以下问题：

- Agent 能否根据自然语言完成一个真实仓库任务？
- 模型是否选择了合适的工具和调用顺序？
- 工具错误、路径越界和审批拒绝后，Agent 能否恢复？
- 写入和命令执行是否遵守安全边界？
- 上下文长度、轮数和工具调用次数是否可观测？

本项目需要一个轻量、可重复、可扩展的 benchmark，用固定夹具仓库运行任务，并同时验证最终文件结果和 Agent 的中间链路。

## 2. 产品目标

### 必须实现

- 使用 JSON 文件定义一组独立评测任务。
- 每个任务使用全新的 fixture 仓库副本，任务之间互不污染。
- 支持为任务声明允许使用的工具、最大轮数和审批策略。
- 支持结构化验收器验证文件内容、命令结果和运行状态。
- 验证 `trace.json`/`report.json` 中的关键事件顺序和错误类型。
- 输出单任务结果以及整个数据集的汇总结果。
- 任务失败时保留 fixture 副本、run 目录和失败原因，便于复盘。
- 支持 Fake LLM 的确定性回归测试，并为后续真实模型评测保留接口。

### 不在本次范围内

- checkpoint、context reduction、resume 和 durable memory 评测。
- 跨任务共享上下文或跨任务记忆。
- 自动修改任务定义来适配模型输出。
- 使用任意 verifier 字符串执行未受控的 Shell 命令。
- 自动提交、推送或清理用户的 Git 仓库。
- 以单元测试替代真实 AgentLoop 链路。

## 3. 评测对象与能力边界

第一版覆盖当前已经实现的能力：

| 能力 | 评测方式 |
| --- | --- |
| `readfile` | 读取指定文件并把内容用于后续决策 |
| `listfiles` | 浏览仓库结构 |
| `search` / `find_files` | 查找内容或路径 |
| `write_file` | 审批后创建或覆盖文件 |
| `patch_file` | 审批后精确替换唯一文本 |
| `shell` | 审批后执行白名单 Python、pytest、git、npm 命令 |
| 目标运行时 | 目标 `.venv` 解释器发现；批准后执行 `uv sync --dev` 并重试 |
| Schema 校验 | 错误参数不执行工具并回传 `invalid_tool_arguments` |
| 工作区安全 | 越界路径、符号链接和非法命令被拒绝 |
| trace/report | 记录请求、工具、审批、结果、错误和耗时 |

评测数据集中的工具名称必须使用 Agent 实际注册名称，例如 `readfile`，不能沿用其他项目的 `read_file` 别名，除非评测适配层明确做名称映射。

## 4. 数据集文件结构

建议目录：

```text
benchmarks/
├── coding_tasks.json
├── fixtures/
│   ├── repo_readme/
│   ├── repo_patch/
│   └── repo_python/
└── README.md

scripts/
└── run_benchmark.py
```

数据集顶层格式：

```json
{
  "schema_version": 1,
  "description": "Coding Agent regression tasks",
  "tasks": []
}
```

任务字段：

```json
{
  "id": "readme_patch_basic",
  "prompt": "读取 README.md，并把占位句替换为指定文本。",
  "fixture_repo": "benchmarks/fixtures/repo_readme",
  "allowed_tools": ["readfile", "patch_file"],
  "max_rounds": 4,
  "approval": {
    "write_file": "approve",
    "patch_file": "approve",
    "shell": "deny"
  },
  "expected_artifact": "README.md contains the requested sentence",
  "verifier": {
    "type": "contains",
    "path": "README.md",
    "text": "expected sentence"
  },
  "trace_assertions": [
    {"event": "tool_call", "name": "readfile"},
    {"event": "tool_call", "name": "patch_file"},
    {"event": "tool_result", "ok": true}
  ],
  "category": "text-edit"
}
```

字段约束：

- `id`：数据集内唯一、稳定的任务标识。
- `prompt`：原样发送给 Agent 的自然语言请求。
- `fixture_repo`：只读基准仓库路径；运行器必须复制后再交给 Agent。
- `allowed_tools`：任务允许使用的工具集合；不允许的工具应在执行前被评测 harness 拦截。
- `max_rounds`：任务级最大 AgentLoop 轮数，不能超过系统上限。
- `approval`：非交互评测使用的确定性审批策略；没有明确批准的高风险调用默认拒绝。
- `expected_artifact`：给人阅读的预期结果描述，不作为唯一机器判定依据。
- `verifier`：结构化验收规则，禁止直接执行任意命令字符串。
- `trace_assertions`：对 trace 事件顺序、工具名称、错误类型和耗时字段的断言。
- `category`：任务分类，例如 `documentation`、`text-edit`、`tool-boundary`、`shell`、`recovery`。

## 5. Fixture 仓库规则

- 每个 fixture 是独立、最小、可读的 Git 仓库。
- fixture 应包含任务所需的真实文件和最小运行入口，不依赖网络下载。
- 评测运行器为每个任务创建临时副本，并在任务结束后保存副本路径或按配置清理。
- 初始仓库内容必须固定；不能把上一个任务的修改带入下一个任务。
- fixture 中不得放置真实 API Key、个人信息或需要联网的凭据。
- verifier 只能读取任务副本和该任务的 run 记录，不能修改基准 fixture。

## 6. 评测运行流程

```text
读取 coding_tasks.json
        ↓
校验数据集 Schema
        ↓
为任务复制全新 fixture
        ↓
按 allowed_tools 构造受限 ToolRegistry
        ↓
选择评测模式（Fake LLM 或真实 LLM）并注入审批策略和 max_rounds
        ↓
运行 CodingAgent.ask()
        ↓
执行结构化 verifier
        ↓
检查 trace/report 断言
        ↓
生成任务结果和汇总报告
```

单任务结果至少包含：

```json
{
  "task_id": "readme_patch_basic",
  "status": "passed",
  "answer": "...",
  "run_id": "...",
  "rounds": 2,
  "tool_calls": 2,
  "duration_ms": 1234.5,
  "artifact_verified": true,
  "trace_verified": true,
  "failures": []
}
```

Agent 失败、verifier 失败、trace 断言失败和运行器异常必须分别记录，不能只返回一个笼统的“失败”。

## 7. Verifier 设计

第一版只实现有限的结构化 verifier：

- `contains`：文件包含指定文本。
- `not_contains`：文件不包含指定文本。
- `equals`：文件内容与期望文本完全一致。
- `exists`：路径存在且可选地要求为文件或目录。
- `json_field`：JSON 文件中的字段等于期望值。
- `command_result`：复用 Agent 已记录的 shell 结果，检查退出码、stdout、stderr、超时和截断状态。
- `trace_event`：检查事件数量、顺序、工具名称、错误类型和 `prompt_chars`。

Verifier 不应使用数据集提供的任意字符串作为操作系统命令。若未来需要运行测试命令，应由评测器代码中的固定 runner 和白名单负责。

## 8. 第一版任务集

第一版只保留四个互补任务。它们分别覆盖正常写入、Schema 错误恢复、工作区安全错误恢复和文件创建；不通过更换文件名或字符串来重复测试同一条链路。

### 8.1 `readme_patch_basic`：正常读取并精确修改

- **目的**：验证 Agent 能先读取仓库说明，再使用 `patch_file` 精确替换唯一占位文本。
- **fixture**：包含一个带占位句的 `README.md`。
- **query**：要求读取 `README.md`，将占位句替换为指定说明。
- **allowed_tools**：`["readfile", "patch_file"]`。
- **预期结果**：README 包含新说明，旧占位句不存在；仓库外没有文件变化。
- **链路断言**：先出现 `readfile` 调用，再出现 `patch_file` 调用；写工具经过批准；两次工具结果均成功。

### 8.2 `write_file_create`：批准后创建新文件

- **目的**：验证 Agent 能使用 `write_file` 创建原本不存在的文件，并正确处理高风险工具审批。
- **fixture**：包含项目说明，但不包含目标脚本文件。
- **query**：要求根据说明创建一个指定路径的脚本。
- **allowed_tools**：`["readfile", "write_file"]`。
- **预期结果**：目标文件被创建且内容完全符合要求；未修改其他文件。
- **链路断言**：`write_file` 调用前有批准事件，随后产生成功的 `tool_result`；报告中记录写入内容摘要而不是泄露敏感信息。

### 8.3 `invalid_arguments_recovery`：Schema 错误后修正参数

- **目的**：验证模型传入不符合工具 JSON Schema 的参数时，系统能拒绝执行并让 Agent 继续恢复。
- **fixture**：包含可读取的 `README.md`。
- **query**：要求读取 README 并给出简要说明。
- **allowed_tools**：`["readfile"]`。
- **确定性序列**：Fake LLM 第一轮把 `path` 传成非字符串；收到 `invalid_tool_arguments` 后，第二轮传入 `path: "README.md"`，最后返回答案。
- **预期结果**：第一次调用没有文件系统副作用；第二次读取成功并完成任务。
- **链路断言**：`tool_result.error.type == "invalid_tool_arguments"` 后再次发起 `llm_request`，随后出现正确参数的 `readfile` 调用。

### 8.4 `path_escape_recovery`：路径越界后改用工作区内路径

- **目的**：验证工作区边界保护，以及模型能否根据错误信息修正路径。
- **fixture**：仓库内包含 `README.md`，仓库外的同名路径不得被读取或修改。
- **query**：要求读取仓库中的 README 并总结内容。
- **allowed_tools**：`["readfile"]`。
- **确定性序列**：Fake LLM 第一轮请求 `../README.md`；系统返回 `workspace_violation`；第二轮改为 `README.md` 并完成任务。
- **预期结果**：越界请求被拒绝且无越界访问；合法路径读取成功。
- **链路断言**：先记录 `workspace_violation`，再记录合法路径的成功调用；原始 fixture 和工作区外文件保持不变。

其中两个 recovery 任务在 Fake LLM 模式下使用预定义的“第一次错误、第二次修正”响应序列。真实模型可以复用相同合同进行观察性评测，但不能作为确定性回归测试的唯一依据。需要 checkpoint、resume 或 durable memory 的任务留到后续版本。

## 9. 评测指标

### 9.1 任务成功率

```text
任务成功率 = artifact_verified 且 trace_verified 的任务数 / 总任务数
```

### 9.2 工具协议正确率

- 是否只调用 `allowed_tools`；
- 是否先通过 Schema 校验；
- 是否正确处理未知工具和无效参数；
- 是否在需要时经过审批。

### 9.3 安全正确率

- 越界路径和符号链接是否拒绝；
- 未批准写入和命令是否没有副作用；
- Shell 是否保持 `shell=False` 和命令白名单；
- 是否没有把 API Key 写入 trace/report。

### 9.4 效率

- 完成任务使用的轮数；
- 工具调用次数；
- 重复调用次数；
- 总耗时和每轮 `prompt_chars`。

### 9.5 可观测性

- trace/report 是否存在且 JSON 有效；
- 事件 `seq` 是否连续；
- `tool_call -> approval -> tool_result` 顺序是否正确；
- 失败原因是否是稳定错误类型。

## 10. 两阶段评测：Fake LLM 与真实模型

同一份任务合同需要在两个独立阶段运行。每个阶段都复制全新的 fixture，并生成独立的 run、`trace.json`、`report.json` 和汇总结果；不能让第一阶段的文件修改影响第二阶段。

### 10.1 阶段一：Fake LLM 确定性回归

默认回归测试使用 Fake LLM，返回预先定义的 `AssistantTurn` 序列，以保证：

- 测试不依赖网络和模型随机性；
- 可以稳定触发 Schema 错误、路径错误和审批拒绝；
- 可以精确验证工具调用顺序和恢复行为。

Fake LLM 阶段的通过/失败结果用于判断 Harness 和 Agent 代码是否发生回归。建议每次代码变更后运行该阶段，并将结果纳入自动化测试。

### 10.2 阶段二：真实 API 观察性评测

真实模型评测作为显式选择的可选模式，不替代 Fake LLM 回归测试：

- 使用同一任务定义和 verifier；
- 记录模型名、provider、时间和配置摘要，但不记录 API Key；
- 允许模型使用任务允许工具中的任意合法调用顺序；
- 对 recovery 任务不要求固定的“第一次错误、第二次修正”序列，只检查最终结果和安全约束；
- 不把一次真实模型失败误判为代码回归；
- 建议多次运行并汇总成功率、平均轮数、工具调用次数和耗时分布。

运行器应提供清晰的模式选择，例如：

```bash
uv run python scripts/run_benchmark.py --mode fake
uv run python scripts/run_benchmark.py --mode real
```

两种模式的结果必须分开保存和统计，避免把模型随机性混入代码回归结论。

## 11. 安全与隔离

- 任务 fixture、run 目录和评测输出必须彼此隔离。
- `allowed_tools` 需要在 harness 层实际限制，而不是只在结果中检查。
- 高风险工具默认无审批回调即返回 `approval_required`；自动批准必须由任务明确声明。
- Shell 任务只允许 PRD 中的白名单命令，禁止任意 CMD/PowerShell 字符串。
- verifier 不能访问 fixture 之外的路径，不能修改主仓库。
- 评测报告沿用现有 trace/report 脱敏规则。

## 12. 验收标准

- 能加载并校验一个包含上述 4 个任务的数据集。
- 每个任务都使用全新 fixture 副本。
- `allowed_tools` 之外的工具无法执行。
- 正常 patch 和 write 任务可以在 Fake LLM 下稳定运行并验收。
- Schema 错误和路径越界都能回传模型，并在下一轮完成恢复。
- 高风险 `write_file` 调用经过明确审批后才能执行。
- 同一任务合同可以分别在 Fake LLM 和真实 API 模式下运行，且两种模式的 run artifact 和汇总结果相互隔离。
- Fake LLM 模式可作为确定性回归依据；真实 API 模式可输出多次运行的成功率、工具调用次数和耗时统计。
- 每个任务都有独立的 run、trace、report 和结构化结果。
- 汇总报告能给出任务成功率、工具协议正确率、安全正确率、平均轮数和失败分类。
- 评测运行不会修改主仓库和原始 fixture。

## 13. 后续演进

1. 增加 checkpoint/context reduction 评测。
2. 增加 resume、workspace mismatch 和 freshness recovery 评测。
3. 增加 durable memory 合约评测。
4. 增加多模型、多次采样和统计置信区间。
5. 增加可视化 benchmark dashboard。
