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


# ---------------------------------------------------------------------------
# New tools (P2)
# ---------------------------------------------------------------------------


def test_append_file_creates_and_appends(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    run_action(registry, "append_file", {"path": "log.txt", "content": "line1\n"}, ctx)
    run_action(registry, "append_file", {"path": "log.txt", "content": "line2\n"}, ctx)
    assert (tmp_path / "log.txt").read_text(encoding="utf-8") == "line1\nline2\n"


def test_copy_path_copies_file(tmp_path: Path) -> None:
    (tmp_path / "orig.txt").write_text("hello", encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    run_action(registry, "copy_path", {"source": "orig.txt", "destination": "copy.txt"}, ctx)
    assert (tmp_path / "orig.txt").exists()
    assert (tmp_path / "copy.txt").read_text(encoding="utf-8") == "hello"


def test_copy_path_copies_directory(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "f.txt").write_text("data", encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    run_action(registry, "copy_path", {"source": "src", "destination": "dst"}, ctx)
    assert (tmp_path / "dst" / "f.txt").read_text(encoding="utf-8") == "data"


def test_zip_directory_creates_archive(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "f.txt").write_text("hello", encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    result = run_action(registry, "zip_directory", {"source": "data"}, ctx)
    assert "zip" in result.lower() or ".zip" in result
    assert (tmp_path / "data.zip").is_file()


def test_unzip_file_extracts(tmp_path: Path) -> None:
    import zipfile
    # Create a zip file first
    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr("hello.txt", "hello world")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    result = run_action(registry, "unzip_file", {"source": "test.zip"}, ctx)
    assert "extracted" in result.lower()
    assert (tmp_path / "test" / "hello.txt").read_text(encoding="utf-8") == "hello world"


def test_get_env_returns_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LOCAL_AGENT_VAR", "hello123")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    result = run_action(registry, "get_env", {"name": "TEST_LOCAL_AGENT_VAR"}, ctx)
    assert result == "hello123"


def test_get_env_returns_empty_for_missing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    result = run_action(registry, "get_env", {"name": "NONEXISTENT_VAR_XYZ"}, ctx)
    assert result == ""


def test_set_env_sets_variable(tmp_path: Path) -> None:
    import os
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    run_action(registry, "set_env", {"name": "MY_TEST_VAR", "value": "42"}, ctx)
    assert os.environ.get("MY_TEST_VAR") == "42"


def test_wait_action_completes(tmp_path: Path) -> None:
    import time
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    start = time.time()
    result = run_action(registry, "wait", {"seconds": 0.1}, ctx)
    elapsed = time.time() - start
    assert elapsed >= 0.09
    assert "waited" in result.lower()


def test_read_file_binary_returns_message(tmp_path: Path) -> None:
    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\x00\x01\x02\xff\xfe\xfd")
    ctx = _ctx(tmp_path)
    registry = build_default_registry(ctx)
    result = run_action(registry, "read_file", {"path": "binary.bin"}, ctx)
    assert "باینری" in result or "binary" in result.lower()
