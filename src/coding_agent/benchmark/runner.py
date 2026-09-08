from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Literal

from ..agent import AgentLimits
from ..coding_agent import CodingAgent
from ..config import Settings
from ..trace import RunRecorder
from .contracts import BenchmarkDataset, BenchmarkTask
from .fake_llm import ScriptedLLM, turns_for_task
from .verifiers import verify_artifact, verify_trace


BenchmarkMode = Literal["fake", "real"]


@dataclass(frozen=True)
class BenchmarkResult:
    task_id: str
    mode: BenchmarkMode
    status: str
    answer: str
    run_id: str
    rounds: int
    tool_calls: int
    duration_ms: float
    artifact_verified: bool
    trace_verified: bool
    failures: list[str]
    fixture_copy: str | None = None


@dataclass(frozen=True)
class BenchmarkSummary:
    mode: BenchmarkMode
    results: tuple[BenchmarkResult, ...]

    @property
    def passed(self) -> int:
        return sum(result.status == "passed" for result in self.results)

    def as_dict(self) -> dict[str, object]:
        return {"mode": self.mode, "passed": self.passed, "total": len(self.results), "results": [asdict(item) for item in self.results]}


class BenchmarkRunner:
    def __init__(
        self,
        dataset: BenchmarkDataset,
        *,
        runs_root: Path,
        project_root: Path,
        settings: Settings | None = None,
        llm_client_factory: Any | None = None,
    ) -> None:
        self.dataset = dataset
        self.runs_root = Path(runs_root).expanduser().resolve()
        self.project_root = Path(project_root).expanduser().resolve()
        self.settings = settings
        self.llm_client_factory = llm_client_factory
        self._results: dict[str, list[BenchmarkResult]] = {"fake": [], "real": []}

    def run_task(self, task: BenchmarkTask, *, mode: BenchmarkMode) -> BenchmarkResult:
        if mode not in {"fake", "real"}:
            raise ValueError("mode must be 'fake' or 'real'")
        mode_root = self.runs_root / mode
        mode_root.mkdir(parents=True, exist_ok=True)
        fixture_root = mode_root / "fixtures"
        fixture_root.mkdir(parents=True, exist_ok=True)
        fixture_copy = Path(tempfile.mkdtemp(prefix=f"{task.id}-", dir=fixture_root))
        shutil.rmtree(fixture_copy)
        shutil.copytree(task.fixture_repo, fixture_copy)
        subprocess.run(["git", "init", str(fixture_copy)], check=True, capture_output=True, text=True)
        recorder = RunRecorder.create(task.prompt, fixture_copy, mode_root / "runs")
        started = time.perf_counter()
        try:
            client = self._client_for(task, mode)
            agent = CodingAgent.from_settings(
                fixture_copy,
                self.settings or Settings(api_key="benchmark", model="fake"),
                approval_ask=self._approval_for(task),
                limits=AgentLimits(max_rounds=task.max_rounds),
                llm_client=client,
                llm_client_factory=self.llm_client_factory,
                allowed_tools=set(task.allowed_tools),
            )
            answer = agent.ask(task.prompt, recorder=recorder)
            trace_path = recorder.trace_path
            artifact = verify_artifact(task.verifier, fixture_copy, answer=answer)
            trace = verify_trace(task.trace_assertions, trace_path)
            failures = artifact.failures + trace.failures
            status = "passed" if not failures else "failed"
        except Exception as exc:
            if recorder.status == "running":
                recorder.fail("benchmark_error", "Benchmark task failed")
            answer = ""
            artifact = trace = type("Result", (), {"passed": False, "failures": [str(exc)]})()
            failures = [str(exc)]
            status = "failed"
        document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
        events = document.get("events", [])
        result = BenchmarkResult(
            task_id=task.id,
            mode=mode,
            status=status,
            answer=answer,
            run_id=recorder.run_id,
            rounds=sum(event.get("type") == "llm_request" for event in events),
            tool_calls=sum(event.get("type") == "tool_call" for event in events),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            artifact_verified=artifact.passed,
            trace_verified=trace.passed,
            failures=failures,
            fixture_copy=str(fixture_copy),
        )
        self._results[mode].append(result)
        self._write_summary(mode)
        return result

    def run_all(self, *, mode: BenchmarkMode) -> BenchmarkSummary:
        results = tuple(self.run_task(task, mode=mode) for task in self.dataset.tasks)
        return BenchmarkSummary(mode, results)

    def _client_for(self, task: BenchmarkTask, mode: BenchmarkMode) -> Any:
        if mode == "fake":
            return ScriptedLLM(turns_for_task(task.id))
        if self.settings is None:
            raise ValueError("Real benchmark mode requires Settings")
        if self.llm_client_factory is not None:
            return self.llm_client_factory(
                api_key=self.settings.api_key,
                model=self.settings.model,
                base_url=self.settings.base_url,
                timeout=self.settings.timeout,
            )
        return None

    @staticmethod
    def _approval_for(task: BenchmarkTask):
        def approve(preview: Any) -> bool:
            key = "patch_file" if preview.operation == "patch" else "write_file"
            return task.approval.get(key) == "approve"
        return approve

    def _write_summary(self, mode: BenchmarkMode) -> None:
        path = self.runs_root / mode / "summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"mode": mode, "results": [asdict(item) for item in self._results[mode]]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
