"""Tests for the HTTP + SSE server."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from local_agent.bridge.protocol import PROTOCOL_VERSION
from local_agent.bridge.server.server import BridgeServer
from local_agent.core.config import AssistantSettings


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(server: BridgeServer, *, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.is_running():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def http_server(tmp_path: Path) -> BridgeServer:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    server = BridgeServer(settings)
    # Force a known port
    from local_agent.bridge.server.server import ServerConfig
    server.config = ServerConfig(host="127.0.0.1", port=_free_port(), token=server.token, allow_remote=False)
    server.start_in_thread()
    assert _wait_for_server(server)
    yield server
    server.stop()


def _base(server: BridgeServer) -> str:
    return f"http://127.0.0.1:{server.actual_port}"


def _auth_headers(server: BridgeServer) -> dict[str, str]:
    return {"Authorization": f"Bearer {server.token}"}


def test_health_endpoint(http_server: BridgeServer) -> None:
    response = requests.get(f"{_base(http_server)}/health", timeout=3)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["protocol_version"] == PROTOCOL_VERSION


def test_unauthorized_request_is_rejected(http_server: BridgeServer) -> None:
    response = requests.get(f"{_base(http_server)}/welcome", timeout=3)
    assert response.status_code == 401


def test_welcome_endpoint(http_server: BridgeServer) -> None:
    response = requests.get(
        f"{_base(http_server)}/welcome", headers=_auth_headers(http_server), timeout=3
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "result" in body
    assert body["result"]["protocol_version"] == PROTOCOL_VERSION


def test_rpc_list_actions(http_server: BridgeServer) -> None:
    response = requests.post(
        f"{_base(http_server)}/rpc",
        json={"id": "1", "type": "list_actions", "payload": {}},
        headers=_auth_headers(http_server),
        timeout=5,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert any(d.startswith("open_application") for d in body["result"])


def test_rpc_invoke_action(http_server: BridgeServer, tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
    response = requests.post(
        f"{_base(http_server)}/rpc",
        json={
            "id": "1",
            "type": "invoke_action",
            "payload": {
                "name": "read_file",
                "arguments": {"path": "hello.txt"},
                "auto_confirm": True,
            },
        },
        headers=_auth_headers(http_server),
        timeout=5,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "hi" in body["result"]["text"]


def test_rpc_unknown_type(http_server: BridgeServer) -> None:
    response = requests.post(
        f"{_base(http_server)}/rpc",
        json={"id": "1", "type": "nope", "payload": {}},
        headers=_auth_headers(http_server),
        timeout=5,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "unknown_type"


def test_stream_endpoint_emits_events(http_server: BridgeServer, tmp_path: Path) -> None:
    """Drive a chat run via the SSE endpoint and collect events."""
    from local_agent.llm.client import ModelReply, ToolCall
    from local_agent.bridge import api as bridge_api

    # Replace the LLM factory so the chat loop uses our scripted client
    class _ScriptedLLM:
        provider_name = "scripted"
        model_name = "test"
        def __init__(self) -> None:
            self._replies = [
                ModelReply(content="ok", tool_calls=()),
            ]
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            return self._replies.pop(0) if self._replies else ModelReply(content="done")
        def list_models(self):
            return ["test"]

    bridge_api.handlers.create_client = lambda settings: _ScriptedLLM()  # type: ignore[assignment]

    response = requests.post(
        f"{_base(http_server)}/stream",
        json={"id": "1", "type": "chat", "payload": {"message": "hello"}},
        headers=_auth_headers(http_server),
        stream=True,
        timeout=15,
    )
    assert response.status_code == 200
    events: list[dict[str, Any]] = []
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[len("data:"):].strip()))
            except ValueError:
                continue
    types = [e.get("event_type") for e in events]
    assert "chat_started" in types
    assert "chat_done" in types or "chat_failed" in types
