from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


class RepositoryError(RuntimeError):
    """Raised when a Git repository cannot be resolved."""


class WorkspaceViolation(ValueError):
    """Raised when a path escapes the repository workspace."""


def resolve_repository(start: Path) -> Path:
    """Resolve the Git top-level directory containing ``start``."""

    start = Path(start).expanduser().resolve()
    if start.is_file():
        start = start.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RepositoryError("Unable to find a Git repository") from exc
    return Path(result.stdout.strip()).resolve()


@dataclass(frozen=True)
class Workspace:
    root: Path

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        if not root.is_dir():
            raise RepositoryError("Repository root is not a directory")
        object.__setattr__(self, "root", root)

    def resolve_relative(self, path: str) -> Path:
        if not isinstance(path, str) or not path:
            raise WorkspaceViolation("Path must be a non-empty relative path")
        candidate_path = Path(path)
        if candidate_path.is_absolute() or ".." in candidate_path.parts:
            raise WorkspaceViolation("Path must stay inside the repository")
        candidate = (self.root / candidate_path).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise WorkspaceViolation("Path must stay inside the repository")
        return candidate
