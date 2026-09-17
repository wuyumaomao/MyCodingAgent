from __future__ import annotations

import pytest

from coding_agent.repository import Workspace
from coding_agent.tools.readfile import ReadFileTool


def test_readfile_schema_declares_object_and_required_path():
    schema = ReadFileTool.parameters
    assert schema["type"] == "object"
    assert schema["required"] == ["path"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["start"]["minimum"] == 1
    assert schema["properties"]["end"]["minimum"] == 1


def test_readfile_reads_one_based_inclusive_range_with_line_numbers(sample_git_repo):
    target = sample_git_repo / "README.md"
    target.write_text("zero\none\ntwo\nthree\n", encoding="utf-8")
    result = ReadFileTool(Workspace(sample_git_repo)).execute(
        {"path": "README.md", "start": 2, "end": 3}
    )
    assert result == {
        "ok": True,
        "path": "README.md",
        "content": "2: one\n3: two",
        "start": 2,
        "end": 3,
        "line_count": 4,
        "truncated": False,
    }


@pytest.mark.parametrize("arguments", [
    {"path": "README.md", "start": 0},
    {"path": "README.md", "end": 0},
    {"path": "README.md", "start": 4, "end": 3},
])
def test_readfile_rejects_invalid_line_range(sample_git_repo, arguments):
    result = ReadFileTool(Workspace(sample_git_repo)).execute(arguments)
    assert result["error"]["type"] == "invalid_arguments"


def test_readfile_returns_utf8_text(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "README.md"})
    assert result["ok"] is True
    assert "Sample Repository" in result["content"]


def test_readfile_allows_absolute_path_inside_repository(sample_git_repo):
    path = str(sample_git_repo / "README.md")
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": path})
    assert result["ok"] is True


def test_readfile_allows_parent_path_that_stays_inside(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "src/../README.md"})
    assert result["ok"] is True


def test_readfile_returns_stable_errors(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "missing.txt"})
    assert result == {"ok": False, "error": {"type": "file_not_found", "message": "File not found"}}


def test_readfile_rejects_directory(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "src"})
    assert result["error"]["type"] == "not_a_file"


def test_readfile_rejects_oversized_file(sample_git_repo):
    (sample_git_repo / "large.txt").write_text("123456", encoding="utf-8")
    result = ReadFileTool(Workspace(sample_git_repo), max_bytes=5).execute({"path": "large.txt"})
    assert result["error"]["type"] == "file_too_large"


def test_readfile_reads_a_line_range_from_a_file_above_the_old_cap(sample_git_repo):
    """The quota must limit the returned range, not the size of the source file."""
    target = sample_git_repo / "big.py"
    target.write_text("\n".join(f"line {number}" for number in range(20000)), encoding="utf-8")
    assert target.stat().st_size > 64 * 1024

    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "big.py", "start": 1, "end": 5})

    assert result["ok"] is True
    assert result["content"] == "1: line 0\n2: line 1\n3: line 2\n4: line 3\n5: line 4"
    assert result["start"] == 1
    assert result["end"] == 5
    assert result["line_count"] == 20000
    assert result["truncated"] is False


def test_readfile_reads_a_middle_range_from_a_large_file(sample_git_repo):
    target = sample_git_repo / "big.py"
    target.write_text("\n".join(f"line {number}" for number in range(20000)), encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "big.py", "start": 19999, "end": 20000})

    assert result["ok"] is True
    assert result["content"] == "19999: line 19998\n20000: line 19999"
    assert result["line_count"] == 20000


def test_readfile_truncates_content_at_the_result_quota(sample_git_repo):
    target = sample_git_repo / "many.txt"
    target.write_text("\n".join(f"row {number}" for number in range(100)), encoding="utf-8")

    result = ReadFileTool(Workspace(sample_git_repo), max_bytes=40).execute({"path": "many.txt"})

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["start"] == 1
    assert result["end"] < result["line_count"]
    assert len(result["content"].encode("utf-8")) <= 40
    assert result["line_count"] == 100


def test_readfile_rejects_invalid_arguments(sample_git_repo):
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "../outside.txt"})
    assert result["error"]["type"] == "workspace_violation"


def test_readfile_rejects_non_utf8_file(sample_git_repo):
    (sample_git_repo / "binary.dat").write_bytes(b"\xff\xfe")
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "binary.dat"})
    assert result["error"]["type"] == "decode_error"


def test_readfile_rejects_symlink_escape(sample_git_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = sample_git_repo / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    result = ReadFileTool(Workspace(sample_git_repo)).execute({"path": "link.txt"})
    assert result["error"]["type"] == "workspace_violation"
