from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from .agent import AgentError, AgentLimits, AgentService
from .coding_agent import CodingAgent
from .config import ConfigError, Settings
from .llm import LLMClient
from .repository import RepositoryError, Workspace, resolve_repository
from .trace import RunRecorder
from .tools.approval import WriteApprovalGate, WritePreview


RUNS_ROOT = Path(__file__).resolve().parents[2] / ".coding-agent" / "runs"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent")
    parser.add_argument("query", nargs="+", help="Natural-language question")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-tool-calls", type=int, default=3)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    recorder: RunRecorder | None = None
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        repository = resolve_repository(args.repo)
        workspace = Workspace(repository)
        recorder = RunRecorder.create(" ".join(args.query), repository, RUNS_ROOT)
        settings = Settings.from_args_and_env(args)
        agent = CodingAgent.from_settings(
            args.repo,
            settings,
            approval_ask=_ask_write_approval if sys.stdin.isatty() else None,
            limits=AgentLimits(max_calls_per_tool=args.max_tool_calls),
            llm_client_factory=LLMClient,
        )
        service = AgentService(agent)
        service.recorder = recorder
        answer = service.run(" ".join(args.query), workspace)
    except Exception as exc:#抛出了异常，记录是哪里出错了
        if recorder is not None and recorder.status == "running":
            error_type = _error_type(exc)
            recorder.fail(error_type, _safe_error_message(exc))
        print(f"Error: {exc}", file=sys.stderr)
        if recorder is not None:
            print(f"Run: {recorder.run_id}", file=sys.stderr)
            print(f"Trace: {recorder.trace_path}", file=sys.stderr)
            print(f"Report: {recorder.report_path}", file=sys.stderr)
        return 1
    if recorder is not None:
        print(f"Run: {recorder.run_id}", file=sys.stderr)
        print(f"Trace: {recorder.trace_path}", file=sys.stderr)
        print(f"Report: {recorder.report_path}", file=sys.stderr)
    print(answer)
    return 0


def _error_type(error: Exception) -> str:
    if isinstance(error, ConfigError):
        return "configuration_error"
    if isinstance(error, RepositoryError):
        return "repository_error"
    if isinstance(error, AgentError):
        return "agent_error"
    return "runtime_error"


def _ask_write_approval(preview: WritePreview) -> bool:
    print("\nWrite approval required", file=sys.stderr)
    print(f"Operation: {preview.operation}", file=sys.stderr)
    print(f"Path: {preview.path}", file=sys.stderr)
    if preview.existed:
        print("WARNING: this will overwrite an existing file.", file=sys.stderr)
    if preview.content is not None:
        print("Content preview:", file=sys.stderr)
        print(_preview_text(preview.content), file=sys.stderr)
    if preview.old_text is not None or preview.new_text is not None:
        print("Patch preview:", file=sys.stderr)
        print(f"- {_preview_text(preview.old_text or '')}", file=sys.stderr)
        print(f"+ {_preview_text(preview.new_text or '')}", file=sys.stderr)
    try:
        answer = input("Approve this write? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _preview_text(value: str, limit: int = 2000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...[truncated]"


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, (ConfigError, RepositoryError, AgentError)):
        return str(error)
    return "The coding agent failed to start"
