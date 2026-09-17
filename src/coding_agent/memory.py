from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable

from .models import ToolCall
from .session import SessionState

FILE_SUMMARY_SYSTEM_PROMPT = (
    "你是代码文件索引摘要器。只根据用户提供的完整源码工作，返回严格 JSON，"
    "不要输出 Markdown 或任何额外文字。summary 用一句话描述文件职责，"
    "保留源码中出现的英文标识符与技术术语（例如 ShellPolicy、pytest、JSON），"
    "不要把它们整体翻译成中文，否则检索时无法按关键词召回；"
    "symbols 只列真实存在的顶层类、函数、导出变量或全局常量；"
    "line_index 只列关键逻辑区间及其简短描述。不要编造代码。"
)


class LLMFileSummaryProvider:
    """Generate a validated file summary through an injected no-tools LLM call."""

    def __init__(self, complete_text: Callable[[list[dict[str, Any]]], str]) -> None:
        self.complete_text = complete_text

    def __call__(self, path: str, content: str, result: dict[str, Any]) -> dict[str, Any]:
        raw = self.complete_text([
            {"role": "system", "content": FILE_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"文件路径：{path}\n完整源码：\n{content}"},
        ])
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("FileSummary response was not valid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("summary"), str) or not value["summary"].strip():
            raise ValueError("FileSummary summary is required")
        symbols = value.get("symbols", [])
        line_index = value.get("line_index", [])
        if not isinstance(symbols, list) or not all(isinstance(item, str) for item in symbols):
            raise ValueError("FileSummary symbols must be a string list")
        if not isinstance(line_index, list) or not all(isinstance(item, dict) and isinstance(item.get("lines"), str) and isinstance(item.get("desc"), str) for item in line_index):
            raise ValueError("FileSummary line_index is invalid")
        return {"summary": value["summary"][:500], "symbols": symbols[:30], "line_index": line_index[:20]}


TOOL_RESULT_SUMMARY_SYSTEM_PROMPT = (
    "你是工具结果压缩器。只根据用户提供的工具结果 JSON 工作，返回严格 JSON，"
    "不要输出 Markdown 或任何额外文字。observation 用一到两句话概括对当前任务"
    "仍有用的信息，保留关键路径、数字、退出码和错误类型；不要编造结果中没有的内容。"
)


class LLMToolResultSummaryProvider:
    """Compress one oversized tool result through an injected no-tools LLM call.

    The compressed payload replaces the raw tool result in the transcript only;
    the full result stays in the session history and the run report.
    """

    def __init__(self, complete_text: Callable[[list[dict[str, Any]]], str]) -> None:
        self.complete_text = complete_text

    def __call__(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(result, ensure_ascii=False)[:20000]
        raw = self.complete_text([
            {"role": "system", "content": TOOL_RESULT_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"工具：{name}\n工具结果 JSON：\n{payload}"},
        ])
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("ToolResultSummary response was not valid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("observation"), str) or not value["observation"].strip():
            raise ValueError("ToolResultSummary observation is required")
        return {"ok": result.get("ok", False), "observation": value["observation"][:800]}


class MemoryManager:
    def __init__(self, session: SessionState, summary_provider: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.session = session
        self.summary_provider = summary_provider

    @property
    def data(self) -> dict[str, Any]:
        return self.session.memory

    def begin_run(self, query: str) -> None:
        working = self.data.setdefault("working_memory", {})
        working["task_summary"] = query[:600]
        working["constraints"] = _constraints(query)
        working["latest_tool_error"] = None
        working.setdefault("recent_modified_files", [])

    def observe_tool_result(self, call: ToolCall, result: dict[str, Any], *, run_id: str, round_number: int, raw_content: str | None = None) -> None:
        working = self.data.setdefault("working_memory", {})
        path = result.get("path") or call.arguments.get("path")
        if isinstance(path, str):
            recent = [item for item in working.setdefault("recent_files", []) if item != path]
            working["recent_files"] = [path, *recent][:10]
        if result.get("ok") is True and call.name == "readfile" and isinstance(path, str):
            start, end, count = result.get("start"), result.get("end"), result.get("line_count")
            if start == 1 and end == count and isinstance(count, int) and result.get("truncated") is not True:
                content = raw_content if raw_content is not None else str(result.get("content", ""))
                current_freshness = _freshness(content)
                existing = self.data.setdefault("file_summaries", {}).get(path)
                if isinstance(existing, dict) and existing.get("freshness") == current_freshness:
                    return
                summary = None
                if self.summary_provider is not None:
                    try:
                        candidate = self.summary_provider(path, content, result)
                        if isinstance(candidate, dict) and candidate.get("summary"):
                            summary = candidate
                    except Exception:
                        summary = None
                summary = summary or {"summary": _summarize(content)}
                self.data.setdefault("file_summaries", {})[path] = {
                    **summary, "line_count": count,
                    "freshness": current_freshness, "source_range": {"start": start, "end": end},
                    "updated_at": _now(),
                }
                self._append_note(f"已检查文件 {path}", {"readfile", path.lower()}, run_id, round_number, path=path, freshness=self.data["file_summaries"][path]["freshness"])
        if result.get("ok") is True and call.name in {"write_file", "patch_file"} and isinstance(path, str):
            recent_modified = [item for item in working.setdefault("recent_modified_files", []) if item != path]
            working["recent_modified_files"] = [path, *recent_modified][:10]
            self.data.setdefault("file_summaries", {}).pop(path, None)
            self.data["episodic_notes"] = [n for n in self.data.setdefault("episodic_notes", []) if n.get("path") != path]
        if result.get("ok") is False:
            error = result.get("error") or {}
            error_type = str(error.get("type", "tool_error"))
            message = str(error.get("message", "Tool execution failed"))
            working["latest_tool_error"] = {"tool": call.name, "type": error_type, "message": message[:300]}
            if call.name == "shell" or error_type in {"approval_denied", "tool_error"}:
                self._append_note(f"{call.name} 失败：{error_type} - {message[:180]}", {call.name, error_type}, run_id, round_number, path=path if isinstance(path, str) else None)
        elif result.get("ok") is True:
            # The surviving failure would otherwise stay in the [Memory] block for
            # the rest of the run, even after later tools succeeded.
            working["latest_tool_error"] = None

    def retrieve_relevant(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """Recall notes related to what the user is asking about.

        Recall follows only the question and the run task summary.
        ``latest_tool_error`` is deliberately excluded: it is already rendered
        into the [Memory] block, and the note describing that failure quotes the
        same error text, so using it as a retrieval key would let that note
        outrank the note the user actually asked about.
        """
        working = self.data.get("working_memory", {})
        terms = _keywords(query + " " + str(working.get("task_summary", "")))
        scored = []
        for index, note in enumerate(self.data.setdefault("episodic_notes", [])):
            score = len(terms & _note_keywords(note))
            if score:
                scored.append((score, index, note))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [note for _, _, note in scored[:limit]]

    def retrieve_file_summaries(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Return file summaries relevant to the current task, ranked locally.

        Matching uses the same keyword set as note recall: ASCII identifiers are
        matched whole, runs of Chinese characters as bigrams. Symbol names stay
        the most reliable handle because they are stable ASCII identifiers.
        """
        terms = _keywords(query)
        recent = self.data.get("working_memory", {}).get("recent_files", [])
        modified = self.data.get("working_memory", {}).get("recent_modified_files", [])
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for index, (path, summary) in enumerate(self.data.setdefault("file_summaries", {}).items()):
            if not isinstance(summary, dict):
                continue
            searchable = " ".join(
                [path, str(summary.get("summary", "")), " ".join(map(str, _as_list(summary.get("symbols"))))]
            )
            overlap = len(terms & _keywords(searchable))
            if overlap == 0:
                continue
            bonus = 3 if path in modified else (1 if path in recent else 0)
            scored.append((overlap + bonus, -index, {"path": path, **summary}))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item for _, _, item in scored[: max(0, limit)]]

    def render_memory(self, *, max_chars: int = 3000) -> str:
        working = self.data.get("working_memory", {})
        recent = working.get("recent_modified_files", working.get("recent_files", []))
        lines = ["[Memory]", f"task_summary: {working.get('task_summary', '')}", f"constraints: {', '.join(working.get('constraints', [])) or 'none'}", f"recent_modified_files: {', '.join(recent) or 'none'}"]
        if working.get("latest_tool_error"):
            lines.append(f"latest_tool_error: {working['latest_tool_error']}")
        return "\n".join(lines)[:max_chars]

    def render_file_summaries(self, query: str, *, max_chars: int = 2600, limit: int = 5) -> str:
        lines = ["[Relevant File Summaries]"]
        for item in self.retrieve_file_summaries(query, limit=limit):
            path = item.get("path", "")
            body = item.get("summary") or ""
            symbols = _as_list(item.get("symbols"))
            line_index = _as_list(item.get("line_index"))
            lines.append(f"- {path}: {body}")
            if symbols:
                lines.append(f"  symbols: {', '.join(map(str, symbols))}")
            if line_index:
                lines.append(f"  line_index: {json_like(line_index)}")
        if len(lines) == 1:
            lines.append("- none")
        return "\n".join(lines)[:max_chars]

    def render_relevant_memory(self, query: str, *, max_chars: int = 1800) -> str:
        lines = ["[Relevant Memory]"]
        lines.extend(f"- {note.get('content', '')}" for note in self.retrieve_relevant(query))
        return "\n".join(lines)[:max_chars]

    def _append_note(self, content: str, keywords: set[str], run_id: str, round_number: int, *, path: str | None = None, freshness: str | None = None) -> None:
        notes = self.data.setdefault("episodic_notes", [])
        notes.append({"id": f"note-{len(notes) + 1}", "content": content[:300], "keywords": sorted(_keywords(" ".join(keywords))), "source": {"tool": "memory", "run_id": run_id, "round": round_number}, "path": path, "freshness": freshness, "created_at": _now()})
        del notes[:-12]


def _keywords(text: str) -> set[str]:
    """Extract retrieval terms from mixed Chinese/English text.

    ASCII identifiers are matched whole; runs of Chinese characters are cut
    into bigrams so that a Chinese requirement can still recall a Chinese
    summary without a segmentation dependency.
    """
    words = {word.lower() for word in re.findall(r"[A-Za-z0-9_]+", text) if len(word) >= 2}
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[index : index + 2] for index in range(len(run) - 1))
    return words


def _as_list(value: Any) -> list[Any]:
    """Tolerate malformed provider payloads without breaking rendering."""
    return value if isinstance(value, list) else []


def _note_keywords(note: dict[str, Any]) -> set[str]:
    """Tokenize a note when it is read, not when it was written.

    The stored ``keywords`` are a snapshot taken at write time and only ever
    hold ASCII tool and path names. Re-tokenizing the note text on every read
    lets Chinese requirements match it, and lets sessions written before a
    tokenizer change benefit without a data migration.
    """
    stored = _as_list(note.get("keywords"))
    return _keywords(str(note.get("content", "")) + " " + " ".join(map(str, stored)))


def json_like(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _constraints(query: str) -> list[str]:
    return [line.strip() for line in re.findall(r"[^。.!?\n]*(?:不要|不修改|不删除|do not|don't)[^。.!?\n]*", query, flags=re.I)][:5]


def _freshness(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _summarize(content: str) -> str:
    useful = []
    for line in content.splitlines():
        text = re.sub(r"^\s*\d+:\s?", "", line).strip()
        if text and not text.startswith("#"):
            useful.append(text[:120])
    return " | ".join(useful[:6])[:600]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
