from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import platform
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import os
from typing import Any

from .repository import Workspace

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
_SESSION_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")


class SessionError(RuntimeError):
    pass


def empty_memory() -> dict[str, Any]:
    # recent_read_files 只记 readfile 成功读过的文件；recent_modified_files 只记写工具
    # 改过的文件。两者都渲染进 [Memory]，所以必须是两个字段。
    return {"working_memory": {"task_summary": "", "constraints": [], "recent_read_files": [], "recent_modified_files": [], "latest_tool_error": None}, "file_summaries": {}, "episodic_notes": []}


def empty_checkpoints() -> dict[str, Any]:
    return {"active": None, "recent": []}


def empty_resume_state() -> dict[str, Any]:
    return {"status": "clean", "checkpoint_id": None, "checked_at": None, "workspace_changed": False, "unresolved_calls": []}


def empty_run_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "run_id": None,
        "query": None,
        "round": 0,
        "reason": None,
        "started_at": None,
        "ended_at": None,
    }


def default_runtime_identity(repo_root: Path) -> dict[str, Any]:
    return {
        "repo_root": str(Path(repo_root).resolve()),
        "platform": sys.platform,
        "python": platform.python_version(),
    }


def update_runtime_identity(
    session: SessionState,
    *,
    provider: str | None = None,
    model: str | None = None,
    context_window_tokens: int | None = None,
) -> None:
    """Record non-secret runtime facts used to decide whether recovery is safe."""
    identity = session.runtime_identity or default_runtime_identity(session.repo_root)
    identity.setdefault("repo_root", str(session.repo_root.resolve()))
    identity.setdefault("platform", sys.platform)
    identity.setdefault("python", platform.python_version())
    # Existing values are evidence from the previous run.  Keep them so the
    # loop can detect a model/context change instead of silently overwriting it.
    if model and "model" not in identity:
        identity["model"] = model
    if provider and "provider" not in identity:
        identity["provider"] = provider
    if context_window_tokens is not None and "context_window_tokens" not in identity:
        identity["context_window_tokens"] = context_window_tokens
    session.runtime_identity = identity


def runtime_identity_mismatches(session: SessionState, current: dict[str, Any] | None) -> list[str]:
    """Return persisted identity fields that differ from the current runtime."""
    if not current:
        return []
    persisted = session.runtime_identity or {}
    mismatches: list[str] = []
    for key in ("repo_root", "platform", "python", "provider", "model", "context_window_tokens"):
        if key in persisted and key in current and persisted[key] != current[key]:
            mismatches.append(key)
    return mismatches


@dataclass
class SessionState:
    session_id: str
    repo_root: Path
    history: list[dict[str, Any]] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=empty_memory)
    checkpoints: dict[str, Any] = field(default_factory=empty_checkpoints)
    resume_state: dict[str, Any] = field(default_factory=empty_resume_state)
    run_state: dict[str, Any] = field(default_factory=empty_run_state)
    runtime_identity: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class SessionStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root = workspace.root / ".coding-agent" / "sessions"

    def path_for(self, session_id: str) -> Path:
        if not _SESSION_ID.fullmatch(session_id):
            raise SessionError("Invalid session ID")
        return self.root / f"{session_id}.json"

    def create(self) -> SessionState:
        now = _now()
        session = SessionState(
            f"{now_id()}-{secrets.token_hex(3)}",
            self.workspace.root,
            created_at=now,
            updated_at=now,
            runtime_identity=default_runtime_identity(self.workspace.root),
        )
        self.save(session)
        return session

    def load(self, session_id: str) -> SessionState:
        path = self.path_for(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionError("Unable to load session") from exc
        if payload.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
            raise SessionError("Unsupported session version")
        if payload.get("session_id") != session_id:
            raise SessionError("Session ID does not match its filename")
        try:
            repo_root = Path(payload["repo_root"]).expanduser().resolve()
            if repo_root != self.workspace.root:
                raise SessionError("Session belongs to a different repository")
            return SessionState(
                session_id=str(payload["session_id"]), repo_root=repo_root,
                history=list(payload.get("history", [])), memory=dict(payload.get("memory", empty_memory())),
                checkpoints=dict(payload.get("checkpoints") or empty_checkpoints()),
                resume_state=dict(payload.get("resume_state") or empty_resume_state()),
                run_state=dict(payload.get("run_state") or empty_run_state()),
                runtime_identity={**default_runtime_identity(repo_root), **dict(payload.get("runtime_identity") or {})},
                created_at=str(payload.get("created_at", "")), updated_at=str(payload.get("updated_at", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError("Invalid session document") from exc

    def save(self, session: SessionState) -> None:
        if session.repo_root.resolve() != self.workspace.root:
            raise SessionError("Session belongs to a different repository")
        self.root.mkdir(parents=True, exist_ok=True)
        session.updated_at = _now()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session.session_id,
            "repo_root": str(session.repo_root),
            "created_at": session.created_at or session.updated_at,
            "updated_at": session.updated_at,
            "history": _sanitize(session.history),
            "memory": _sanitize(session.memory),
            "checkpoints": _sanitize(session.checkpoints),
            "resume_state": _sanitize(session.resume_state),
            "run_state": _sanitize(session.run_state),
            "runtime_identity": _sanitize(session.runtime_identity or default_runtime_identity(session.repo_root)),
        }
        target = self.path_for(session.session_id)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, prefix=".session-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush(); os.fsync(handle.fileno())
            temporary.replace(target)
        except OSError as exc:
            raise SessionError("Unable to save session") from exc
        finally:
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if any(x in str(k).lower() for x in ("api_key", "authorization", "token", "secret")) else _sanitize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def workspace_snapshot(workspace: Workspace, paths: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """Capture small, deterministic evidence about the repository state."""
    head = ""
    status = ""
    try:
        head = subprocess.run(
            ["git", "-C", str(workspace.root), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(workspace.root), "status", "--porcelain", "--untracked-files=all"],
            check=False, capture_output=True, text=True,
        ).stdout
    except OSError:
        pass
    files: dict[str, Any] = {}
    for relative in paths:
        if not isinstance(relative, str):
            continue
        try:
            target = workspace.resolve_relative(relative)
            if not target.exists():
                files[relative] = {"exists": False}
            elif target.is_file():
                files[relative] = {"exists": True, "sha256": _file_sha256(target), "size": target.stat().st_size}
            else:
                files[relative] = {"exists": True, "kind": "directory"}
        except (OSError, ValueError):
            files[relative] = {"exists": False, "unreadable": True}
    return {
        "git_head": head,
        "git_status_sha256": _sha256(status),
        "files": files,
    }


def reconcile_checkpoint(workspace: Workspace, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Reconcile interrupted calls without executing any tool."""
    tool_results: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    workspace_changed = False
    for item in checkpoint.get("calls") or []:
        if not isinstance(item, dict) or item.get("status") in {"completed", "completed_error", "reconciled"}:
            continue
        call_id = str(item.get("id", ""))
        name = str(item.get("name", ""))
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        result = _reconcile_call(workspace, name, arguments, item.get("workspace_before") or {})
        workspace_changed = workspace_changed or _workspace_changed(workspace, arguments, item.get("workspace_before") or {})
        if result.get("recovered") is True:
            item["status"] = "reconciled"
        else:
            item["status"] = "interrupted"
            unresolved.append({"id": call_id, "tool": name, "state": "unknown"})
        tool_results.append({"id": call_id, "name": name, "arguments": arguments, "result": result})
    status = "reconciled" if not unresolved else "resume_required"
    return {
        "tool_results": tool_results,
        "resume_state": {
            "status": status,
            "checkpoint_id": checkpoint.get("id"),
            "checked_at": _now(),
            "workspace_changed": workspace_changed,
            "unresolved_calls": unresolved,
        },
    }


def begin_checkpoint(session: SessionState, run_id: str, round_number: int, calls: list[Any], workspace: Workspace) -> dict[str, Any]:
    checkpoint = {
        "id": f"{run_id}-r{round_number}-{secrets.token_hex(3)}",
        "run_id": run_id,
        "round": round_number,
        "phase": "tool_execution",
        "status": "pending",
        "history_cursor": len(session.history),
        "calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": dict(call.arguments),
                "status": "pending",
                "workspace_before": {},
            }
            for call in calls
        ],
        "created_at": _now(),
        "updated_at": _now(),
    }
    session.checkpoints["active"] = checkpoint
    return checkpoint


def mark_checkpoint_running(session: SessionState, call_id: str, workspace: Workspace, arguments: dict[str, Any]) -> None:
    active = session.checkpoints.get("active")
    if not isinstance(active, dict):
        return
    for item in active.get("calls") or []:
        if item.get("id") == call_id:
            path = arguments.get("path")
            paths = [path] if isinstance(path, str) else []
            item["status"] = "running"
            item["workspace_before"] = workspace_snapshot(workspace, paths)
            active["status"] = "running"
            active["updated_at"] = _now()
            return


def mark_checkpoint_result(session: SessionState, call_id: str, result: dict[str, Any]) -> None:
    active = session.checkpoints.get("active")
    if not isinstance(active, dict):
        return
    for item in active.get("calls") or []:
        if item.get("id") == call_id:
            item["status"] = "completed" if result.get("ok") is True else "completed_error"
            item["result_type"] = (result.get("error") or {}).get("type") if isinstance(result.get("error"), dict) else None
            item["updated_at"] = _now()
            active["updated_at"] = item["updated_at"]
            return


def finish_checkpoint(session: SessionState, *, status: str = "completed") -> None:
    active = session.checkpoints.get("active")
    if not isinstance(active, dict):
        return
    active["status"] = status
    active["updated_at"] = _now()
    recent = session.checkpoints.setdefault("recent", [])
    recent.append(active)
    del recent[:-10]
    session.checkpoints["active"] = None
    session.resume_state = empty_resume_state()


def cancel_checkpoint(session: SessionState, reason: str = "keyboard_interrupt") -> dict[str, Any] | None:
    """Mark an active checkpoint as user-cancelled without replaying tools."""
    active = session.checkpoints.get("active")
    if not isinstance(active, dict):
        return None
    for item in active.get("calls") or []:
        if not isinstance(item, dict) or item.get("status") in {"completed", "completed_error", "reconciled"}:
            continue
        item["status"] = "interrupted" if item.get("status") == "running" else "cancelled"
        item["cancel_reason"] = reason
        item["updated_at"] = _now()
    active["status"] = "cancelled"
    active["cancel_reason"] = reason
    active["updated_at"] = _now()
    return active


def _reconcile_call(workspace: Workspace, name: str, arguments: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    path = arguments.get("path")
    if not isinstance(path, str):
        return _interrupted_result("Tool arguments were incomplete when the run stopped")
    try:
        target = workspace.resolve_relative(path)
        content = target.read_text(encoding="utf-8") if target.is_file() else None
    except (OSError, UnicodeError, ValueError):
        content = None
    if name == "write_file" and isinstance(arguments.get("content"), str):
        if content == arguments["content"]:
            return {"ok": True, "path": path, "operation": "reconciled", "recovered": True}
        return _interrupted_result("write_file may have run, but the target content cannot be confirmed")
    if name == "patch_file" and isinstance(content, str):
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if isinstance(old_text, str) and isinstance(new_text, str) and old_text not in content and content.count(new_text) == 1:
            return {"ok": True, "path": path, "operation": "reconciled", "recovered": True}
    return _interrupted_result("Previous tool execution was interrupted; workspace state is unknown")


def _workspace_changed(workspace: Workspace, arguments: dict[str, Any], before: dict[str, Any]) -> bool:
    """Compare the small pre-call snapshot without treating an absent snapshot as a change."""
    if not before:
        return False
    path = arguments.get("path")
    paths = [path] if isinstance(path, str) else []
    after = workspace_snapshot(workspace, paths)
    for key in ("git_head", "git_status_sha256"):
        if before.get(key) and before.get(key) != after.get(key):
            return True
    before_file = (before.get("files") or {}).get(path) if isinstance(path, str) else None
    after_file = (after.get("files") or {}).get(path) if isinstance(path, str) else None
    return before_file is not None and before_file != after_file


def _interrupted_result(message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": "execution_interrupted", "message": message}}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
