from __future__ import annotations

import json
import pytest

from coding_agent.repository import Workspace
from coding_agent.session import SessionError, SessionStore


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
