# Coding Agent 只读 MVP PRD

## 文档状态

- 状态：待评审
- 方案：A，单进程分层架构
- 范围：面向本地 Git 仓库的只读 Coding Agent

## 1. 产品概述

Coding Agent 是一个以 CLI 为入口的本地代码仓库 Agent Harness。用户用自然语言提出问题，Agent 读取仓库结构和脚本，通过原生 tool calling 请求只读工具，并返回对仓库的解释。

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
- 严格限制工具只能访问仓库目录内的内容
- 最多执行 4 轮工具调用
- 返回清晰的仓库结构、脚本用途和验证结果解释
- 使用固定测试夹具验证主链路

### 不在 MVP 范围内

- 写文件、执行 Shell、运行测试命令
- 多 Agent 协作
- Web UI 或本地 HTTP 服务
- 跨任务记忆和断点恢复
- 多模型编排
- 插件市场和远程仓库访问

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
5. 在 4 轮内输出基于实际文件内容的解释

### 验收标准

- 在 Git 仓库根目录或任意子目录启动，结果使用同一个仓库根目录
- 请求仓库外路径时，工具调用被拒绝且不会读取文件
- 文件不存在、不可读或超过大小限制时，模型收到结构化错误
- 模型没有新的 tool call 时，Agent 正常结束并输出最终文本
- 达到 4 轮工具调用时，Agent 停止并明确说明原因
- 在 `tests/fixtures/sample-repo` 上可以完成一次端到端测试

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
- 预留 `--verbose`，用于显示工具调用过程；MVP 可先不实现

## 5. 方案 A 架构

```text
CLI
└── AgentService
    ├── RepositoryResolver
    ├── ContextBuilder
    ├── AgentLoop
    │   ├── LLMClient
    │   └── ToolRegistry
    │       ├── listfiles
    │       └── readfile
    └── OutputRenderer
```

### 组件职责

#### CLI

解析命令行参数，调用 `AgentService`，渲染最终结果并设置退出码。

#### RepositoryResolver

从当前目录或 `--repo` 起始目录向上查找 Git 根目录。找不到 Git 根目录时直接报错退出。

#### ContextBuilder

在第一次 LLM 请求前生成仓库上下文前缀，包含仓库根目录和受限文件清单。它与 `listfiles` 共用扫描和路径校验逻辑。

#### AgentLoop

维护消息历史，向 LLM 请求下一步动作，分发工具调用，追加工具结果，直到模型返回最终文本或达到 4 轮上限。

#### LLMClient

封装单一 OpenAI-compatible provider。API Key、模型名和 Base URL 通过配置注入，Agent Core 不依赖具体供应商 SDK。

#### ToolRegistry

注册工具定义，校验模型传入的参数，执行工具并将结果转换为统一的工具消息。

#### OutputRenderer

输出最终回答和错误信息，不暴露 API Key，不把不必要的内部调试信息混入最终解释。

## 6. 消息与数据流

首次请求的消息顺序：

1. `system/developer`：Agent 规则、工具使用规则和工作空间边界
2. `repository_context`：仓库根目录和初始文件清单
3. `user`：用户 query

后续每轮：

1. LLM 返回最终文本或一个或多个 tool call
2. Agent 校验并执行工具
3. Agent 追加 assistant tool call 和 tool result
4. Agent 将完整消息历史发送给 LLM

LLM API 无状态时，客户端负责在每次请求中重新发送消息历史。仓库上下文前缀在一次任务内保持不变；只读 MVP 不需要在每轮重新扫描仓库。

## 7. 工具契约

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
- 只接受仓库内相对路径
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

- 只接受仓库内相对路径
- 解析后的真实路径必须位于仓库根目录内
- 拒绝 `..` 路径穿越和逃逸仓库的符号链接
- 默认按 UTF-8 读取
- 单次读取设置最大字节数，超限时返回结构化错误

## 8. Agent Loop 规则

- 单个任务最多 4 轮工具调用
- 模型返回无 tool call 的普通文本时结束
- 达到上限时停止并返回明确的终止说明
- 工具错误作为结构化 tool result 回传，由模型决定重试、换路径或结束
- 模型请求设置超时；超时和网络错误由 Agent 转换为用户可读错误
- MVP 不自动重试模型请求，避免重复工具调用；重试策略留待后续版本

## 9. 配置

首个 provider 使用 OpenAI-compatible 接口，建议配置项为：

- `CODING_AGENT_API_KEY`
- `CODING_AGENT_MODEL`
- `CODING_AGENT_BASE_URL`

配置优先级固定为：CLI 参数 > 环境变量 > 默认值。API Key 不提供硬编码默认值，必须来自环境变量或运行环境注入；任何情况下都不得把密钥写入仓库或日志。

## 10. 安全与错误处理

- 所有工具调用在执行前进行路径规范化和仓库边界校验
- 工具层不得接受绝对路径作为有效业务参数
- 不访问仓库外文件、环境变量内容或用户主目录
- 错误结果包含稳定的错误类型和简短消息，不包含敏感路径细节
- CLI 对参数错误、仓库定位失败、模型失败和工具失败返回非零退出码

## 11. 测试计划

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
- 4 轮上限和模型请求错误

## 12. 后续演进

在只读 MVP 稳定后，按以下顺序考虑扩展：

1. `writefile` 和变更预览
2. Shell/测试工具及审批机制
3. 运行状态持久化和断点恢复
4. 事件日志与可观测性
5. 本地 HTTP 服务和 IDE 客户端
