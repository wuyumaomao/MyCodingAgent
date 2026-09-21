# Session Checkpoint Recovery PRD

## 状态

已确认，开始实施。目标是让 session 在 run 中断后区分已完成、未执行和状态未知的工具调用，并安全恢复。

## 目标

- 保留现有 `history` / `memory` 的会话连续性。
- 增加一个 active checkpoint，记录当前 run 的工具执行边界。
- 工具执行前后原子更新 checkpoint，覆盖“工具已产生副作用但 result 尚未落盘”的崩溃窗口。
- 下次加载 session 时比较 checkpoint 与当前工作区，生成 `resume_state`。
- 高风险工具不能因为恢复而盲目重跑；无法确认时向模型返回结构化 `execution_interrupted` 结果。
- 记录运行环境身份，发现仓库或运行环境不一致时阻止静默恢复。

## 非目标

- 不恢复 Python 调用栈、网络连接或正在运行的子进程。
- 不实现跨进程 shell 进程接管。
- 不自动重跑 `write_file`、`patch_file` 或 `shell`。
- 不把完整 checkpoint 历史无限保存到 session；完整审计继续由 trace/report 负责。

## Session 数据模型

```json
{
  "schema_version": 2,
  "session_id": "...",
  "repo_root": "...",
  "history": [],
  "memory": {},
  "checkpoints": {"active": null, "recent": []},
  "resume_state": {"status": "clean", "checked_at": null},
  "runtime_identity": {
    "repo_root": "...",
    "platform": "win32",
    "python": "3.11",
    "model": "...",
    "context_window_tokens": 128000
  }
}
```

`active` 同一时间最多一个。它包含 `run_id`、round、phase、工具调用状态、history cursor 和相关工作区快照；工具调用状态为 `pending`、`running`、`completed`、`completed_error` 或 `interrupted`。`recent` 只保留少量已结束 checkpoint。

## 执行与恢复流程

```text
LLM 返回 tool calls
  -> 保存 active=pending
  -> 保存 assistant tool call 到 history
  -> 每个工具开始前保存 running 与 workspace_before
  -> 工具返回后写入 tool result、标记 completed/error 并保存
  -> 全部完成后归档 active，resume_state=clean
```

加载带有 active checkpoint 的 session 时：

1. 校验 repo root 和 runtime identity。
2. 获取当前工作区快照，并与工具执行前快照比较。
3. 只对确定已完成的写入生成恢复成功结果。
4. 对只读或未执行工具生成 `execution_interrupted` 结果，交给模型决定是否重试。
5. 对 shell 或无法判断的写入标记 `unknown`，需要检查或用户介入。
6. 将检查结论写入 `resume_state`，再继续当前 query。

## 验收标准

- 新 session 包含 checkpoints、resume_state、runtime_identity 默认结构，并能加载旧 schema session。
- 模型返回工具调用后即使下一步崩溃，session 仍保存 active checkpoint 和 assistant tool call。
- 工具正常错误写入 history，并标记 `completed_error`，不误报为中断。
- 成功 write/patch 后 active 清空或归档，下一次 run 不触发恢复。
- 恢复检查不会自动重跑高风险工具；未确定结果使用 `execution_interrupted`。
- 当前工作区变化、仓库不一致或 runtime identity 不一致会被记录到 resume_state。
- Fake LLM 测试覆盖 checkpoint 生命周期、崩溃恢复、写入 reconciliation 和旧 session 兼容。
