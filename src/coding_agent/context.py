from __future__ import annotations

import copy
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .events import NullEventSink
from .filesystem import FileEntry, scan_files
from .models import AssistantTurn, ToolCall
from .memory import SPAN_SUMMARY_MAX_CHARS, MemoryManager
from .repository import Workspace
from .tools.target_environment import resolve_target_python, target_venv_dir


SYSTEM_PROMPT = (
    "You are a coding agent. Use only the provided tools, "
    "stay inside the repository workspace, and explain findings based on evidence. "
    "The repository navigation map in the next message lists the source files and their sizes — "
    "use it to pick a target instead of exploring the tree. "
    "When only part of a file matters, locate it with search and read the range around the hit; "
    "prefer one or two medium ranges over many small ones, and read a file whole only when it is "
    "small or the whole file really is the evidence. "
    "If python or pytest fails because target dependencies are missing, request approval "
    "to run uv sync --dev, then retry the command. "
    "For file writes or patches, directly call write_file or patch_file. "
    "Do not ask for approval in ordinary text; those tools automatically request user approval. "
    "When multiple known files are independent, prefer returning multiple tool calls in the same response "
    "instead of reading one file per round. "
    "Compressed historical tool results are not complete evidence; when details are missing, call readfile "
    "again with start and end line numbers. When using search, prefer the smallest known directory "
    "such as src or tests; use the repository root only when the location is unknown."
)

_IMPORTANT_FILES = (
    "README.md",
    "pyproject.toml",
    "package.json",
    "AGENTS.md",
    ".env.example",
)
_ENTRYPOINT_NAMES = ("main.py", "app.py", "run.py", "cli.py")
# 导航图里"源码清单"小节：列哪些后缀、最多几条、最多多少字符。
# 这段进静态前缀、每轮重发，所以必须有界。
_SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".kt",
    ".sh", ".ps1", ".sql", ".toml", ".cfg", ".ini", ".yaml", ".yml",
})
_SOURCE_FILE_LIMIT = 20
_SOURCE_FILES_MAX_CHARS = 1200

REPOSITORY_INSTRUCTION_FILE = "AGENTS.md"
REPOSITORY_INSTRUCTION_MAX_BYTES = 32768
REPOSITORY_INSTRUCTION_HEADER = "[Repository Instructions]"


def build_repository_instructions(workspace: Workspace, max_bytes: int = REPOSITORY_INSTRUCTION_MAX_BYTES) -> str:
    """Load the target repository's own conventions for the agent.

    The file belongs to the repository rather than to this program, so pointing
    ``--repo`` at another checkout brings that project's conventions without any
    code change here. A missing file is normal and yields an empty string.
    """
    path = workspace.root / REPOSITORY_INSTRUCTION_FILE
    try:
        if not path.is_file() or path.is_symlink():
            return ""
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        return ""
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", errors="ignore").strip()
    if not text:
        return ""
    header = f"{REPOSITORY_INSTRUCTION_HEADER}\nsource: {REPOSITORY_INSTRUCTION_FILE}"
    if truncated:
        header += f"\ntruncated: true (only the first {max_bytes} bytes are included)"
    return f"{header}\n\n{text}"


# 预算由模型窗口推导，不再是拍出来的绝对字符数。
#
# `deepseek-flash`（DeepSeek-V41-Flash）= 1,000,000 token。来源：本机 DSH 的
# DeepSeek 模型目录（`dsh-llm-deepseek`，`DEFAULT_CONTEXT_WINDOW = 1e6`，
# 注释为 "Default combined request/response context capacity"）。
# 换模型时改这一个数，或调用方传 `context_window_tokens` / `transcript_budget_chars`。
DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000
# 代码大约 3.5 个字符一个 token（英文散文约 4，中文约 1.5）。
CHARS_PER_TOKEN = 3.5
# prompt 允许占窗口的比例。DSH 在 0.8 处压缩，这里取 0.20 保守得多——
# 留出输出空间，也避免长上下文里"中段信息被忽略"的退化。
PROMPT_BUDGET_RATIO = 0.20
# 每轮固定开销：3 条 system 消息（行为规范 + 导航图 + AGENTS.md）+ 工具定义 + 记忆快照。
FIXED_PROMPT_OVERHEAD_CHARS = 16_000


def derive_transcript_budget(context_window_tokens: int) -> int:
    """窗口 → 历史视图的字符预算。

    1M token 窗口下得到 684,000 字符（触发线 547,200），是原写死值 120,000 的 5.7 倍。
    原来的数是从"读两个文件要多少空间"倒推的，全程没看窗口——于是只用了 1M 窗口的
    2.7%，"读几个文件就触发压缩"。
    """
    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens must be positive")
    allowance = int(context_window_tokens * CHARS_PER_TOKEN * PROMPT_BUDGET_RATIO)
    return max(20_000, allowance - FIXED_PROMPT_OVERHEAD_CHARS)


class ConversationContext:
    """Build static system messages and maintain the per-run message history."""

    def __init__(
        self,
        workspace: Workspace,
        repository_context_builder: Callable[[Workspace], str] | None = None,
        memory: MemoryManager | None = None,
        # 预算由**模型窗口**推导，不是一个拍出来的绝对字符数。
        #
        # 血泪史：原实现写死 `transcript_budget_chars = 120000`（≈27K token）——
        # 那是从"读两个文件需要多少空间"倒推的，全程没有考虑模型能装多少。
        # 而 `deepseek-flash`（DeepSeek-V41-Flash）的窗口是 1,000,000 token
        # （来源：本机 DSH 的 DeepSeek 模型目录，`DEFAULT_CONTEXT_WINDOW = 1e6`），
        # 于是只用了窗口的 2.7%，"读几个文件就触发压缩"。DSH 自己的触发线是窗口的 80%。
        #
        # 推导：窗口 token × 每 token 字符数 × prompt 占比 − 固定开销。
        # 占比取 0.20 是保守值（DSH 用 0.8，我留 4 倍余量给输出与长上下文质量退化）。
        transcript_budget_chars: int | None = None,
        context_window_tokens: int | None = None,
        tool_result_budget_chars: int = 6000,
        compaction_summarizer: Callable[[list[dict[str, Any]]], str] | None = None,
        compaction_threshold_ratio: float = 0.8,
        compaction_target_ratio: float = 0.25,
        event_sink: Any | None = None,
    ) -> None:
        if repository_context_builder is None:#在这里扫描整个仓库
            repository_context_builder = build_repository_context
        self.system_messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": repository_context_builder(workspace)},
        ]
        # Repository conventions belong to the target repository. Read once per
        # run and kept in the static prefix so the prompt cache stays valid.
        instructions = build_repository_instructions(workspace)
        if instructions:
            self.system_messages.append({"role": "system", "content": instructions})
        self.history: list[dict[str, Any]] = []
        self.history_exclusions: list[dict[str, Any]] = []
        self.memory = memory
        self.context_window_tokens = context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS
        if transcript_budget_chars is None:
            transcript_budget_chars = derive_transcript_budget(self.context_window_tokens)
        if transcript_budget_chars <= 0:
            raise ValueError("transcript_budget_chars must be positive")
        self.transcript_budget_chars = transcript_budget_chars
        if tool_result_budget_chars <= 0:
            raise ValueError("tool_result_budget_chars must be positive")
        self.tool_result_budget_chars = tool_result_budget_chars
        if not 0 < compaction_target_ratio < compaction_threshold_ratio:
            raise ValueError("compaction ratios must satisfy 0 < target < threshold")
        self.compaction_summarizer = compaction_summarizer
        self.compaction_threshold_chars = int(transcript_budget_chars * compaction_threshold_ratio)
        self.compaction_target_chars = int(transcript_budget_chars * compaction_target_ratio)
        # Persisted span compaction: the first `covered` history messages are
        # represented by `summary`（LLM 叙述）+ `ledger`（机器生成的进度台账）。
        # The raw history is never rewritten.
        self._compaction: dict[str, Any] = {"covered": 0, "summary": "", "ledger": {}}
        if memory is not None:
            stored = memory.data.get("compaction")
            if isinstance(stored, dict):
                try:
                    self._compaction["covered"] = max(0, int(stored.get("covered", 0) or 0))
                except (TypeError, ValueError):
                    self._compaction["covered"] = 0
                self._compaction["summary"] = str(stored.get("summary", "") or "")
                if isinstance(stored.get("ledger"), dict):
                    self._compaction["ledger"] = stored["ledger"]
        self._compaction_attempted = False
        self._compaction_retry_chars = 0
        self._dropped_groups = 0
        # 已经上报过修剪事件的调用 id：修剪每轮都会重算（history 保持原文），
        # 但事件只能发一次，否则 report 会被噪音淹没。
        self._reported_prunes: set[str] = set()
        # 最近一次组装出的视图长度：压缩阈值比较的就是这个数。
        self._view_chars = 0
        self._previous_messages: list[dict[str, Any]] | None = None
        # 每次成功压缩自增：调用方据此知道旧工具结果可能已不在上下文中。
        self.compaction_serial = 0
        # 压缩是 agent 里最贵的操作之一，必须留下可观测的痕迹。
        self.event_sink = event_sink if event_sink is not None else NullEventSink()

    def add_user_request(self, query: str) -> None:
        self.history.append({"role": "user", "content": query})

    def add_assistant_turn(self, turn: AssistantTurn) -> None:
        self.history.append(
            {
                "role": "assistant",
                "content": turn.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in turn.tool_calls
                ],
            }
        )

    def add_tool_result(self, call: ToolCall, result: dict[str, Any]) -> None:
        """Append the tool result **exactly as the tool returned it**.

        不在摄入时截断。工具自身的返回上限（`readfile` 64 KiB / DSH 是 2000 行或
        50 KiB）已经约束了单结果大小，而"上下文放不放得下"是**组装 prompt 时**才
        知道的事。摄入时截断的代价实测很重：一个 574 行 / 22.7 KB 的文件在 96 KB 的
        视图里明明放得下，却被切成 4 段，模型要读 5 次；同一个文件在 DSH 的 read 下
        是**一次**读完。

        截断推迟到真有压力时做（`_history_view`），那时它仍然是确定性的：同一份历史、
        同一个预算，修剪结果逐字符唯一。
        """
        self.history.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    def invalidate_compaction(self) -> None:
        """Drop the current handoff view after repository state changes.

        ``history`` is intentionally preserved. The next prompt rebuilds from
        the complete history and the updated memory instead of reusing facts
        summarized before a write or patch.
        """
        if not self._compaction["summary"] and not self._compaction["ledger"] and self._compaction["covered"] == 0:
            return
        self._compaction = {"covered": 0, "summary": "", "ledger": {}}
        if self.memory is not None:
            self.memory.data["compaction"] = {"covered": 0, "summary": "", "ledger": {}}
        self._compaction_attempted = False
        self._compaction_retry_chars = 0
        self.compaction_serial += 1

    def messages(self, query: str | None = None) -> list[dict[str, Any]]:
        if self.memory is None:
            return copy.deepcopy(self.system_messages + self.history)
        view = self._history_view()
        result = self.system_messages + view + [{"role": "user", "content": self._memory_snapshot(query or "")}]
        if query is not None and not _contains_user_request(result, query):
            # Trimming drops the oldest groups first, and the request being
            # answered is the oldest group of all. Re-append it so the model is
            # never asked to continue without the question itself.
            result.append({"role": "user", "content": query})
        return copy.deepcopy(result)

    def prompt_metrics(self, messages: list[dict[str, Any]]) -> dict[str, int]:
        """Measure an already-built prompt; rebuilding it would re-run the summarizer.

        ``transcript_chars`` 是**视图本身**（交接摘要 + 历史），也就是压缩阈值真正
        比较的那个数。它和 ``prompt_chars`` 的差是静态 system 消息加上本轮的记忆
        快照——把快照算进 transcript 会让审计者拿它去比阈值时得出错误结论。
        """
        static = len(self.system_messages)
        snapshot_chars = 0
        for message in reversed(messages):
            if str(message.get("content", "")).startswith("[Memory]"):
                snapshot_chars = len(str(message.get("content", "")))
                break
        stable = self._stable_prefix_chars(messages)
        self._previous_messages = copy.deepcopy(messages)
        return {"snapshot_chars": snapshot_chars, "history_covered": self._compaction["covered"], "dropped_groups": self._dropped_groups, "stable_prefix_chars": stable, "transcript_chars": self._view_chars, "compaction_threshold_chars": self.compaction_threshold_chars, "prompt_chars": len(json.dumps(messages, ensure_ascii=False))}

    def _stable_prefix_chars(self, messages: list[dict[str, Any]]) -> int:
        """Characters shared with the previous round's prompt (cache evidence)."""
        previous = self._previous_messages
        if previous is None:
            return 0
        common = 0
        for old, new in zip(previous, messages):
            old_text = json.dumps(old, ensure_ascii=False, sort_keys=True)
            new_text = json.dumps(new, ensure_ascii=False, sort_keys=True)
            if old_text == new_text:
                common += len(new_text)
                continue
            for old_char, new_char in zip(old_text, new_text):
                if old_char != new_char:
                    break
                common += 1
            break
        return common

    @property
    def compaction_floor_chars(self) -> int:
        """压缩后视图的下界：目标线 + 交接摘要上限 + 台账上限。

        必须严格小于触发线，而且余量要够大——否则压缩刚做完就再次越线，每轮调一次
        LLM 写摘要（抖动）。真实故障：地板 10000 > 阈值 9600。
        """
        return self.compaction_target_chars + SPAN_SUMMARY_MAX_CHARS + SPAN_LEDGER_MAX_CHARS

    def _handoff_text(self) -> str:
        """LLM 叙述 + 机器生成的进度台账，合成一条交接消息。

        两段分开渲染而不是让 LLM 一起写：台账里的路径和行区间本来就躺在工具调用
        参数里，确定性生成既不会漏也不会编，同一份历史还能逐字符复现。
        """
        ledger = _render_ledger(self._compaction.get("ledger") or {})
        return f"{self._compaction['summary']}\n\n{ledger}".strip() if ledger else self._compaction["summary"]

    def _memory_snapshot(self, query: str) -> str:
        """Working-set reminder, rebuilt each round but placed at the tail so the
        append-only history in front of it stays a stable cache prefix."""
        return "\n\n".join([
            self.memory.render_memory(),
            self.memory.render_relevant_memory(query),
            self.memory.render_file_summaries(query),
        ])

    def _history_view(self) -> list[dict[str, Any]]:
        excluded: set[int] = set()
        for item in self.history_exclusions:
            if not isinstance(item, dict):
                continue
            try:
                start = max(0, int(item.get("start", 0)))
                end = max(start, int(item.get("end", start)))
            except (TypeError, ValueError):
                continue
            excluded.update(range(start, min(end, len(self.history))))
        visible_history = [message for index, message in enumerate(self.history) if index not in excluded]
        covered = min(self._compaction["covered"], len(visible_history))
        view: list[dict[str, Any]] = []
        if self._compaction["summary"]:
            view.append({"role": "user", "content": self._handoff_text()})
        view.extend(copy.deepcopy(visible_history[covered:]))
        if _encoded_len(view) <= self.compaction_threshold_chars:
            self._view_chars = _encoded_len(view)
            return view
        # 有压力了。第一级响应：按需修剪超大的工具结果（纯本地规则，不调模型）。
        # 只修到装得下为止、从最大的开始——不把所有超预算的结果一次全砍掉，那会
        # 白白丢掉本来放得下的内容。
        pruned = self._prune_to_fit(view)
        if _encoded_len(pruned) <= self.compaction_threshold_chars:
            self._view_chars = _encoded_len(pruned)
            return pruned
        # 第二级响应：修剪还不够，才动 LLM 段落摘要。
        merged = self._merge_oldest(pruned, covered)
        self._view_chars = _encoded_len(merged)
        return merged

    def _prune_targets(self) -> list[int]:
        """Progressively tighter size targets for the local pruning tier.

        第一档是常规粒度（处理"有超大结果"的常见情况）。如果一轮压完仍然越线，
        就把目标**减半再来一轮**，一直降到下限为止——因为一轮修剪的**下限是
        N × 目标**：结果数量一多（默认阈值 96000 ÷ 粒度 6000 ≥ 16 个），第一档
        无论如何都压不到阈值以下，于是一整档本地压缩被跳过、直接落到 LLM 摘要，
        而行级细节一旦进了摘要就永久丢了（P0）。收紧一轮能省掉这次模型调用，
        而且保住"首尾真代码 + 缺口行号 + 续读区间"。
        """
        budget = self.tool_result_budget_chars
        floor = max(_MIN_TIGHTEN_CHARS, budget // 8)
        targets = [budget]
        current = budget
        while current > floor:
            current = max(floor, current // 2)
            if current not in targets:
                targets.append(current)
        return targets

    def _prune_to_fit(self, view: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Shrink oversized tool results until the view fits, biggest first.

        只在视图已经越线时调用：没有压力就一个字节都不动（模型读 22.7 KB 的文件
        就该一次读完）。修剪本身是确定性的，而且**不改动 history**——原文仍在
        `session.history` 与当次 run 的 `report.json` 里。

        每一档都从**原文**重算 stub，绝不在已压过的 stub 上再压：stub 的 content 是
        "文件头 + 文件尾"的拼接，不是连续区间，在它上面再算 omitted_lines 会算错。
        """
        originals: dict[int, dict[str, Any]] = {}
        for index, message in enumerate(view):
            if message.get("role") != "tool":
                continue
            try:
                parsed = json.loads(str(message.get("content", "")))
            except json.JSONDecodeError:
                continue
            # **全部**结果都留着：常规粒度看不到"略小于粒度"的结果，但更紧的那一档
            # 要能把它们收进来。
            if isinstance(parsed, dict):
                originals[index] = parsed
        # 候选按原始大小降序；同尺寸按下标，保证确定性。
        for target in self._prune_targets():
            if _encoded_len(view) <= self.compaction_threshold_chars:
                break
            candidates = self._prune_candidates(view, originals, target)
            for index in candidates:
                if _encoded_len(view) <= self.compaction_threshold_chars:
                    break
                message = view[index]
                original = originals[index]
                name = resolve_tool_name(view, str(message.get("tool_call_id")))
                stub = _fit_json(_compact_result(name, original, self.memory), target)
                if len(stub) >= len(str(message.get("content", ""))):
                    continue
                message["content"] = stub
        for index, original in originals.items():
            current = str(view[index].get("content", ""))
            before = len(json.dumps(original, ensure_ascii=False))
            if current != json.dumps(original, ensure_ascii=False):
                self._report_pruned(str(view[index].get("tool_call_id")), resolve_tool_name(view, str(view[index].get("tool_call_id"))), before, len(current))
        return view

    def _prune_candidates(self, view: list[dict[str, Any]], originals: dict[int, dict[str, Any]], target: int) -> list[int]:
        """Indices可被这一档压小的结果，最大的优先（已压到目标以下的不再入选）。"""
        selected = []
        for index, parsed in originals.items():
            raw_size = len(json.dumps(parsed, ensure_ascii=False))
            if raw_size <= target:
                continue
            if len(str(view[index].get("content", ""))) <= target:
                continue
            selected.append(index)
        return sorted(selected, key=lambda index: (-len(str(view[index].get("content", ""))), index))

    def _report_pruned(self, call_id: str, tool: str, before: int, after: int) -> None:
        """Emit one event per pruned result, no matter how many rounds re-derive it."""
        if call_id in self._reported_prunes:
            return
        self._reported_prunes.add(call_id)
        self.event_sink.emit(
            "context_compressed",
            kind="tool_result",
            tool=tool,
            before_chars=before,
            after_chars=after,
        )

    def _merge_oldest(self, view: list[dict[str, Any]], covered: int) -> list[dict[str, Any]]:
        """Absorb the oldest span into one persisted handoff message.

        The view only ever changes at its front through a *successful, persisted*
        compaction; without one the M1 view is returned unchanged so the prompt
        stays append-only between compactions.
        """
        prior_summary = self._compaction["summary"]
        prefix_count = 1 if prior_summary else 0
        body = view[prefix_count:]
        groups = _group_messages(body)
        sizes = [_encoded_len(group) for group in groups]
        kept_size = sum(sizes)
        absorb = 0
        # 最近两个 group 永不吸收：模型需要原文形式的最新工具结果。
        min_kept = 2
        # 压到 target **以下**为止，而不是"再压一组就低于 target"就停。
        # 旧写法（`kept_size - sizes[absorb] > target`）会让压缩后的视图落在
        # target 和 target+一组 之间：实测 5 次压缩后剩 15594~26439，地板理想值
        # 13500 从来没达到，于是两轮后又越线。一组 = 1 条 assistant + 它全部工具
        # 结果，可以很大，所以"差一组"的误差不能留给它。
        while absorb < len(groups) - min_kept and kept_size > self.compaction_target_chars:
            kept_size -= sizes[absorb]
            absorb += 1
        if absorb == 0 or self.compaction_summarizer is None:
            return view
        if self._compaction_attempted and _encoded_len(view) < self._compaction_retry_chars:
            # 一次失败不该关闭整个 run 的压缩；等视图明显再长一段再重试。
            return view
        span = [message for group in groups[:absorb] for message in group]
        kept_messages = [message for group in groups[absorb:] for message in group]
        # Phase A: stub every result in the span (a medium readfile result
        # collapses onto its file summary) so the summarizer input is bounded.
        stubbed_span = _stub_history(span, self.memory, self.tool_result_budget_chars, force=True)
        merged = ([{"role": "user", "content": prior_summary}] if prior_summary else []) + stubbed_span
        span_started = time.perf_counter()
        try:
            text = self.compaction_summarizer(merged).strip()
        except Exception as exc:
            return self._note_span_failure(view, type(exc).__name__, str(exc)[:120])
        span_ms = round((time.perf_counter() - span_started) * 1000, 3)
        if not text:
            return self._note_span_failure(view, "empty_summary", "")
        ledger = _merge_ledger(self._compaction.get("ledger") or {}, _ledger_for_span(span))
        handoff = {"role": "user", "content": f"[Context Handoff]\n{text}\n\n{_render_ledger(ledger)}".rstrip()}
        new_view = [handoff] + kept_messages
        # 硬校验：交接摘要必须比它替代的内容更小，否则放弃本次压缩。
        if _encoded_len(new_view) >= _encoded_len(view):
            return self._note_span_failure(view, "not_smaller", f"{_encoded_len(new_view)} >= {_encoded_len(view)}")
        new_covered = covered + len(span)
        self._compaction = {"covered": new_covered, "summary": f"[Context Handoff]\n{text}", "ledger": ledger}
        self.compaction_serial += 1
        if self.memory is not None:
            self.memory.data["compaction"] = {"covered": new_covered, "summary": self._compaction["summary"], "ledger": ledger, "created_at": _utc_now()}
        self.event_sink.emit(
            "context_compressed",
            kind="span",
            covered=new_covered,
            absorbed_messages=len(span),
            before_chars=_encoded_len(view),
            after_chars=_encoded_len(new_view),
            duration_ms=span_ms,
        )
        self._compaction_attempted = False
        self._compaction_retry_chars = 0
        return new_view

    def _note_span_failure(self, view: list[dict[str, Any]], reason: str, detail: str) -> list[dict[str, Any]]:
        """Record a failed span compaction and schedule a later retry.

        失败必须可见——静默失败会让 prompt 无限增长而无人察觉（真实 run 里
        prompt 因此涨到 6.6 万字符）。同时不能一失败就永久关闭：视图再长
        50% 之后允许重试。
        """
        size = _encoded_len(view)
        self._compaction_attempted = True
        self._compaction_retry_chars = int(size * 1.5)
        self.event_sink.emit(
            "context_compressed",
            kind="span_failed",
            reason=reason,
            detail=detail,
            view_chars=size,
            retry_at_chars=self._compaction_retry_chars,
        )
        return view


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _contains_user_request(messages: list[dict[str, Any]], query: str) -> bool:
    """Return whether the assembled prompt already carries the current request."""
    return any(
        message.get("role") == "user" and message.get("content") == query
        for message in messages
    )


def _encoded_len(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False))


def _group_messages(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group assistant tool calls with their results so pairing never breaks."""
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            group = [message]
            ids = {call.get("id") for call in message.get("tool_calls", [])}
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool" and messages[index].get("tool_call_id") in ids:
                group.append(messages[index]); index += 1
            groups.append(group)
        else:
            groups.append([message]); index += 1
    return groups


def resolve_tool_name(messages: list[dict[str, Any]], tool_call_id: str) -> str:
    """Find the tool name a result belongs to, for tool-specific stubs."""
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls", []):
            if str(call.get("id")) == tool_call_id:
                return str(call.get("function", {}).get("name", "tool"))
    return "tool"


def _stub_history(
    messages: list[dict[str, Any]],
    memory: MemoryManager,
    tool_result_budget: int,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Replace oversized tool results with deterministic local stubs.

    单个工具结果一律用**本地规则**压缩，不调用模型。原因是确定性：本地规则是
    纯函数，同一份历史在任何一轮、任何一次运行都会算出逐字符相同的 stub，而
    prompt cache 按精确前缀匹配——LLM 概括每次措辞都不同，会让第一条 stub 之后
    的全部内容失效，也让同一份 session 无法复现出同一个 prompt。主流实现
    （Codex / Claude Code / DSH）对单个工具结果同样是确定性处理；LLM 只用在
    段落交接摘要这一层。

    ``force`` compacts every tool result regardless of size (used when merging a
    span); a stub that is not smaller than the original is discarded. A forced
    merge also drops ``readfile`` content: the span it belongs to is about to be
    replaced by a handoff summary, so what matters there is the file summary and
    the line index, not the raw lines (and the stub must stay strictly smaller
    than the original, otherwise it is thrown away and the summary is lost).
    """
    stubbed = copy.deepcopy(messages)
    for message in stubbed:
        if message.get("role") != "tool":
            continue
        original = str(message.get("content", ""))
        oversized = len(original) > tool_result_budget
        if not oversized and not force:
            continue
        if content_is_compressed(message):
            continue      # 摄入时已压好，不再重复计算
        try:
            result = json.loads(message.get("content", "{}"))
        except (TypeError, json.JSONDecodeError):
            result = {"ok": False}
        name = resolve_tool_name(messages, str(message.get("tool_call_id")))
        content = _fit_json(_compact_result(name, result, memory, keep_content=not force), tool_result_budget)
        if len(content) < len(original):
            message["content"] = content
    return stubbed


def content_is_compressed(message: dict[str, Any]) -> bool:
    """Whether a tool result already carries a stub written at ingestion time."""
    try:
        return json.loads(str(message.get("content", "{}"))).get("compressed") is True
    except (TypeError, json.JSONDecodeError):
        return False


def _fit_json(value: dict[str, Any], budget: int) -> str:
    """Keep a compressed result valid JSON while respecting its character quota.

    配额先分给内容，剩下的才留给元数据——先算"去掉内容型字段之后还剩多少"，
    再按这个余量截断内容：

    * **内容型字段**（``content`` / ``stdout_preview`` / ``stderr_preview``）是
      结果本身，只能按首尾截断，不能整块丢。整块丢掉等于模型这次读取白做了，
      它会换个更小的区间把同一个文件再读一遍——真实 run 里 readfile 的 30 次
      调用配额就是这样耗尽的。
    * **元数据**（文件摘要、行号索引、状态标志）只占几十个字符，而且文件摘要是
      LLM 提炼出来的、丢了就得重新提炼，所以它们排在内容后面被牺牲，只有连
      内容的余量都挤不出来时才按用处从小到大丢。

    连元数据都装不下（预算被调到几百字符）时退到不含内容的 stub，而不是最小
    占位符：摘要和行号索引仍然是对模型有用的导航信息。
    """
    candidate = copy.deepcopy(value)
    for key in _DEGRADABLE_KEYS:
        if _payload_budget(candidate, budget) >= _MIN_PAYLOAD_CHARS:
            break
        candidate.pop(key, None)
    encoded = _trim_payloads(candidate, budget)
    if encoded is not None:
        return encoded
    for key in (*_PAYLOAD_KEYS, "omitted_lines", "next_action_hint", "sample_matches", "sample_paths"):
        candidate.pop(key, None)
    encoded = json.dumps(candidate, ensure_ascii=False)
    if len(encoded) <= budget:
        return encoded
    if "observation" in candidate:
        candidate["observation"] = str(candidate["observation"])[: max(0, budget // 3)]
        encoded = json.dumps(candidate, ensure_ascii=False)
        if len(encoded) <= budget:
            return encoded
        candidate.pop("observation", None)
        encoded = json.dumps(candidate, ensure_ascii=False)
        if len(encoded) <= budget:
            return encoded
    # Minimal valid payload; a marker makes the loss explicit to the model.
    minimal = {"ok": candidate.get("ok", False), "compressed": True, "truncated": True}
    return json.dumps(minimal, ensure_ascii=False)


# 内容型字段：这些东西是"结果本身"，只能截断、不能整个丢。
# 字符串按首尾截断，列表按条数截断——两种都是"尽量多留"。
_STRING_PAYLOAD_KEYS = ("content", "stdout_preview", "stderr_preview")
_LIST_PAYLOAD_KEYS = ("sample_matches", "sample_paths")
_PAYLOAD_KEYS = (*_STRING_PAYLOAD_KEYS, *_LIST_PAYLOAD_KEYS)
# 元数据字段：按用处从小到大排列。文件摘要（LLM 提炼）和行号索引最后才丢，
# 因为重新获得它们要再花一次模型调用。next_action_hint 不在这里——它是内容
# 真被截断时由 _trim_payloads 现算的，算出来就是准确的。
_DEGRADABLE_KEYS = ("output_truncated", "timed_out", "line_count", "truncated", "line_index", "summary")
# 内容至少要有这么多个字符，否则截断出来的碎片对模型没有意义，不如省下配额
# 留给元数据。
_MIN_PAYLOAD_CHARS = 200
# 省略标记本身占的字符数（"... [123 lines omitted] ..."）。
_OMISSION_MARKER_CHARS = 40
# 估算列表型内容每条占多少字符，用来把配额换算成"能留几条"。
_ENTRY_CHARS = 80
# 收紧修剪的目标下限。再小下去 stub 就只剩元数据，等于告诉模型"这次读取白做了"。
_MIN_TIGHTEN_CHARS = 600


def _payload_budget(candidate: dict[str, Any], budget: int) -> int:
    """How many characters are left for content once metadata is accounted for."""
    skeleton = {key: value for key, value in candidate.items() if key not in _PAYLOAD_KEYS}
    return budget - len(json.dumps(skeleton, ensure_ascii=False)) - _OMISSION_MARKER_CHARS


def _trim_payloads(candidate: dict[str, Any], budget: int) -> str | None:
    """按首尾（字符串）或条数（列表）截断内容型字段，直到整个 JSON 装进配额。

    没有内容型字段可截时，直接按当前长度决定成败——**不能因为"没东西可截"就
    跳到丢弃内容的那条路**，否则 search / listfiles 这类只有列表没字符串的结果
    会被整块剥光，只剩一个 match_count。

    每个字段分到的份额从"余量 / 字段数"起步，按实际超出量收敛，因此同一次输入
    永远算出同一个字符串（prompt cache 需要这个确定性）。行号提示会加进 JSON，
    所以每轮都重新量总长，而不是只量内容。
    """
    strings = [key for key in _STRING_PAYLOAD_KEYS if isinstance(candidate.get(key), str) and candidate[key]]
    lists = [key for key in _LIST_PAYLOAD_KEYS if isinstance(candidate.get(key), list) and candidate[key]]
    if not strings and not lists:
        encoded = json.dumps(candidate, ensure_ascii=False)
        return encoded if len(encoded) <= budget else None
    originals = {key: candidate[key] for key in (*strings, *lists)}
    share = _payload_budget(candidate, budget) // len(originals)
    while share >= 60:
        for key in strings:
            if key == "content":
                window, head_lines, tail_lines = _line_window(originals[key], share)
                candidate[key] = window
                if window != originals[key]:
                    _annotate_omitted_lines(candidate, head_lines, tail_lines, share, _average_line_chars(originals[key]))
                else:
                    candidate.pop("omitted_lines", None)
                    candidate.pop("next_action_hint", None)
            else:
                candidate[key] = _preview(originals[key], share * 2 // 3, share // 3)
        for key in lists:
            candidate[key] = originals[key][: max(1, share // _ENTRY_CHARS)]
        encoded = json.dumps(candidate, ensure_ascii=False)
        if len(encoded) <= budget:
            return encoded
        reduced = share - max(1, len(encoded) - budget)
        if reduced >= share:
            break
        share = reduced
    return None


def _average_line_chars(text: str) -> float:
    """Average characters per line of the content that was just truncated.

    用它把"还剩多少字符配额"换算成"还能读多少行"。实测比按固定行数猜准：同一份
    配额下，33 字符/行的文件能读约 170 行，165 字符/行只能读约 34 行。
    """
    return len(text) / max(1, text.count("\n") + 1)


def _line_window(text: str, budget: int) -> tuple[str, int, int]:
    """Keep both ends of ``text`` by whole lines; also report the kept line counts.

    按行切割才能让"省略了第几行"变成可执行的下一步动作。整段只有一行时退化成
    按字符保留首尾，此时行号没有意义，返回的计数留 0。
    """
    lines = text.split("\n")
    if len(lines) < 3 or len(text) <= budget:
        if len(text) <= budget:
            return text, 0, 0
        return _preview(text, budget * 2 // 3, budget // 3), 0, 0
    head_budget = budget * 2 // 3
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > budget - head_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()
    if not head and not tail:
        return _preview(text, budget * 2 // 3, budget // 3), 0, 0
    if len(head) + len(tail) >= len(lines):
        return text, 0, 0
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"... [{omitted} lines omitted] ...", *tail]), len(head), len(tail)


def _readable_window(share: int, avg_line: float) -> int:
    """How many lines the next readfile can take without being truncated again.

    这一段提示唯一的目标就是"照着读，下一次就整段看得见"，所以窗口要按**这份文件
    自己的平均行长**算：

    * 写死行数不行——100 行在平均 33 字符的文件上刚好装得下，在平均 165 字符的
      文件上会立刻触发第二次截断。
    * 只给一半配额也不行——实测里按 2/3 配额算窗口会在每次调用浪费 1/3 预算，
      补齐同一个缺口要多花一轮（5 次 vs 3 次），而轮数才是稀缺资源。

    下限不能设成"看起来合理"的行数（我先试过 20）：平均行长 165 字符的文件上，
    20 行要 3300 字符，远超配额，提示会立刻引发第二次截断——正好违背它存在的
    理由。所以窗口只保证 ≥1 行：配额再小也只是读得慢，不会读不到。
    """
    usable = share - _OMISSION_MARKER_CHARS
    return max(1, int(usable // max(1.0, avg_line)))


def _annotate_omitted_lines(candidate: dict[str, Any], head_lines: int, tail_lines: int, share: int, avg_line: float) -> None:
    """Tell the model exactly which lines are missing, and how to read them next.

    行号只在 readfile 的 stub 里有意义（``start``/``end`` 是这次读取的区间，内容
    带行号前缀）。没有这两个字段就只留一句泛泛的提示，绝不编造行号。
    """
    if head_lines and tail_lines:
        start, end = candidate.get("start"), candidate.get("end")
        if isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool) and not isinstance(end, bool):
            first, last = start + head_lines, end - tail_lines
            if first <= last:
                window = _readable_window(share, avg_line)
                candidate["omitted_lines"] = [first, last]
                candidate["next_action_hint"] = f"第 {first}-{last} 行未显示；用 readfile 传 start={first} end={min(last, first + window - 1)} 继续读取。"
                return
    candidate["next_action_hint"] = "内容过长已截断；用 readfile 的 start / end 读取更小的行范围。"


# 进度台账的上限。它和摘要一起构成"压缩后视图地板"的一部分。
SPAN_LEDGER_MAX_CHARS = 1200
# 台账里最多保留多少个路径 / 多少条命令。
_LEDGER_MAX_PATHS = 12
_LEDGER_MAX_COMMANDS = 8
# 台账小节标题。摘要器看不到它（它由机器生成），但审计和测试要靠它定位。
LEDGER_HEADER = "[Progress Ledger]"
LEDGER_PREAMBLE = "以下内容曾经在上下文中，现已被上方摘要替代。需要细节时按区间定向重读，不要整读。"


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[list[int]]:
    """Merge overlapping/adjacent ``(start, end)`` pairs, sorted."""
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _seen_ranges(payload: dict[str, Any], start: int, end: int) -> tuple[bool, list[tuple[int, int]]]:
    """Which lines of a readfile result the model actually had in front of it.

    台账记的必须是**看见过的行**，不是请求的区间：被截断的读取只留下首尾两段
    （`omitted_lines` 标出中间那段没给），把请求区间记成"已读"会让模型以为
    自己掌握了实际上没拿到的内容。真实 run 里就出现过这种误记。
    """
    omitted = payload.get("omitted_lines")
    count = payload.get("line_count")
    truncated = isinstance(omitted, list) and len(omitted) == 2
    if not truncated and start == 1 and end == count:
        return True, []
    if not truncated:
        return False, [(start, end)]
    first, last = int(omitted[0]), int(omitted[1])
    seen: list[tuple[int, int]] = []
    if start <= first - 1:
        seen.append((start, first - 1))
    if last + 1 <= end:
        seen.append((last + 1, end))
    return False, seen


def _tool_call_pairs(messages: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair every assistant tool call with its tool result inside one span."""
    by_id = {str(message.get("tool_call_id")): message for message in messages if message.get("role") == "tool"}
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            result = by_id.get(str(call.get("id")))
            if result is not None:
                pairs.append((call, result))
    return pairs


def _ledger_for_span(span: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive "what was already done" from the calls inside one absorbed span.

    Deterministic on purpose: the facts (paths, line ranges, commands) are sitting
    in the tool-call arguments, so asking the model to recite them would only add
    omission and invention. Machine-generated also means byte-identical to any
    other replay of the same span.
    """
    ledger: dict[str, Any] = {"read": {}, "wrote": [], "ran": []}
    for call, result in _tool_call_pairs(span):
        name = str(call.get("function", {}).get("name") or "")
        arguments = call.get("function", {}).get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            continue
        try:
            payload = json.loads(str(result.get("content", "{}")))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            continue
        path = payload.get("path") or arguments.get("path")
        if name == "readfile" and isinstance(path, str):
            entry = ledger["read"].setdefault(path, {"whole": False, "lines": None, "ranges": []})
            start = payload.get("start")
            end = payload.get("end")
            lines = payload.get("line_count")
            if isinstance(lines, int) and not isinstance(lines, bool):
                entry["lines"] = entry["lines"] or lines
            if isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool) and not isinstance(end, bool):
                whole, seen = _seen_ranges(payload, start, end)
                if whole:
                    entry["whole"] = True
                    entry["ranges"] = []
                elif not entry["whole"]:
                    entry["ranges"].extend(seen)
        elif name in {"write_file", "patch_file"} and isinstance(path, str):
            ledger["wrote"].append(path)
        elif name == "shell":
            program = str(arguments.get("program") or "")
            command = " ".join([program, *(str(item) for item in arguments.get("args") or [])]).strip()
            if command:
                ledger["ran"].append(command[:80])
    return ledger


def _merge_ledger(previous: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    """Fold a new span's ledger into the persisted one, merging line ranges."""
    read: dict[str, Any] = {}
    for source in (previous.get("read") or {}, addition.get("read") or {}):
        for path, entry in source.items():
            if not isinstance(entry, dict):
                continue
            current = read.setdefault(str(path), {"whole": False, "lines": None, "ranges": []})
            if entry.get("lines") and not current["lines"]:
                current["lines"] = entry["lines"]
            if entry.get("whole"):
                current["whole"] = True
                current["ranges"] = []
                continue
            if current["whole"]:
                continue
            current["ranges"].extend(tuple(item) for item in entry.get("ranges") or [] if len(item) == 2)
    # 最近更新的路径排在后面，截断时优先保留；字典顺序在 Python 里是插入序。
    ordered: dict[str, Any] = {}
    for path in list(read)[-_LEDGER_MAX_PATHS:]:
        entry = read[path]
        if entry["whole"]:
            ordered[path] = {"whole": True, "lines": entry["lines"], "ranges": []}
        else:
            ranges = _merge_ranges([(int(a), int(b)) for a, b in entry["ranges"]])
            if ranges:
                ordered[path] = {"whole": False, "lines": entry["lines"], "ranges": ranges}
    wrote = list(dict.fromkeys([*(previous.get("wrote") or []), *(addition.get("wrote") or [])]))[-_LEDGER_MAX_PATHS:]
    ran = list(dict.fromkeys([*(previous.get("ran") or []), *(addition.get("ran") or [])]))[-_LEDGER_MAX_COMMANDS:]
    return {"read": ordered, "wrote": wrote, "ran": ran}


def _render_ledger(ledger: dict[str, Any]) -> str:
    """Render the ledger as a bounded, deterministic text block."""
    read = ledger.get("read") or {}
    if not read and not ledger.get("wrote") and not ledger.get("ran"):
        return ""
    lines = [LEDGER_HEADER, LEDGER_PREAMBLE]
    if read:
        parts = []
        for path, entry in read.items():
            if entry.get("whole"):
                size = f"全文 {entry['lines']} 行" if entry.get("lines") else "全文"
                parts.append(f"{path} ({size})")
            else:
                spans = ", ".join(f"{start}-{end}" for start, end in entry.get("ranges") or [])
                parts.append(f"{path} ({spans})")
        lines.append("read: " + "; ".join(parts))
    if ledger.get("wrote"):
        lines.append("wrote: " + ", ".join(map(str, ledger["wrote"])))
    if ledger.get("ran"):
        lines.append("ran: " + ", ".join(map(str, ledger["ran"])))
    return "\n".join(lines)[:SPAN_LEDGER_MAX_CHARS]


def _preview(text: str, head: int, tail: int) -> str:
    """Keep both ends of a long output, marking what was dropped in between."""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [{omitted} chars omitted] ...\n{text[-tail:]}"


def _compact_result(name: str, result: dict[str, Any], memory: MemoryManager, keep_content: bool = True) -> dict[str, Any]:
    """每个分支都只负责"这次调用的结果长什么样"，失败原因统一在后面补。

    失败原因（``error_type`` / ``error_message``）在压缩里的优先级高于一切：
    一个只写着 ``ok: false`` 的 stub 会让模型完全不知道刚才为什么失败，只能把
    同一条调用再发一遍。所以它由 ``_preserve_error`` 统一附加，任何工具分支都
    不会漏掉。
    """
    if name == "readfile":
        path = result.get("path")
        summary = memory.data.get("file_summaries", {}).get(path, {}) if isinstance(path, str) else {}
        stub = {"ok": result.get("ok", False), "path": path, "compressed": True, "summary": summary.get("summary"), "start": result.get("start"), "end": result.get("end"), "line_count": result.get("line_count"), "truncated": result.get("truncated", False)}
        # 代码本身才是这个结果的主体。只留元数据的 stub 会让模型看不到任何代码，
        # 于是换个更小的区间把同一个文件再读一遍；超预算时由 _fit_json 按行保留
        # 首尾两段，并把被省略的行号写成可以照着做的下一步。
        if keep_content and isinstance(result.get("content"), str):
            stub["content"] = result["content"]
        # 行号索引只是导航辅助：有配额时装得下就留着，装不下先丢它。
        line_index = summary.get("line_index")
        if isinstance(line_index, list) and line_index:
            stub["line_index"] = line_index[:5]
        return _preserve_error(stub, result)
    if name == "search":
        matches = result.get("matches") if isinstance(result.get("matches"), list) else []
        # 命中的**行内容**才是 search 的价值所在：只给"7 matches"模型什么也做不了。
        # 超出配额时由 _fit_json 按条数裁，不用写死 5 条。
        samples = [
            {"path": match.get("path"), "line": match.get("line"), "text": str(match.get("text", ""))[:200]}
            for match in matches
            if isinstance(match, dict)
        ]
        return _preserve_error({"ok": result.get("ok", False), "compressed": True, "match_count": len(matches), "sample_matches": samples, "truncated": result.get("truncated", False)}, result)
    if name == "shell":
        # 本地规则是唯一的单结果压缩路径，所以预览必须保留**两端**：pytest 这类
        # 工具的关键信息（失败用例名、汇总行）在末尾，错误上下文在开头。只留头部
        # 会正好丢掉最有用的那段。
        output = {"ok": result.get("ok", False), "compressed": True, **{key: result.get(key) for key in ("exit_code", "timed_out", "output_truncated")}, "stdout_preview": _preview(str(result.get("stdout", "")), 600, 300), "stderr_preview": _preview(str(result.get("stderr", "")), 400, 200)}
        if isinstance(result.get("observation"), str):
            output["observation"] = result["observation"]
        return _preserve_error(output, result)
    if name in {"listfiles", "find_files"}:
        # listfiles 的结果键是 `entries`（{path, kind, size}），find_files 是 `files`
        # （字符串列表）。读错键会让 stub 写着 file_count: 0 —— 那不是丢信息，是给出
        # 错误信息：模型问"目录里有什么"，得到"0 个文件"。
        values = result.get("entries") if isinstance(result.get("entries"), list) else result.get("files")
        paths = _entry_paths(values if isinstance(values, list) else [])
        return _preserve_error({"ok": result.get("ok", False), "compressed": True, "file_count": len(paths), "sample_paths": paths, "truncated": result.get("truncated", False)}, result)
    output = {"ok": result.get("ok", False), "compressed": True}
    if isinstance(result.get("observation"), str):
        output["observation"] = result["observation"]
    if isinstance(result.get("path"), str): output["path"] = result["path"]
    if isinstance(result.get("operation"), str): output["operation"] = result["operation"]
    for key in ("bytes_written", "replacements"):
        if key in result: output[key] = result[key]
    return _preserve_error(output, result)


def _entry_paths(values: list[Any]) -> list[str]:
    """Normalize listfiles ``entries`` / find_files ``files`` into plain paths.

    目录加尾斜杠是最省字符的"这是目录"表示法：不用多加一个字段，模型也一眼看得出
    哪些是能进去的目录、哪些是能读的文件。
    """
    paths: list[str] = []
    for value in values:
        if isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str):
                paths.append(f"{path}/" if value.get("kind") == "directory" else path)
        elif isinstance(value, str):
            paths.append(value)
    return paths


def _preserve_error(output: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Attach why the call failed; a stub without this makes the model retry blindly."""
    error = result.get("error")
    if isinstance(error, dict):
        output["error_type"] = error.get("type")
        message = error.get("message")
        if isinstance(message, str):
            output["error_message"] = message[:300]
    return output


def build_repository_context(
    workspace: Workspace,
    max_depth: int = 4,
    max_entries: int = 200,
) -> str:
    entries, truncated = scan_files(
        workspace,
        max_depth=max_depth,
        max_entries=max_entries,
    )
    lines = [
        "[Repository Context]",
        "root: <workspace>",
        "important_files:",
    ]
    # Keep the first prompt as a compact navigation map.  The model can use
    # readfile/listfiles/search to discover details on demand instead of
    # receiving every file in the repository manifest.
    important_paths = _important_file_paths(entries)
    for path in important_paths:
        lines.append(f"- {path}")
    lines.extend(_source_files_context(entries, important_paths))
    lines.extend(_candidate_dirs_context(entries))
    lines.extend(_ignored_runtime_dirs_context())
    lines.append(f"truncated: {str(truncated).lower()}")
    lines.extend(_important_files_context(workspace, entries))
    lines.extend(_git_status_context(workspace))
    lines.extend(_runtime_context(workspace))
    return "\n".join(lines)


def _source_files_context(entries: list[FileEntry], already_listed: list[str]) -> list[str]:
    """List source files **with sizes** so the model need not explore to find them.

    导航图存在的意义就是让模型不用先探索。原实现只列固定的元文件清单（README /
    pyproject / AGENTS.md…）外加硬编码的 `main.py`/`app.py`/`run.py`/`cli.py`，
    于是入口叫 `httpstat.py` 的仓库**一个源码文件都不在图上**——真实 run 里模型
    第一步只能发 `find_files httpstat.py` 去问"入口在哪"。

    带上大小是因为模型要据此决定"整读还是先 search 定位"：没有大小信息，一个
    23 KB 的文件和一个 200 B 的文件看起来一样。

    清单按（目录深度, 路径）排序，浅的优先（顶层文件信息量最高），条数和字符数
    都有上限——这段进的是**静态前缀**，每轮都要重发。
    """
    listed = {path for path in already_listed}
    candidates: list[tuple[int, str, int | None]] = []
    for entry in entries:
        if entry.kind != "file" or entry.path in listed:
            continue
        if not _looks_like_source(entry.path):
            continue
        candidates.append((len(Path(entry.path).parts), entry.path, entry.size))
    candidates.sort()
    selected = candidates[:_SOURCE_FILE_LIMIT]
    lines: list[str] = []
    for _, path, size in selected:
        lines.append(f"- {path} ({_format_size(size)})")
    rendered = "\n".join(lines)
    if len(rendered) > _SOURCE_FILES_MAX_CHARS:
        selected = selected[: max(1, len(selected) // 2)]
        lines = [f"- {path} ({_format_size(size)})" for _, path, size in selected]
    return ["source_files:"] + lines if lines else []


def _looks_like_source(path: str) -> bool:
    return Path(path).suffix.lower() in _SOURCE_SUFFIXES


def _format_size(size: int | None) -> str:
    if not isinstance(size, int) or size < 0:
        return "size unknown"
    if size < 1024:
        return f"{size} B"
    return f"{max(1, round(size / 1024))} KB"


def _important_file_paths(entries: list[FileEntry]) -> list[str]:
    """Return stable, high-signal files for the navigation map."""

    paths = list(_IMPORTANT_FILES)
    for entry in entries:
        if entry.kind != "file":
            continue
        name = Path(entry.path).name
        if name == "__main__.py" or name in _ENTRYPOINT_NAMES:
            if entry.path not in paths:
                paths.append(entry.path)
    return paths


def _candidate_dirs_context(entries: list[FileEntry]) -> list[str]:
    # Only advertise top-level directories.  Nested structure is intentionally
    # left to listfiles/search so the initial prompt remains small.
    preferred = ("src", "tests", "docs", "scripts", "examples", "tools")
    top_level = {
        entry.path.rstrip("/")
        for entry in entries
        if entry.kind == "directory" and "/" not in entry.path.rstrip("/")
    }
    ordered = [name for name in preferred if name in top_level]
    ordered.extend(sorted(top_level - set(ordered)))
    lines = ["", "candidate_dirs:"]
    lines.extend(f"- {name}/" for name in ordered[:8])
    if not ordered:
        lines.append("- (none detected)")
    return lines


def _ignored_runtime_dirs_context() -> list[str]:
    ignored = (".git", ".coding-agent", ".codex", ".venv", ".pytest_cache", "__pycache__", "node_modules", "dist", "build")
    return ["", "ignored_runtime_dirs:", *(f"- {name}/" for name in ignored), "", "navigation_hint:", "Use readfile for known files.", "Use search with the narrowest known directory.", "Use listfiles only when more structure is needed."]


def _runtime_context(workspace: Workspace) -> list[str]:
    venv = target_venv_dir(workspace)
    python = resolve_target_python(workspace)
    lines = ["", "[Runtime Environment]"]
    lines.append(f"target_venv: {'present' if venv.is_dir() else 'missing'}")
    lines.append(f"target_python: {python.relative_to(workspace.root).as_posix() if python else 'unavailable'}")
    lines.append("dependency_sync: approve 'uv sync --dev' when target dependencies are missing")
    return lines


def _important_files_context(workspace: Workspace, entries: list[FileEntry]) -> list[str]:
    paths = list(_IMPORTANT_FILES)
    paths.extend(
        entry.path
        for entry in entries
        if entry.kind == "file"
        and Path(entry.path).name == "__main__.py"
        and entry.path not in paths
    )
    paths.extend(
        entry.path
        for entry in entries
        if entry.kind == "file"
        and Path(entry.path).name in _ENTRYPOINT_NAMES
        and entry.path not in paths
    )

    lines = ["", "[Important Files]"]
    for path in paths:
        lines.append(f"### {path}")
        file_path = workspace.root / path
        if not file_path.is_file() or file_path.is_symlink():
            lines.append("status: missing")
            continue
        try:
            file_path.stat()
        except OSError:
            lines.append("status: unreadable")
            continue
        lines.append("status: present")
    return lines


def _git_status_context(workspace: Workspace) -> list[str]:
    lines = ["", "[Git Status]"]
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace.root), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        lines.append("status: unavailable")
        return lines
    status = result.stdout.strip()
    lines.append(status if status else "clean")
    return lines
