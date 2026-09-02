from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest


@pytest.fixture#pytest.fixture 给它加上 fixture 标记，pytest 记录它依赖一个名为 tmp_path 的 fixture
#打上标记的作用是，在运行时，sample_git_repo这个函数会被发现，pytest会用内置的逻辑和夹具传递给它。等价于先执行sample_git_repo = pytest.fixture(sample_git_repo)
def sample_git_repo(tmp_path):
    repo = tmp_path / "sample-repo"
    fixture = Path(__file__).parent / "fixtures" / "sample-repo"
    shutil.copytree(fixture, repo)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    return repo
