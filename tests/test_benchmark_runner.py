from __future__ import annotations

import json
from pathlib import Path

from coding_agent.benchmark.contracts import load_dataset
from coding_agent.benchmark.runner import BenchmarkRunner


def test_fake_runner_executes_all_tasks_and_isolates_fixtures(tmp_path):
    dataset = load_dataset(Path(__file__).parents[1] / "benchmarks" / "coding_tasks.json")
    runner = BenchmarkRunner(dataset, runs_root=tmp_path / "runs", project_root=Path(__file__).parents[1])

    summary = runner.run_all(mode="fake")

    assert len(summary.results) == 4
    assert all(result.status == "passed" for result in summary.results)
    assert all(result.mode == "fake" for result in summary.results)
    assert all(result.trace_verified for result in summary.results)
    assert (tmp_path / "runs" / "fake" / "summary.json").exists()
    for result in summary.results:
        assert result.run_id
        assert result.fixture_copy is not None
        assert Path(result.fixture_copy).is_dir()


def test_runner_writes_machine_readable_summary(tmp_path):
    dataset = load_dataset(Path(__file__).parents[1] / "benchmarks" / "coding_tasks.json")
    runner = BenchmarkRunner(dataset, runs_root=tmp_path / "runs", project_root=Path(__file__).parents[1])
    runner.run_task(dataset.tasks[0], mode="fake")
    payload = json.loads((tmp_path / "runs" / "fake" / "summary.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "fake"
