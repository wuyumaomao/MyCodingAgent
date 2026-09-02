from __future__ import annotations

import json

from coding_agent.trace import RunRecorder


def test_recorder_creates_run_and_persists_started_event(tmp_path):
    recorder = RunRecorder.create("Explain repo", tmp_path / "repo", tmp_path / "runs")
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert recorder.trace_path.parent.parent == tmp_path / "runs"
    assert document["status"] == "running"
    assert document["query"] == "Explain repo"
    assert document["events"][0]["type"] == "run_started"


def test_recorder_appends_events_and_completes(tmp_path):
    recorder = RunRecorder.create("Explain repo", tmp_path / "repo", tmp_path / "runs")
    recorder.record("tool_call", name="readfile", arguments={"path": "README.md"})
    recorder.complete("Done")
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert document["status"] == "completed"
    assert document["events"][-1]["type"] == "final_answer"
    assert document["events"][-1]["content"] == "Done"
    assert "timestamp" in document["events"][-1]
    assert "duration_ms" in document


def test_recorder_failure_is_persisted_without_secret(tmp_path):
    recorder = RunRecorder.create("Explain repo", tmp_path / "repo", tmp_path / "runs")
    recorder.fail("provider_error", "Request failed")
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["events"][-1]["type"] == "run_failed"
    assert "api-key" not in json.dumps(document).lower()


def test_debug_event_contains_payload_only_when_enabled(tmp_path):
    recorder = RunRecorder.create("q", tmp_path / "repo", tmp_path / "runs", debug=True)
    recorder.record_debug("llm_request", messages=[{"role": "user", "content": "q"}])
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert document["events"][-1]["messages"][0]["content"] == "q"


def test_span_events_include_parent_and_duration(tmp_path):
    recorder = RunRecorder.create("q", tmp_path / "repo", tmp_path / "runs")
    with recorder.span("AgentLoop", "run") as parent:
        with recorder.span("LLMClient", "complete") as child:
            assert child.parent_span_id == parent.span_id
    events = json.loads(recorder.trace_path.read_text(encoding="utf-8"))["events"]
    assert [event["type"] for event in events[-4:]] == ["span_start", "span_start", "span_end", "span_end"]
    assert events[-1]["status"] == "ok"
    assert "duration_ms" in events[-1]
    assert "started_at" in events[-1]
    assert "ended_at" in events[-1]
