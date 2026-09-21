# Session Checkpoint Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable active checkpoints and workspace-aware resume decisions to the existing session lifecycle.

**Status:** Implemented and verified with the full test suite.

**Architecture:** Extend `SessionState` with bounded checkpoint data, resume state, and runtime identity. Add deterministic checkpoint/reconciliation helpers in `session.py`; `AgentLoop` persists checkpoint boundaries around each tool and injects interruption results before the next model request. Keep raw history and trace/report responsibilities unchanged.

**Tech Stack:** Python 3.11+, dataclasses, JSON, hashlib, subprocess, pytest, existing atomic session store.

**Spec:** `docs/superpowers/specs/2026-09-21-session-checkpoint-recovery-prd.md`

## Global Constraints

- Keep `history` as the source of conversation messages; checkpoints store execution metadata, not duplicate transcripts.
- Use atomic session persistence for every checkpoint boundary.
- Never automatically rerun `write_file`, `patch_file`, or `shell` during recovery.
- Preserve existing tool approval, schema validation, trace/report, and session repository-boundary checks.
- Do not commit changes.

### Task 1: Extend the session model and persistence

**Files:**
- Modify: `src/coding_agent/session.py`
- Test: `tests/test_session.py`

- [ ] Add failing tests for default `checkpoints`, `resume_state`, and `runtime_identity`, and for round-tripping them through `SessionStore.save/load`.
- [ ] Add `SessionState.checkpoints`, `resume_state`, and `runtime_identity` fields with JSON-safe defaults.
- [ ] Bump the document schema and migrate the existing schema-1 payload by supplying empty recovery fields.
- [ ] Keep `session_id` as the Python/API compatibility name; expose JSON `id` only if migration can remain backward-compatible.
- [ ] Add atomic helpers to update active checkpoint, archive recent checkpoint, and set resume state.
- [ ] Run `uv run pytest tests/test_session.py -q`.

### Task 2: Add workspace snapshots and reconciliation

**Files:**
- Modify: `src/coding_agent/session.py`
- Test: `tests/test_session.py`

- [ ] Add deterministic `workspace_snapshot(workspace, paths)` returning git HEAD/status fingerprint plus target file existence/hash.
- [ ] Add reconciliation tests for read-only interruption, write content already applied, patch new text already applied, shell unknown, and changed workspace.
- [ ] Implement `reconcile_checkpoint()` returning synthetic tool results and a structured `resume_state`; never execute tools.
- [ ] Use `execution_interrupted` for unresolved calls and `reconciled_success` only when file evidence is sufficient.
- [ ] Run the focused reconciliation tests.

### Task 3: Persist checkpoint boundaries in AgentLoop

**Files:**
- Modify: `src/coding_agent/agent.py`
- Modify: `src/coding_agent/coding_agent.py`
- Test: `tests/test_agent.py`, `tests/test_trace_integration.py`

- [ ] Add a failing test that simulates a crash after checkpoint creation and verifies the session contains an active running tool.
- [ ] Before appending/executing a model tool batch, save active pending calls and the assistant history cursor.
- [ ] Before each tool, mark it running and save its workspace snapshot.
- [ ] After adding the tool result to history, mark it completed/error and save in one atomic session write.
- [ ] Archive the checkpoint after all calls complete; preserve recent metadata only.
- [ ] On a new run, reconcile an existing active checkpoint before appending the new query; append synthetic results to history so OpenAI tool-call/result pairing remains valid.
- [ ] Pass model/context runtime identity into the session without exposing API keys.
- [ ] Run agent and trace integration tests.

### Task 4: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/run-report-reader.md`
- Modify: `学习文档.md`

- [ ] Document session recovery, active/recent checkpoint retention, resume states, and unsafe retry behavior.
- [ ] Add CLI/session recovery examples and explain that trace/report remain the detailed audit source.
- [ ] Run `uv run pytest -q --basetemp .pytest-checkpoint`, `uv run python -m compileall -q src`, and `git diff --check`.
- [ ] Remove the temporary test directory and report results without committing.
