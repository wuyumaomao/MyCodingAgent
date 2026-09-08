from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


KNOWN_TOOLS = frozenset({"listfiles", "readfile", "search", "find_files", "write_file", "patch_file", "shell"})
REQUIRED_TASK_FIELDS = frozenset({
    "id", "prompt", "fixture_repo", "allowed_tools", "max_rounds", "approval",
    "expected_artifact", "verifier", "trace_assertions", "category",
})


class BenchmarkContractError(ValueError):
    """Raised when a benchmark dataset does not satisfy its contract."""


@dataclass(frozen=True)
class BenchmarkTask:
    id: str
    prompt: str
    fixture_repo: Path
    allowed_tools: tuple[str, ...]
    max_rounds: int
    approval: dict[str, str]
    expected_artifact: str
    verifier: dict[str, object]
    trace_assertions: tuple[dict[str, object], ...]
    category: str


@dataclass(frozen=True)
class BenchmarkDataset:
    schema_version: int
    description: str
    tasks: tuple[BenchmarkTask, ...]


def load_dataset(path: Path, *, project_root: Path | None = None) -> BenchmarkDataset:
    path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkContractError(f"Unable to load dataset: {path}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkContractError("Dataset must be a JSON object")
    if payload.get("schema_version") != 1:
        raise BenchmarkContractError("Unsupported schema_version")
    description = payload.get("description")
    raw_tasks = payload.get("tasks")
    if not isinstance(description, str) or not description.strip() or not isinstance(raw_tasks, list):
        raise BenchmarkContractError("Dataset requires description and tasks")
    root = (Path(project_root) if project_root is not None else path.parent.parent).expanduser().resolve()
    tasks: list[BenchmarkTask] = []
    seen: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise BenchmarkContractError("Each task must be an object")
        missing = REQUIRED_TASK_FIELDS - raw.keys()
        if missing:
            raise BenchmarkContractError(f"Task is missing fields: {sorted(missing)}")
        task_id = raw["id"]
        prompt = raw["prompt"]
        fixture_value = raw["fixture_repo"]
        allowed = raw["allowed_tools"]
        max_rounds = raw["max_rounds"]
        approval = raw["approval"]
        expected = raw["expected_artifact"]
        verifier = raw["verifier"]
        assertions = raw["trace_assertions"]
        category = raw["category"]
        if not isinstance(task_id, str) or not task_id.strip():
            raise BenchmarkContractError("Task id must be a non-empty string")
        if task_id in seen:
            raise BenchmarkContractError("Task ids must be unique")
        seen.add(task_id)
        if not isinstance(prompt, str) or not prompt.strip():
            raise BenchmarkContractError(f"Task {task_id} prompt must be a non-empty string")
        if not isinstance(fixture_value, str) or not fixture_value:
            raise BenchmarkContractError(f"Task {task_id} fixture_repo must be a path")
        fixture = (root / fixture_value).resolve() if not Path(fixture_value).is_absolute() else Path(fixture_value).resolve()
        if not fixture.is_relative_to(root):
            raise BenchmarkContractError(f"Task {task_id} fixture must stay inside project root")
        if not fixture.is_dir():
            raise BenchmarkContractError(f"Task {task_id} fixture does not exist: {fixture}")
        if not isinstance(allowed, list) or not allowed or not all(isinstance(name, str) for name in allowed):
            raise BenchmarkContractError(f"Task {task_id} allowed_tools must be a non-empty list")
        unknown = set(allowed) - KNOWN_TOOLS
        if unknown:
            raise BenchmarkContractError(f"Task {task_id} uses unknown tools: {sorted(unknown)}")
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds <= 0:
            raise BenchmarkContractError(f"Task {task_id} max_rounds must be positive")
        if not isinstance(approval, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in approval.items()):
            raise BenchmarkContractError(f"Task {task_id} approval must be an object of strings")
        if not isinstance(expected, str) or not isinstance(verifier, dict) or not verifier.get("type"):
            raise BenchmarkContractError(f"Task {task_id} requires expected_artifact and verifier.type")
        if not isinstance(assertions, list) or not all(isinstance(item, dict) for item in assertions):
            raise BenchmarkContractError(f"Task {task_id} trace_assertions must be a list of objects")
        if not isinstance(category, str) or not category.strip():
            raise BenchmarkContractError(f"Task {task_id} category must be a non-empty string")
        tasks.append(BenchmarkTask(task_id, prompt, fixture, tuple(allowed), max_rounds, dict(approval), expected, dict(verifier), tuple(dict(item) for item in assertions), category))
    if not tasks:
        raise BenchmarkContractError("Dataset must contain at least one task")
    return BenchmarkDataset(1, description, tuple(tasks))
