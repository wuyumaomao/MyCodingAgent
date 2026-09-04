from __future__ import annotations

import os

import pytest

from coding_agent.repository import Workspace
from coding_agent.tools.approval import WriteApprovalGate
from coding_agent.tools.writefile import WriteFileTool


def make_tool(repo, answer=True):
    return WriteFileTool(Workspace(repo), WriteApprovalGate(ask=lambda _: answer))


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
        ({"path": "missing/new.py", "content": "x"}, "parent_not_found"),
    ],
)
def test_write_file_rejects_invalid_requests_without_writing(sample_git_repo, arguments, error_type):
    result = make_tool(sample_git_repo).execute(arguments)

    assert result["error"]["type"] == error_type


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
