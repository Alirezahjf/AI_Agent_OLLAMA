from pathlib import Path

from agent.storage import Storage


def test_preferences_and_audit_do_not_persist_file_content(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.sqlite3"
    storage = Storage(db_path)
    storage.set_preference(12, "avalai", "claude-sonnet-5")
    assert storage.preference(12, "ollama") == ("avalai", "claude-sonnet-5")

    storage.put_pending(
        "action-1",
        12,
        {"tool": "write_file", "args": {"path": "src/app.py", "content": "SUPER_SECRET_CONTENT"}},
    )
    events = storage.recent_audit(12)
    assert "SUPER_SECRET_CONTENT" not in "\n".join(event["detail"] for event in events)
    assert "src/app.py" in "\n".join(event["detail"] for event in events)


def test_history_honours_context_budget(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "agent.sqlite3")
    storage.add(1, "user", "old " + "x" * 200)
    storage.add(1, "assistant", "new")
    history = storage.history(1, max_chars=20)
    assert history == [{"role": "assistant", "content": "new"}]
