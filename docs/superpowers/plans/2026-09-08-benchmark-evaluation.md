# Benchmark Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable benchmark harness that runs the same four coding tasks twice—first with a deterministic Fake LLM and then optionally with the configured real API—and verifies both final artifacts and the recorded execution chain.

**Architecture:** A JSON contract loader produces validated `BenchmarkTask` objects. A runner copies each fixture into an isolated temporary workspace, builds a restricted `CodingAgent`, injects either a scripted Fake LLM or the real `LLMClient`, executes `agent.ask()`, then runs structured verifiers against the copied workspace and run artifacts. Fake and real results are stored separately and never share fixtures.

**Tech Stack:** Python 3.11+, dataclasses, `json`, `pathlib`, pytest, existing `CodingAgent`/`AgentLoop`/`RunRecorder` APIs, and the existing OpenAI-compatible `LLMClient`.

**Spec:** `docs/superpowers/specs/2026-09-08-coding-agent-benchmark-prd.md`

## Global Constraints

- The dataset contains exactly four first-version tasks: `readme_patch_basic`, `write_file_create`, `invalid_arguments_recovery`, and `path_escape_recovery`.
- Every task runs against a fresh copy of its fixture; the original fixture and main repository must remain unchanged.
- Tool names use the project names `readfile`, `write_file`, and `patch_file`.
- High-risk writes require an explicit benchmark approval policy; no interactive prompt is used by the runner.
- Fake LLM is the deterministic regression baseline; real API mode is observational and must not replace the Fake baseline.
- API keys and authorization values must never be written to benchmark artifacts.
- Do not run `git commit` unless the user explicitly requests it.

---

### Task 1: Define and validate the benchmark contract

**Files:**
- Create: `src/coding_agent/benchmark/__init__.py`
- Create: `src/coding_agent/benchmark/contracts.py`
- Create: `tests/test_benchmark_contracts.py`

**Interfaces:**
- `BenchmarkTask` dataclass fields: `id: str`, `prompt: str`, `fixture_repo: Path`, `allowed_tools: tuple[str, ...]`, `max_rounds: int`, `approval: dict[str, str]`, `expected_artifact: str`, `verifier: dict[str, object]`, `trace_assertions: tuple[dict[str, object], ...]`, `category: str`.
- `BenchmarkDataset` dataclass fields: `schema_version: int`, `description: str`, `tasks: tuple[BenchmarkTask, ...]`.
- `load_dataset(path: Path, *, project_root: Path | None = None) -> BenchmarkDataset`.

- [ ] **Step 1: Write failing contract tests**

  Test that a valid JSON document loads, task IDs are unique, fixture paths resolve under the project root, and an unknown tool or missing required field raises `BenchmarkContractError`.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run: `uv run pytest tests/test_benchmark_contracts.py -q`

  Expected: FAIL because the benchmark contract module does not exist.

- [ ] **Step 3: Implement the dataclasses and loader**

  Parse the top-level `schema_version`, `description`, and `tasks`; normalize relative fixture paths against `project_root`; reject duplicate IDs, non-positive `max_rounds`, unknown tool names, and paths outside the project root.

- [ ] **Step 4: Run the focused tests and verify they pass**

  Run: `uv run pytest tests/test_benchmark_contracts.py -q`

- [ ] **Step 5: Do not commit yet**

  Keep the change available on `main`; commit only after explicit user instruction.

### Task 2: Add the four fixed fixture repositories and task contract

**Files:**
- Create: `benchmarks/coding_tasks.json`
- Create: `benchmarks/fixtures/bench_repo_readme/README.md`
- Create: `benchmarks/fixtures/bench_repo_write/README.md`
- Create: `benchmarks/fixtures/bench_repo_recovery/README.md`
- Create: `benchmarks/README.md`
- Modify: `tests/test_benchmark_contracts.py`

**Interfaces:**
- The JSON must load through `load_dataset()` and contain exactly the four IDs from the spec.
- The fixture directories are source snapshots; the runner, not the tests, owns temporary copies.

- [ ] **Step 1: Add tests for the concrete dataset**

  Assert the four IDs, expected tool sets, valid verifier types, and existence of every fixture path.

- [ ] **Step 2: Run the dataset tests and verify they fail**

  Run: `uv run pytest tests/test_benchmark_contracts.py -q`

  Expected: FAIL because the JSON and fixture files are not present.

- [ ] **Step 3: Create the four task definitions**

  Define `readme_patch_basic`, `write_file_create`, `invalid_arguments_recovery`, and `path_escape_recovery` with concrete prompts, artifact expectations, verifier objects, approval policies, and trace assertions. Recovery tasks must include deterministic Fake LLM metadata describing the scripted bad and corrected calls.

- [ ] **Step 4: Create minimal fixture contents**

  Include only the README and source files required by the four tasks. Do not include credentials or network-dependent setup.

- [ ] **Step 5: Run the dataset tests and verify they pass**

  Run: `uv run pytest tests/test_benchmark_contracts.py -q`

### Task 3: Implement deterministic Fake LLM sequences

**Files:**
- Create: `src/coding_agent/benchmark/fake_llm.py`
- Create: `tests/test_benchmark_fake_llm.py`

**Interfaces:**
- `ScriptedLLM` constructor: `ScriptedLLM(turns: Sequence[AssistantTurn])`.
- `ScriptedLLM.complete(messages: list[dict[str, object]], tools: list[dict[str, object]]) -> AssistantTurn`.
- The client records each request and raises `AssertionError` if the script is exhausted or if the supplied tool definitions do not contain a requested tool.

- [ ] **Step 1: Write failing tests**

  Test that scripted turns are returned in order, request history is retained, and exhaustion fails loudly.

- [ ] **Step 2: Run tests and verify failure**

  Run: `uv run pytest tests/test_benchmark_fake_llm.py -q`

- [ ] **Step 3: Implement `ScriptedLLM`**

  Return a copy of the next `AssistantTurn`, increment an index, and expose `requests` for trace assertions. Do not make network calls.

- [ ] **Step 4: Add the four task scripts**

  Encode successful read/patch, successful write, invalid-schema-then-correct-read, and path-escape-then-correct-read sequences.

- [ ] **Step 5: Run tests and verify pass**

  Run: `uv run pytest tests/test_benchmark_fake_llm.py -q`

### Task 4: Implement structured verifiers and trace assertions

**Files:**
- Create: `src/coding_agent/benchmark/verifiers.py`
- Create: `tests/test_benchmark_verifiers.py`

**Interfaces:**
- `verify_artifact(rule: dict[str, object], workspace_root: Path) -> VerificationResult`.
- `verify_trace(assertions: Sequence[dict[str, object]], trace_path: Path) -> VerificationResult`.
- `VerificationResult` fields: `passed: bool`, `failures: list[str]`.

- [ ] **Step 1: Write failing verifier tests**

  Cover `contains`, `not_contains`, `exists`, and ordered event assertions including `error.type`, tool name, and `prompt_chars`.

- [ ] **Step 2: Run tests and verify failure**

  Run: `uv run pytest tests/test_benchmark_verifiers.py -q`

- [ ] **Step 3: Implement bounded, non-shell verifiers**

  Resolve verifier paths under the task workspace; reject escape paths; never execute arbitrary command strings. Return individual failure messages instead of one generic failure.

- [ ] **Step 4: Run tests and verify pass**

  Run: `uv run pytest tests/test_benchmark_verifiers.py -q`

### Task 5: Implement the two-mode benchmark runner

**Files:**
- Create: `src/coding_agent/benchmark/runner.py`
- Create: `tests/test_benchmark_runner.py`
- Modify: `src/coding_agent/coding_agent.py`

**Interfaces:**
- `BenchmarkRunner(dataset: BenchmarkDataset, *, runs_root: Path, project_root: Path)`.
- `run_task(task: BenchmarkTask, *, mode: Literal["fake", "real"]) -> BenchmarkResult`.
- `run_all(*, mode: Literal["fake", "real"]) -> BenchmarkSummary`.
- `BenchmarkResult` records task ID, mode, status, answer, run ID, rounds, tool calls, duration, artifact/trace verification flags, and categorized failures.

- [ ] **Step 1: Write failing runner tests**

  Test fixture copying, isolation between two tasks, Fake mode execution, deterministic approval, allowed-tool restriction, separate fake/real output directories, and preservation of failed copies.

- [ ] **Step 2: Run tests and verify failure**

  Run: `uv run pytest tests/test_benchmark_runner.py -q`

- [ ] **Step 3: Add an optional `allowed_tools` filter to `CodingAgent.from_settings()`**

  Register only tools listed by the task while preserving current behavior when the argument is omitted. Unknown requested tools must fail contract validation before Agent execution.

- [ ] **Step 4: Implement isolated task execution**

  Copy the fixture to a unique temporary directory, create a `RunRecorder` under a mode-specific run root, configure auto-approval from the task contract, inject `ScriptedLLM` in Fake mode, and inject the configured `LLMClient` in Real mode.

- [ ] **Step 5: Run the agent and classify failures**

  Distinguish agent/runtime failure, artifact verifier failure, trace assertion failure, and budget failure. Preserve the copied workspace for failures and include model metadata only in the report, with secrets redacted.

- [ ] **Step 6: Run runner tests and verify pass**

  Run: `uv run pytest tests/test_benchmark_runner.py -q`

### Task 6: Add the benchmark CLI, documentation, and full verification

**Files:**
- Create: `scripts/run_benchmark.py`
- Modify: `benchmarks/README.md`
- Create: `tests/test_benchmark_cli.py`

**Interfaces:**
- CLI options: `--mode {fake,real}`, `--dataset PATH`, `--runs-root PATH`, and optional `--task ID`.
- The CLI prints a concise summary and writes machine-readable summary JSON separately for each mode.

- [ ] **Step 1: Write failing CLI tests**

  Assert `--mode fake` exits zero for the four deterministic tasks, invalid modes exit non-zero, and `--mode real` reports a clear configuration error when API settings are absent.

- [ ] **Step 2: Run tests and verify failure**

  Run: `uv run pytest tests/test_benchmark_cli.py -q`

- [ ] **Step 3: Implement the CLI entry point**

  Load the contract, instantiate `BenchmarkRunner`, run the selected mode, print pass/fail counts, and return a non-zero exit status when any task fails.

- [ ] **Step 4: Document both evaluation stages**

  Explain that Fake mode validates the harness deterministically, while Real mode uses the same contract and verifier for repeated observational evaluation. Include example commands and artifact locations.

- [ ] **Step 5: Run the complete verification suite**

  Run: `uv run pytest -q` and `git diff --check`.

- [ ] **Step 6: Review the final diff**

  Confirm no credentials, arbitrary shell verifier, fixture mutations, or automatic git commit were introduced.

