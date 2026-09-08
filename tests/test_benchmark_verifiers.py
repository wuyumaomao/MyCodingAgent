from __future__ import annotations

import json

from coding_agent.benchmark.verifiers import verify_artifact, verify_trace


def test_artifact_verifiers_check_file_content_and_existence(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("hello benchmark\n", encoding="utf-8")
    assert verify_artifact({"type": "contains", "path": "README.md", "text": "benchmark"}, tmp_path).passed
    assert verify_artifact({"type": "equals", "path": "README.md", "text": "hello benchmark\n"}, tmp_path).passed
    assert verify_artifact({"type": "exists", "path": "README.md"}, tmp_path).passed
    assert verify_artifact({"type": "not_contains", "path": "README.md", "text": "missing"}, tmp_path).passed


def test_trace_verifier_checks_order_and_error_types(tmp_path):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"events": [
        {"type": "tool_result", "ok": False, "result": {"error": {"type": "invalid_tool_arguments"}}},
        {"type": "llm_request", "prompt_chars": 10},
    ]}), encoding="utf-8")
    result = verify_trace([
        {"type": "error_type", "error_type": "invalid_tool_arguments"},
        {"type": "event_after", "before": "invalid_tool_arguments", "after": "llm_request"},
    ], trace)
    assert result.passed


def test_verifiers_reject_workspace_escape(tmp_path):
    result = verify_artifact({"type": "exists", "path": "../outside.txt"}, tmp_path)
    assert not result.passed
