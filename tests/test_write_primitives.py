from __future__ import annotations

import pytest

from coding_agent.tools.approval import WriteApprovalGate, WritePreview
from coding_agent.tools.atomic_writer import AtomicWriter, WriteError


def test_approval_gate_calls_callback_once():
    previews = []
    gate = WriteApprovalGate(ask=lambda preview: previews.append(preview) or True)

    assert gate.approve(WritePreview("create", "new.py", content="x")) == "approved"
    assert len(previews) == 1


def test_approval_gate_without_callback_requires_approval():
    assert WriteApprovalGate().approve(WritePreview("overwrite", "a.py")) == "required"


def test_approval_gate_denies_when_callback_returns_false():
    assert WriteApprovalGate(ask=lambda _: False).approve(WritePreview("create", "a.py")) == "denied"


def test_atomic_writer_writes_utf8_content(tmp_path):
    target = tmp_path / "a.py"

    written = AtomicWriter().write(target, "你好\n")

    assert written == len("你好\n".encode("utf-8"))
    assert target.read_text(encoding="utf-8") == "你好\n"


def test_atomic_writer_preserves_original_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        "coding_agent.tools.atomic_writer.os.replace",
        lambda *_: (_ for _ in ()).throw(OSError("disk")),
    )

    with pytest.raises(WriteError):
        AtomicWriter().write(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
