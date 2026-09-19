from __future__ import annotations

import pytest

from coding_agent.repository import Workspace
from coding_agent.tools.readfile import ReadFileTool


def test_readfile_schema_declares_object_and_required_path():
    schema = ReadFileTool.parameters
    assert schema["type"] == "object"
    assert schema["required"] == ["path"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["start"]["minimum"] == 1
    assert schema["properties"]["end"]["minimum"] == 1


def test_readfile_reads_one_based_inclusive_range_with_line_numbers(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("zero\none\ntwo\nthree\n", encoding="utf-8")
    result = ReadFileTool(Workspace(sample_git_repo)).execute(
        {"path": "README.md", "start": 2, "end": 3}
    )
    assert result == {
        "ok": True,
        "path": "README.md",
        "content": "2: one\n3: two",
        "start": 2,
        "end": 3,
        "line_count": 4,
        "truncated": False,
        "footer": "(Showing lines 2-3 of 4. Use start=4 to read the next range.)",
    }


@pytest.mark.parametrize("arguments", [
    {"path": "README.md", "start": 0},
    {"path": "README.md", "end": 0},
    {"path": "README.md", "start": 4, "end": 3},
])
def test_readfile_rejects_invalid_line_range(sample_git_repo, arguments):
    result = ReadFileTool(Workspace(sample_git_repo)).execute(arguments)
    assert result["error"]["type"] == "invalid_arguments"


def test_readfile_returns_utf8_text(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "README.md"})
    assert result["ok"] is True
    assert "Sample Repository" in result["content"]


def test_readfile_allows_absolute_path_inside_repository(sample_git_repo):
    path = str(sample_git_repo / "README.md")
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": path})
    assert result["ok"] is True


def test_readfile_allows_parent_path_that_stays_inside(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "src/../README.md"})
    assert result["ok"] is True


def test_readfile_returns_stable_errors(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "missing.txt"})
    assert result == {"ok": False, "error": {"type": "file_not_found", "message": "File not found"}}


def test_readfile_rejects_directory(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "src"})
    assert result["error"]["type"] == "not_a_file"


def test_readfile_rejects_oversized_file(sample_git_repo):
    (sample_git_repo / "large.txt").write_text("123456", encoding="utf-8")
    result = ReadFileTool(Workspace(sample_git_repo), max_bytes=5).execute({"path": "large.txt"})
    assert result["error"]["type"] == "file_too_large"


def test_readfile_reads_a_line_range_from_a_file_above_the_old_cap(sample_git_repo):
    """The quota must limit the returned range, not the size of the source file."""
    target = sample_git_repo / "big.py"
    target.write_text("\n".join(f"line {number}" for number in range(20000)), encoding="utf-8")
    assert target.stat().st_size > 64 * 1024

    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "big.py", "start": 1, "end": 5})

    assert result["ok"] is True
    assert result["content"] == "1: line 0\n2: line 1\n3: line 2\n4: line 3\n5: line 4"
    assert result["start"] == 1
    assert result["end"] == 5
    assert result["line_count"] == 20000
    assert result["truncated"] is False


def test_readfile_reads_a_middle_range_from_a_large_file(sample_git_repo):
    target = sample_git_repo / "big.py"
    target.write_text("\n".join(f"line {number}" for number in range(20000)), encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "big.py", "start": 19999, "end": 20000})

    assert result["ok"] is True
    assert result["content"] == "19999: line 19998\n20000: line 19999"
    assert result["line_count"] == 20000


def test_readfile_truncates_content_at_the_result_quota(sample_git_repo):
    target = sample_git_repo / "many.txt"
    target.write_text("\n".join(f"row {number}" for number in range(100)), encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo), max_bytes=40).execute({"path": "many.txt"})

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["start"] == 1
    assert result["end"] < result["line_count"]
    assert len(result["content"].encode("utf-8")) <= 40
    assert result["line_count"] == 100


def test_readfile_footer_says_how_to_continue_a_middle_range(sample_git_repo):
    """工具自己要说清"下一步读哪里"，而不是把这个职责外包给压缩层。

    DSH 的 read 结果带一条分页 footer（`Use offset=<next> to continue.`）。我原来只返回
    `end`，模型得自己推 `start = end + 1`——而提示只在结果被修剪时才由压缩层补上，
    一个 64 KiB 的截断结果小于压缩触发线，压根不会被修剪，模型拿到的是裸的 `end`。
    """
    target = sample_git_repo / "big.py"
    target.write_text("\n".join(f"line {number}" for number in range(1, 501)), encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "big.py", "start": 100, "end": 200})

    assert result["ok"] is True
    assert "100-200" in result["footer"], result["footer"]
    assert "start=201" in result["footer"], result["footer"]
    assert "of 500" in result["footer"], "要报出文件总行数，模型才知道还有多少"


def test_readfile_footer_marks_the_end_of_file(sample_git_repo):
    """读到文件尾要有明确信号，否则模型会再发一次空区间去确认。"""
    target = sample_git_repo / "small.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "small.py"})

    assert result["ok"] is True
    assert "end of file" in result["footer"].lower()
    assert "3" in result["footer"]


def test_readfile_footer_marks_a_capped_output(sample_git_repo):
    """被 64 KiB 配额截断时，footer 必须给出续读起点，并标明这是"被上限截断"。"""
    target = sample_git_repo / "huge.py"
    target.write_text("\n".join(f"row {number} " + "x" * 40 for number in range(1, 5001)), encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "huge.py"})

    assert result["ok"] is True
    assert result["truncated"] is True
    assert "capped" in result["footer"].lower(), result["footer"]
    assert f"start={result['end'] + 1}" in result["footer"], result["footer"]


def test_a_single_overlong_line_is_truncated_instead_of_failing(sample_git_repo):
    """压缩过的 JS / 单行 JSON：一行就超配额时应该**截断这一行**，而不是整体报错。

    原来返回 `file_too_large` 并建议"narrow the range"——对"单行本身太长"这种情况，
    缩小行范围毫无帮助，模型会照着建议反复重试。
    """
    target = sample_git_repo / "bundle.min.js"
    target.write_text("var a=1;" + "z" * 100000 + ";\nvar b=2;\n", encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "bundle.min.js"})

    assert result["ok"] is True, result
    assert "1: var a=1;zzz" in result["content"]
    assert "line truncated" in result["content"], "长行要被标出来"
    assert "2: var b=2;" in result["content"], "后面的行仍然要能读到"
    assert result["line_count"] == 2


def test_a_range_read_from_a_huge_file_is_allowed(sample_git_repo):
    """文件总量上限只该挡"整读"，不该挡住"我只想读第 1-5 行"。

    原来 `max_file_bytes` 检查的是整个文件大小、跟请求范围无关，于是一个 100 MB 的
    日志文件连第 1-5 行都读不出来（`_read_lines` 本来就是流式的，内存上没有风险）。
    """
    target = sample_git_repo / "app.log"
    target.write_text("\n".join(f"log line {number}" for number in range(1, 2001)), encoding="utf-8")

    tool = ReadFileTool(Workspace(sample_git_repo), max_file_bytes=1000)

    whole = tool.execute({"path": "app.log"})
    assert whole["error"]["type"] == "file_too_large"
    assert "start" in whole["error"]["message"], "拒绝消息要指出可用的写法"

    ranged = tool.execute({"path": "app.log", "start": 1990, "end": 2000})
    assert ranged["ok"] is True
    assert ranged["content"].startswith("1990: log line 1990")
    assert ranged["line_count"] == 2000


def test_one_read_never_returns_more_than_the_line_cap(sample_git_repo):
    """单次读取最多 2000 行（对齐 DSH 的 readLimit），即使字节数远没到配额。

    没有行数上限时，模型无法预测一次能拿到多少内容；有了它，footer 给出的续读
    起点就是稳定的。
    """
    target = sample_git_repo / "long.py"
    target.write_text("\n".join(f"line {number}" for number in range(1, 3001)), encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "long.py"})

    assert result["ok"] is True
    assert result["start"] == 1
    assert result["end"] == 2000
    assert result["truncated"] is True
    assert result["line_count"] == 3000, "总行数必须是真实总数"
    assert "Use start=2001 to continue." in result["footer"]


def test_the_line_cap_can_be_raised_for_a_call_site(sample_git_repo):
    target = sample_git_repo / "long.py"
    target.write_text("\n".join(f"line {number}" for number in range(1, 3001)), encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo), max_lines=5000).execute({"path": "long.py"})

    assert result["truncated"] is False
    assert result["end"] == 3000


def test_readfile_rejects_invalid_arguments(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "../outside.txt"})
    assert result["error"]["type"] == "workspace_violation"


def test_readfile_rejects_non_utf8_file(sample_git_repo):
    (sample_git_repo / "binary.dat").write_bytes(b"\xff\xfe")
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "binary.dat"})
    assert result["error"]["type"] == "decode_error"


def test_readfile_rejects_symlink_escape(sample_git_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = sample_git_repo / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "link.txt"})
    assert result["error"]["type"] == "workspace_violation"
