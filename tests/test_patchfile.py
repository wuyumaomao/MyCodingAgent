from __future__ import annotations

import pytest

from coding_agent.repository import Workspace
from coding_agent.tools.approval import WriteApprovalGate
from coding_agent.tools.patchfile import PatchFileTool


def make_tool(repo, answer=True, max_bytes=64 * 1024):
    return PatchFileTool(Workspace(repo), WriteApprovalGate(ask=lambda _: answer), max_bytes=max_bytes)


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
