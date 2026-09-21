from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import platform
import sys
from typing import Any

from .agent import AgentLimits, AgentLoop
from .config import Settings
from .context import build_repository_context
from .events import NullEventSink, RecorderEventSink
from .llm import LLMClient
from .memory import LLMFileSummaryProvider, LLMSpanSummaryProvider
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
from .session import SessionState, SessionStore, update_runtime_identity


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
        session: SessionState | None = None,
        session_store: SessionStore | None = None,
        transcript_budget_chars: int | None = None,
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
        if session is not None:
            # Store only facts needed for safe recovery; never persist API credentials.
            update_runtime_identity(
                session,
                model=getattr(settings, "model", None),
                context_window_tokens=settings.context_window_tokens,
            )
            if session_store is not None:
                session_store.save(session)
        loop = AgentLoop(
            client,
            registry,
            repository_context_builder,
            limits=limits,
            session=session,
            session_store=session_store,
            summary_provider=(LLMFileSummaryProvider(client.complete_text) if hasattr(client, "complete_text") else None),
            compaction_summarizer=(LLMSpanSummaryProvider(client.complete_text) if hasattr(client, "complete_text") else None),
            transcript_budget_chars=transcript_budget_chars,
            context_window_tokens=settings.context_window_tokens,
            runtime_identity={
                "repo_root": str(workspace.root),
                "platform": sys.platform,
                "python": platform.python_version(),
                "model": settings.model,
                **({"context_window_tokens": settings.context_window_tokens} if settings.context_window_tokens is not None else {}),
            },
        )
        return cls(loop, workspace, registry)

    @property
    def limits(self) -> AgentLimits:
        return self.loop.limits

    def ask(self, query: str, *, recorder: RunRecorder | None = None, session: SessionState | None = None) -> str:
        if session is not None and session is not self.loop.session:
            self.loop.session = session
            self.loop.session_store = SessionStore(self.workspace)
            update_runtime_identity(
                session,
                model=getattr(self.loop.llm_client, "model", None),
                context_window_tokens=self.loop.context_window_tokens,
            )
            self.loop.session_store.save(session)
        sink = RecorderEventSink(recorder) if recorder is not None else NullEventSink()
        return self.loop.run(query, self.workspace, event_sink=sink)
