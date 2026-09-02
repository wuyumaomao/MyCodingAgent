from __future__ import annotations

import json

from coding_agent.trace import RunRecorder


def test_recorder_creates_run_and_persists_started_event(tmp_path):
    recorder = RunRecorder.create("Explain repo", tmp_path / "repo", tmp_path / "runs")
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert recorder.trace_path.parent.parent == tmp_path / "runs"
    assert recorder.report_path.name == "report.json"
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
    assert "duration_ms" in document


def test_recorder_failure_is_persisted_without_secret(tmp_path):
    recorder = RunRecorder.create("Explain repo", tmp_path / "repo", tmp_path / "runs")
    recorder.fail("provider_error", "Request failed")
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["events"][-1]["type"] == "run_failed"
    assert "api-key" not in json.dumps(document).lower()


def test_recorder_persists_summary_trace_and_detailed_report(tmp_path):
    recorder = RunRecorder.create("q", tmp_path / "repo", tmp_path / "runs")
    recorder.record(
        "llm_request",
        round=1,
        message_count=3,
        report_payload={"messages": [{"role": "user", "content": "q"}]},
    )
    trace = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    report = json.loads(recorder.report_path.read_text(encoding="utf-8"))
    assert "messages" not in trace["events"][-1]
    assert report["events"][-1]["messages"][0]["content"] == "q"


def test_trace_uses_step_duration_without_span_timestamps(tmp_path):
    recorder = RunRecorder.create("q", tmp_path / "repo", tmp_path / "runs")
    recorder.record("llm_response", duration_ms=12.5)
    event = json.loads(recorder.trace_path.read_text(encoding="utf-8"))["events"][-1]
    assert event["duration_ms"] == 12.5
    assert "started_at" not in event
    assert "ended_at" not in event
    assert all(item["type"] not in {"span_start", "span_end"} for item in recorder._document["events"])
