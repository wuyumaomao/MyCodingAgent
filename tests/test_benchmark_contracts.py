from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.benchmark.contracts import BenchmarkContractError, load_dataset


def _write_dataset(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "fixture").mkdir()
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _task(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "one",
        "prompt": "Read README",
        "fixture_repo": "fixture",
        "allowed_tools": ["readfile"],
        "max_rounds": 4,
        "approval": {},
        "expected_artifact": "answer",
        "verifier": {"type": "exists", "path": "README.md"},
        "trace_assertions": [],
        "category": "read_only_qa",
    }
    value.update(overrides)
    return value


def test_load_dataset_validates_contract_and_resolves_fixture(tmp_path):
    path = _write_dataset(
        tmp_path,
        {"schema_version": 1, "description": "demo", "tasks": [_task()]},
    )

    dataset = load_dataset(path, project_root=tmp_path)

    assert dataset.schema_version == 1
    assert dataset.tasks[0].id == "one"
    assert dataset.tasks[0].fixture_repo == (tmp_path / "fixture").resolve()
    assert dataset.tasks[0].allowed_tools == ("readfile",)


@pytest.mark.parametrize(
    "change",
    [
        {"allowed_tools": ["missing_tool"]},
        {"max_rounds": 0},
        {"id": ""},
        {"verifier": {}},
    ],
)
def test_load_dataset_rejects_invalid_task(change, tmp_path):
    task = _task(**change)
    with pytest.raises(BenchmarkContractError):
        load_dataset(
            _write_dataset(
                tmp_path,
                {"schema_version": 1, "description": "demo", "tasks": [task]},
            ),
            project_root=tmp_path,
        )


def test_load_dataset_rejects_duplicate_ids_and_fixture_escape(tmp_path):
    (tmp_path / "outside").mkdir()
    duplicate = {"schema_version": 1, "description": "demo", "tasks": [_task(), _task()]}
    with pytest.raises(BenchmarkContractError, match="unique"):
        load_dataset(_write_dataset(tmp_path, duplicate), project_root=tmp_path)

    path = tmp_path / "escape.json"
    path.write_text(
        json.dumps({"schema_version": 1, "description": "demo", "tasks": [_task(fixture_repo="../outside")]}),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkContractError, match="inside"):
        load_dataset(path, project_root=tmp_path)


def test_concrete_dataset_contains_exactly_four_tasks():
    dataset_path = Path(__file__).parents[1] / "benchmarks" / "coding_tasks.json"
    dataset = load_dataset(dataset_path)
    assert {task.id for task in dataset.tasks} == {
        "readme_patch_basic",
        "write_file_create",
        "invalid_arguments_recovery",
        "path_escape_recovery",
    }
