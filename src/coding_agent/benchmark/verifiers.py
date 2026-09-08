from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    failures: list[str]


def verify_artifact(rule: dict[str, object], workspace_root: Path, *, answer: str | None = None) -> VerificationResult:
    kind = rule.get("type")
    if kind == "answer_contains":
        text = rule.get("text")
        if not isinstance(text, str) or answer is None or text not in answer:
            return VerificationResult(False, ["final answer does not contain expected text"])
        return VerificationResult(True, [])
    path_value = rule.get("path")
    if not isinstance(path_value, str):
        return VerificationResult(False, ["verifier path is missing"])
    target = (workspace_root / path_value).resolve() if not Path(path_value).is_absolute() else Path(path_value).resolve()
    root = Path(workspace_root).resolve()
    if not target.is_relative_to(root):
        return VerificationResult(False, ["verifier path escapes workspace"])
    if kind == "exists":
        return VerificationResult(target.exists(), [] if target.exists() else [f"missing artifact: {path_value}"])
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return VerificationResult(False, [f"cannot read artifact {path_value}: {exc}"])
    expected = rule.get("text")
    if not isinstance(expected, str):
        return VerificationResult(False, ["content verifier requires text"])
    if kind == "contains":
        passed = expected in content
    elif kind == "not_contains":
        passed = expected not in content
    elif kind == "equals":
        passed = content == expected
    else:
        return VerificationResult(False, [f"unsupported verifier type: {kind}"])
    return VerificationResult(passed, [] if passed else [f"artifact verifier failed: {kind} {path_value}"])


def verify_trace(assertions: Sequence[dict[str, object]], trace_path: Path) -> VerificationResult:
    try:
        events = json.loads(Path(trace_path).read_text(encoding="utf-8"))["events"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return VerificationResult(False, [f"invalid trace: {exc}"])
    failures: list[str] = []
    for assertion in assertions:
        kind = assertion.get("type")
        if kind == "tool_called":
            name = assertion.get("name")
            if not any(event.get("type") == "tool_call" and event.get("name") == name for event in events):
                failures.append(f"tool was not called: {name}")
        elif kind == "ordered_tools":
            names = assertion.get("names", [])
            observed = [event.get("name") for event in events if event.get("type") == "tool_call"]
            positions = []
            for name in names:
                try:
                    positions.append(observed.index(name, positions[-1] + 1 if positions else 0))
                except ValueError:
                    failures.append(f"tool order missing: {name}")
                    break
        elif kind == "all_tool_results_ok":
            if any(event.get("type") == "tool_result" and event.get("ok") is not True for event in events):
                failures.append("not all tool results were successful")
        elif kind == "approval":
            expected_operation = assertion.get("operation", assertion.get("name"))
            if not any(event.get("type") == "approval_result" and event.get("operation") == expected_operation and event.get("decision") == assertion.get("decision") for event in events):
                failures.append("expected approval decision was not recorded")
        elif kind == "error_type":
            expected = assertion.get("error_type")
            if not any(_event_error_type(event) == expected for event in events):
                failures.append(f"error type was not recorded: {expected}")
        elif kind == "event_after":
            before = assertion.get("before")
            after = assertion.get("after")
            before_indices = [index for index, event in enumerate(events) if _event_error_type(event) == before or event.get("type") == before]
            after_indices = [index for index, event in enumerate(events) if event.get("type") == after]
            if not before_indices or not any(index > before_indices[0] for index in after_indices):
                failures.append(f"event {after} did not follow {before}")
        else:
            failures.append(f"unsupported trace assertion: {kind}")
    return VerificationResult(not failures, failures)


def _event_error_type(event: dict[str, Any]) -> object:
    if event.get("error_type"):
        return event["error_type"]
    result = event.get("result")
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict):
            return error.get("type")
    return None
