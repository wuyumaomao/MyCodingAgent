from __future__ import annotations

import pytest

from coding_agent.repository import Workspace
from coding_agent.tools.approval import WriteApprovalGate
from coding_agent.tools.patchfile import PatchFileTool


def make_tool(repo, answer=True, max_bytes=64 * 1024):
    return PatchFileTool(Workspace(repo), WriteApprovalGate(ask=lambda _: answer), max_bytes=max_bytes)


def test_patch_matches_a_multiline_old_text_in_an_lf_file(sample_git_repo):
    """`old_text` 含换行时必须能匹配。

    真实 run 里踩到：`readfile` 用通用换行读取（模型看到的是 LF），而 `patch_file`
    用 `read_bytes()`（保留磁盘上的 CRLF），两边表示不一致，于是模型照着 readfile
    写出的 `old_text="TODO\\n"` 报 `text_not_found`——它试了两次才摸到"只传单行不带
    换行"这个 workaround。现有测试的 old_text 全是**不含换行的单词**，所以没抓到。
    """
    target = sample_git_repo / "notes.md"
    target.write_bytes(b"# Notes\nTODO\nEND\n")

    result = make_tool(sample_git_repo).execute({"path": "notes.md", "old_text": "TODO\n", "new_text": "DONE\n"})

    assert result["ok"] is True, result
    assert target.read_bytes() == b"# Notes\nDONE\nEND\n"


def test_patch_a_crlf_file_keeps_it_crlf_and_does_not_inject_blank_lines(sample_git_repo):
    """CRLF 文件 patch 之后必须还是 CRLF，而且不能多出空行。

    这是最严重的一处：`AtomicWriter` 用文本模式打开临时文件（没传 `newline=""`），
    Windows 上会把每个 `\\n` 再翻成 `\\r\\n`——而 `patch_file` 读进来的是保留 `\\r\\n`
    的内容，于是 `\\r\\n` 变成 `\\r\\r\\n`，`readfile` 读出来就是每行后面多一个空行。
    每 patch 一次累积一次。
    """
    target = sample_git_repo / "crlf.txt"
    target.write_bytes(b"line1\r\nline2\r\nline3\r\n")

    result = make_tool(sample_git_repo).execute({"path": "crlf.txt", "old_text": "line2", "new_text": "CHANGED"})

    assert result["ok"] is True, result
    assert target.read_bytes() == b"line1\r\nCHANGED\r\nline3\r\n"
    assert b"\r\r" not in target.read_bytes()


def test_repeated_patches_do_not_grow_the_file(sample_git_repo):
    """连续 patch 不能让文件越长越大（每行多一个 \\r）。"""
    target = sample_git_repo / "crlf.txt"
    target.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    tool = make_tool(sample_git_repo)

    tool.execute({"path": "crlf.txt", "old_text": "line2", "new_text": "TWO"})
    tool.execute({"path": "crlf.txt", "old_text": "line3", "new_text": "THREE"})

    assert target.read_bytes() == b"line1\r\nTWO\r\nTHREE\r\n"


def test_patch_file_replaces_one_match_after_approval(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("before\nvalue\nafter\n", encoding="utf-8")

    result = make_tool(sample_git_repo).execute(
        {"path": "README.md", "old_text": "value", "new_text": "updated"}
    )

    assert result["ok"] is True
    assert result["replacements"] == 1
    assert "updated" in target.read_text(encoding="utf-8")


def test_patch_file_rejects_missing_and_duplicate_text(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("same\nsame\n", encoding="utf-8")
    tool = make_tool(sample_git_repo)

    assert tool.execute({"path": "README.md", "old_text": "absent", "new_text": "x"})["error"]["type"] == "text_not_found"
    assert tool.execute({"path": "README.md", "old_text": "same", "new_text": "x"})["error"]["type"] == "text_not_unique"
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


def test_patch_file_does_not_modify_when_denied(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("old", encoding="utf-8")

    result = make_tool(sample_git_repo, answer=False).execute(
        {"path": "README.md", "old_text": "old", "new_text": "new"}
    )

    assert result["error"]["type"] == "approval_denied"
    assert target.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        ({}, "invalid_arguments"),
        ({"path": "README.md", "old_text": "x"}, "invalid_arguments"),
        ({"path": "README.md", "old_text": 1, "new_text": "x"}, "invalid_arguments"),
        ({"path": "../README.md", "old_text": "x", "new_text": "y"}, "workspace_violation"),
        ({"path": "missing.md", "old_text": "x", "new_text": "y"}, "file_not_found"),
        ({"path": "src", "old_text": "x", "new_text": "y"}, "not_a_file"),
    ],
)
def test_patch_file_rejects_invalid_requests_without_approval(sample_git_repo, arguments, error_type):
    result = make_tool(sample_git_repo).execute(arguments)

    assert result["error"]["type"] == error_type


def test_patch_file_rejects_non_utf8_file(sample_git_repo):
    target = sample_git_repo / "binary.bin"
    target.write_bytes(b"\xff")

    result = make_tool(sample_git_repo).execute(
        {"path": "binary.bin", "old_text": "x", "new_text": "y"}
    )

    assert result["error"]["type"] == "decode_error"


def test_patch_file_rejects_result_over_limit_before_approval(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("old", encoding="utf-8")

    result = PatchFileTool(
        Workspace(sample_git_repo),
        WriteApprovalGate(ask=lambda _: pytest.fail("approval requested")),
        max_bytes=3,
    ).execute({"path": "README.md", "old_text": "old", "new_text": "four"})

    assert result["error"]["type"] == "file_too_large"
    assert target.read_text(encoding="utf-8") == "old"


def test_patch_file_rejects_symlink_target(sample_git_repo):
    target = sample_git_repo / "link.md"
    try:
        target.symlink_to(sample_git_repo / "README.md")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    result = make_tool(sample_git_repo).execute(
        {"path": "link.md", "old_text": "x", "new_text": "y"}
    )

    assert result["error"]["type"] == "workspace_violation"
