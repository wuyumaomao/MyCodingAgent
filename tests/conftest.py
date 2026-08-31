from __future__ import annotations

import subprocess

import pytest


@pytest.fixture
def sample_git_repo(tmp_path):
    repo = tmp_path / "sample-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "placeholder.txt").write_text("placeholder", encoding="utf-8")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    return repo
