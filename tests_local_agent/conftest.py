"""Pytest configuration for the local assistant tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest


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
