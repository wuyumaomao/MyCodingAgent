from __future__ import annotations

import os
from pathlib import Path

from ..repository import Workspace


def target_venv_dir(workspace: Workspace) -> Path:
    """Return the target repository's conventional virtual-environment path."""
    return workspace.root / ".venv"


def resolve_target_python(
    workspace: Workspace,
    *,
    platform_name: str | None = None,
) -> Path | None:
    """Find a usable Python executable in the target repository's .venv."""
    platform_name = os.name if platform_name is None else platform_name
    venv = target_venv_dir(workspace)
    relative = Path("Scripts/python.exe") if platform_name == "nt" else Path("bin/python")
    candidate = venv / relative
    # Unix virtual environments commonly expose Python as a symlink.
    if candidate.is_file():
        return candidate
    return None


def target_venv_bin(workspace: Workspace, *, platform_name: str | None = None) -> Path | None:
    """Return the target environment's executable directory when it exists."""
    platform_name = os.name if platform_name is None else platform_name
    directory = target_venv_dir(workspace) / ("Scripts" if platform_name == "nt" else "bin")
    return directory if directory.is_dir() else None
