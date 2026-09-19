from __future__ import annotations

import os
from pathlib import Path
import tempfile


class WriteError(RuntimeError):
    """Raised when an atomic file replacement fails."""


class AtomicWriter:
    """Write a file through a temporary sibling and an atomic replacement."""

    def write(self, target: Path, content: str) -> int:
        """Write ``content`` verbatim; the return value is the real on-disk byte count.

        ``newline=""`` 关掉文本模式的换行翻译。不关的话 Windows 上每个 ``\\n`` 会被
        翻成 ``\\r\\n``：模型写 17 字节磁盘上却是 20 字节，而 ``bytes_written`` 报的是
        输入长度。后果不只是数字不对——``patch_file`` 用 ``read_bytes()`` 读回 CRLF，
        于是 ``old_text`` 里的 ``\\n`` 再也匹配不上，而且写回时 ``\\r\\n`` 会变成
        ``\\r\\r\\n``（``readfile`` 读出来就是每行后多一个空行，每 patch 一次累积一次）。
        """
        target = Path(target)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
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
