from __future__ import annotations

from coding_agent.repository import Workspace
from coding_agent.tools.search import SearchTool


def test_search_returns_matching_lines_with_repository_relative_paths(sample_git_repo):
    result = SearchTool(Workspace(sample_git_repo)).execute({"pattern": "Sample Repository"})

    assert result == {
        "ok": True,
        "matches": [{"path": "README.md", "line": 1, "text": "# Sample Repository"}],
        "truncated": False,
    }


def test_search_returns_no_matches(sample_git_repo):
    result = SearchTool(Workspace(sample_git_repo)).execute({"pattern": "not-present"})

    assert result == {"ok": True, "matches": [], "truncated": False}


def test_search_rejects_paths_outside_workspace(sample_git_repo):
    result = SearchTool(Workspace(sample_git_repo)).execute({"pattern": "README", "path": "../outside"})

    assert result["ok"] is False
    assert result["error"]["type"] == "workspace_violation"


def test_search_falls_back_when_rg_is_unavailable(monkeypatch, sample_git_repo):
    monkeypatch.setattr("coding_agent.tools.search.shutil.which", lambda _: None)

    result = SearchTool(Workspace(sample_git_repo)).execute({"pattern": "Sample Repository"})

    assert result["matches"] == [{"path": "README.md", "line": 1, "text": "# Sample Repository"}]


def test_search_description_explains_directory_path_requirement(sample_git_repo):
    tool = SearchTool(Workspace(sample_git_repo))

    assert "directory" in tool.description.lower()
    assert "not a file" in tool.description.lower()
    assert "directory" in tool.parameters["properties"]["path"]["description"].lower()
