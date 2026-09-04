from __future__ import annotations

import os
from pathlib import Path
import tempfile


class WriteError(RuntimeError):
    """Raised when an atomic file replacement fails."""


class AtomicWriter:
    """Write a file through a temporary sibling and an atomic replacement."""

    def write(self, target: Path, content: str) -> int:
        target = Path(target)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                written = temporary.tell()
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
            return len(content.encode("utf-8"))
        except (OSError, UnicodeError) as exc:
            raise WriteError("Unable to write file atomically") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
