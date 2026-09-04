from __future__ import annotations

from typing import Any, Protocol


class EventSink(Protocol):
    """Minimal event boundary used by one Agent run."""

    def emit(self, event_type: str, **payload: object) -> None: ...

    def complete(self, answer: str) -> None: ...

    def fail(
        self,
        error_type: str,
        message: str,
        *,
        duration_ms: float | None = None,
    ) -> None: ...


class NullEventSink:
    """Discard run events when persistence is not requested."""

    def emit(self, event_type: str, **payload: object) -> None:
        return None

    def complete(self, answer: str) -> None:
        return None

    def fail(
        self,
        error_type: str,
        message: str,
        *,
        duration_ms: float | None = None,
    ) -> None:
        return None


class RecorderEventSink:
    """Adapt a RunRecorder to the per-run EventSink contract."""

    def __init__(self, recorder: Any) -> None:
        self._recorder = recorder
        self._finished = False

    def emit(self, event_type: str, **payload: object) -> None:
        if not self._finished:
            self._recorder.record(event_type, **payload)

    def complete(self, answer: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._recorder.complete(answer)

    def fail(
        self,
        error_type: str,
        message: str,
        *,
        duration_ms: float | None = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        if duration_ms is None:
            self._recorder.fail(error_type, message)
        else:
            self._recorder.fail(error_type, message, duration_ms=duration_ms)
