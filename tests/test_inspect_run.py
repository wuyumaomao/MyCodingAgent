from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path


def load_inspector():
    path = Path(__file__).parents[1] / "scripts" / "inspect_run.py"
    spec = importlib.util.spec_from_file_location("inspect_run", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_run(run_dir: Path) -> None:
    trace = {
        "run_id": "run-1",
        "query": "Create a script",
        "status": "completed",
        "duration_ms": 123.4,
        "events": [
            {"seq": 1, "type": "run_started"},
            {"seq": 2, "type": "llm_request", "round": 1, "prompt_chars": 456},
            {"seq": 3, "type": "llm_response", "round": 1, "tool_call_count": 1, "duration_ms": 30.0},
            {"seq": 4, "type": "tool_call", "name": "write_file"},
            {"seq": 5, "type": "approval_request", "operation": "create", "path": "tools/x.py"},
            {"seq": 6, "type": "approval_result", "decision": "approved"},
            {"seq": 7, "type": "tool_result", "name": "write_file", "ok": True, "duration_ms": 4.0},
            {"seq": 8, "type": "final_answer"},
        ],
    }
    report = {**trace, "events": [{**event} for event in trace["events"]]}
    (run_dir / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")


def test_inspector_formats_key_chain_without_full_prompt(tmp_path):
    module = load_inspector()
    write_run(tmp_path)

    output = module.inspect_run(tmp_path)

    assert "Run: run-1" in output
    assert "Status: completed" in output
    assert "prompt_chars=456" in output
    assert "approval_result" in output
    assert "write_file" in output
    assert "Create a script" not in output


def test_inspector_uses_latest_run_when_no_path(tmp_path):
    module = load_inspector()
    older = tmp_path / "older"
    latest = tmp_path / "latest"
    older.mkdir()
    latest.mkdir()
    write_run(older)
    write_run(latest)
    os.utime(older / "trace.json", (1, 1))
    os.utime(older / "report.json", (1, 1))
    os.utime(latest / "trace.json", (2, 2))
    os.utime(latest / "report.json", (2, 2))

    assert module.find_run(tmp_path) == latest


def write_rich_run(run_dir: Path) -> None:
    request = {
        "seq": 2,
        "type": "llm_request",
        "round": 1,
        "prompt_chars": 1000,
        "stable_prefix_chars": 800,
        "history_covered": 3,
        "messages": [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "inspect the repo"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "readfile", "arguments": "{\"path\": \"README.md\"}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\": true}"},
        ],
    }
    response = {
        "seq": 3,
        "type": "llm_response",
        "round": 1,
        "finish_reason": "tool_calls",
        "api_attempts": 1,
        "content": None,
        "tool_calls": [{"id": "c1", "name": "readfile", "arguments": {"path": "README.md"}}],
    }
    tool_result = {"seq": 4, "type": "tool_result", "id": "c1", "name": "readfile", "ok": True, "duration_ms": 3.5, "result": {"ok": True, "path": "README.md"}}
    report = {"run_id": "run-2", "status": "completed", "events": [request, response, tool_result]}
    (run_dir / "trace.json").write_text(json.dumps({"run_id": "run-2", "status": "completed", "events": []}), encoding="utf-8")
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")


def test_prompt_reader_renders_rounds_metrics_and_tool_calls(tmp_path):
    module = load_inspector()
    write_rich_run(tmp_path)

    output = module.render_prompts(tmp_path)

    assert "round 1 request" in output
    assert "prompt_chars=1000" in output
    assert "stable_prefix=800 (80%)" in output
    assert "covered=3" in output
    assert "[3] tool" in output
    assert "call: readfile" in output
    assert "tool_result  readfile  ok=True" in output


def test_prompt_reader_survives_reports_without_messages(tmp_path):
    module = load_inspector()
    write_run(tmp_path)

    output = module.render_prompts(tmp_path)

    assert "no messages recorded" in output


def test_reader_accepts_a_file_inside_the_run_directory(tmp_path):
    module = load_inspector()
    write_rich_run(tmp_path)

    assert module.normalize_run_dir(tmp_path / "trace.json") == tmp_path
    assert module.normalize_run_dir(tmp_path / "report.json") == tmp_path
    assert module.normalize_run_dir(tmp_path) == tmp_path
    # the CLI prints file paths, so both forms must render the same report
    assert module.render_prompts(tmp_path / "trace.json") == module.render_prompts(tmp_path)
