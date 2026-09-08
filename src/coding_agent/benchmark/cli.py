from __future__ import annotations

import argparse
from argparse import Namespace
from pathlib import Path
import sys

from ..config import ConfigError, Settings
from .contracts import BenchmarkContractError, load_dataset
from .runner import BenchmarkRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_benchmark")
    parser.add_argument("--mode", choices=("fake", "real"), required=True)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--task")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[3]
    dataset_path = (args.dataset or project_root / "benchmarks" / "coding_tasks.json").resolve()
    runs_root = (args.runs_root or project_root / ".coding-agent" / "benchmark-runs").resolve()
    try:
        dataset = load_dataset(dataset_path, project_root=project_root)
        if args.task:
            selected = tuple(task for task in dataset.tasks if task.id == args.task)
            if not selected:
                raise BenchmarkContractError(f"Unknown task: {args.task}")
            dataset = type(dataset)(dataset.schema_version, dataset.description, selected)
        settings = None if args.mode == "fake" else Settings.from_args_and_env(Namespace())
        summary = BenchmarkRunner(dataset, runs_root=runs_root, project_root=project_root, settings=settings).run_all(mode=args.mode)
    except (BenchmarkContractError, ConfigError, ValueError, OSError) as exc:
        print(f"Benchmark error: {exc}", file=sys.stderr)
        return 2
    print(f"{summary.mode}: {summary.passed}/{len(summary.results)} tasks passed")
    print(f"Summary: {runs_root / summary.mode / 'summary.json'}")
    return 0 if summary.passed == len(summary.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
