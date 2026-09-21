from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any, Callable

from .models import ToolCall
from .events import NullEventSink
from .session import SessionState

# 2026-09-17 冻结：episodic note 的内容是工具活动流水（"已检查文件 X"），
# 信息量低于 file_summaries 已有的内容，召回价值有限。停止写入新 note；
# 读取与旧 session 的召回保持不变，待上下文压缩重构完成后再决定是否
# 改造为模型驱动的经验笔记（Claude memory tool / MCP memory 范式）。
EPISODIC_NOTES_FROZEN = True

FILE_SUMMARY_SYSTEM_PROMPT = (
    "你是代码文件索引摘要器。只根据用户提供的完整源码工作，返回严格 JSON，"
    "不要输出 Markdown 或任何额外文字。summary 用一句话描述文件职责，"
    "保留源码中出现的英文标识符与技术术语（例如 ShellPolicy、pytest、JSON），"
    "不要把它们整体翻译成中文，否则检索时无法按关键词召回；"
    "symbols 只列真实存在的顶层类、函数、导出变量或全局常量；"
    "line_index 只列关键逻辑区间及其简短描述。不要编造代码。"
)


_MODEL_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
# 降级路径也要保住符号：没有 LLM 摘要时，用正则抽出顶层定义。
_FALLBACK_SYMBOL = re.compile(r"^\s{0,4}(?:async\s+)?(?:def|class|function)\s+([A-Za-z_]\w*)", re.M)


def parse_model_json(raw: Any) -> dict[str, Any] | None:
    """Parse a model reply into an object, tolerating fences and surrounding prose.

    模型多包一层 ```json 代码块、或者前后多说一句"好的，这是摘要："，都不该让
    整份摘要降级——只有拿不到任何 JSON 对象时才算失败。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    candidates: list[str] = []
    fenced = _MODEL_JSON_FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def fallback_symbols(content: str) -> list[str]:
    """Extract top-level names without a model, so symbol recall survives a failure."""
    seen: list[str] = []
    for name in _FALLBACK_SYMBOL.findall(content):
        if name not in seen:
            seen.append(name)
    return seen[:30]


_LINE_KEYS = ("lines", "range", "line", "lines_range", "span")
_DESC_KEYS = ("desc", "description", "summary", "purpose", "note")


def normalize_symbols(value: Any) -> list[str]:
    """Accept a list of names, or a list of objects carrying a name."""
    names: list[str] = []
    for item in _as_list(value):
        name = item if isinstance(item, str) else (item.get("name") if isinstance(item, dict) else None)
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names[:30]


def normalize_line_index(value: Any) -> list[dict[str, str]]:
    """Normalize line-index entries, dropping unusable ones instead of failing.

    模型的输出形态不稳定：同一个字段可能写成 lines / range / line，或者拆成
    start + end。任何一种写法都合法，**不能因为一个条目的格式不符就让整份摘要
    作废**——那样连正确的 summary 和 symbols 也会一起丢掉。
    """
    entries: list[dict[str, str]] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        lines = next((item[key] for key in _LINE_KEYS if isinstance(item.get(key), str)), None)
        if lines is None:
            start, end = item.get("start"), item.get("end")
            if isinstance(start, int) and isinstance(end, int):
                lines = f"{start}-{end}"
            elif isinstance(start, int):
                lines = str(start)
        desc = next((item[key] for key in _DESC_KEYS if isinstance(item.get(key), str)), None)
        if isinstance(lines, str) and isinstance(desc, str):
            entries.append({"lines": lines, "desc": desc})
    return entries[:20]


class LLMFileSummaryProvider:
    """Generate a validated file summary through an injected no-tools LLM call."""

    def __init__(self, complete_text: Callable[[list[dict[str, Any]]], str]) -> None:
        self.complete_text = complete_text

    def __call__(self, path: str, content: str, result: dict[str, Any]) -> dict[str, Any]:
        raw = self.complete_text([
            {"role": "system", "content": FILE_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"文件路径：{path}\n完整源码：\n{content}"},
        ])
        value = parse_model_json(raw)
        if value is None:
            raise ValueError("FileSummary response was not valid JSON")
        summary = value.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("FileSummary summary is required")
        # 只有 summary 缺失才算失败；symbols / line_index 一律尽量抢救。
        return {
            "summary": summary[:500],
            "symbols": normalize_symbols(value.get("symbols")),
            "line_index": normalize_line_index(value.get("line_index")),
        }


SPAN_SUMMARY_SYSTEM_PROMPT = (
    "你是 coding agent 的上下文交接摘要器。只根据提供的对话片段工作，输出纯文本，"
    "不要使用 JSON 或代码块。按下面的小节输出，每节用「## 小节名」开头，每节一到三句话：\n"
    "## 任务\n用户的原始请求与当前意图（含明确约束，例如「不要修改测试」）。\n"
    "## 已确认\n已经用工具验证过的事实：文件路径、函数名、行号、命令、数字。\n"
    "## 结论\n已得出的判断，以及它们各自依据了什么证据。\n"
    "## 未完成\n还没做的事、已知的失败与原因（错误类型和消息）。\n"
    "## 下一步\n紧接着该做的第一件事，要具体到可直接执行。\n"
    "保留文件路径、函数名、命令和数字。只写片段里出现过的信息，不要编造；"
    "不确定的地方写「未知」，不要猜测。工具结果已被截断时，注明「内容可能不完整」。"
)


# 交接摘要的字符上限（只算 LLM 叙述那一段，台账另算）。它是"压缩后视图地板"的
# 一部分：地板 = compaction_target_chars + 本上限 + SPAN_LEDGER_MAX_CHARS，
# 必须严格小于触发线，否则压缩抖动。
SPAN_SUMMARY_MAX_CHARS = 3000
# 摘要器**输入**的上限。它是独立一次 LLM 调用的成本上限，所以和输出的上限分开看。
_SPAN_INPUT_CHARS = 24000


class LLMSpanSummaryProvider:
    """Write the handoff summary for one compaction span (no tools, plain text).

    The summary replaces the oldest turns of the transcript; it is persisted so
    later rounds reuse it instead of re-summarizing the same history.
    """

    def __init__(self, complete_text: Callable[[list[dict[str, Any]]], str]) -> None:
        self.complete_text = complete_text

    def __call__(self, messages: list[dict[str, Any]]) -> str:
        raw = self.complete_text([
            {"role": "system", "content": SPAN_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": "以下是需要压缩的对话片段：\n\n" + _serialize_span(messages)},
        ])
        text = str(raw or "").strip()
        if not text:
            raise ValueError("SpanSummary response was empty")
        # 摘要必须远小于阈值，否则压缩完仍贴着触发线 → 每轮重复压缩。
        # 地板 = 压缩目标 + 本上限，触发线见 context.py；改任一条都要重算余量。
        return text[:SPAN_SUMMARY_MAX_CHARS]


def _keep_both_ends(text: str, head: int, tail: int) -> str:
    """Keep both ends of a long text, marking how much was dropped.

    省略标记本身要占长度，所以先给它留出位置——否则结果会比 ``head + tail`` 长，
    调用方按这个数做的预算就会失效。
    """
    if len(text) <= head + tail:
        return text
    reserve = 64
    head = max(1, head - reserve * 3 // 4)
    tail = max(1, tail - reserve // 4)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [{omitted} chars omitted] ...\n{text[-tail:]}"


def _serialize_span(messages: list[dict[str, Any]]) -> str:
    """Serialize a span for the summarizer, keeping **both ends** when oversized.

    只留头部是错的：被吸收的段落里最新的部分承载当前进度，被静默丢掉就永久丢失
    （摘要器看不到的内容没人再有机会看到）。所以超长时保留头尾两端并标出省略量。
    """
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role"))
        body = str(message.get("content") or "")
        if message.get("tool_calls"):
            calls = ", ".join(
                f"{call.get('function', {}).get('name')}({call.get('function', {}).get('arguments')})"
                for call in message["tool_calls"]
            )
            lines.append(f"[assistant 调用工具] {calls}")
            continue
        if role == "tool":
            lines.append(f"[工具结果] {body}")
            continue
        lines.append(f"[{role}] {body}")
    return _keep_both_ends("\n".join(lines), _SPAN_INPUT_CHARS * 3 // 4, _SPAN_INPUT_CHARS // 4)


class MemoryManager:
    def __init__(self, session: SessionState, summary_provider: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None, event_sink: Any | None = None) -> None:
        self.session = session
        self.summary_provider = summary_provider
        # 文件摘要是另一处昂贵的 LLM 调用，同样需要可观测。
        self.event_sink = event_sink if event_sink is not None else NullEventSink()

    @property
    def data(self) -> dict[str, Any]:
        return self.session.memory

    def begin_run(self, query: str) -> None:
        working = self.data.setdefault("working_memory", {})
        working["task_summary"] = query[:600]
        working["constraints"] = _constraints(query)
        working["latest_tool_error"] = None
        working.setdefault("recent_modified_files", [])

    def invalidate_path(self, path: str) -> None:
        """Invalidate knowledge derived from a file after a successful write.

        A handoff summary is a snapshot of repository facts, so keeping it after
        a write can make the model trust conclusions about the old file. Clear
        the whole compaction snapshot conservatively; the raw history remains
        available and can be compacted again using the new file state.
        """
        self.data.setdefault("file_summaries", {}).pop(path, None)
        self.data["episodic_notes"] = [
            note for note in self.data.setdefault("episodic_notes", [])
            if note.get("path") != path
        ]
        self.data["compaction"] = {"covered": 0, "summary": "", "ledger": {}}

    def observe_tool_result(self, call: ToolCall, result: dict[str, Any], *, run_id: str, round_number: int, raw_content: str | None = None) -> None:
        working = self.data.setdefault("working_memory", {})
        path = result.get("path") or call.arguments.get("path")
        if result.get("ok") is True and call.name == "readfile" and isinstance(path, str):
            # 只有真正读过内容的文件才算"读过"。listfiles / find_files 的结果里也带
            # path，但那是目录——混进来会让这一栏变成 "a.py, kb, tests, ." 这种噪音。
            recent = [item for item in working.setdefault("recent_read_files", []) if item != path]
            working["recent_read_files"] = [path, *recent][:10]
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
                    started = time.perf_counter()
                    try:
                        candidate = self.summary_provider(path, content, result)
                        if isinstance(candidate, dict) and candidate.get("summary"):
                            summary = candidate
                    except Exception as exc:
                        # 失败必须可见：静默降级会让召回质量悄悄退化。
                        self.event_sink.emit(
                            "context_compressed",
                            kind="file_summary_failed",
                            path=path,
                            reason=type(exc).__name__,
                            detail=str(exc)[:120],
                        )
                    if summary is not None:
                        self.event_sink.emit(
                            "context_compressed",
                            kind="file_summary",
                            path=path,
                            before_chars=len(content),
                            after_chars=len(str(summary.get("summary", ""))),
                            duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        )
                if summary is None:
                    # 降级也保住符号：没有 LLM 也要能用符号名召回。
                    summary = {"summary": _summarize(content), "symbols": fallback_symbols(content), "line_index": [], "fallback": True}
                self.data.setdefault("file_summaries", {})[path] = {
                    **summary, "line_count": count,
                    "freshness": current_freshness, "source_range": {"start": start, "end": end},
                    "updated_at": _now(),
                }
                if not EPISODIC_NOTES_FROZEN:
                    self._append_note(f"已检查文件 {path}", {"readfile", path.lower()}, run_id, round_number, path=path, freshness=self.data["file_summaries"][path]["freshness"])
        if result.get("ok") is True and call.name in {"write_file", "patch_file"} and isinstance(path, str):
            recent_modified = [item for item in working.setdefault("recent_modified_files", []) if item != path]
            working["recent_modified_files"] = [path, *recent_modified][:10]
            self.invalidate_path(path)
        if result.get("ok") is False:
            error = result.get("error") or {}
            error_type = str(error.get("type", "tool_error"))
            message = str(error.get("message", "Tool execution failed"))
            working["latest_tool_error"] = {"tool": call.name, "type": error_type, "message": message[:300]}
            if not EPISODIC_NOTES_FROZEN and (call.name == "shell" or error_type in {"approval_denied", "tool_error"}):
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
        recent = self.data.get("working_memory", {}).get("recent_read_files", [])
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
        modified = working.get("recent_modified_files", [])
        read = working.get("recent_read_files", [])
        lines = ["[Memory]", f"task_summary: {working.get('task_summary', '')}", f"constraints: {', '.join(working.get('constraints', [])) or 'none'}", f"recent_read_files: {', '.join(read) or 'none'}", f"recent_modified_files: {', '.join(modified) or 'none'}"]
        if working.get("latest_tool_error"):
            lines.append(f"latest_tool_error: {working['latest_tool_error']}")
        return "\n".join(lines)[:max_chars]

    def render_file_summaries(self, query: str, *, max_chars: int = 2600, limit: int = 5) -> str:
        lines = ["[Relevant File Summaries]"]
        for item in self.retrieve_file_summaries(query, limit=limit):
            path = item.get("path", "")
            body = item.get("summary") or ""
            if item.get("fallback"):
                body = f"[fallback summary: LLM summarization failed] {body}"
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
