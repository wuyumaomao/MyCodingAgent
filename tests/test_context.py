from __future__ import annotations

from coding_agent.context import build_repository_context
from coding_agent.repository import Workspace


def test_context_contains_root_and_relative_manifest(sample_git_repo):
    context = build_repository_context(Workspace(sample_git_repo))
    assert "Repository Context" in context
    assert "README.md" in context
    assert str(sample_git_repo) not in context


def test_context_marks_truncated_manifest(sample_git_repo):
    context = build_repository_context(Workspace(sample_git_repo), max_entries=1)
    assert "truncated: true" in context
