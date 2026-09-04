# Coding Agent 写工具扩展 PRD

## 文档状态

- 状态：已实现
- 版本：v0.1
- 基础 PRD：`docs/superpowers/specs/2026-08-31-coding-agent-readonly-mvp-prd.md`
- 范围：在现有只读 Coding Agent 上增加受用户批准的文件写操作

## 1. 产品目标

为 Coding Agent 增加两个文件修改工具，使 Agent 能在仓库工作区内完成受控写操作：

- `write_file`：创建文件或覆盖已有文件
- `patch_file`：精确替换已有文件中的一段文本

两个工具都是高风险工具。模型只能提出写入请求，不能直接获得写权限；每个工具调用都必须单独经过用户批准。

## 2. 范围

### 必须实现

- 注册 `write_file` 和 `patch_file` 两个工具
- 工具参数使用统一 JSON Schema 校验
- 工具只能访问当前 Git 仓库工作区内的路径
- 允许仓库内解析后的相对路径、绝对路径和 `..` 路径
- 拒绝解析后逃逸工作区的路径和符号链接路径
- 每个写工具调用单独进入待审批状态
- CLI 在写入前展示操作类型、目标路径和变更预览
- 用户明确批准后才真正修改文件
- 用户拒绝时不修改文件，并将结构化结果回传模型
- 非交互环境没有用户批准时拒绝写入
- 使用临时文件替换完成原子写入，写入失败时保留原文件
- 记录审批结果、写入结果和耗时到 `trace.json` 与 `report.json`

### 不在本次范围内

- Shell 命令和测试命令执行
- 自动提交、推送或创建 Pull Request
- 多文件事务和跨工具回滚
- 模糊匹配、正则替换或补丁格式解析
- 自动批准策略
- 写入仓库外路径

## 3. 工具契约

### 3.1 `write_file`

用途：使用 UTF-8 文本创建或整体覆盖一个仓库内文件。

输入：

```json
{
  "path": "src/example.py",
  "content": "print('hello')\n"
}
```

规则：

- `path` 必须是非空字符串
- `content` 必须是字符串
- 新文件的父目录必须已经存在；本工具不自动创建目录
- 已有文件允许覆盖，但预览必须标记为 `create` 或 `overwrite`
- 单次写入内容最多 64 KiB（按 UTF-8 编码后的字节数计算）
- 目标是符号链接时拒绝执行
- 目标路径必须通过 `Workspace` 边界校验

成功结果：

```json
{
  "ok": true,
  "path": "src/example.py",
  "operation": "create",
  "bytes_written": 18
}
```

### 3.2 `patch_file`

用途：在一个仓库内 UTF-8 文本文件中，将一段精确文本替换为另一段文本。

输入：

```json
{
  "path": "src/example.py",
  "old_text": "print('hello')",
  "new_text": "print('updated')"
}
```

规则：

- `path`、`old_text`、`new_text` 必须是字符串
- 目标文件必须存在且是普通文件
- `old_text` 必须恰好匹配一次
- 匹配 0 次返回 `text_not_found`，不得写入
- 匹配多次返回 `text_not_unique`，不得写入
- 不进行模糊匹配、正则匹配或自动纠正换行符
- 替换后的文件大小不得超过 64 KiB
- 目标是符号链接时拒绝执行
- 目标路径必须通过 `Workspace` 边界校验

成功结果：

```json
{
  "ok": true,
  "path": "src/example.py",
  "operation": "patch",
  "replacements": 1,
  "bytes_written": 20
}
```

## 4. 审批流程

每个工具调用都独立审批，即使模型在同一响应中返回多个写工具调用，也必须逐个询问：

```text
模型返回 write_file A、patch_file B
    ↓
展示 A 的预览并询问
    ↓
批准或拒绝 A
    ↓
展示 B 的预览并询问
    ↓
批准或拒绝 B
```

审批前工具只计算预览，不修改文件。预览至少包含：

- 工具名称和操作类型
- 仓库相对路径
- 新文件内容，或 patch 的旧文本/新文本差异
- 覆盖已有文件时的明确警告

审批结果：

- 批准：继续执行该工具调用
- 拒绝：返回 `approval_denied`，文件不发生变化
- 无法询问用户：返回 `approval_required`，文件不发生变化

审批是一次性的，只对当前工具调用有效；后续调用必须重新询问。

## 5. 安全与一致性

- 所有路径先解析再校验，边界以 `Workspace.root` 为准
- 允许解析后仍在仓库内的绝对路径和 `..` 路径
- 拒绝解析后逃逸仓库的路径
- 拒绝符号链接目标，避免通过链接绕过工作区边界
- 不读取或写入真实 `.env` 中的密钥内容作为审批信息
- 写入使用同目录临时文件，刷新并替换目标文件
- 临时文件写入或替换失败时，原文件保持不变
- 工具结果只返回稳定错误类型和必要摘要，不返回完整绝对路径

## 6. 错误类型

工具错误统一使用：

```json
{
  "ok": false,
  "error": {
    "type": "stable_error_type",
    "message": "Human-readable summary"
  }
}
```

`write_file` 错误类型：

- `invalid_arguments`
- `workspace_violation`
- `parent_not_found`
- `permission_denied`
- `write_error`
- `file_too_large`
- `approval_denied`
- `approval_required`

`patch_file` 错误类型：

- `invalid_arguments`
- `workspace_violation`
- `file_not_found`
- `not_a_file`
- `symlink_not_allowed`
- `decode_error`
- `text_not_found`
- `text_not_unique`
- `file_too_large`
- `permission_denied`
- `write_error`
- `approval_denied`
- `approval_required`

## 7. 架构调整

```text
AgentLoop
├── ResponseParser
├── ConversationContext
└── ToolRegistry
    ├── listfiles
    ├── readfile
    ├── write_file
    └── patch_file
            ↓
      WriteApprovalGate
            ↓
      Workspace + AtomicWriter
```

- `ToolRegistry` 继续负责工具注册、Schema 校验和分发
- `WriteApprovalGate` 负责逐调用审批，不负责路径校验或文件写入
- `write_file`/`patch_file` 负责业务校验、生成预览和调用批准后的写入器
- `AgentLoop` 负责把批准结果或写入结果作为 `role: tool` 消息回传模型
- `ConversationContext` 仍然保存动态工具调用历史；写入完成后不自动重新生成稳定仓库上下文

## 8. Trace 与 Report

每个写工具调用至少记录以下事件：

- `tool_call`：工具名称和参数摘要
- `approval_request`：审批请求和预览摘要
- `approval_result`：批准、拒绝或无法询问
- `tool_result`：成功或失败、错误类型、写入字节数和耗时

`trace.json` 保存摘要，`report.json` 保存完整的工具参数和预览内容；两者继续遵守现有递归脱敏规则，不记录 API Key。

## 9. 验收标准

- `write_file` 可以在仓库内创建新文件
- `write_file` 可以在用户批准后覆盖已有文件
- 未批准、拒绝批准或非交互环境下，`write_file` 不修改文件
- `patch_file` 只在旧文本恰好匹配一次且用户批准后写入
- `patch_file` 在 0 次或多次匹配时拒绝写入
- 两个工具都拒绝越界路径和符号链接路径
- 写入失败时原文件内容保持不变
- 同一模型响应中的多个写调用逐个审批
- 工具错误通过 `role: tool` 回传模型，模型可以决定结束或继续
- trace/report 按顺序记录审批和写入事件

## 10. 测试计划

- `write_file` 创建新文件
- `write_file` 覆盖已有文件
- `write_file` 拒绝越界路径、符号链接和不存在的父目录
- `write_file` 拒绝超出 64 KiB 的内容
- `patch_file` 成功替换唯一匹配文本
- `patch_file` 对 0 次和多次匹配返回不同错误
- `patch_file` 拒绝非 UTF-8 文件和超大结果
- 审批批准、拒绝和非交互场景
- 多个写工具调用逐个审批
- 原子写入失败时保留原文件
- AgentLoop 将写入结果追加到上下文并继续或结束
- report 包含写入预览，trace 只保存摘要
