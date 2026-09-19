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

- `shell` 只允许 `python` / `pytest` / `git` / `npm` / `uv` 的白名单子命令；只读 git 查询自动放行，`python -c <代码>`（含 `uv run python -c`）需要批准，其余也都要批准。`-c` 的**代码那一格跳过 shell 元字符检查**（那是数据不是 shell 语法，runner 用 `shell=False` + argv 启动），其余 token 照旧严查；`python -m` 和裸 flag 仍然拒绝。
- `readfile` 有两条返回上限，谁先到算谁：**2000 行**（`max_lines`）和 **64 KiB 字节**（作用于返回内容，不是源文件）。每个成功结果带一条 footer，明确写出下一段该用哪个 `start`；读到文件尾报 `End of file - total N lines`。单行超过 2000 字符会被截断并标记，不再让整个读取失败。源文件总量上限（64 MiB）**只挡整读**——带了 `start`/`end` 就放行（流式扫描，范围外的行不保留）。
- `write_file` 会自己建出缺失的父目录，并把要建的目录列进审批预览；只允许工作区内、非符号链接路径，单文件上限 64 KiB。
- 两级压缩的预算在 `context.py`，**由模型窗口推导，不要拍绝对数字**：`DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000`（deepseek-flash）→ `derive_transcript_budget` 算出 `transcript_budget_chars`（684,000），触发线是它的 0.8 倍（547,200）。推导公式 `窗口 × CHARS_PER_TOKEN(3.5) × PROMPT_BUDGET_RATIO(0.20) − FIXED_PROMPT_OVERHEAD_CHARS(16,000)`；换模型改窗口常量，`--transcript-budget` / `context_window_tokens` 可覆盖。**单结果在摄入时原样保存，只有视图越线后才按需修剪**（`_prune_to_fit`，从最大的开始、只修到装得下为止；一轮不够就把目标减半再来一轮，直到 `_MIN_TIGHTEN_CHARS`）——摄入时截断会让一个 22.7 KB 的文件在 684 KB 的视图里也被切成 4 段。两条硬约束：**触发线 / 单结果配额 ≥ 8**，以及**触发线 > 压缩目标 + 摘要上限 + 台账上限**（否则压缩抖动）。
- 工具调用按**重复**判定：只读工具的完全相同调用再次出现会被拒绝（`repeated_tool_call`），写工具与 `shell` 不参与。`--max-tool-calls`（默认 30）只是安全阀，`tool_call_limit` 正常不该出现。
