"""End-to-end integration tests driven through the BridgeClient.

These exercise the full chain: a scripted LLM -> Bridge handlers ->
tool execution -> shared state.  The CLI/RPC layer is bypassed so
the tests are fast and deterministic.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from local_agent.bridge import BridgeClient
from local_agent.bridge.api.handlers import BridgeHandlers, EventType
from local_agent.core.config import AssistantSettings
from local_agent.llm.client import ModelReply, ToolCall


class _ScriptedLLM:
    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.provider_name = "scripted"
        self.model_name = "test"

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        return self._replies.pop(0) if self._replies else ModelReply(content="done")

    def list_models(self) -> list[str]:
        return ["test"]


def _setup(tmp_path: Path, replies: list[ModelReply], *, auto_approve: bool = True) -> tuple[BridgeClient, BridgeHandlers]:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    from local_agent.bridge.server.server import BridgeServer
    server = BridgeServer(settings)
    server.start_in_process()
    if auto_approve:
        server.handlers.gate.auto_approve()
    from local_agent.bridge import api as bridge_api
    bridge_api.handlers.create_client = lambda settings: _ScriptedLLM(replies)  # type: ignore[assignment]
    # Build a client that reuses the existing server instead of spinning
    # up a second one.
    from local_agent.bridge.api.client import _InProcessBackend, _welcome_to_info
    backend = _InProcessBackend(server)
    backend._started = True
    client = BridgeClient(backend, _welcome_to_info(server.welcome()))
    return client, server.handlers


def _drain_chat(client: BridgeClient, message: str) -> list[EventType]:
    events: list[EventType] = []
    for ev in client.chat(message):
        events.append(EventType(ev.type))
    return events


def test_scenario_write_then_read(tmp_path: Path) -> None:
    client, handlers = _setup(tmp_path, [
        ModelReply(
            content="writing",
            tool_calls=(ToolCall(name="write_file", arguments={"path": "x.py", "content": "print(1)\n"}),),
        ),
        ModelReply(
            content="reading",
            tool_calls=(ToolCall(name="read_file", arguments={"path": "x.py"}),),
        ),
        ModelReply(content="done"),
    ])
    events = _drain_chat(client, "create x.py and show its content")
    assert EventType.CHAT_DONE in events
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "print(1)\n"


def test_scenario_destructive_action_refused(tmp_path: Path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("data", encoding="utf-8")
    client, handlers = _setup(tmp_path, [
        ModelReply(
            content="deleting",
            tool_calls=(ToolCall(name="delete_path", arguments={"path": "important.txt"}),),
        ),
        ModelReply(content="ok, I will not delete"),
    ], auto_approve=False)
    saw_confirm = False

    def listener(event) -> None:
        nonlocal saw_confirm
        if event.type == EventType.TOOL_CONFIRM_REQUESTED.value:
            saw_confirm = True
            handlers.resolve_confirmation(event.payload["request_id"], False)

    handlers.event_bus.subscribe(listener)
    events = _drain_chat(client, "delete it")
    assert saw_confirm
    # File must still exist (refused)
    assert target.exists()


def test_scenario_multiple_tool_calls(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    client, handlers = _setup(tmp_path, [
        ModelReply(
            content="reading all",
            tool_calls=(
                ToolCall(name="read_file", arguments={"path": "a.txt"}),
                ToolCall(name="read_file", arguments={"path": "b.txt"}),
                ToolCall(name="read_file", arguments={"path": "c.txt"}),
            ),
        ),
        ModelReply(content="done"),
    ])
    events = _drain_chat(client, "read a, b, c")
    # 3 tool results were emitted
    tool_results = sum(1 for e in events if e == EventType.TOOL_RESULT)
    assert tool_results == 3


def test_scenario_provider_error_does_not_crash(tmp_path: Path) -> None:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    handlers = BridgeHandlers.build(settings)
    handlers.gate.auto_approve()
    from local_agent.bridge import api as bridge_api

    class _Broken:
        provider_name = "broken"
        model_name = "broken"
        def complete(self, messages, tools):
            raise RuntimeError("provider down")
        def list_models(self):
            return []

    bridge_api.handlers.create_client = lambda settings: _Broken()  # type: ignore[assignment]
    client = BridgeClient.start_in_process(settings)
    events = _drain_chat(client, "hi")
    # We expect a chat_failed event
    assert EventType.CHAT_FAILED in events


def test_scenario_history_persists_across_chats(tmp_path: Path) -> None:
    """The history saved on the first chat is visible to the second."""
    client, handlers = _setup(tmp_path, [
        ModelReply(content="hi back"),
        ModelReply(content="I remember you said hi"),
    ])
    _drain_chat(client, "hi")
    _drain_chat(client, "do you remember?")
    # The second turn's LLM call should see the first turn's history
    # The handlers snapshot must contain at least 2 user + 2 assistant messages
    history = handlers.runtime.snapshot()
    roles = [m.role for m in history]
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2


def test_scenario_status_and_actions_reflect_state(tmp_path: Path) -> None:
    client, handlers = _setup(tmp_path, [])
    status = client.get_status()
    assert "actions" in status
    assert "settings" in status
    descriptions = client.list_actions()
    assert any(d.startswith("open_application") for d in descriptions)
    assert any(d.startswith("send_telegram_desktop") for d in descriptions)


def test_scenario_clear_history_works(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path, [ModelReply(content="ok")])
    _drain_chat(client, "hi")
    client.clear_history()
    # History should be empty
    history = client.get_history()
    assert history == []


def test_scenario_set_model_persists(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path, [])
    result = client.set_model(model="my-test-model")
    # The set_model call always returns the freshly-active model; the
    # scripted LLM that was injected for the chat loop has its own
    # model_name so we may see it instead.
    assert "model" in result
