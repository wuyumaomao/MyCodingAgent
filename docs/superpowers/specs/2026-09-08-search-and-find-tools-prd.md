# Search 与 Find Files 工具 PRD

## 1. 背景

Agent 当前可以列出目录、读取已知文件并修改文件，但用户经常只用自然语言描述目标文件的用途，例如“排序脚本”或“统计脚本”，并不知道具体文件名。需要提供两个只读工具，分别支持按内容定位代码和按文件名定位文件。

## 2. 目标

- 提供 `search`，在 workspace 文件内容中搜索文本或正则表达式。
- 提供 `find_files`，按文件名或 glob 模式查找文件。
- 两个工具都只读，不触发审批，不要求 Agent 调用高风险 `shell` 工具。
- 统一返回仓库相对路径、截断状态和结构化错误。
- 保证所有路径都不能逃逸 Git repository workspace。

## 3. 非目标

- 不实现语义向量搜索或自然语言 embedding 检索。
- 不搜索 workspace 外部文件。
- 不允许通过这两个工具修改、删除或执行文件。
- 不把文件名匹配和内容匹配混合在同一个工具模式中。

## 4. 用户场景

### 4.1 按代码内容定位

用户说“找出哪里使用了 ShellPolicy”，Agent 调用：

```json
{"pattern": "ShellPolicy", "path": "src"}
```

工具返回匹配文件、行号和文本，Agent 再调用 `readfile` 确认上下文。

### 4.2 按文件名定位

用户说“找一下所有 shell 相关的 Python 文件”，Agent 调用：

```json
{"pattern": "*shell*.py", "path": "."}
```

工具只返回匹配的仓库相对文件路径。

## 5. 接口设计

### 5.1 `search`

参数：

- `pattern`：必填字符串，按正则表达式匹配每一行文本。
- `path`：可选字符串，仓库相对目录，默认 `.`。
- `max_results`：可选正整数，默认 200。

成功返回：

```json
{
  "ok": true,
  "matches": [
    {"path": "src/coding_agent/agent.py", "line": 42, "text": "..."}
  ],
  "truncated": false
}
```

实现优先使用 `rg`；不可用或启动失败时使用 Python UTF-8 文本遍历作为 fallback。无法按 UTF-8 解码的文件跳过，不影响其他文件搜索。

### 5.2 `find_files`

参数：

- `pattern`：必填字符串，文件名或 glob 模式。
- `path`：可选字符串，仓库相对目录，默认 `.`。
- `max_results`：可选正整数，默认 200。

工具同时匹配文件名和仓库相对路径，例如 `*.py`、`*shell*`、`src/**/*.py`，成功返回：

```json
{
  "ok": true,
  "files": ["src/coding_agent/tools/shell.py"],
  "truncated": false
}
```

## 6. 安全与限制

- 使用 `Workspace.resolve_relative()` 校验搜索根目录，拒绝 `..` 或绝对路径造成的 workspace 逃逸。
- 只遍历普通文件，不跟随符号链接。
- 跳过 `.git`、`.codex`、`.venv` 和 `node_modules`，并遵循仓库 `.gitignore`。
- 结果按仓库相对路径排序，达到 `max_results` 后停止并设置 `truncated: true`。
- 参数类型错误、正则非法、搜索目录不存在或路径越界时返回统一 `ok: false` 错误结构。

## 7. 验收标准

- Agent 注册表包含 `search` 和 `find_files` 两个工具定义。
- 能返回内容匹配的相对路径、行号和文本。
- 能按精确文件名及 glob 查找文件。
- 无匹配时返回空结果而非异常。
- `rg` 不可用时 `search` 仍可工作。
- 路径越界、非法参数和结果截断均有自动化测试覆盖。
- 全量测试和 Python 编译检查通过。
