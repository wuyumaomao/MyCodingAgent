from __future__ import annotations

from coding_agent.events import NullEventSink, RecorderEventSink


class FakeRecorder:
    def __init__(self):
        self.calls = []

    def record(self, event_type, **payload):
        self.calls.append(("record", event_type, payload))

    def complete(self, answer):
        self.calls.append(("complete", answer))

    def fail(self, error_type, message, duration_ms=None):
        self.calls.append(("fail", error_type, message, duration_ms))


def test_null_event_sink_is_a_noop():
    sink = NullEventSink()
    sink.emit("llm_request", round=1)
    sink.complete("done")
    sink.fail("provider_error", "failed")


def test_recorder_event_sink_forwards_events_and_terminal_state_once():
    recorder = FakeRecorder()
    sink = RecorderEventSink(recorder)

    sink.emit("llm_request", round=1, report_payload={"messages": []})
    sink.complete("done")
    sink.complete("ignored")
    sink.fail("provider_error", "ignored")

    assert recorder.calls == [
        ("record", "llm_request", {"round": 1, "report_payload": {"messages": []}}),
        ("complete", "done"),
    ]
