from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import tempfile
import os
from typing import Any

from .repository import Workspace

SCHEMA_VERSION = 1
_SESSION_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")


class SessionError(RuntimeError):
    pass


def empty_memory() -> dict[str, Any]:
    # recent_read_files 只记 readfile 成功读过的文件；recent_modified_files 只记写工具
    # 改过的文件。两者都渲染进 [Memory]，所以必须是两个字段。
    return {"working_memory": {"task_summary": "", "constraints": [], "recent_read_files": [], "recent_modified_files": [], "latest_tool_error": None}, "file_summaries": {}, "episodic_notes": []}


@dataclass
class SessionState:
    session_id: str
    repo_root: Path
    history: list[dict[str, Any]] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=empty_memory)
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
        session = SessionState(f"{now_id()}-{secrets.token_hex(3)}", self.workspace.root, created_at=now, updated_at=now)
        self.save(session)
        return session

    def load(self, session_id: str) -> SessionState:
        path = self.path_for(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionError("Unable to load session") from exc
        if payload.get("schema_version") != SCHEMA_VERSION:
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
                created_at=str(payload.get("created_at", "")), updated_at=str(payload.get("updated_at", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionError("Invalid session document") from exc

    def save(self, session: SessionState) -> None:
        if session.repo_root.resolve() != self.workspace.root:
            raise SessionError("Session belongs to a different repository")
        self.root.mkdir(parents=True, exist_ok=True)
        session.updated_at = _now()
        payload = {"schema_version": SCHEMA_VERSION, "session_id": session.session_id, "repo_root": str(session.repo_root), "created_at": session.created_at or session.updated_at, "updated_at": session.updated_at, "history": _sanitize(session.history), "memory": _sanitize(session.memory)}
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
