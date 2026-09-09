# 目标项目运行时环境实施计划

> 按 Inline Execution 执行；本任务不提交 Git。

**目标：** 让 shell 工具在目标仓库的 `.venv` 中运行 `python`/`pytest`，并允许模型在用户批准后执行精确的 `uv sync --dev` 来创建或补齐环境。

**边界：** Agent 主进程解释器不切换；第一阶段只放行 `uv sync --dev`，其他 `uv` 子命令继续拒绝。每次同步都走现有 ShellApprovalGate。

## 任务

1. **目标解释器发现**（已完成）
   - 新增 `target_environment.py`，根据平台解析 `<workspace>/.venv/Scripts/python.exe` 或 `<workspace>/.venv/bin/python`。
   - 不存在时保留当前解释器回退行为。
   - 为 Windows、Unix、缺失环境增加测试。

2. **Shell 策略扩展**（已完成）
   - 将 `uv` 加入 Schema 和程序白名单。
   - 允许精确的 `['sync', '--dev']`，以及 `run <workspace .py script> [args]`；拒绝 `uv add`、`uv pip install`、`uv tool` 等命令。
   - 增加策略测试，验证合法和拒绝路径。

3. **子进程环境接入**（已完成）
   - `python` 使用目标 `.venv` 解释器；`pytest` 使用目标解释器的 `-m pytest`。
   - 将目标 `.venv` 的 `Scripts`/`bin` 放在 PATH 首位，并设置 `VIRTUAL_ENV`。
   - `uv sync --dev` 在目标 workspace 执行，使用系统 PATH 中的 `uv`，完成后下一次调用动态发现新环境。
   - 保留敏感环境变量过滤和 `shell=False`。

4. **运行时上下文提示**（已完成）
   - 在仓库上下文中报告 `.venv` 是否存在、目标 Python 路径和依赖同步建议。
   - 在系统规则中说明：Python/pytest 因依赖缺失失败时，请求批准后调用 `uv sync --dev` 并重试。

5. **验证**（已完成）
   - 运行新增专项测试和现有 shell/context 测试。
   - 运行完整 `uv run pytest -q`、`uv run python -m compileall -q src`、`git diff --check`。
