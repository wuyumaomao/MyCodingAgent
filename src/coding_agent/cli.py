from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from .agent import AgentError, AgentLoop, AgentService
from .config import ConfigError, Settings
from .llm import LLMClient
from .repository import RepositoryError, Workspace, resolve_repository
from .tools.listfiles import ListFilesTool
from .tools.readfile import ReadFileTool
from .tools.registry import ToolRegistry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coding-agent")
    parser.add_argument("query", nargs="+", help="Natural-language question")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        settings = Settings.from_args_and_env(args)
        repository = resolve_repository(args.repo)
        workspace = Workspace(repository)
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
        )
        service = AgentService(AgentLoop(llm_client, registry))
        answer = service.run(" ".join(args.query), workspace)
    except (ConfigError, RepositoryError, AgentError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(answer)
    return 0
