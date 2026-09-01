from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import tempfile
from typing import Any, Literal


RunStatus = Literal["running", "completed", "failed"]


class TraceWriteError(RuntimeError):
    """Raised when a trace cannot be persisted safely."""


@dataclass
class RunRecorder:
    run_id: str
    trace_path: Path
    _document: dict[str, Any]

    @property
    def status(self) -> RunStatus:
        return self._document["status"]

    @classmethod
    def create(
        cls,
        query: str,
        repo_root: Path,
        runs_root: Path | None = None,
    ) -> "RunRecorder":
        if runs_root is None:
            project_root = Path(__file__).resolve().parents[2]
            runs_root = project_root / ".coding-agent" / "runs"
        runs_root = Path(runs_root).expanduser().resolve()
        run_id = f"{_utc_now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        recorder = cls(
            run_id=run_id,
            trace_path=run_dir / "trace.json",
            _document={
                "run_id": run_id,
                "query": query,
                "repo_root": str(Path(repo_root).expanduser().resolve()),
                "status": "running",
                "started_at": _utc_now().isoformat().replace("+00:00", "Z"),
                "ended_at": None,
                "events": [],
            },
        )
        recorder.record("run_started")
        return recorder

    def record(self, event_type: str, **payload: object) -> None:
        event: dict[str, Any] = {
            "seq": len(self._document["events"]) + 1,
            "type": event_type,
        }
        event.update(_sanitize(payload))
        self._document["events"].append(event)
        self._persist()

    def complete(self, answer: str) -> None:
        self._document["status"] = "completed"
        self._document["ended_at"] = _utc_now().isoformat().replace("+00:00", "Z")
        self.record("final_answer", content=answer)

    def fail(self, error_type: str, message: str) -> None:
        self._document["status"] = "failed"
        self._document["ended_at"] = _utc_now().isoformat().replace("+00:00", "Z")
        self.record("run_failed", error_type=error_type, message=message)

    def _persist(self) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.trace_path.parent,
                prefix=".trace-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(self._document, temporary, ensure_ascii=False, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(self.trace_path)
        except OSError as exc:
            raise TraceWriteError("Unable to persist run trace") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sanitize(value: object) -> object:
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(marker in key_text.lower() for marker in ("api_key", "authorization", "token", "secret")):
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = _sanitize(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value
