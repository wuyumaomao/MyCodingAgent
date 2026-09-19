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


def test_search_description_explains_accepted_paths(sample_git_repo):
    tool = SearchTool(Workspace(sample_git_repo))

    assert "file path is also accepted" in tool.description.lower()
    assert "file" in tool.parameters["properties"]["path"]["description"].lower()


def test_search_accepts_a_file_path(sample_git_repo):
    """模型经常把已知文件当 path 传进来；报错只会浪费一轮，直接搜那个文件。"""
    (sample_git_repo / "target.py").write_text("def wanted():\n    pass\n", encoding="utf-8")

    result = SearchTool(Workspace(sample_git_repo)).execute({"pattern": "^def wanted", "path": "target.py"})

    assert result["ok"] is True
    assert [match["path"] for match in result["matches"]] == ["target.py"]
    assert result["matches"][0]["line"] == 1


def test_search_accepts_a_file_path_without_rg(monkeypatch, sample_git_repo):
    monkeypatch.setattr("coding_agent.tools.search.shutil.which", lambda _: None)
    (sample_git_repo / "target.py").write_text("def wanted():\n    pass\n", encoding="utf-8")

    result = SearchTool(Workspace(sample_git_repo)).execute({"pattern": "^def wanted", "path": "target.py"})

    assert [match["path"] for match in result["matches"]] == ["target.py"]


def test_search_reports_a_missing_path(sample_git_repo):
    result = SearchTool(Workspace(sample_git_repo)).execute({"pattern": "x", "path": "nope.py"})

    assert result["error"]["type"] == "path_not_found"


def test_search_excludes_agent_and_generated_directories(sample_git_repo):
    for directory in (".coding-agent", ".pytest_cache", "dist", "build"):
        target = sample_git_repo / directory
        target.mkdir()
        (target / "secret.txt").write_text("needle", encoding="utf-8")

    result = SearchTool(Workspace(sample_git_repo)).execute({"pattern": "needle", "path": "."})

    assert result["matches"] == []
