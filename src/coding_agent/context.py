from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from .filesystem import FileEntry, scan_files
from .models import AssistantTurn, ToolCall
from .memory import MemoryManager
from .repository import Workspace
from .tools.target_environment import resolve_target_python, target_venv_dir


SYSTEM_PROMPT = (
    "You are a  coding agent. Use only the provided tools, "
    "stay inside the repository workspace, and explain findings based on evidence. "
    "If python or pytest fails because target dependencies are missing, request approval "
    "to run uv sync --dev, then retry the command. "
    "For file writes or patches, directly call write_file or patch_file. "
    "Do not ask for approval in ordinary text; those tools automatically request user approval. "
    "When multiple known files are independent, prefer returning multiple tool calls in the same response "
    "instead of reading one file per round. "
    "Compressed historical tool results are not complete evidence; when details are missing, call readfile "
    "again with start and end line numbers. When using search, prefer the smallest known directory "
    "such as src or tests; use the repository root only when the location is unknown."
)

_IMPORTANT_FILES = (
    "README.md",
    "pyproject.toml",
    "package.json",
    "AGENTS.md",
    ".env.example",
)
_ENTRYPOINT_NAMES = ("main.py", "app.py", "run.py", "cli.py")

REPOSITORY_INSTRUCTION_FILE = "AGENTS.md"
REPOSITORY_INSTRUCTION_MAX_BYTES = 32768
REPOSITORY_INSTRUCTION_HEADER = "[Repository Instructions]"


def build_repository_instructions(workspace: Workspace, max_bytes: int = REPOSITORY_INSTRUCTION_MAX_BYTES) -> str:
    """Load the target repository's own conventions for the agent.

    The file belongs to the repository rather than to this program, so pointing
    ``--repo`` at another checkout brings that project's conventions without any
    code change here. A missing file is normal and yields an empty string.
    """
    path = workspace.root / REPOSITORY_INSTRUCTION_FILE
    try:
        if not path.is_file() or path.is_symlink():
            return ""
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        return ""
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", errors="ignore").strip()
    if not text:
        return ""
    header = f"{REPOSITORY_INSTRUCTION_HEADER}\nsource: {REPOSITORY_INSTRUCTION_FILE}"
    if truncated:
        header += f"\ntruncated: true (only the first {max_bytes} bytes are included)"
    return f"{header}\n\n{text}"


class ConversationContext:
    """Build static system messages and maintain the per-run message history."""

    def __init__(
        self,
        workspace: Workspace,
        repository_context_builder: Callable[[Workspace], str] | None = None,
        memory: MemoryManager | None = None,
        transcript_budget_chars: int = 12000,
        tool_result_budget_chars: int = 2000,
        result_summary_provider: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        if repository_context_builder is None:#在这里扫描整个仓库
            repository_context_builder = build_repository_context
        self.system_messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": repository_context_builder(workspace)},
        ]
        # Repository conventions belong to the target repository. Read once per
        # run and kept in the static prefix so the prompt cache stays valid.
        instructions = build_repository_instructions(workspace)
        if instructions:
            self.system_messages.append({"role": "system", "content": instructions})
        self.history: list[dict[str, Any]] = []
        self.memory = memory
        self.transcript_budget_chars = transcript_budget_chars
        if tool_result_budget_chars <= 0:
            raise ValueError("tool_result_budget_chars must be positive")
        self.tool_result_budget_chars = tool_result_budget_chars
        self.result_summary_provider = result_summary_provider
        # One run reuses the same history every round, so summarize each tool
        # result at most once instead of re-calling the model on every prompt.
        self._result_summary_cache: dict[str, dict[str, Any] | None] = {}

    def add_user_request(self, query: str) -> None:
        self.history.append({"role": "user", "content": query})

    def add_assistant_turn(self, turn: AssistantTurn) -> None:
        self.history.append(
            {
                "role": "assistant",
                "content": turn.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in turn.tool_calls
                ],
            }
        )

    def add_tool_result(self, call: ToolCall, result: dict[str, Any]) -> None:
        self.history.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    def messages(self, query: str | None = None) -> list[dict[str, Any]]:
        if self.memory is None:
            return copy.deepcopy(self.system_messages + self.history)
        memory_message = {"role": "system", "content": self.memory.render_memory()}
        relevant_message = {"role": "system", "content": self.memory.render_relevant_memory(query or "")}
        file_summary_message = {"role": "system", "content": self.memory.render_file_summaries(query or "")}
        reduced = _reduce_history(self.history, self.transcript_budget_chars, self.memory, self.tool_result_budget_chars, self.result_summary_provider, self._result_summary_cache)
        result = self.system_messages + [memory_message, relevant_message, file_summary_message] + reduced
        if query is not None and not _contains_user_request(result, query):
            # Trimming drops the oldest groups first, and the request being
            # answered is the oldest group of all. Re-append it so the model is
            # never asked to continue without the question itself.
            result.append({"role": "user", "content": query})
        return copy.deepcopy(result)

    def prompt_metrics(self, messages: list[dict[str, Any]]) -> dict[str, int]:
        """Measure an already-built prompt; rebuilding it would re-run the summarizer.

        The memory blocks follow the static system messages, whose count is not
        fixed (repository instructions are appended only when the target
        repository provides them), so offsets are derived rather than hardcoded.
        """
        static = len(self.system_messages)

        def block(offset: int) -> int:
            position = static + offset
            return len(str(messages[position].get("content", ""))) if len(messages) > position else 0

        return {"memory_chars": block(0), "relevant_memory_chars": block(1), "file_summary_chars": block(2), "transcript_chars": len(json.dumps(messages[static + 3 :], ensure_ascii=False)), "prompt_chars": len(json.dumps(messages, ensure_ascii=False))}


def _contains_user_request(messages: list[dict[str, Any]], query: str) -> bool:
    """Return whether the assembled prompt already carries the current request."""
    return any(
        message.get("role") == "user" and message.get("content") == query
        for message in messages
    )


def _reduce_history(history: list[dict[str, Any]], budget: int, memory: MemoryManager, tool_result_budget: int = 2000, result_summary_provider: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None, summary_cache: dict[str, dict[str, Any] | None] | None = None) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(history):
        message = history[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            group = [message]
            ids = {call.get("id") for call in message.get("tool_calls", [])}
            index += 1
            while index < len(history) and history[index].get("role") == "tool" and history[index].get("tool_call_id") in ids:
                group.append(history[index]); index += 1
            groups.append(group)
        else:
            groups.append([message]); index += 1
    selected: list[list[dict[str, Any]]] = []
    used = 0
    for group in reversed(groups):
        encoded = len(json.dumps(group, ensure_ascii=False))
        candidate = group
        oversized_result = any(
            message.get("role") == "tool" and len(str(message.get("content", ""))) > tool_result_budget
            for message in group
        )
        if oversized_result or used + encoded > budget:
            candidate = _compact_group(group, memory, tool_result_budget, result_summary_provider, summary_cache)
            encoded = len(json.dumps(candidate, ensure_ascii=False))
        if used + encoded <= budget:
            selected.insert(0, candidate); used += encoded
    return [message for group in selected for message in group]


def _compact_group(group: list[dict[str, Any]], memory: MemoryManager, tool_result_budget: int = 2000, result_summary_provider: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None, summary_cache: dict[str, dict[str, Any] | None] | None = None) -> list[dict[str, Any]]:
    compacted = copy.deepcopy(group)
    for message in compacted:
        if message.get("role") != "tool":
            continue
        try:
            result = json.loads(message.get("content", "{}"))
        except (TypeError, json.JSONDecodeError):
            result = {"ok": False}
        name = next((call.get("function", {}).get("name") for call in compacted if call.get("role") == "assistant" for call in call.get("tool_calls", []) if call.get("id") == message.get("tool_call_id")), "tool")
        provider_result = None
        if result_summary_provider is not None and len(message.get("content", "")) > tool_result_budget:
            cache_key = str(message.get("tool_call_id"))
            if summary_cache is not None and cache_key in summary_cache:
                provider_result = summary_cache[cache_key]
            else:
                try:
                    summarized = result_summary_provider(name, result)
                    if isinstance(summarized, dict) and ("ok" in summarized or "observation" in summarized):
                        provider_result = summarized
                except Exception:
                    provider_result = None
                if summary_cache is not None:
                    # Cache failures too: a provider that fails once should not
                    # be retried on every following round of the same run.
                    summary_cache[cache_key] = provider_result
        compressed = ({"compressed": True, **provider_result} if provider_result is not None else _compact_result(name, result, memory))
        message["content"] = _fit_json(compressed, tool_result_budget)
    return compacted


def _fit_json(value: dict[str, Any], budget: int) -> str:
    """Keep a compressed result valid JSON while respecting its character quota."""
    candidate = copy.deepcopy(value)
    for key in ("stderr_preview", "stdout_preview", "sample_matches", "sample_paths", "next_action_hint", "summary"):
        encoded = json.dumps(candidate, ensure_ascii=False)
        if len(encoded) <= budget:
            return encoded
        candidate.pop(key, None)
    encoded = json.dumps(candidate, ensure_ascii=False)
    if len(encoded) <= budget:
        return encoded
    if "observation" in candidate:
        candidate["observation"] = str(candidate["observation"])[: max(0, budget // 3)]
        encoded = json.dumps(candidate, ensure_ascii=False)
        if len(encoded) <= budget:
            return encoded
    # Minimal valid payload; a marker makes the loss explicit to the model.
    minimal = {"ok": candidate.get("ok", False), "compressed": True, "truncated": True}
    return json.dumps(minimal, ensure_ascii=False)


def _compact_result(name: str, result: dict[str, Any], memory: MemoryManager) -> dict[str, Any]:
    if name == "readfile":
        path = result.get("path")
        summary = memory.data.get("file_summaries", {}).get(path, {}) if isinstance(path, str) else {}
        return {"ok": result.get("ok", False), "path": path, "compressed": True, "summary": summary.get("summary"), "start": result.get("start"), "end": result.get("end"), "line_count": result.get("line_count"), "next_action_hint": "需要具体代码时，重新调用 readfile 并提供 start 和 end。"}
    if name == "search":
        matches = result.get("matches", []) if isinstance(result.get("matches"), list) else []
        return {"ok": result.get("ok", False), "compressed": True, "match_count": len(matches), "sample_matches": [{"path": m.get("path"), "line": m.get("line")} for m in matches[:5] if isinstance(m, dict)], "truncated": result.get("truncated", False)}
    if name == "shell":
        output = {"ok": result.get("ok", False), "compressed": True, **{key: result.get(key) for key in ("exit_code", "timed_out", "output_truncated")}, "stdout_preview": str(result.get("stdout", ""))[:500], "stderr_preview": str(result.get("stderr", ""))[:500]}
        if isinstance(result.get("observation"), str):
            output["observation"] = result["observation"]
        return output
    if name in {"listfiles", "find_files"}:
        values = result.get("files", []) if isinstance(result.get("files"), list) else []
        return {"ok": result.get("ok", False), "compressed": True, "file_count": len(values), "sample_paths": values[:10], "truncated": result.get("truncated", False)}
    output = {"ok": result.get("ok", False), "compressed": True}
    if isinstance(result.get("observation"), str):
        output["observation"] = result["observation"]
    if isinstance(result.get("path"), str): output["path"] = result["path"]
    if isinstance(result.get("operation"), str): output["operation"] = result["operation"]
    if isinstance(result.get("error"), dict): output["error_type"] = result["error"].get("type")
    for key in ("bytes_written", "replacements"):
        if key in result: output[key] = result[key]
    return output


def build_repository_context(
    workspace: Workspace,
    max_depth: int = 4,
    max_entries: int = 200,
) -> str:
    entries, truncated = scan_files(
        workspace,
        max_depth=max_depth,
        max_entries=max_entries,
    )
    lines = [
        "[Repository Context]",
        "root: <workspace>",
        "important_files:",
    ]
    # Keep the first prompt as a compact navigation map.  The model can use
    # readfile/listfiles/search to discover details on demand instead of
    # receiving every file in the repository manifest.
    important_paths = _important_file_paths(entries)
    for path in important_paths:
        lines.append(f"- {path}")
    lines.extend(_candidate_dirs_context(entries))
    lines.extend(_ignored_runtime_dirs_context())
    lines.append(f"truncated: {str(truncated).lower()}")
    lines.extend(_important_files_context(workspace, entries))
    lines.extend(_git_status_context(workspace))
    lines.extend(_runtime_context(workspace))
    return "\n".join(lines)


def _important_file_paths(entries: list[FileEntry]) -> list[str]:
    """Return stable, high-signal files for the navigation map."""

    paths = list(_IMPORTANT_FILES)
    for entry in entries:
        if entry.kind != "file":
            continue
        name = Path(entry.path).name
        if name == "__main__.py" or name in _ENTRYPOINT_NAMES:
            if entry.path not in paths:
                paths.append(entry.path)
    return paths


def _candidate_dirs_context(entries: list[FileEntry]) -> list[str]:
    # Only advertise top-level directories.  Nested structure is intentionally
    # left to listfiles/search so the initial prompt remains small.
    preferred = ("src", "tests", "docs", "scripts", "examples", "tools")
    top_level = {
        entry.path.rstrip("/")
        for entry in entries
        if entry.kind == "directory" and "/" not in entry.path.rstrip("/")
    }
    ordered = [name for name in preferred if name in top_level]
    ordered.extend(sorted(top_level - set(ordered)))
    lines = ["", "candidate_dirs:"]
    lines.extend(f"- {name}/" for name in ordered[:8])
    if not ordered:
        lines.append("- (none detected)")
    return lines


def _ignored_runtime_dirs_context() -> list[str]:
    ignored = (".git", ".coding-agent", ".codex", ".venv", ".pytest_cache", "__pycache__", "node_modules", "dist", "build")
    return ["", "ignored_runtime_dirs:", *(f"- {name}/" for name in ignored), "", "navigation_hint:", "Use readfile for known files.", "Use search with the narrowest known directory.", "Use listfiles only when more structure is needed."]


def _runtime_context(workspace: Workspace) -> list[str]:
    venv = target_venv_dir(workspace)
    python = resolve_target_python(workspace)
    lines = ["", "[Runtime Environment]"]
    lines.append(f"target_venv: {'present' if venv.is_dir() else 'missing'}")
    lines.append(f"target_python: {python.relative_to(workspace.root).as_posix() if python else 'unavailable'}")
    lines.append("dependency_sync: approve 'uv sync --dev' when target dependencies are missing")
    return lines


def _important_files_context(workspace: Workspace, entries: list[FileEntry]) -> list[str]:
    paths = list(_IMPORTANT_FILES)
    paths.extend(
        entry.path
        for entry in entries
        if entry.kind == "file"
        and Path(entry.path).name == "__main__.py"
        and entry.path not in paths
    )
    paths.extend(
        entry.path
        for entry in entries
        if entry.kind == "file"
        and Path(entry.path).name in _ENTRYPOINT_NAMES
        and entry.path not in paths
    )

    lines = ["", "[Important Files]"]
    for path in paths:
        lines.append(f"### {path}")
        file_path = workspace.root / path
        if not file_path.is_file() or file_path.is_symlink():
            lines.append("status: missing")
            continue
        try:
            file_path.stat()
        except OSError:
            lines.append("status: unreadable")
            continue
        lines.append("status: present")
    return lines


def _git_status_context(workspace: Workspace) -> list[str]:
    lines = ["", "[Git Status]"]
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace.root), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        lines.append("status: unavailable")
        return lines
    status = result.stdout.strip()
    lines.append(status if status else "clean")
    return lines
