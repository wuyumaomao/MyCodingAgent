# AGENTS.md

本文件给在本仓库工作的 coding agent 提供项目约定。它属于仓库，不属于 agent 程序——换一个 `--repo` 就会换成那个仓库自己的约定。

## 项目是什么

受控操作的本地 coding agent（Python 3.11+，依赖用 uv 管理）。设计重心不是"更自主"，而是让 agent 在真实仓库里**可控、可审计**：工作区边界、写操作审批、shell 白名单、trace/report。

## 常用命令

- 全量测试：`uv run pytest -q`
- 单个测试文件：`uv run pytest tests/test_context.py -q`
- 编译检查：`uv run python -m compileall -q src`
- benchmark 确定性基线（不调用模型）：`uv run python scripts/run_benchmark.py --mode fake`
- 查看最近一次 run 的链路：`uv run python scripts/inspect_run.py`

## 代码约定

- 每个工具是一个类，放在 `src/coding_agent/tools/`，暴露 `name` / `description` / `parameters` / `execute` 四个成员。
- 新增工具必须同时注册到 `src/coding_agent/coding_agent.py` 的 tools 列表。
- 工具参数用 JSON Schema 描述，`additionalProperties` 一律为 `false`。
- **改行为之前先写失败测试**，再改实现——本仓库一直按这个顺序做。
- 测试不得依赖真实模型：使用 Fake LLM，或向 `from_settings()` 注入 provider。

## 不要做

- 不要执行 `git commit` / `git push`（是否提交由人决定）。
- 不要修改 `.coding-agent/` 下的运行产物（trace、report、session、benchmark 结果）。
- 不要读取或外传 `.env`（内含 API key）。
- 不要绕过审批门直接写文件。

## 已知边界

- `shell` 只允许 `python` / `pytest` / `git` / `npm` / `uv` 的白名单子命令；只读 git 查询自动放行，其余需要批准。
- `readfile` 单次返回上限 64 KiB：超出时返回 `truncated: true` 与实际行号，用 `start` / `end` 继续读取。
- 每个工具默认最多调用 3 次，可用 `--max-tool-calls` 调整。
