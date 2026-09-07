from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Iterator, Literal


ApprovalDecision = Literal["approved", "denied", "required"]


_current_event_sink: ContextVar[object | None] = ContextVar(
    "coding_agent_current_event_sink", default=None
)


@contextmanager
def event_sink_context(sink: object) -> Iterator[None]:
    """Bind the current run's event sink while a tool executes."""
    token = _current_event_sink.set(sink)
    try:
        yield
    finally:
        _current_event_sink.reset(token)


def emit_current_event(event_type: str, **payload: object) -> None:
    """Emit an event to the sink bound to the currently executing tool."""
    sink = _current_event_sink.get()
    if sink is not None:
        sink.emit(event_type, **payload)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class WritePreview:
    """The information shown to a user before a write is performed."""

    operation: str
    path: str
    content: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    existed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class WriteApprovalGate:
    """Require an explicit decision for each write-tool invocation."""

    def __init__(
        self,
        ask: Callable[[WritePreview], bool] | None = None,
        record: Callable[..., None] | None = None,
    ) -> None:
        self._ask = ask
        self._record = record

    def approve(self, preview: WritePreview) -> ApprovalDecision:
        self._emit(
            "approval_request",
            operation=preview.operation,
            path=preview.path,
            existed=preview.existed,
            report_payload={"preview": preview.as_dict()},
        )
        if self._ask is None:
            decision: ApprovalDecision = "required"
        else:
            try:
                decision = "approved" if self._ask(preview) else "denied"
            except Exception:
                decision = "required"
        self._emit(
            "approval_result",
            operation=preview.operation,
            path=preview.path,
            decision=decision,
        )
        return decision

    def _emit(self, event_type: str, **payload: object) -> None:
        if _current_event_sink.get() is not None:
            emit_current_event(event_type, **payload)
            return
        if self._record is not None:
            self._record(event_type, **payload)
