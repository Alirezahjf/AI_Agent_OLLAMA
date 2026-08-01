"""Tests for the robust encoding helpers (fixes mojibake on Persian Windows)."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from local_agent.utils.encoding import (
    TEXT_IO,
    decode_output,
    looks_like_mojibake,
    repair_mojibake,
)


def test_text_io_flag_is_correct():
    assert TEXT_IO == {"text": False, "encoding": None}


def test_decode_output_accepts_str_none_and_empty():
    assert decode_output(None) == ""
    assert decode_output("") == ""
    assert decode_output("already a string") == "already a string"


def test_decode_output_utf8():
    data = "فهرست پوشه کاری".encode("utf-8")
    assert decode_output(data) == "فهرست پوشه کاری"


def test_decode_output_utf8_with_bom():
    data = b"\xef\xbb\xbfسلام"
    assert decode_output(data) == "سلام"


def test_decode_output_cp1256_persian():
    # Simulate Persian Windows (cp1256)
    persian = "فهرست"
    data = persian.encode("cp1256")
    # On a non-Persian system this will still decode correctly via the chain
    result = decode_output(data)
    assert result == persian or "فهرست" in result  # tolerant


def test_decode_output_invalid_bytes_never_raises():
    bad = b"good \xff\xfe bad"
    result = decode_output(bad)
    assert isinstance(result, str)
    assert "good" in result and "bad" in result


def test_looks_like_mojibake_detects_common_case():
    mojibake = "ÙÙØ±Ø³Øª"  # typical mojibake for "فهرست"
    assert looks_like_mojibake(mojibake) is True


def test_looks_like_mojibake_false_positive_on_clean_persian():
    clean = "فهرست پوشه کاری"
    assert looks_like_mojibake(clean) is False


def test_looks_like_mojibake_false_positive_on_english():
    english = "hello world this is a test"
    assert looks_like_mojibake(english) is False


def test_repair_mojibake_roundtrip():
    original = "فهرست"
    mojibake = original.encode("utf-8").decode("latin-1")
    repaired = repair_mojibake(mojibake)
    assert repaired == original or "فهرست" in repaired


def test_run_shell_end_to_end_persian(monkeypatch):
    """Real subprocess test with Persian text (skipped on non-Linux)."""
    if sys.platform == "win32":
        pytest.skip("real bash not available on Windows CI")

    from local_agent.actions.system import run_shell
    from local_agent.core.config import AssistantSettings, LLMSettings
    from local_agent.core.context import RuntimeContext
    from local_agent.actions.registry import ActionContext, ConfirmationGate

    settings = AssistantSettings(
        data_dir="/tmp",
        work_dir="/tmp",
        llm=LLMSettings(provider="openai_compatible", openai_base_url="http://127.0.0.1:1/v1", openai_api_key="sk-test"),
    )
    runtime = RuntimeContext(settings)
    context = ActionContext(
        runtime=runtime,
        confirmation_gate=ConfirmationGate(settings.safety),
        work_dir=settings.work_dir,
    )

    result = run_shell(command="echo 'فهرست پوشه کاری'", context=context)
    assert "فهرست پوشه کاری" in result or "فهرست" in result


def test_run_shell_invalid_bytes_does_not_crash(monkeypatch):
    """Ensure decode_output is used and never raises even with bad bytes."""
    if sys.platform == "win32":
        pytest.skip("printf behavior differs on Windows")

    from local_agent.actions.system import run_shell
    from local_agent.core.config import AssistantSettings, LLMSettings
    from local_agent.core.context import RuntimeContext
    from local_agent.actions.registry import ActionContext, ConfirmationGate

    settings = AssistantSettings(
        data_dir="/tmp",
        work_dir="/tmp",
        llm=LLMSettings(provider="openai_compatible", openai_base_url="http://127.0.0.1:1/v1", openai_api_key="sk-test"),
    )
    runtime = RuntimeContext(settings)
    context = ActionContext(
        runtime=runtime,
        confirmation_gate=ConfirmationGate(settings.safety),
        work_dir=settings.work_dir,
    )

    # printf with invalid bytes
    result = run_shell(command="printf 'good \\xff\\xfe bad\\n'", context=context)
    assert "good" in result and "bad" in result
    assert isinstance(result, str)
