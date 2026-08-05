"""Pytest configuration for the local assistant tests."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
import requests


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(server, *, timeout: float = 5.0) -> bool:
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
def web_server(tmp_path: Path):
    """A real FastAPI web server with an in-process Bridge."""
    from local_agent.bridge.server.server import BridgeServer
    from local_agent.core.config import AssistantSettings
    from local_agent.web.app import WebServer

    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    bridge = BridgeServer(settings)
    bridge.start_in_process()
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


@pytest.fixture(autouse=True)
def _restore_llm_factory():
    """Undo global monkeypatching of the Bridge's LLM factory.

    Several tests swap ``bridge.api.handlers.create_client`` for a
    scripted fake by assigning to the module attribute directly.  Without
    this fixture the fake leaks into every test that runs afterwards,
    which makes the suite order-dependent.
    """
    from local_agent.bridge.api import handlers as bridge_handlers

    original = bridge_handlers.create_client
    yield
    bridge_handlers.create_client = original


@pytest.fixture(autouse=True)
def _isolate_agent_env(monkeypatch):
    """Keep LOCAL_AGENT_* out of tests that do not set it themselves.

    ``load_settings`` layers every ``LOCAL_AGENT_*`` variable on top of
    the config file, so one test leaking a variable silently rewrites
    the configuration of every test after it.  Tests that need a
    specific value still set it with ``monkeypatch.setenv``.
    """
    import os

    for key in [k for k in os.environ if k.startswith("LOCAL_AGENT_")]:
        monkeypatch.delenv(key, raising=False)
