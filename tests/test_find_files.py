from __future__ import annotations

from coding_agent.repository import Workspace
from coding_agent.tools.find_files import FindFilesTool


def test_find_files_matches_a_filename_anywhere_in_workspace(sample_git_repo):
    nested = sample_git_repo / "src" / "shell_policy.py"
    nested.parent.mkdir(exist_ok=True)
    nested.write_text("", encoding="utf-8")

    result = FindFilesTool(Workspace(sample_git_repo)).execute({"pattern": "shell_policy.py"})

    assert result == {"ok": True, "files": ["src/shell_policy.py"], "truncated": False}


def test_find_files_matches_glob_against_filename_and_relative_path(sample_git_repo):
    result = FindFilesTool(Workspace(sample_git_repo)).execute({"pattern": "*.md"})

    assert result == {"ok": True, "files": ["README.md"], "truncated": False}


def test_find_files_rejects_paths_outside_workspace(sample_git_repo):
    result = FindFilesTool(Workspace(sample_git_repo)).execute({"pattern": "*.py", "path": "../outside"})

    assert result["ok"] is False
    assert result["error"]["type"] == "workspace_violation"


def test_find_files_reports_truncation(sample_git_repo):
    result = FindFilesTool(Workspace(sample_git_repo), max_results=1).execute({"pattern": "*"})

    assert result["truncated"] is True
    assert len(result["files"]) == 1
