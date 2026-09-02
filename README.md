# Coding Agent

一个用于探索和构建 Coding Agent 的项目。

## 项目状态

项目处于只读 MVP 阶段，当前支持通过 CLI 读取并解释本地 Git 仓库。

## 项目目标

逐步构建一个能够理解开发任务、构造上下文、调用工具并交付结果的 Coding Agent。

计划关注以下能力：

- 任务理解与澄清
- 计划生成与执行
- 代码库浏览和修改
- 测试与结果验证
- 可追踪的执行过程

## 快速开始

环境要求：Python 3.11+。

安装开发依赖：

```bash
uv sync --extra dev
```

配置模型服务：

复制 `.env.example` 为 `.env`，然后填写真实配置：

```bash
copy .env.example .env
```

`.env` 会被自动读取且不会提交到 Git。也可以直接设置同名环境变量。

运行只读查询：

```bash
coding-agent "解释这个仓库的启动和测试脚本"
coding-agent "package.json 里有哪些可用命令" --repo .
```

当前 MVP 仅提供 `listfiles` 和 `readfile` 两个只读工具，不会修改文件或执行 Shell 命令。

每次 CLI 提问都会创建一个独立的 run。运行结束后，CLI 会在标准错误中显示 run ID 和 trace 路径：

```text
.coding-agent/runs/<run-id>/trace.json
```

trace 会记录模型请求、工具调用、工具结果、错误和最终回答，且不会写入目标仓库或保存 API Key。

需要复盘真实消息流和模块调用方向时，可开启 debug trace：

```bash
coding-agent "解释这个仓库的启动和测试脚本" --repo . --debug
```

debug trace 会在同一个 `trace.json` 中附加脱敏后的完整 LLM 消息、工具定义、规范化模型响应以及 span 调用层级。debug 默认关闭；如果读取的文件包含密钥等敏感文本，这些内容仍可能出现在工具结果中，请谨慎使用。

## 开发约定

- 先明确目标和边界，再开始实现
- 为重要行为补充自动化验证
- 保持提交范围小而清晰
- 在完成前运行与变更相匹配的检查

## 后续计划

1. 增加 `writefile` 和变更预览
2. 增加 Shell/测试工具及审批机制
3. 增加运行状态持久化和断点恢复
4. 增加本地 HTTP 服务和 IDE 客户端
