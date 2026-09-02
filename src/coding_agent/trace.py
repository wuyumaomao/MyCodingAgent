from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Any, Iterator, Literal


RunStatus = Literal["running", "completed", "failed"]


class TraceWriteError(RuntimeError):
    """Raised when a trace cannot be persisted safely."""


@dataclass(frozen=True)
class SpanHandle:
    span_id: str
    parent_span_id: str | None


@dataclass
class RunRecorder:
    run_id: str
    trace_path: Path
    _document: dict[str, Any]
    debug: bool = False
    _span_stack: list[SpanHandle] = field(default_factory=list, repr=False)
    _started_monotonic: float = field(default_factory=time.perf_counter, repr=False)

    @property
    def status(self) -> RunStatus:
        return self._document["status"]

    @classmethod
    def create(
        cls,
        query: str,
        repo_root: Path,
        runs_root: Path | None = None,
        *,
        debug: bool = False,
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
            debug=debug,
        )
        recorder.record("run_started")
        return recorder

    def record(self, event_type: str, **payload: object) -> None:
        event: dict[str, Any] = {
            "seq": len(self._document["events"]) + 1,
            "type": event_type,
            "timestamp": _timestamp(),
        }
        event.update(_sanitize(payload))
        self._document["events"].append(event)
        self._persist()

    def record_debug(self, event_type: str, **payload: object) -> None:
        """Record payload details only when debug tracing is enabled."""

        if self.debug:
            self.record(event_type, **payload)
        else:
            self.record(event_type)

    @contextmanager
    def span(self, component: str, operation: str) -> Iterator[SpanHandle]:
        parent = self._span_stack[-1] if self._span_stack else None
        handle = SpanHandle(
            span_id=f"span-{secrets.token_hex(4)}",
            parent_span_id=parent.span_id if parent else None,
        )
        started = time.perf_counter()
        started_at = _timestamp()
        self._span_stack.append(handle)
        self.record(
            "span_start",
            span_id=handle.span_id,
            parent_span_id=handle.parent_span_id,
            component=component,
            operation=operation,
            started_at=started_at,
        )
        status = "ok"
        error_type: str | None = None
        try:
            yield handle
        except BaseException as exc:
            status = "error"
            error_type = type(exc).__name__
            raise
        finally:
            self._span_stack.pop()
            payload: dict[str, object] = {
                "span_id": handle.span_id,
                "parent_span_id": handle.parent_span_id,
                "component": component,
                "operation": operation,
                "status": status,
                "started_at": started_at,
                "ended_at": _timestamp(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            if error_type is not None:
                payload["error_type"] = error_type
            self.record("span_end", **payload)

    def complete(self, answer: str) -> None:
        self._document["status"] = "completed"
        self._document["ended_at"] = _utc_now().isoformat().replace("+00:00", "Z")
        self._document["duration_ms"] = round(
            (time.perf_counter() - self._started_monotonic) * 1000, 3
        )
        self.record("final_answer", content=answer)

    def fail(self, error_type: str, message: str) -> None:
        self._document["status"] = "failed"
        self._document["ended_at"] = _utc_now().isoformat().replace("+00:00", "Z")
        self._document["duration_ms"] = round(
            (time.perf_counter() - self._started_monotonic) * 1000, 3
        )
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


def _timestamp() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


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
