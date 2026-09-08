from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agent import AgentLimits, AgentLoop
from .config import Settings
from .context import build_repository_context
from .events import NullEventSink, RecorderEventSink
from .llm import LLMClient
from .repository import Workspace, resolve_repository
from .tools.approval import WriteApprovalGate, WritePreview
from .tools.listfiles import ListFilesTool
from .tools.find_files import FindFilesTool
from .tools.patchfile import PatchFileTool
from .tools.readfile import ReadFileTool
from .tools.search import SearchTool
from .tools.registry import ToolRegistry
from .tools.shell import ShellApprovalGate, ShellPreview, ShellTool
from .tools.shell_policy import ShellPolicy
from .tools.shell_runner import WindowsProcessRunner
from .tools.writefile import WriteFileTool
from .trace import RunRecorder


class CodingAgent:
    """Reusable public facade for one repository and model configuration."""

    def __init__(self, loop: AgentLoop, workspace: Workspace, registry: ToolRegistry) -> None:
        self.loop = loop
        self.workspace = workspace
        self.registry = registry

    @classmethod
    def from_settings(
        cls,
        repo: Path,
        settings: Settings,
        *,
        approval_ask: Callable[[WritePreview], bool] | None = None,
        shell_approval_ask: Callable[[ShellPreview], bool] | None = None,
        approval_record: Callable[..., None] | None = None,
        limits: AgentLimits | None = None,
        repository_context_builder: Callable[[Workspace], str] = build_repository_context,
        llm_client: Any | None = None,
        llm_client_factory: Callable[..., Any] | None = None,
        shell_policy: ShellPolicy | None = None,
        shell_runner: WindowsProcessRunner | None = None,
        allowed_tools: set[str] | frozenset[str] | None = None,
    ) -> "CodingAgent":
        repository = resolve_repository(Path(repo))
        workspace = Workspace(repository)
        registry = ToolRegistry()
        approval_gate = WriteApprovalGate(ask=approval_ask, record=approval_record)
        shell_tool = ShellTool(
            workspace,
            policy=shell_policy,
            approval_gate=ShellApprovalGate(ask=shell_approval_ask),
            runner=shell_runner,
        )#实例化各个工具并注册
        tools = [
            ListFilesTool(workspace),
            ReadFileTool(workspace),
            SearchTool(workspace),
            FindFilesTool(workspace),
            WriteFileTool(workspace, approval_gate),
            PatchFileTool(workspace, approval_gate),
            shell_tool,
        ]
        for tool in tools:
            if allowed_tools is not None and tool.name not in allowed_tools:
                continue
            registry.register(
                tool.name,
                tool.execute,
                tool.parameters,
                description=tool.description,
            )
        client_factory = llm_client_factory or LLMClient
        if llm_client is None:
            client = client_factory(
                api_key=settings.api_key,
                model=settings.model,
                base_url=settings.base_url,
                timeout=settings.timeout,
            )
        else:
            client = llm_client
        loop = AgentLoop(
            client,
            registry,
            repository_context_builder,
            limits=limits,
        )
        return cls(loop, workspace, registry)

    @property
    def limits(self) -> AgentLimits:
        return self.loop.limits

    def ask(self, query: str, *, recorder: RunRecorder | None = None) -> str:
        sink = RecorderEventSink(recorder) if recorder is not None else NullEventSink()
        return self.loop.run(query, self.workspace, event_sink=sink)
