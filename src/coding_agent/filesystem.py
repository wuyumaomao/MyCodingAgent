from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pathspec

from .repository import Workspace, WorkspaceViolation


@dataclass(frozen=True)
class FileEntry:
    path: str
    kind: Literal["file", "directory"]
    size: int | None


def _load_ignore_spec(root: Path) -> pathspec.PathSpec:
    ignore_file = root / ".gitignore"
    try:
        lines = ignore_file.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        lines = []
    return pathspec.PathSpec.from_lines("gitignore", lines)


def scan_files(
    workspace: Workspace,
    path: str = ".",
    max_depth: int = 4,
    max_entries: int = 200,
) -> tuple[list[FileEntry], bool]:
    """Scan repository-relative entries in deterministic order."""

    if max_depth < 0 or max_entries <= 0:
        raise ValueError("max_depth must be non-negative and max_entries must be positive")
    base = workspace.resolve_relative(path)
    if not base.is_dir():
        raise NotADirectoryError(path)
    ignore_spec = _load_ignore_spec(workspace.root)
    base_relative = base.relative_to(workspace.root)
    entries: list[FileEntry] = []
    truncated = False

    def is_ignored(relative: Path, is_directory: bool) -> bool:
        value = relative.as_posix()
        if is_directory:
            value += "/"
        return ignore_spec.match_file(value)

    def visit(directory: Path, depth: int) -> None:
        nonlocal truncated
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            return
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                return
            relative = child.relative_to(workspace.root)
            if ".git" in relative.parts:
                continue
            is_directory = child.is_dir()
            if is_ignored(relative, is_directory):
                continue
            size = None if is_directory else _file_size(child)
            entries.append(
                FileEntry(
                    path=relative.as_posix(),
                    kind="directory" if is_directory else "file",
                    size=size,
                )
            )
            if is_directory and depth < max_depth and not child.is_symlink():
                visit(child, depth + 1)

    initial_depth = len(base_relative.parts)
    visit(base, initial_depth)
    return entries, truncated


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None
