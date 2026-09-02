from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from .agent import AgentError, AgentLoop, AgentService
from .config import ConfigError, Settings
from .llm import LLMClient
from .repository import RepositoryError, Workspace, resolve_repository
from .trace import RunRecorder
from .tools.listfiles import ListFilesTool
from .tools.readfile import ReadFileTool
from .tools.registry import ToolRegistry


RUNS_ROOT = Path(__file__).resolve().parents[2] / ".coding-agent" / "runs"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent")
    parser.add_argument("query", nargs="+", help="Natural-language question")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--debug", action="store_true", help="Persist detailed message trace")
    parser.add_argument("--timeout", type=float)
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
        recorder = RunRecorder.create(
            " ".join(args.query), repository, RUNS_ROOT, debug=args.debug
        )
        settings = Settings.from_args_and_env(args)
        registry = ToolRegistry()
        listfiles = ListFilesTool(workspace)
        readfile = ReadFileTool(workspace)
        registry.register(
            listfiles.name,
            listfiles.execute,
            listfiles.parameters,
            description=listfiles.description,
        )
        registry.register(
            readfile.name,
            readfile.execute,
            readfile.parameters,
            description=readfile.description,
        )
        llm_client = LLMClient(
            api_key=settings.api_key,
            model=settings.model,
            base_url=settings.base_url,
            timeout=settings.timeout,
        )
        #cli装配好llm，tool-registry，encoder，contextbuilder给agentloop。run的时候传入query和workspace
        service = AgentService(AgentLoop(llm_client, registry, recorder=recorder))
        answer = service.run(" ".join(args.query), workspace)
    except Exception as exc:#抛出了异常，记录是哪里出错了
        if recorder is not None and recorder.status == "running":
            error_type = _error_type(exc)
            recorder.fail(error_type, _safe_error_message(exc))
        print(f"Error: {exc}", file=sys.stderr)
        if recorder is not None:
            print(f"Run: {recorder.run_id}", file=sys.stderr)
            print(f"Trace: {recorder.trace_path}", file=sys.stderr)
        return 1
    if recorder is not None:
        print(f"Run: {recorder.run_id}", file=sys.stderr)
        print(f"Trace: {recorder.trace_path}", file=sys.stderr)
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


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, (ConfigError, RepositoryError, AgentError)):
        return str(error)
    return "The coding agent failed to start"
