from __future__ import annotations

import pytest
import subprocess

from coding_agent.repository import RepositoryError, Workspace, WorkspaceViolation, resolve_repository


def test_resolve_repository_from_nested_directory(sample_git_repo):
    assert resolve_repository(sample_git_repo / "src") == sample_git_repo.resolve()


def test_resolve_repository_rejects_non_repository(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr("coding_agent.repository.subprocess.run", fail)
    with pytest.raises(RepositoryError):#with 上下文管理器，pytest.raises用来捕获并检查是否抛出某个异常
        resolve_repository(tmp_path)


def test_workspace_allows_parent_traversal_that_stays_inside(sample_git_repo):
    resolved = Workspace(sample_git_repo).resolve_relative("src/../README.md")
    assert resolved == (sample_git_repo / "README.md").resolve()


def test_workspace_rejects_parent_traversal_that_escapes(sample_git_repo):
    with pytest.raises(WorkspaceViolation):
        Workspace(sample_git_repo).resolve_relative("../outside.txt")


def test_workspace_allows_absolute_path_inside_repository(sample_git_repo):
    resolved = Workspace(sample_git_repo).resolve_relative(str(sample_git_repo / "src" / "placeholder.txt"))
    assert resolved == (sample_git_repo / "src" / "placeholder.txt").resolve()


def test_workspace_rejects_absolute_path_outside_repository(sample_git_repo, tmp_path):
    with pytest.raises(WorkspaceViolation):
        Workspace(sample_git_repo).resolve_relative(str(tmp_path / "outside.txt"))


def test_workspace_rejects_symlink_escape(sample_git_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = sample_git_repo / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(WorkspaceViolation):
        Workspace(sample_git_repo).resolve_relative("link.txt")
