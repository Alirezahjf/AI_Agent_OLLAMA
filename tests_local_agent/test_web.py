"""Tests for the Web UI app."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from local_agent.bridge.server.server import BridgeServer
from local_agent.core.config import AssistantSettings
from local_agent.web.app import WebServer, create_app


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(server: WebServer, *, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"http://127.0.0.1:{server.port}/", timeout=1)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.1)
    return False


@pytest.fixture
def web_server(tmp_path: Path) -> WebServer:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    bridge = BridgeServer(settings)
    bridge.start_in_process()
    # Reuse the existing bridge for the web client
    from local_agent.bridge.api.client import BridgeClient, _InProcessBackend, _welcome_to_info
    backend = _InProcessBackend(bridge)
    backend._started = True
    client = BridgeClient(backend, _welcome_to_info(bridge.welcome()))

    server = WebServer(settings, client, host="127.0.0.1", port=_free_port())
    server.start_in_thread()
    if not _wait_for_server(server):
        server.stop()
        pytest.fail("web server did not start")
    yield server
    server.stop()


def test_root_endpoint_returns_html(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/", timeout=3)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_api_status(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/api/status", timeout=3)
    assert r.status_code == 200
    body = r.json()
    assert "bridge" in body
    assert "settings" in body


def test_api_actions(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/api/actions", timeout=3)
    assert r.status_code == 200
    actions = r.json()
    assert any(d.startswith("open_application") for d in actions)


def test_api_invoke(web_server: WebServer, tmp_path: Path) -> None:
    (tmp_path / "web-test.txt").write_text("from web", encoding="utf-8")
    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/invoke",
        json={"name": "read_file", "arguments": {"path": "web-test.txt"}, "auto_confirm": True},
        timeout=5,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert "from web" in body["text"]


def test_api_clear(web_server: WebServer) -> None:
    r = requests.post(f"http://127.0.0.1:{web_server.port}/api/clear", timeout=3)
    assert r.status_code == 200
    body = r.json()
    assert body["cleared"] is True
    # History should be empty now
    r2 = requests.get(f"http://127.0.0.1:{web_server.port}/api/history", timeout=3)
    assert r2.json() == []


def test_static_files_served(web_server: WebServer) -> None:
    r = requests.get(f"http://127.0.0.1:{web_server.port}/static/app.js", timeout=3)
    # Static may or may not exist depending on installation; we just verify
    # that the route doesn't 500
    assert r.status_code in {200, 404}
