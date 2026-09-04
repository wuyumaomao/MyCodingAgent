from __future__ import annotations

from coding_agent.filesystem import scan_files
from coding_agent.repository import Workspace
from coding_agent.tools.listfiles import ListFilesTool


def test_listfiles_schema_declares_object_and_numeric_bounds():
    schema = ListFilesTool.parameters
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["max_depth"]["minimum"] == 0
    assert schema["properties"]["max_entries"]["minimum"] == 1


def test_listfiles_honors_ignore_and_depth(sample_git_repo):
    entries, truncated = scan_files(Workspace(sample_git_repo), max_depth=1)
    paths = {entry.path for entry in entries}
    assert "README.md" in paths
    assert "ignored.txt" not in paths
    assert truncated is False


def test_listfiles_returns_truncated_when_entry_limit_is_reached(sample_git_repo):
    result = ListFilesTool(Workspace(sample_git_repo)).execute({"max_entries": 1})
    assert result["truncated"] is True
    assert len(result["entries"]) == 1


def test_listfiles_rejects_invalid_arguments(sample_git_repo):
    result = ListFilesTool(Workspace(sample_git_repo)).execute({"max_depth": -1})
    assert result["ok"] is False
    assert result["error"]["type"] == "invalid_arguments"
