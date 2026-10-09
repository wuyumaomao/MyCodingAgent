from __future__ import annotations

import json
import pytest

from coding_agent.repository import Workspace
from coding_agent.session import (
    SessionError,
    SessionStore,
    reconcile_checkpoint,
    workspace_snapshot,
)


def test_session_store_creates_and_round_trips_state(sample_git_repo):
    store = SessionStore(Workspace(sample_git_repo))
    created = store.create()
    created.history.append({"role": "user", "content": "inspect README"})
    created.memory["working_memory"]["task_summary"] = "inspect README"
    store.save(created)
    loaded = store.load(created.session_id)
    assert loaded.session_id == created.session_id
    assert loaded.repo_root == Workspace(sample_git_repo).root
    assert loaded.history[-1]["content"] == "inspect README"
    assert store.path_for(created.session_id).is_file()


def test_session_store_rejects_repository_mismatch(sample_git_repo, tmp_path):
    store = SessionStore(Workspace(sample_git_repo))
    session = store.create()
    payload = json.loads(store.path_for(session.session_id).read_text(encoding="utf-8"))
    payload["repo_root"] = str(tmp_path / "other")
    store.path_for(session.session_id).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionError, match="different repository"):
        store.load(session.session_id)


def test_session_has_recovery_state_and_round_trips_it(sample_git_repo):
    store = SessionStore(Workspace(sample_git_repo))
    session = store.create()

    assert session.checkpoints == {"active": None, "recent": []}
    assert session.resume_state["status"] == "clean"
    assert session.runtime_identity["repo_root"] == str(Workspace(sample_git_repo).root)

    session.checkpoints["active"] = {"id": "cp-1", "status": "running"}
    session.resume_state = {"status": "resume_required", "checkpoint_id": "cp-1"}
    session.cancelled_runs = [{"run_id": "run-1", "start": 0, "end": 2}]
    store.save(session)
    loaded = store.load(session.session_id)

    assert loaded.checkpoints["active"]["id"] == "cp-1"
    assert loaded.resume_state["status"] == "resume_required"
    assert loaded.cancelled_runs == [{"run_id": "run-1", "start": 0, "end": 2}]


def test_session_run_state_defaults_and_round_trips(sample_git_repo):
    store = SessionStore(Workspace(sample_git_repo))
    session = store.create()
    assert session.run_state["status"] == "idle"
    session.run_state.update({"status": "cancelled", "run_id": "run-1", "reason": "keyboard_interrupt"})
    store.save(session)
    loaded = store.load(session.session_id)
    assert loaded.run_state["status"] == "cancelled"
    assert loaded.run_state["run_id"] == "run-1"
    assert loaded.run_state["reason"] == "keyboard_interrupt"


def test_reconcile_write_checkpoint_when_expected_content_is_already_present(sample_git_repo):
    target = sample_git_repo / "result.txt"
    target.write_text("done\n", encoding="utf-8")
    checkpoint = {
        "id": "cp-1",
        "status": "interrupted",
        "calls": [{
            "id": "w-1",
            "name": "write_file",
            "arguments": {"path": "result.txt", "content": "done\n"},
            "status": "running",
            "workspace_before": {"files": {"result.txt": {"exists": False}}},
        }],
    }

    result = reconcile_checkpoint(Workspace(sample_git_repo), checkpoint)

    assert result["resume_state"]["status"] == "reconciled"
    assert result["tool_results"][0]["result"]["ok"] is True
    assert result["tool_results"][0]["result"]["recovered"] is True


def test_reconcile_patch_checkpoint_does_not_claim_success_without_file_evidence(sample_git_repo):
    target = sample_git_repo / "result.txt"
    target.write_text("different\n", encoding="utf-8")
    checkpoint = {
        "id": "cp-1",
        "status": "interrupted",
        "calls": [{
            "id": "p-1",
            "name": "patch_file",
            "arguments": {"path": "result.txt", "old_text": "before", "new_text": "after"},
            "status": "running",
            "workspace_before": {"files": {"result.txt": {"exists": True, "sha256": "old"}}},
        }],
    }

    result = reconcile_checkpoint(Workspace(sample_git_repo), checkpoint)

    assert result["resume_state"]["status"] == "resume_required"
    assert result["tool_results"][0]["result"]["error"]["type"] == "execution_interrupted"
