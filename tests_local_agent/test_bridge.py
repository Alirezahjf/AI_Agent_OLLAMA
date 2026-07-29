"""Tests for the Bridge: protocol, in-process client, handlers, and HTTP server."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from local_agent.actions.registry import ConfirmationGate
from local_agent.bridge import BridgeClient, BridgeConnectionError
from local_agent.bridge.api.handlers import BridgeHandlers, EventType
from local_agent.bridge.protocol import (
    ActionInvocation,
    ActionResult,
    Hello,
    MessageType,
    PROTOCOL_VERSION,
    Welcome,
    encode_message,
    decode_message,
)
from local_agent.core.config import AssistantSettings
from local_agent.core.context import ConversationMessage, RuntimeContext
from local_agent.core.errors import AssistantError
from local_agent.llm.client import ModelReply, ToolCall


# ---------------------------------------------------------------------------
# Protocol tests
# ---------------------------------------------------------------------------


def test_protocol_roundtrip() -> None:
    msg = {"id": "abc", "type": "list_actions", "payload": {}}
    encoded = encode_message(msg)
    decoded = decode_message(encoded)
    assert decoded == msg


def test_hello_has_protocol_version() -> None:
    hello = Hello()
    assert hello.protocol_version == PROTOCOL_VERSION
    payload = hello.to_dict()
    assert payload["protocol_version"] == PROTOCOL_VERSION


def test_action_invocation_from_dict() -> None:
    inv = ActionInvocation.from_dict({
        "name": "read_file",
        "arguments": {"path": "x.txt"},
        "auto_confirm": True,
    })
    assert inv.name == "read_file"
    assert inv.arguments == {"path": "x.txt"}
    assert inv.auto_confirm is True


def test_action_result_to_dict() -> None:
    result = ActionResult(name="x", text="ok", success=True, artifacts=["a.png"])
    payload = result.to_dict()
    assert payload["name"] == "x"
    assert payload["success"] is True
    assert payload["artifacts"] == ["a.png"]


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path) -> AssistantSettings:
    return AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)


def test_handler_welcome(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    handlers = BridgeHandlers.build(settings)
    welcome = handlers.welcome()
    assert welcome.protocol_version == PROTOCOL_VERSION
    assert welcome.session_id
    assert "actions" in welcome.capabilities


def test_handler_list_actions(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    handlers = BridgeHandlers.build(settings)
    response = handlers.handle({"id": "1", "type": "list_actions", "payload": {}})
    assert response["ok"] is True
    result = response["result"]
    assert any(d.startswith("open_application") for d in result)


def test_handler_invoke_safe_action(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    handlers = BridgeHandlers.build(settings)
    (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
    response = handlers.handle({
        "id": "1",
        "type": "invoke_action",
        "payload": {"name": "read_file", "arguments": {"path": "x.txt"}, "auto_confirm": True},
    })
    assert response["ok"] is True
    assert "hi" in response["result"]["text"]


def test_handler_unknown_type(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    handlers = BridgeHandlers.build(settings)
    response = handlers.handle({"id": "1", "type": "no_such_type", "payload": {}})
    assert response["ok"] is False
    assert "unknown" in response["error"]["code"].lower()


def test_handler_get_status(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    handlers = BridgeHandlers.build(settings)
    response = handlers.handle({"id": "1", "type": "get_status", "payload": {}})
    assert response["ok"] is True
    assert "settings" in response["result"]
    assert "history" in response["result"]


def test_handler_clear_history(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    handlers = BridgeHandlers.build(settings)
    handlers.runtime.append(ConversationMessage(role="user", content="hi"))
    response = handlers.handle({"id": "1", "type": "clear_history", "payload": {}})
    assert response["ok"] is True
    assert handlers.runtime.snapshot() == []


def test_handler_set_model(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    handlers = BridgeHandlers.build(settings)
    response = handlers.handle({
        "id": "1",
        "type": "set_model",
        "payload": {"model": "gpt-4-test"},
    })
    assert response["ok"] is True
    assert response["result"]["model"] == "gpt-4-test"


# ---------------------------------------------------------------------------
# In-process BridgeClient
# ---------------------------------------------------------------------------


def test_in_process_client_works(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    client = BridgeClient.start_in_process(settings)
    assert client.info is not None
    assert client.info.protocol_version == PROTOCOL_VERSION
    actions = client.list_actions()
    # Actions are returned as description strings; the name appears first
    assert any(a.startswith("open_application ") or a.startswith("open_application  ") for a in actions)


def test_in_process_client_invoke(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    (tmp_path / "data.txt").write_text("hello", encoding="utf-8")
    client = BridgeClient.start_in_process(settings)
    result = client.invoke_action(
        "read_file", {"path": "data.txt"}, auto_confirm=True
    )
    assert result.success
    assert "hello" in result.text


# ---------------------------------------------------------------------------
# Chat streaming (uses scripted LLM)
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []
        self.provider_name = "scripted"
        self.model_name = "test"

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls.append(list(messages))
        return self._replies.pop(0) if self._replies else ModelReply(content="done")

    def list_models(self) -> list[str]:
        return ["test"]


def _patch_llm(handlers: BridgeHandlers, scripted: _ScriptedLLM) -> None:
    """Replace the LLM factory used by the chat loop."""
    from local_agent.bridge import api as bridge_api
    bridge_api.handlers.create_client = lambda settings: scripted  # type: ignore[assignment]


def test_chat_stream_emits_events(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    handlers = BridgeHandlers.build(settings)
    # Auto-approve destructive actions so the test doesn't wait for confirmation
    handlers.gate.auto_approve()
    scripted = _ScriptedLLM([
        ModelReply(
            content="writing",
            tool_calls=(ToolCall(name="write_file", arguments={"path": "f.txt", "content": "ok"}),),
        ),
        ModelReply(content="done."),
    ])
    _patch_llm(handlers, scripted)

    # Subscribe a listener BEFORE starting the run so we see all events.
    events: list[str] = []
    event_holder: list[Any] = []
    finished = threading.Event()

    def listener(event) -> None:
        event_holder.append(event)
        events.append(event.type)
        if event.type in {EventType.CHAT_DONE.value, EventType.CHAT_FAILED.value}:
            finished.set()

    handlers.event_bus.subscribe(listener)
    run_id = handlers._start_chat_run("write a file")
    queue = handlers.event_bus.create_run_queue(run_id)
    # Drain the run queue so the worker can move on
    import queue as _q
    while not finished.is_set():
        try:
            queue.get(timeout=0.2)
        except _q.Empty:
            continue
    assert EventType.CHAT_STARTED.value in events
    assert EventType.TOOL_PROPOSED.value in events
    assert EventType.TOOL_RESULT.value in events
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "ok"


def test_chat_respects_max_turns(tmp_path: Path) -> None:
    settings = AssistantSettings(
        data_dir=tmp_path, work_dir=tmp_path,
    )
    from dataclasses import replace
    settings = replace(settings, safety=replace(settings.safety, max_agent_turns=2))
    handlers = BridgeHandlers.build(settings)
    scripted = _ScriptedLLM([
        ModelReply(
            content="loopy",
            tool_calls=(ToolCall(name="system_info", arguments={}),),
        ),
    ] * 10)
    _patch_llm(handlers, scripted)

    seen_turns: list[int] = []
    finished = threading.Event()

    def listener(event) -> None:
        if event.type == EventType.TURN_STARTED.value:
            seen_turns.append(event.payload.get("turn", 0))
        if event.type in {EventType.CHAT_DONE.value, EventType.CHAT_FAILED.value}:
            finished.set()

    handlers.event_bus.subscribe(listener)
    handlers._start_chat_run("do stuff")
    finished.wait(timeout=5)
    # Should not have exceeded 2 turns
    assert max(seen_turns) <= 2 if seen_turns else True


def test_chat_destructive_action_requires_confirmation(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    (tmp_path / "doomed.txt").write_text("bye", encoding="utf-8")
    handlers = BridgeHandlers.build(settings)
    scripted = _ScriptedLLM([
        ModelReply(
            content="deleting",
            tool_calls=(ToolCall(name="delete_path", arguments={"path": "doomed.txt"}),),
        ),
        ModelReply(content="skipped."),
    ])
    _patch_llm(handlers, scripted)

    saw_confirm = False
    finished = threading.Event()

    def listener(event) -> None:
        nonlocal saw_confirm
        if event.type == EventType.TOOL_CONFIRM_REQUESTED.value:
            saw_confirm = True
            handlers.resolve_confirmation(event.payload["request_id"], False)
        if event.type in {EventType.CHAT_DONE.value, EventType.CHAT_FAILED.value}:
            finished.set()

    handlers.event_bus.subscribe(listener)
    handlers._start_chat_run("delete it")
    finished.wait(timeout=130)
    assert saw_confirm
    assert (tmp_path / "doomed.txt").exists()
