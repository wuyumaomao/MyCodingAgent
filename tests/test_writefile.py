from __future__ import annotations

import os

import pytest

from coding_agent.repository import Workspace
from coding_agent.tools.approval import WriteApprovalGate
from coding_agent.tools.writefile import WriteFileTool


def make_tool(repo, answer=True):
    return WriteFileTool(Workspace(repo), WriteApprovalGate(ask=lambda _: answer))


def test_write_file_preserves_the_exact_bytes_it_was_given(sample_git_repo):
    """模型给什么就写什么，不翻译换行。

    `AtomicWriter` 原来用文本模式（`newline` 未指定），Windows 上把每个 `\\n` 翻成
    `\\r\\n`——模型写 17 字节，磁盘上却是 20 字节；接着 `patch_file` 用 `read_bytes()`
    读回 CRLF，`old_text` 里的 `\\n` 就再也匹配不上了（还会把 `\\r\\n` 弄成 `\\r\\r\\n`）。
    """
    content = "# Notes\nTODO\nEND\n"

    result = make_tool(sample_git_repo).execute({"path": "notes.md", "content": content})

    assert result["bytes_written"] == len(content.encode("utf-8"))
    on_disk = (sample_git_repo / "notes.md").read_bytes()
    assert on_disk == content.encode("utf-8"), on_disk
    assert b"\r" not in on_disk


def test_write_file_keeps_crlf_when_the_model_sends_crlf(sample_git_repo):
    """反过来也要成立：模型明确给 CRLF 时就写 CRLF。"""
    content = "a\r\nb\r\n"
    make_tool(sample_git_repo).execute({"path": "crlf.txt", "content": content})

    assert (sample_git_repo / "crlf.txt").read_bytes() == content.encode("utf-8")


def test_write_file_creates_after_approval(sample_git_repo):
    result = make_tool(sample_git_repo).execute({"path": "new.py", "content": "print('ok')\n"})

    assert result == {
        "ok": True,
        "path": "new.py",
        "operation": "create",
        "bytes_written": len("print('ok')\n".encode("utf-8")),
    }
    assert (sample_git_repo / "new.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_write_file_overwrites_after_approval(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("old", encoding="utf-8")

    result = make_tool(sample_git_repo).execute({"path": "README.md", "content": "new"})

    assert result["operation"] == "overwrite"
    assert target.read_text(encoding="utf-8") == "new"


def test_write_file_does_not_modify_when_denied(sample_git_repo):
    target = sample_git_repo / "README.md"
    before = target.read_bytes()

    result = make_tool(sample_git_repo, answer=False).execute({"path": "README.md", "content": "new"})

    assert result["error"]["type"] == "approval_denied"
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        ({}, "invalid_arguments"),
        ({"path": "new.py"}, "invalid_arguments"),
        ({"path": "new.py", "content": 1}, "invalid_arguments"),
        ({"path": "../outside.py", "content": "x"}, "workspace_violation"),
        ({"path": "D:/elsewhere/x.py", "content": "x"}, "workspace_violation"),
    ],
)
def test_write_file_rejects_invalid_requests_without_writing(sample_git_repo, arguments, error_type):
    result = make_tool(sample_git_repo).execute(arguments)

    assert result["error"]["type"] == error_type


def test_write_file_creates_a_missing_parent_directory(sample_git_repo):
    """父目录不存在时自己建，而不是回一句 "Parent directory does not exist"。

    原来硬拒的代价（真实 run 实测）：模型先试 `python -c os.makedirs` 被 shell 策略
    拒两次，再写一个临时脚本、跑它、然后才写成文件——**6 个整轮**只为建一个空目录。
    写操作本来就有审批门，审批时会把要建的目录一并显示出来。
    """
    assert not (sample_git_repo / "kb").exists()

    result = make_tool(sample_git_repo).execute({"path": "kb/agent-review.md", "content": "审查\n"})

    assert result["ok"] is True
    assert result["created_directories"] == ["kb"]
    assert (sample_git_repo / "kb" / "agent-review.md").read_text(encoding="utf-8") == "审查\n"


def test_nested_parent_directories_are_all_created_and_reported(sample_git_repo):
    result = make_tool(sample_git_repo).execute({"path": "docs/notes/deep/x.md", "content": "x"})

    assert result["ok"] is True
    assert result["created_directories"] == ["docs", "docs/notes", "docs/notes/deep"]
    assert (sample_git_repo / "docs" / "notes" / "deep" / "x.md").is_file()


def test_existing_parent_directories_are_not_reported(sample_git_repo):
    (sample_git_repo / "kb").mkdir()

    result = make_tool(sample_git_repo).execute({"path": "kb/x.md", "content": "x"})

    assert result["ok"] is True
    assert "created_directories" not in result


def test_approval_preview_lists_the_directories_it_will_create(sample_git_repo):
    """用户要能看见这个副作用，否则"批准写文件"就悄悄变成了"批准建目录树"。"""
    seen = []

    def ask(preview):
        seen.append(preview)
        return True

    WriteFileTool(Workspace(sample_git_repo), WriteApprovalGate(ask=ask)).execute({"path": "a/b/x.md", "content": "x"})

    assert seen and seen[0].creates_directories == ("a", "a/b")


def test_denied_write_does_not_create_directories(sample_git_repo):
    result = make_tool(sample_git_repo, answer=False).execute({"path": "a/b/x.md", "content": "x"})

    assert result["error"]["type"] == "approval_denied"
    assert not (sample_git_repo / "a").exists()


def test_write_file_rejects_a_symlinked_parent_directory(sample_git_repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = sample_git_repo / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    result = make_tool(sample_git_repo).execute({"path": "linked/x.md", "content": "x"})

    assert result["error"]["type"] == "workspace_violation"
    assert not (outside / "x.md").exists()


def test_write_file_rejects_directory_target(sample_git_repo):
    result = make_tool(sample_git_repo).execute({"path": "src", "content": "x"})

    assert result["error"]["type"] == "permission_denied"


def test_write_file_rejects_content_over_limit(sample_git_repo):
    result = WriteFileTool(
        Workspace(sample_git_repo), WriteApprovalGate(ask=lambda _: pytest.fail("approval requested")), max_bytes=3
    ).execute({"path": "new.py", "content": "four"})

    assert result["error"]["type"] == "file_too_large"
    assert not (sample_git_repo / "new.py").exists()


def test_write_file_rejects_symlink_target(sample_git_repo, tmp_path):
    target = sample_git_repo / "link.py"
    try:
        target.symlink_to(sample_git_repo / "README.md")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    result = make_tool(sample_git_repo).execute({"path": "link.py", "content": "new"})

    assert result["error"]["type"] == "workspace_violation"
    assert (sample_git_repo / "README.md").read_text(encoding="utf-8") != "new"


def test_write_file_allows_absolute_path_inside_workspace(sample_git_repo):
    target = sample_git_repo / "absolute.py"

    result = make_tool(sample_git_repo).execute({"path": str(target), "content": "x"})

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "x"


def test_write_file_allows_parent_traversal_that_resolves_inside_workspace(sample_git_repo):
    result = make_tool(sample_git_repo).execute({"path": "src/../traversed.py", "content": "x"})

    assert result["ok"] is True
    assert (sample_git_repo / "traversed.py").read_text(encoding="utf-8") == "x"
