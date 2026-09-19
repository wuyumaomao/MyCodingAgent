from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def find_run(runs_root: Path) -> Path:
    candidates = [
        path
        for path in Path(runs_root).iterdir()
        if path.is_dir() and (path / "trace.json").is_file() and (path / "report.json").is_file()
    ]
    if not candidates:
        raise FileNotFoundError(f"No complete run found in {runs_root}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def normalize_run_dir(path: Path) -> Path:
    """Accept a run directory or any file inside it (the CLI prints file paths)."""
    path = Path(path)
    return path.parent if path.is_file() else path


def inspect_run(run_dir: Path) -> str:
    run_dir = normalize_run_dir(run_dir)
    trace = _load_json(run_dir / "trace.json")
    # Loading report validates that this run has its detailed companion file.
    _load_json(run_dir / "report.json")
    lines = [
        f"Run: {trace.get('run_id', run_dir.name)}",
        f"Status: {trace.get('status', 'unknown')}",
    ]
    if "duration_ms" in trace:
        lines.append(f"Duration: {trace['duration_ms']} ms")
    lines.append("")
    for event in sorted(trace.get("events", []), key=lambda item: item.get("seq", 0)):
        lines.append(_format_event(event))
    return "\n".join(lines)


def render_prompts(run_dir: Path) -> str:
    """Render every round's prompt and response from report.json.

    Auditing tool choice and per-round prompts means reading the full messages,
    which the compact event chain deliberately omits.
    """
    run_dir = normalize_run_dir(run_dir)
    report = _load_json(run_dir / "report.json")
    lines = [
        f"Run: {report.get('run_id', run_dir.name)}",
        f"Status: {report.get('status', 'unknown')}",
        "",
    ]
    for event in sorted(report.get("events", []), key=lambda item: item.get("seq", 0)):
        event_type = event.get("type")
        if event_type == "llm_request":
            lines.extend(_format_request(event))
            lines.append("")
        elif event_type == "llm_response":
            lines.extend(_format_response(event))
            lines.append("")
        elif event_type == "tool_result":
            lines.extend(_format_tool_result(event))
            lines.append("")
        elif event_type == "final_answer":
            lines.append(f"final answer: {_preview(str(event.get('content', '')), 400)}")
    return "\n".join(lines).rstrip() + "\n"


def _format_request(event: dict[str, Any]) -> list[str]:
    prompt_chars = event.get("prompt_chars", 0)
    stable = event.get("stable_prefix_chars", 0)
    ratio = f"{stable / prompt_chars:.0%}" if prompt_chars else "n/a"
    covered = event.get("history_covered", 0)
    dropped = event.get("dropped_groups", 0)
    # 视图长度 vs 压缩触发线：这两个数放在一起，"该压没压"才看得出来。
    transcript = event.get("transcript_chars")
    threshold = event.get("compaction_threshold_chars")
    budget = ""
    if isinstance(transcript, int) and isinstance(threshold, int) and threshold:
        budget = f"  transcript_chars={transcript}/{threshold} ({transcript / threshold:.0%})"
    extra = f"  covered={covered}" if covered else ""
    if dropped:
        extra += f"  dropped_groups={dropped}"
    lines = [
        f"round {event.get('round')} request  prompt_chars={prompt_chars}  "
        f"stable_prefix={stable} ({ratio}){budget}{extra}"
    ]
    messages = event.get("messages")
    if not isinstance(messages, list):
        lines.append("  (no messages recorded)")
        return lines
    for index, message in enumerate(messages):
        role = str(message.get("role", "?"))
        body = str(message.get("content") or "")
        detail = f"tool_calls={[call.get('function', {}).get('name') for call in message.get('tool_calls', [])]}" if message.get("tool_calls") else ""
        if message.get("tool_call_id"):
            detail = f"-> {message['tool_call_id']}"
        lines.append(f"  [{index}] {role:<9} {len(body):>6}  {_preview(body, 70)!r}{('  ' + detail) if detail else ''}")
    return lines


def _format_response(event: dict[str, Any]) -> list[str]:
    lines = [
        f"round {event.get('round')} response  finish={event.get('finish_reason')}  "
        f"api_attempts={event.get('api_attempts')}"
    ]
    content = str(event.get("content") or "")
    if content:
        lines.append(f"  content: {_preview(content, 120)!r}")
    for call in event.get("tool_calls", []):
        name = call.get("name") or call.get("function", {}).get("name")
        arguments = call.get("arguments") or call.get("function", {}).get("arguments")
        lines.append(f"  call: {name}({_preview(str(arguments), 90)})")
    return lines


def _format_tool_result(event: dict[str, Any]) -> list[str]:
    result = event.get("result")
    result = result if isinstance(result, dict) else {}
    error = result.get("error")
    error_type = error.get("type") if isinstance(error, dict) else None
    lines = [
        f"tool_result  {event.get('name')}  ok={result.get('ok')}"
        f"{f'  error={error_type}' if error_type else ''}  {event.get('duration_ms')}ms"
    ]
    return lines


def _preview(value: str, limit: int) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Invalid run document: {path}")
    return value


def _format_event(event: dict[str, Any]) -> str:
    seq = event.get("seq", "?")
    event_type = event.get("type", "unknown")
    details: list[str] = []
    if event_type in {"llm_request", "llm_response"} and event.get("round") is not None:
        details.append(f"round={event['round']}")
    if event_type == "llm_request":
        for key in ("message_count", "prompt_chars", "transcript_chars", "compaction_threshold_chars", "stable_prefix_chars", "history_covered", "dropped_groups"):
            if key in event:
                details.append(f"{key}={event[key]}")
    elif event_type == "llm_response":
        if "tool_call_count" in event:
            details.append(f"tool_calls={event['tool_call_count']}")
        _append_if_present(details, event, "duration_ms")
    elif event_type == "tool_call":
        _append_if_present(details, event, "name")
    elif event_type == "tool_call_limit":
        _append_if_present(details, event, "name")
        if "max_calls" in event:
            details.append(f"max_calls={event['max_calls']}")
    elif event_type == "tool_call_repeat":
        _append_if_present(details, event, "name")
        _append_if_present(details, event, "reason")
    elif event_type == "context_compressed":
        _append_if_present(details, event, "kind")
        _append_if_present(details, event, "tool")
        if "before_chars" in event and "after_chars" in event:
            details.append(f"{event['before_chars']}->{event['after_chars']}")
        _append_if_present(details, event, "duration_ms")
    elif event_type == "approval_request":
        _append_if_present(details, event, "operation")
        _append_if_present(details, event, "path")
    elif event_type == "approval_result":
        _append_if_present(details, event, "decision")
    elif event_type == "tool_result":
        _append_if_present(details, event, "name")
        if "ok" in event:
            details.append(f"ok={str(event['ok']).lower()}")
        _append_if_present(details, event, "error_type")
        _append_if_present(details, event, "duration_ms")
    elif event_type == "run_failed":
        _append_if_present(details, event, "error_type")
    suffix = f"  {' '.join(details)}" if details else ""
    return f"{seq:>3}  {event_type:<18}{suffix}"


def _append_if_present(details: list[str], event: dict[str, Any], key: str) -> None:
    if key in event:
        details.append(f"{key}={event[key]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the key event chain of a coding-agent run.")
    parser.add_argument("run_dir", nargs="?", type=Path)
    parser.add_argument(
        "--prompts",
        action="store_true",
        help="Render every round's full prompt and response instead of the event chain.",
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".coding-agent" / "runs",
        help="Root containing run directories when run_dir is omitted.",
    )
    args = parser.parse_args(argv)
    try:
        run_dir = args.run_dir or find_run(args.runs_root)
        print(render_prompts(run_dir) if args.prompts else inspect_run(run_dir))
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
