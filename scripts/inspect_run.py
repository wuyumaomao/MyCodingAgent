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


def inspect_run(run_dir: Path) -> str:
    run_dir = Path(run_dir)
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
        for key in ("message_count", "prompt_chars"):
            if key in event:
                details.append(f"{key}={event[key]}")
    elif event_type == "llm_response":
        if "tool_call_count" in event:
            details.append(f"tool_calls={event['tool_call_count']}")
        _append_if_present(details, event, "duration_ms")
    elif event_type == "tool_call":
        _append_if_present(details, event, "name")
    elif event_type == "approval_request":
        _append_if_present(details, event, "operation")
        _append_if_present(details, event, "path")
    elif event_type == "approval_result":
        _append_if_present(details, event, "decision")
    elif event_type == "tool_result":
        _append_if_present(details, event, "name")
        if "ok" in event:
            details.append(f"ok={str(event['ok']).lower()}")
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
        "--runs-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".coding-agent" / "runs",
        help="Root containing run directories when run_dir is omitted.",
    )
    args = parser.parse_args(argv)
    try:
        run_dir = args.run_dir or find_run(args.runs_root)
        print(inspect_run(run_dir))
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
