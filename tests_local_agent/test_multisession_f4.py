"""F4 — every chat tab is an independent session.

Two concurrent runs with different session_ids must keep their own history
and their own run; neither's output may leak into the other.  Fully offline:
two real WebSockets against a uvicorn server with a scripted LLM.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from websockets.sync.client import connect as ws_connect

from local_agent.bridge.server.server import BridgeServer
from local_agent.core.config import AssistantSettings
from local_agent.llm.client import ModelReply


class _PacedLLM:
    """A synchronous fake LLM that tags its reply with the last user message."""

    def __init__(self, delay: float = 0.05) -> None:
        self._delay = delay
        self.provider_name = "scripted"
        self.model_name = "test"

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        # messages[0] is the system prompt; the last user message is the tag.
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = str(m.get("content", ""))
                break
        if self._delay:
            time.sleep(self._delay)
        return ModelReply(content=f"[tag:{user}]")

    def list_models(self) -> list[str]:
        return ["test"]


@pytest.fixture
def ws_app(tmp_path: Path):
    import socket as _socket

    from local_agent.bridge.api import handlers as bridge_handlers
    from local_agent.bridge.api.client import BridgeClient, _InProcessBackend, _welcome_to_info
    from local_agent.web.app import WebServer

    def _free_port() -> int:
        sock = _socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    bridge = BridgeServer(settings)
    bridge.start_in_process()
    backend = _InProcessBackend(bridge)
    backend._started = True
    client = BridgeClient(backend, _welcome_to_info(bridge.welcome()))
    server = WebServer(settings, client, host="127.0.0.1", port=_free_port())
    server.start_in_thread()
    if not server.wait_until_ready():
        server.stop()
        pytest.fail("web server did not start")

    bridge_handlers.create_client = lambda settings: _PacedLLM()  # type: ignore[assignment]

    yield {
        "url": f"ws://127.0.0.1:{server.port}/ws",
        "http": f"http://127.0.0.1:{server.port}",
        "settings": settings,
        "handlers": bridge.handlers,
    }
    server.stop()


def _collect_run(ws, session_tag: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Send a chat for ``session_tag`` and collect all events until chat_done."""
    ws.send(json.dumps({"type": "chat", "message": session_tag, "session_id": session_tag}))
    events: list[dict[str, Any]] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
        if msg.get("type") == "event":
            events.append(msg)
            if msg.get("event_type") == "chat_done":
                return events
    raise AssertionError(f"session {session_tag} never finished")


def test_two_tabs_keep_separate_history_and_runs(ws_app) -> None:
    """Two sessions running concurrently never cross-talk."""
    with ws_connect(ws_app["url"]) as a, ws_connect(ws_app["url"]) as b:
        # Launch both concurrently.
        a.send(json.dumps({"type": "chat", "message": "TAB-A", "session_id": "A"}))
        b.send(json.dumps({"type": "chat", "message": "TAB-B", "session_id": "B"}))

        # Drain each socket and check the assistant reply only references its
        # own session's message.
        ea = _collect_run(a, "TAB-A")
        eb = _collect_run(b, "TAB-B")

        def _final(events):
            for e in events:
                if e.get("event_type") == "assistant_final":
                    return e["payload"].get("text", "")
            return ""

        assert "TAB-A" in _final(ea)
        assert "TAB-B" in _final(eb)
        assert "TAB-A" not in _final(eb), "history of tab B must not leak into A"
        assert "TAB-B" not in _final(ea), "history of tab A must not leak into B"

    # Per-session history files exist.
    assert (ws_app["settings"].data_dir / "history" / "A.jsonl").is_file()
    assert (ws_app["settings"].data_dir / "history" / "B.jsonl").is_file()


def test_per_session_clear(ws_app) -> None:
    import requests

    # Run session C once.
    with ws_connect(ws_app["url"]) as ws:
        _collect_run(ws, "C")
    hist = requests.get(
        ws_app["http"] + "/api/history", params={"session_id": "C"}, timeout=5
    ).json()
    assert any(m.get("role") == "user" for m in hist)
    # Clear only session C; default session untouched.
    r = requests.post(ws_app["http"] + "/api/clear", params={"session_id": "C"}, timeout=5)
    assert r.json()["cleared"] is True
    hist2 = requests.get(
        ws_app["http"] + "/api/history", params={"session_id": "C"}, timeout=5
    ).json()
    assert hist2 == []
