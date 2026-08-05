"""B1 — WebSocket confirm/control messages must be read mid-run.

Regression tests for the worst bug in the app: when a ``chat`` message
arrived, ``ws()`` entered a blocking inner loop consuming the event-bus
queue and never called ``receive_text()`` again, so ``confirm`` /
``interrupt`` / ``ping`` sent from the UI while a run was in flight piled
up in the socket buffer until the run finished.  An approval clicked
mid-run was therefore ignored and the action timed out as "refused".

The rewritten handler runs a reader task plus per-run forwarder tasks so
control messages are handled immediately.  These tests drive the *real*
uvicorn server (as the app runs in production) over a real websocket
using the ``websockets`` sync client, with a scripted LLM — fully offline.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from websockets.sync.client import connect as ws_connect

from local_agent.bridge.server.server import BridgeServer
from local_agent.core.config import AssistantSettings
from local_agent.llm.client import ModelReply, ToolCall

_WAIT = 10.0


class _ScriptedLLM:
    """Synchronous fake LLM (like the one in test_bridge)."""

    def __init__(self, replies: list[ModelReply], *, block: float = 0.0) -> None:
        self._replies = list(replies)
        self._block = block
        self.provider_name = "scripted"
        self.model_name = "test"

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        if self._block:
            time.sleep(self._block)
        return self._replies.pop(0) if self._replies else ModelReply(content="done")

    def list_models(self) -> list[str]:
        return ["test"]


@pytest.fixture
def ws_app(tmp_path: Path):
    """A real uvicorn web server with an in-process Bridge (like web_server)."""
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

    def _patch(scripted: _ScriptedLLM) -> None:
        bridge_handlers.create_client = lambda settings: scripted  # type: ignore[assignment]

    _patch(_ScriptedLLM([ModelReply(content="سلام")]))

    yield {
        "url": f"ws://127.0.0.1:{server.port}/ws",
        "http": f"http://127.0.0.1:{server.port}",
        "settings": settings,
        "handlers": bridge.handlers,
        "patch": _patch,
    }
    server.stop()


def _recv_event(ws, want: str, timeout: float = _WAIT) -> dict[str, Any]:
    """Read websocket frames until we see ``want`` as an event_type."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        msg = json.loads(ws.recv(timeout=remaining))
        if msg.get("type") == "event" and msg.get("event_type") == want:
            return msg
    raise AssertionError(f"never saw event {want!r}")


def test_confirm_is_read_mid_run_and_action_runs_fast(ws_app) -> None:
    """chat → tool_confirm_requested → confirm on the SAME ws → tool_result < 2s."""
    ws_app["patch"](_ScriptedLLM([
        ModelReply(
            content="خواهم حذف",
            tool_calls=(ToolCall(name="delete_path", arguments={"path": "x.txt"}),),
        ),
        ModelReply(content="انجام شد"),
    ]))
    (ws_app["settings"].work_dir / "x.txt").write_text("bye", encoding="utf-8")

    with ws_connect(ws_app["url"]) as ws:
        ws.send(json.dumps({"type": "chat", "message": "حذفش کن"}))
        confirm = _recv_event(ws, "tool_confirm_requested")
        request_id = confirm["payload"]["request_id"]

        started = time.time()
        ws.send(json.dumps({
            "type": "confirm", "request_id": request_id, "approved": True,
        }))
        # The approval card must also be closed via TOOL_CONFIRM_RESOLVED
        # (published right after the decision, before the action runs).
        _recv_event(ws, "tool_confirm_resolved")
        result = _recv_event(ws, "tool_result")
        elapsed = time.time() - started
        assert result["payload"]["success"] is True
        assert not (ws_app["settings"].work_dir / "x.txt").exists()
        assert elapsed < 2.0, f"confirm took {elapsed:.2f}s to resolve — B1 not fixed"

        _recv_event(ws, "chat_done")


def test_interrupt_stops_a_long_run(ws_app) -> None:
    """interrupt mid-run → chat_failed with reason=interrupted."""
    # A long run of many safe tool turns, each paced so the run stays alive
    # long enough for the interrupt to land at a turn boundary (the check
    # that publishes chat_failed=interrupted).
    ws_app["patch"](_ScriptedLLM(
        [ModelReply(content="مهم", tool_calls=(ToolCall(name="system_info", arguments={}),))] * 200,
        block=0.05,
    ))
    with ws_connect(ws_app["url"]) as ws:
        ws.send(json.dumps({"type": "chat", "message": "کار طولانی"}))
        started = _recv_event(ws, "chat_started")
        run_id = started["run_id"]
        _recv_event(ws, "tool_result")  # first turn finished
        ws.send(json.dumps({"type": "interrupt", "run_id": run_id}))
        failed = _recv_event(ws, "chat_failed", timeout=15.0)
        assert failed["payload"].get("reason") == "interrupted"


def test_closing_client_mid_run_does_not_crash_handler(ws_app, caplog) -> None:
    """A disconnect while a run is blocked must not log ERROR / crash."""
    ws_app["patch"](_ScriptedLLM(
        [ModelReply(content="خواب")], block=3.0,
    ))
    ws = ws_connect(ws_app["url"])
    ws.send(json.dumps({"type": "chat", "message": "شروع"}))
    time.sleep(0.3)  # let the run start
    with caplog.at_level(logging.ERROR, logger="web"):
        ws.close()
        time.sleep(0.5)
    assert "websocket handler crashed" not in caplog.text


def test_http_confirm_endpoint_resolves(ws_app) -> None:
    """POST /api/confirm resolves a pending confirmation without the WS."""
    import requests

    ws_app["patch"](_ScriptedLLM([
        ModelReply(
            content="خواهم حذف",
            tool_calls=(ToolCall(name="delete_path", arguments={"path": "y.txt"}),),
        ),
        ModelReply(content="انجام شد"),
    ]))
    (ws_app["settings"].work_dir / "y.txt").write_text("bye", encoding="utf-8")
    with ws_connect(ws_app["url"]) as ws:
        ws.send(json.dumps({"type": "chat", "message": "حذف کن"}))
        confirm = _recv_event(ws, "tool_confirm_requested")
        request_id = confirm["payload"]["request_id"]

        r = requests.post(
            ws_app["http"] + "/api/confirm",
            json={"request_id": request_id, "approved": True},
            timeout=5,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        result = _recv_event(ws, "tool_result")
        assert result["payload"]["success"] is True


def test_http_confirm_unknown_request_returns_false(ws_app) -> None:
    import requests

    r = requests.post(
        ws_app["http"] + "/api/confirm",
        json={"request_id": "nope", "approved": True},
        timeout=5,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False
