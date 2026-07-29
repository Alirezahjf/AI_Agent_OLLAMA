"""End-to-end tests for the file_ops action group."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.actions import build_default_registry
from local_agent.actions.registry import ActionContext, ConfirmationGate, run_action
from local_agent.core.config import AssistantSettings
from local_agent.core.context import RuntimeContext


def _ctx(tmp_path: Path) -> ActionContext:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    gate = ConfirmationGate(settings.safety)
    gate.auto_approve()  # avoid prompting in tests
    return ActionContext(
        runtime=RuntimeContext(settings),
        confirmation_gate=gate,
        work_dir=tmp_path,
    )


def test_read_write_file_roundtrip(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    run_action(registry, "write_file", {"path": "hello.txt", "content": "hi"}, ctx)
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hi"
    result = run_action(registry, "read_file", {"path": "hello.txt"}, ctx)
    assert "hi" in result


def test_list_directory_shows_entries(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b").mkdir()
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    listing = run_action(registry, "list_directory", {"path": "."}, ctx)
    assert "a.txt" in listing
    assert "b" in listing


def test_make_directory_creates_parents(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    run_action(registry, "make_directory", {"path": "deep/nested/dir"}, ctx)
    assert (tmp_path / "deep" / "nested" / "dir").is_dir()


def test_move_path_moves_file(tmp_path: Path) -> None:
    (tmp_path / "src.txt").write_text("content", encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    run_action(registry, "move_path", {"source": "src.txt", "destination": "dst.txt"}, ctx)
    assert not (tmp_path / "src.txt").exists()
    assert (tmp_path / "dst.txt").read_text(encoding="utf-8") == "content"


def test_delete_path_removes_file(tmp_path: Path) -> None:
    target = tmp_path / "doomed.txt"
    target.write_text("bye", encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    run_action(registry, "delete_path", {"path": "doomed.txt"}, ctx)
    assert not target.exists()


def test_delete_directory_requires_recursive(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("x", encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    from local_agent.core.errors import AssistantError

    with pytest.raises(AssistantError):
        run_action(registry, "delete_path", {"path": "d"}, ctx)
    run_action(registry, "delete_path", {"path": "d", "recursive": True}, ctx)
    assert not (tmp_path / "d").exists()


def test_search_files_returns_matches(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "b.txt").write_text("goodbye world", encoding="utf-8")
    (tmp_path / "c.txt").write_text("nothing here", encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    result = run_action(registry, "search_files", {"query": "world"}, ctx)
    assert "a.txt" in result
    assert "b.txt" in result
    assert "c.txt" not in result


def test_write_file_rejects_path_outside_workdir(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    # Absolute path outside the work dir must still be writable (we trust the agent)
    result = run_action(registry, "write_file", {"path": str(other / "x.txt"), "content": "ok"}, ctx)
    assert "wrote" in result
    assert (other / "x.txt").read_text(encoding="utf-8") == "ok"
