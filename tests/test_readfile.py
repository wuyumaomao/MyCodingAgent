from __future__ import annotations

import pytest

from coding_agent.repository import Workspace
from coding_agent.tools.readfile import ReadFileTool


def test_readfile_returns_utf8_text(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "README.md"})
    assert result["ok"] is True
    assert "Sample Repository" in result["content"]


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
