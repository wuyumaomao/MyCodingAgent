from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def sample_git_repo(tmp_path):
    repo = tmp_path / "sample-repo"
    fixture = Path(__file__).parent / "fixtures" / "sample-repo"
    shutil.copytree(fixture, repo)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    return repo
