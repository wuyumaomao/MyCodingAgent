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
    assert document["events"][-1] == {"seq": 3, "type": "final_answer", "content": "Done"}


def test_recorder_failure_is_persisted_without_secret(tmp_path):
    recorder = RunRecorder.create("Explain repo", tmp_path / "repo", tmp_path / "runs")
    recorder.fail("provider_error", "Request failed")
    document = json.loads(recorder.trace_path.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["events"][-1]["type"] == "run_failed"
    assert "api-key" not in json.dumps(document).lower()

