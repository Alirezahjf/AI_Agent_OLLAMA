"""Tests for RuntimeContext (history persistence, compaction, events)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from local_agent.core.config import AssistantSettings
from local_agent.core.context import ConversationMessage, RuntimeContext


def _make(tmp_path: Path) -> RuntimeContext:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    return RuntimeContext(settings)


def test_append_persists_immediately(tmp_path: Path) -> None:
    ctx = _make(tmp_path)
    ctx.append(ConversationMessage(role="user", content="hi"))
    raw = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    assert "hi" in raw
    record = json.loads(raw.strip().splitlines()[0])
    assert record["role"] == "user"


def test_snapshot_is_independent_of_internal_state(tmp_path: Path) -> None:
    ctx = _make(tmp_path)
    ctx.append(ConversationMessage(role="user", content="first"))
    snap = ctx.snapshot()
    ctx.append(ConversationMessage(role="user", content="second"))
    assert len(snap) == 1
    assert len(ctx.snapshot()) == 2


def test_compact_drops_oldest_until_within_budget(tmp_path: Path) -> None:
    ctx = _make(tmp_path)
    for i in range(10):
        ctx.append(ConversationMessage(role="user", content="x" * 100))
    ctx.compact(max_messages=3, max_chars=10_000)
    snap = ctx.snapshot()
    assert len(snap) == 3


def test_replace_last_assistant(tmp_path: Path) -> None:
    ctx = _make(tmp_path)
    ctx.append(ConversationMessage(role="user", content="hi"))
    ctx.append(ConversationMessage(role="assistant", content="draft"))
    ctx.replace_last_assistant("final")
    last = ctx.snapshot()[-1]
    assert last.role == "assistant"
    assert last.content == "final"


def test_clear_empties_history(tmp_path: Path) -> None:
    ctx = _make(tmp_path)
    ctx.append(ConversationMessage(role="user", content="hi"))
    ctx.clear()
    assert ctx.snapshot() == []


def test_on_invokes_listeners(tmp_path: Path) -> None:
    ctx = _make(tmp_path)
    received: list[tuple[str, dict]] = []

    def listener(event: str, payload: dict) -> None:
        received.append((event, payload))

    ctx.on(listener)
    ctx.append(ConversationMessage(role="user", content="hi"))
    ctx.clear()
    events = [item[0] for item in received]
    assert "message" in events
    assert "cleared" in events


def test_concurrent_appends_are_safe(tmp_path: Path) -> None:
    ctx = _make(tmp_path)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for j in range(20):
                ctx.append(ConversationMessage(role="user", content=f"t{i}-m{j}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(ctx.snapshot()) == 100


def test_to_openai_renders_tool_and_assistant() -> None:
    msg = ConversationMessage(role="tool", content="ok", name="ls", tool_call_id="abc")
    rendered = msg.to_openai()
    assert rendered["role"] == "tool"
    assert rendered["tool_call_id"] == "abc"
    user = ConversationMessage(role="user", content="hi")
    assert user.to_openai() == {"role": "user", "content": "hi"}
    assistant = ConversationMessage(role="assistant", content="x", name="bot")
    assert assistant.to_openai()["name"] == "bot"
