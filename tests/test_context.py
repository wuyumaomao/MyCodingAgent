"""ConversationContext 的契约测试。

2026-09-18 重建说明：这个文件在工作区被误用 `git checkout` 恢复过一次，本会话改动的
测试全部丢失（56 → 23）。重建时按**当前实现**的契约重写，并保留原有仍然成立的用例。
覆盖三块：

* 单结果 —— 摄入时**不截断**，只在视图越线后才按需修剪（`_prune_to_fit`）
* stub 形状 —— 内容优先、失败原因永不丢、缺口要给出可执行的续读区间
* 段落压缩 —— 分组不破、最近两组不吸收、压到目标线以下、交接摘要带机器生成的台账
"""

from __future__ import annotations

import json
import re

import pytest

from coding_agent.context import SPAN_LEDGER_MAX_CHARS, ConversationContext, build_repository_context
from coding_agent.memory import SPAN_SUMMARY_MAX_CHARS, MemoryManager
from coding_agent.models import AssistantTurn, ToolCall
from coding_agent.repository import Workspace
from coding_agent.session import SessionStore


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def make_context(sample_git_repo, *, transcript: int | None = None, tool_result: int = 6000, store_session: bool = True, window_tokens: int | None = None):
    workspace = Workspace(sample_git_repo)
    session = SessionStore(workspace).create() if store_session else None
    memory = MemoryManager(session) if store_session else None
    return ConversationContext(
        workspace,
        memory=memory,
        transcript_budget_chars=transcript,
        tool_result_budget_chars=tool_result,
        context_window_tokens=window_tokens,
    )


def add_read(context, path: str, *, start: int | None = None, end: int | None = None, chars: int = 1900, line_count: int = 2000, index: int = 0):
    arguments: dict[str, object] = {"path": path}
    if start is not None:
        arguments["start"] = start
        arguments["end"] = end
    call = ToolCall(f"r-{path}-{start}-{index}", "readfile", arguments)
    context.add_assistant_turn(AssistantTurn(None, [call]))
    context.add_tool_result(call, {
        "ok": True,
        "path": path,
        "content": "x" * chars,
        "start": start if start is not None else 1,
        "end": end if end is not None else line_count,
        "line_count": line_count,
    })


def tool_messages(messages):
    return [message for message in messages if message.get("role") == "tool"]


def handoff_message(messages):
    return next(m for m in messages if str(m.get("content", "")).startswith("[Context Handoff]"))


def fill_until_compaction(context, *, rounds: int = 8, chars: int = 4800, prefix: str = "src/filler"):
    """铺到**本地修剪无解**为止：结果小到压不动，只能靠段落摘要。

    2026-09-18 改：收紧修剪（`_prune_targets`）之后，阶段 2 不再被"少量中等结果"
    触发——那些本地就压下去了。要触发段落压缩，得让结果数量多到"连最紧的一档乘
    数量也超过阈值"。
    """
    for position in range(rounds):
        add_read(context, f"{prefix}_{position}.py", chars=chars, index=position)


# --------------------------------------------------------------------------- #
# 静态前缀与 prompt 组装
# --------------------------------------------------------------------------- #

def test_context_contains_root_and_relative_manifest(sample_git_repo):
    context = build_repository_context(Workspace(sample_git_repo))
    assert "Repository Context" in context
    assert "README.md" in context
    assert str(sample_git_repo) not in context


def test_context_marks_truncated_manifest(sample_git_repo):
    context = build_repository_context(Workspace(sample_git_repo), max_entries=1)
    assert "truncated: true" in context


def test_context_includes_important_files_entrypoint_env_and_git_status(sample_git_repo):
    (sample_git_repo / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
    (sample_git_repo / "AGENTS.md").write_text("Keep changes focused.\n", encoding="utf-8")
    (sample_git_repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
    context = build_repository_context(Workspace(sample_git_repo))
    assert "pyproject.toml" in context
    assert "main.py" in context
    assert "target_python" in context


def _source_section(context: str) -> str:
    """取出 `source_files:` 小节（到下一个小节标题为止）。"""
    if "source_files:\n" not in context:
        return ""
    block: list[str] = []
    for line in context.split("source_files:\n", 1)[1].splitlines():
        if not line.startswith("- "):
            break
        block.append(line)
    return "\n".join(block)


def test_navigation_map_lists_source_files_even_when_the_entrypoint_name_is_custom(sample_git_repo):
    """导航图必须列出源码文件——否则模型只能靠探索去找入口。

    真实 run 里踩到：httpstat 仓库的入口叫 `httpstat.py`，而导航图只列 README /
    pyproject / AGENTS.md 这些元文件（外加硬编码的 `main.py`/`app.py`/`run.py`/`cli.py`），
    于是模型第一步先发了一个 `find_files` 去问"httpstat.py 在哪"——**导航图存在的
    全部意义就是省掉这一步**。
    """
    (sample_git_repo / "httpstat.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (sample_git_repo / "pkg").mkdir()
    (sample_git_repo / "pkg" / "helper.py").write_text("x = 1\n", encoding="utf-8")

    context = build_repository_context(Workspace(sample_git_repo))
    section = _source_section(context)

    assert "httpstat.py" in section, section
    assert "pkg/helper.py" in section, section


def test_navigation_map_shows_file_sizes_so_the_model_can_judge_before_reading(sample_git_repo):
    """有大小，模型才能判断"这个文件该整读还是先定位"。"""
    (sample_git_repo / "small.py").write_text("x = 1\n", encoding="utf-8")
    (sample_git_repo / "large.py").write_text("y = 1\n" * 4000, encoding="utf-8")

    section = _source_section(build_repository_context(Workspace(sample_git_repo)))

    assert "small.py" in section
    assert "large.py" in section
    assert "KB" in section, f"没有大小信息: {section}"
    assert "B)" in section, f"小文件也该给大小: {section}"


def test_navigation_map_does_not_duplicate_the_important_files(sample_git_repo):
    """README.md 已经在 important_files 里，不该在源码清单里再出现一次。"""
    section = _source_section(build_repository_context(Workspace(sample_git_repo)))

    assert "README.md" not in section


def test_navigation_map_is_bounded(sample_git_repo):
    """源码清单要有上限，否则大仓库会把静态前缀撑爆。"""
    for index in range(60):
        (sample_git_repo / f"mod_{index:02d}.py").write_text("x = 1\n", encoding="utf-8")

    section = _source_section(build_repository_context(Workspace(sample_git_repo)))

    assert 0 < section.count("\n") + 1 <= 20, f"清单没有被限制:\n{section}"


def test_context_does_not_embed_a_large_important_file(sample_git_repo):
    """导航图只给路径，不把大文件内容塞进静态前缀。"""
    (sample_git_repo / "main.py").write_text("print('hello')\n" * 5000, encoding="utf-8")
    context = build_repository_context(Workspace(sample_git_repo))
    assert "main.py" in context
    assert "hello" * 100 not in context


def test_repository_instructions_are_injected_as_a_static_system_message(sample_git_repo):
    (sample_git_repo / "AGENTS.md").write_text("Run tests with: uv run pytest -q\n", encoding="utf-8")
    context = make_context(sample_git_repo)
    assert context.system_messages[-1]["role"] == "system"
    assert "[Repository Instructions]" in context.system_messages[-1]["content"]
    assert "uv run pytest -q" in context.system_messages[-1]["content"]


def test_missing_repository_instructions_add_no_message(sample_git_repo):
    context = make_context(sample_git_repo)
    assert all("[Repository Instructions]" not in str(m.get("content")) for m in context.system_messages)


def test_repository_instructions_are_capped_at_the_byte_limit(sample_git_repo):
    (sample_git_repo / "AGENTS.md").write_text("x" * 40000, encoding="utf-8")
    context = make_context(sample_git_repo)
    assert "truncated" in context.system_messages[-1]["content"]


def test_repository_instructions_ignore_symlinks(sample_git_repo, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = sample_git_repo / "AGENTS.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = make_context(sample_git_repo)
    assert all("[Repository Instructions]" not in str(m.get("content")) for m in context.system_messages)


def test_context_keeps_static_system_messages_and_appends_history(sample_git_repo):
    """历史追加在静态前缀之后；带记忆时快照压在最后。"""
    context = make_context(sample_git_repo)
    before = [dict(m) for m in context.system_messages]
    context.add_user_request("inspect")
    context.add_assistant_turn(AssistantTurn("ok", []))
    messages = context.messages("inspect")
    assert messages[: len(before)] == before
    assert [m["role"] for m in messages[len(before):]] == ["user", "assistant", "user"]
    assert messages[-1]["content"].startswith("[Memory]")


def test_messages_returns_a_copy_that_callers_cannot_mutate(sample_git_repo):
    context = make_context(sample_git_repo, store_session=False)
    context.add_user_request("inspect")
    first = context.messages()
    first[-1]["content"] = "tampered"
    assert context.messages()[-1]["content"] == "inspect"


def test_memory_snapshot_is_the_last_message_while_append_only(sample_git_repo):
    """快照每轮重算，必须压在最后，否则整条缓存前缀失效。"""
    context = make_context(sample_git_repo)
    context.memory.begin_run("inspect")
    context.add_user_request("inspect")
    messages = context.messages("inspect")
    assert str(messages[-1]["content"]).startswith("[Memory]")


def test_stable_prefix_chars_is_zero_on_the_first_round(sample_git_repo):
    context = make_context(sample_git_repo)
    context.memory.begin_run("inspect")
    context.add_user_request("inspect")
    assert context.prompt_metrics(context.messages("inspect"))["stable_prefix_chars"] == 0


def test_stable_prefix_chars_covers_the_history_when_append_only(sample_git_repo):
    context = make_context(sample_git_repo, transcript=12000)
    context.memory.begin_run("inspect")
    context.add_user_request("inspect")
    first = context.messages("inspect")
    assert context.prompt_metrics(first)["stable_prefix_chars"] == 0

    add_read(context, "README.md", start=1, end=1, chars=10, line_count=1)
    second = context.messages("inspect")
    metrics = context.prompt_metrics(second)
    assert metrics["stable_prefix_chars"] >= len(json.dumps(first[:-1], ensure_ascii=False)) - 5


def test_transcript_chars_measures_the_view_the_threshold_compares(sample_git_repo):
    """`transcript_chars` 必须是不含记忆快照的视图长度——阈值比的就是它。"""
    context = make_context(sample_git_repo, transcript=10000, tool_result=500)
    context.memory.begin_run("inspect")
    context.add_user_request("inspect")
    add_read(context, "README.md", start=1, end=1, chars=300, line_count=1)

    messages = context.messages("inspect")
    metrics = context.prompt_metrics(messages)
    transcript = messages[len(context.system_messages):]

    assert metrics["snapshot_chars"] == len(str(transcript[-1]["content"]))
    assert metrics["transcript_chars"] == len(json.dumps(transcript[:-1], ensure_ascii=False))
    assert metrics["compaction_threshold_chars"] == context.compaction_threshold_chars


def test_default_budgets_let_the_model_see_a_source_file(sample_git_repo):
    """预算由**模型窗口**推导，不是拍一个绝对字符数。

    原实现写死 120000 字符（≈27K token）。而 `deepseek-flash`（DeepSeek-V41-Flash）
    的窗口是 1,000,000 token（来源：本机 DSH 的 DeepSeek 模型目录
    `dsh-llm-deepseek`，`DEFAULT_CONTEXT_WINDOW = 1e6`）——也就是只用了窗口的 2.7%，
    于是"读几个文件就触发压缩"。DSH 自己的触发线是窗口的 80%。
    """
    context = make_context(sample_git_repo)

    assert context.tool_result_budget_chars >= 4000
    assert context.compaction_threshold_chars >= 8 * context.tool_result_budget_chars
    assert context.compaction_threshold_chars >= 50000
    assert context.compaction_floor_chars < context.compaction_threshold_chars
    assert context.compaction_threshold_chars - context.compaction_floor_chars >= 20000
    # 关键：远远大于"读几个文件就会碰到"的量级
    assert context.compaction_threshold_chars >= 480000, "窗口 1M token 下阈值不该这么小"


def test_transcript_budget_is_derived_from_the_context_window(sample_git_repo):
    """窗口越大预算越大——换模型时自动适配，而不是靠人记得改常量。"""
    small = make_context(sample_git_repo, window_tokens=128_000)
    large = make_context(sample_git_repo, window_tokens=1_000_000)

    assert small.transcript_budget_chars < large.transcript_budget_chars
    assert large.transcript_budget_chars > 4 * small.transcript_budget_chars
    # 推导必须留出固定开销的余量，不能把整个窗口都当视图
    from coding_agent.context import CHARS_PER_TOKEN, FIXED_PROMPT_OVERHEAD_CHARS, PROMPT_BUDGET_RATIO

    expected = int(1_000_000 * CHARS_PER_TOKEN * PROMPT_BUDGET_RATIO) - FIXED_PROMPT_OVERHEAD_CHARS
    assert large.transcript_budget_chars == expected


def test_an_explicit_budget_still_wins_over_the_derived_one(sample_git_repo):
    """`--transcript-budget` 必须仍然能覆盖推导值（验证压缩行为时要用）。"""
    context = make_context(sample_git_repo, transcript=20000, window_tokens=1_000_000)
    assert context.transcript_budget_chars == 20000


# --------------------------------------------------------------------------- #
# 单结果：摄入不截断，压力才修剪
# --------------------------------------------------------------------------- #

def test_a_large_tool_result_enters_history_verbatim(sample_git_repo):
    """摄入时不截断：22.7 KB 的文件必须原样进历史。

    这是被 DSH 对比打脸的修正。原来在结果产生时就按 6000 字符截断，于是一个
    574 行的文件被切成 4 段、模型要读 5 次；而 DSH 的 read（2000 行 / 50 KiB 上限）
    下这是一次。上下文放不放得下是**组装 prompt 时**才知道的事。
    """
    context = make_context(sample_git_repo, transcript=120000)
    content = "x" * 22671
    add_read(context, "big.py", chars=22671, line_count=574)

    stored = str(context.history[-1]["content"])

    assert content in stored, "结果被截断了"
    assert len(stored) > 22000
    assert "compressed" not in json.loads(stored)


def test_pruning_tightens_before_falling_back_to_the_llm(sample_git_repo):
    """阶段 1 一轮不够时，先把目标收紧再压一轮，而不是立刻交给 LLM。

    实测缺口：结果数量 ≥ 阈值/粒度 时，阶段 1 的下限（N × 粒度）本身就超过阈值，
    于是本地这一档彻底失效、直接落到 LLM 摘要——而行级细节一旦进了摘要就永久丢了。
    这里 10 个结果**全部低于粒度**，旧实现下阶段 1 一个候选都没有。
    """
    calls = []
    context = make_context(sample_git_repo, transcript=30000, tool_result=4000)
    context.compaction_summarizer = lambda messages: calls.append(1) or "## 任务\nx\n\n## 结论\ny\n\n## 下一步\nz"
    for index in range(10):
        add_read(context, f"src/m{index}.py", chars=3500, index=index)

    messages = context.messages("分析")

    assert calls == [], "本地收紧就能压下来，不该调用模型写摘要"
    assert context._compaction["covered"] == 0, "不该发生段落压缩"
    assert context._view_chars <= context.compaction_threshold_chars
    assert tool_messages(messages), "内容不该被整块丢掉"


def test_a_tightened_stub_is_recomputed_from_the_original_not_from_the_stub(sample_git_repo):
    """收紧时必须从**原文**重算，不能在已经压过的 stub 上再压一次。

    在 stub 上再压会把"首尾拼接后的文本"当成一段连续区间，算出来的 omitted_lines
    会是错的（stub 的 content 是文件头 + 文件尾，不是连续行）。
    """
    context = make_context(sample_git_repo, transcript=30000, tool_result=4000)
    call = ToolCall("c1", "readfile", {"path": "big.py"})
    context.add_assistant_turn(AssistantTurn(None, [call]))
    lines = [f"{number}: " + "x" * 60 for number in range(1, 301)]
    context.add_tool_result(call, {
        "ok": True,
        "path": "big.py",
        "content": "\n".join(lines),
        "start": 1,
        "end": 300,
        "line_count": 300,
    })
    # 再铺几条把它们顶到需要收紧
    for index in range(9):
        add_read(context, f"src/m{index}.py", chars=3500, index=index)

    payload = json.loads(tool_messages(context.messages())[0]["content"])

    first, last = payload["omitted_lines"]
    assert 1 < first <= last < 300
    assert payload["content"].startswith("1: "), "头部必须是文件真正的开头"
    assert "300: " in payload["content"], "尾部必须是文件真正的结尾"


def test_no_pruning_happens_while_the_view_is_below_the_threshold(sample_git_repo):
    """没有压力就一个字节都不动。"""
    context = make_context(sample_git_repo, transcript=120000)
    add_read(context, "big.py", chars=20000, line_count=600)

    payload = json.loads(tool_messages(context.messages())[0]["content"])

    assert payload.get("compressed") is not True
    assert payload["content"].count("x") == 20000, "内容被砍了"


def test_pruning_starts_only_when_the_view_exceeds_the_threshold(sample_git_repo):
    """越线后，超预算的结果才被修剪成 stub，并且带缺口信息。"""
    context = make_context(sample_git_repo, transcript=30000, tool_result=4000)
    call = ToolCall("c1", "readfile", {"path": "big.py"})
    context.add_assistant_turn(AssistantTurn(None, [call]))
    # 多行内容：单行超长时按字符切，行号没有意义，也就不会给出 omitted_lines
    context.add_tool_result(call, {
        "ok": True,
        "path": "big.py",
        "content": "\n".join(f"{number}: " + "x" * 120 for number in range(1, 201)),
        "start": 1,
        "end": 200,
        "line_count": 200,
    })

    payload = json.loads(tool_messages(context.messages())[0]["content"])

    assert payload.get("compressed") is True
    assert payload.get("omitted_lines"), "被修剪的结果必须给出缺口"
    assert payload["next_action_hint"]
    assert len(json.dumps(payload, ensure_ascii=False)) <= 4000


def test_pruning_does_not_rewrite_history(sample_git_repo):
    """修剪只影响视图；原文必须留在 history（和 report.json）里。"""
    context = make_context(sample_git_repo, transcript=30000, tool_result=4000)
    add_read(context, "big.py", chars=26000, line_count=900)

    context.messages()

    assert "x" * 26000 in str(context.history[-1]["content"]), "history 被改写了"


def test_pruning_shrinks_from_the_biggest_result_first(sample_git_repo):
    """只修到装得下为止、从最大的开始——不能把所有超预算结果一次全砍掉。"""
    context = make_context(sample_git_repo, transcript=30000, tool_result=4000)
    add_read(context, "huge.py", chars=30000, line_count=900, index=0)
    add_read(context, "small.py", chars=5000, line_count=200, index=1)

    payloads = [json.loads(m["content"]) for m in tool_messages(context.messages())]
    by_path = {p.get("path"): p for p in payloads}

    assert by_path["huge.py"].get("compressed") is True, "最大的结果应该先被修剪"
    assert by_path["small.py"].get("compressed") is not True, "放得下的结果不该被砍"


def test_pruning_is_deterministic_across_rebuilds(sample_git_repo):
    context = make_context(sample_git_repo, transcript=30000, tool_result=4000)
    add_read(context, "big.py", chars=20000, line_count=600)

    first = json.dumps(context.messages(), ensure_ascii=False)
    second = json.dumps(context.messages(), ensure_ascii=False)

    assert first == second


def test_an_already_compressed_result_is_not_compressed_twice(sample_git_repo):
    """压缩必须幂等：重算会把 omitted_lines 这类"看见过哪些行"的事实推掉。"""
    context = make_context(sample_git_repo, transcript=120000)
    call = ToolCall("c1", "readfile", {"path": "big.py"})
    context.add_assistant_turn(AssistantTurn(None, [call]))
    context.add_tool_result(call, {
        "ok": True,
        "path": "big.py",
        "compressed": True,
        "content": "1: head",
        "start": 1,
        "end": 600,
        "line_count": 600,
        "omitted_lines": [2, 599],
    })

    payload = json.loads(str(context.history[-1]["content"]))

    assert payload["omitted_lines"] == [2, 599]


# --------------------------------------------------------------------------- #
# stub 形状：内容优先
# --------------------------------------------------------------------------- #

def test_readfile_stub_keeps_the_code_it_read(sample_git_repo):
    """stub 必须留下代码本身，而不是只剩元数据。"""
    context = make_context(sample_git_repo, transcript=20000, tool_result=1500)
    content = "\n".join(f"{number}: def step_{number}(): return {number}" for number in range(1, 201))
    call = ToolCall("c1", "readfile", {"path": "big.py"})
    context.add_assistant_turn(AssistantTurn(None, [call]))
    context.add_tool_result(call, {"ok": True, "path": "big.py", "content": content, "start": 1, "end": 200, "line_count": 200, "truncated": False})
    # 绕过"视图未越线就不修剪"，直接检查 stubbing 函数
    from coding_agent.context import _compact_result, _fit_json

    stub = _fit_json(_compact_result("readfile", {"ok": True, "path": "big.py", "content": content, "start": 1, "end": 200, "line_count": 200}, context.memory), 1500)
    payload = json.loads(stub)

    assert len(stub) <= 1500
    assert "1: def step_1(): return 1" in payload["content"]
    assert "200: def step_200(): return 200" in payload["content"]
    assert "lines omitted" in payload["content"]

    first, last = payload["omitted_lines"]
    assert 1 < first <= last < 200
    assert f"start={first}" in payload["next_action_hint"]


def test_following_the_readfile_hint_never_truncates_again(sample_git_repo):
    """提示给的窗口必须让下一次读取整段装得进配额（写死 100 行会失败）。"""
    from coding_agent.context import _compact_result, _fit_json

    context = make_context(sample_git_repo, transcript=20000, tool_result=1200)
    lines = [f"{index}: " + "x" * 160 for index in range(1, 121)]
    result = {"ok": True, "path": "big.py", "content": "\n".join(lines), "start": 1, "end": 120, "line_count": 120, "truncated": False}
    stub = _fit_json(_compact_result("readfile", result, context.memory), 1200)
    payload = json.loads(stub)

    match = re.search(r"start=(\d+) end=(\d+)", payload["next_action_hint"])
    assert match
    start, end = int(match.group(1)), int(match.group(2))
    assert end - start + 1 < 100, "窗口没有随行长收缩"

    following = {"ok": True, "path": "big.py", "content": "\n".join(lines[start - 1 : end]), "start": start, "end": end, "line_count": 120, "truncated": False}
    followed = json.loads(_fit_json(_compact_result("readfile", following, context.memory), 1200))

    assert "omitted_lines" not in followed, "照着提示读又被截断了"


def test_readfile_hint_window_grows_with_shorter_lines(sample_git_repo):
    from coding_agent.context import _compact_result, _fit_json

    context = make_context(sample_git_repo, transcript=20000, tool_result=4000)

    def window_for(width: int) -> int:
        lines = [f"{index}: " + "y" * width for index in range(1, 401)]
        result = {"ok": True, "path": "wide.py", "content": "\n".join(lines), "start": 1, "end": 400, "line_count": 400, "truncated": False}
        payload = json.loads(_fit_json(_compact_result("readfile", result, context.memory), 4000))
        match = re.search(r"start=(\d+) end=(\d+)", payload["next_action_hint"])
        return int(match.group(2)) - int(match.group(1)) + 1

    assert window_for(20) > window_for(120)


def test_shell_preview_keeps_the_tail_where_failures_are_reported(sample_git_repo):
    from coding_agent.context import _compact_result, _fit_json

    context = make_context(sample_git_repo, transcript=20000, tool_result=1800)
    result = {"ok": False, "exit_code": 1, "stdout": "x" * 3000 + "\n3 failed, 2 passed in 0.5s\n", "timed_out": False}
    payload = json.loads(_fit_json(_compact_result("shell", result, context.memory), 1800))

    assert "3 failed, 2 passed" in payload["stdout_preview"]
    assert payload["exit_code"] == 1


def test_search_stub_keeps_the_matching_lines(sample_git_repo):
    from coding_agent.context import _compact_result, _fit_json

    context = make_context(sample_git_repo, transcript=20000, tool_result=6000)
    matches = [{"path": f"src/m{index}.py", "line": index * 10, "text": f"def main_{index}():  # 命中行 {index}"} for index in range(7)]
    payload = json.loads(_fit_json(_compact_result("search", {"ok": True, "matches": matches, "truncated": False}, context.memory), 6000))

    assert payload["match_count"] == 7
    assert "命中行 0" in payload["sample_matches"][0]["text"]


def test_file_listing_stub_reports_the_entries_the_tool_returned(sample_git_repo):
    """`listfiles` 的结果键是 `entries`，读错键会给出 `file_count: 0` 这种错误信息。"""
    from coding_agent.context import _compact_result, _fit_json

    context = make_context(sample_git_repo)
    entries = [{"path": "src", "kind": "directory", "size": None}, {"path": "main.py", "kind": "file", "size": 120}]
    payload = json.loads(_fit_json(_compact_result("listfiles", {"ok": True, "entries": entries, "truncated": False}, context.memory), 6000))

    assert payload["file_count"] == 2
    assert payload["sample_paths"] == ["src/", "main.py"]


def test_failed_tool_result_keeps_its_failure_reason(sample_git_repo):
    """失败原因优先级高于任何预览——只写 `ok: false` 会让模型盲目重发同一条命令。"""
    from coding_agent.context import _compact_result, _fit_json

    context = make_context(sample_git_repo, transcript=20000, tool_result=1500)
    result = {"ok": False, "exit_code": 1, "stdout": "x" * 5000, "error": {"type": "shell_failed", "message": "boom"}, "timed_out": False}
    payload = json.loads(_fit_json(_compact_result("shell", result, context.memory), 1500))

    assert payload["error_type"] == "shell_failed"
    assert payload["error_message"] == "boom"
    assert payload["exit_code"] == 1


# --------------------------------------------------------------------------- #
# 段落压缩
# --------------------------------------------------------------------------- #

def test_threshold_triggers_merge_and_persists_the_summary(sample_git_repo):
    context = make_context(sample_git_repo, transcript=20000)
    context.add_user_request("inspect")
    context.compaction_summarizer = lambda messages: "## 任务\ninspect\n\n## 结论\n看了。\n\n## 下一步\n继续。"
    fill_until_compaction(context, rounds=30, chars=400)

    messages = context.messages("inspect")

    assert context._compaction["covered"] > 0
    assert handoff_message(messages)
    assert context.memory.data["compaction"]["covered"] == context._compaction["covered"]


def test_recent_groups_are_never_absorbed(sample_git_repo):
    """最近两组必须保持原文形式：模型需要刚刚拿到的工具结果。"""
    context = make_context(sample_git_repo, transcript=20000)
    context.add_user_request("inspect")
    context.compaction_summarizer = lambda messages: "## 任务\ninspect\n\n## 结论\n看了。\n\n## 下一步\n继续。"
    fill_until_compaction(context, rounds=30, chars=400)
    add_read(context, "src/last_a.py", chars=4800, index=90)
    add_read(context, "src/last_b.py", chars=4800, index=91)

    messages = context.messages("inspect")
    paths = [json.loads(m["content"]).get("path") for m in tool_messages(messages) if "path" in json.loads(m["content"])]

    assert "src/last_a.py" in paths
    assert "src/last_b.py" in paths


def test_history_is_never_rewritten_by_compaction(sample_git_repo):
    context = make_context(sample_git_repo, transcript=20000)
    context.add_user_request("inspect")
    context.compaction_summarizer = lambda messages: "## 任务\ninspect\n\n## 结论\n看了。\n\n## 下一步\n继续。"
    fill_until_compaction(context, rounds=30, chars=400)
    before = json.dumps(context.history, ensure_ascii=False)

    context.messages("inspect")

    assert json.dumps(context.history, ensure_ascii=False) == before


def test_compaction_lands_below_the_target_not_above_it(sample_git_repo):
    context = make_context(sample_git_repo, transcript=20000)
    context.add_user_request("inspect")
    context.compaction_summarizer = lambda messages: "## 任务\ninspect\n\n## 结论\n看了。\n\n## 下一步\n继续。"
    fill_until_compaction(context, rounds=30, chars=400)

    metrics = context.prompt_metrics(context.messages("inspect"))

    assert metrics["history_covered"] > 0
    assert metrics["transcript_chars"] <= context.compaction_target_chars + SPAN_SUMMARY_MAX_CHARS + SPAN_LEDGER_MAX_CHARS + 600


def test_span_summary_that_is_not_smaller_is_rejected(sample_git_repo):
    context = make_context(sample_git_repo, transcript=20000)
    context.add_user_request("inspect")
    fill_until_compaction(context, rounds=30, chars=400)
    context.compaction_summarizer = lambda messages: "冗长" * 20000

    context.messages("inspect")

    assert context._compaction["covered"] == 0


def test_failed_span_compaction_is_visible_and_retried_later(sample_git_repo):
    """静默失败会让 prompt 无限增长而无人察觉——必须发事件并安排重试。"""
    events = []
    context = make_context(sample_git_repo, transcript=20000)
    context.event_sink.emit = lambda event_type, **payload: events.append((event_type, payload))
    context.add_user_request("inspect")
    fill_until_compaction(context, rounds=30, chars=400)
    context.compaction_summarizer = lambda messages: (_ for _ in ()).throw(RuntimeError("boom"))

    context.messages("inspect")

    failures = [payload for kind, payload in events if kind == "context_compressed" and payload.get("kind") == "span_failed"]
    assert failures, "压缩失败没有留下任何可见痕迹"
    assert failures[0]["retry_at_chars"] > 0


def test_handoff_carries_a_machine_generated_ledger(sample_git_repo):
    """交接摘要要带"做过什么"的台账，由机器生成、不靠 LLM 回忆。"""
    context = make_context(sample_git_repo, transcript=20000)
    context.add_user_request("分析")
    context.compaction_summarizer = lambda messages: "## 任务\n分析\n\n## 结论\n看了。\n\n## 下一步\n继续。"
    fill_until_compaction(context, rounds=30, chars=400)

    ledger = handoff_message(context.messages("分析"))["content"].split("[Progress Ledger]")[1]

    assert "src/filler_" in ledger, f"台账没有记录被吸收的读取: {ledger}"
    assert context.memory.data["compaction"]["ledger"]


def test_ledger_records_only_the_lines_the_model_actually_saw(sample_git_repo):
    """被截断的读取只能记看见过的行，不能记成请求的区间。"""
    from coding_agent.context import _compact_result, _fit_json

    context = make_context(sample_git_repo, transcript=20000, tool_result=1200)
    lines = [f"{index}: " + "x" * 55 for index in range(1, 575)]
    result = {"ok": True, "path": "big.py", "content": "\n".join(lines), "start": 1, "end": 574, "line_count": 574, "truncated": False}
    stub = json.loads(_fit_json(_compact_result("readfile", result, context.memory), 1200))
    first, last = stub["omitted_lines"]

    from coding_agent.context import _ledger_for_span, _render_ledger

    span = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "readfile", "arguments": json.dumps({"path": "big.py"})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(stub)},
    ]
    ledger = _render_ledger(_ledger_for_span(span))

    assert f"big.py (1-{first - 1}, {last + 1}-574)" in ledger
    assert "big.py (1-574)" not in ledger, "把请求区间当成已读是错的"


def test_ledger_records_writes_and_commands(sample_git_repo):
    from coding_agent.context import _ledger_for_span, _render_ledger

    span = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "w", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "kb/x.md"})}},
            {"id": "s", "type": "function", "function": {"name": "shell", "arguments": json.dumps({"program": "uv", "args": ["run", "pytest", "-q"]})}},
        ]},
        {"role": "tool", "tool_call_id": "w", "content": json.dumps({"ok": True, "path": "kb/x.md"})},
        {"role": "tool", "tool_call_id": "s", "content": json.dumps({"ok": True, "exit_code": 0})},
    ]
    ledger = _render_ledger(_ledger_for_span(span))

    assert "kb/x.md" in ledger
    assert "uv run pytest -q" in ledger


def test_ledger_is_bounded_and_deterministic(sample_git_repo):
    from coding_agent.context import _ledger_for_span, _merge_ledger, _render_ledger

    span = []
    for index in range(30):
        call_id = f"c{index}"
        span.append({"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "readfile", "arguments": json.dumps({"path": f"src/file_{index}.py"})}}]})
        span.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"ok": True, "path": f"src/file_{index}.py", "start": 1, "end": 100, "line_count": 2000})})

    first = _render_ledger(_merge_ledger({}, _ledger_for_span(span)))
    second = _render_ledger(_merge_ledger({}, _ledger_for_span(span)))

    assert first == second
    assert len(first) <= SPAN_LEDGER_MAX_CHARS
    assert first.count("src/file_") <= 12
