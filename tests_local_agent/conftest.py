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
