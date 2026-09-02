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
        )#若执行错误，python系统自动抛出异常(这个异常不用自己写)
    except (OSError, subprocess.CalledProcessError) as exc:#表示捕获两种异常中的任意一种，把捕获到的原始异常保存到变量 exc
        raise RepositoryError("Unable to find a Git repository") from exc #捕获了后执行这个，这个是异常包装成一个新异常
    return Path(result.stdout.strip()).resolve()


@dataclass(frozen=True)
class Workspace:
    root: Path

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        if not root.is_dir():
            raise RepositoryError("Repository root is not a directory")
        object.__setattr__(self, "root", root)

    def resolve_relative(self, path: str) -> Path:#路径拼接后不能逃逸
        if not isinstance(path, str) or not path:
            raise WorkspaceViolation("Path must be a non-empty relative path")
        candidate_path = Path(path)
        base = candidate_path if candidate_path.is_absolute() else self.root / candidate_path
        candidate = base.resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise WorkspaceViolation("Path must stay inside the repository")
        return candidate
