"""Regression tests for the Windows Persian mojibake fix.

On Windows, Python may emit/read text with a legacy codepage (CP1252 /
CP720), so valid UTF-8 Persian shows up as ``Ø³Ù„Ø§Ù…`` or ``???``.  The
fix (``local_agent/utils/encoding.py::ensure_utf8_stdio`` plus
``encoding="utf-8"`` on subprocess calls) is verified here at three
layers: console stdout, ``history.jsonl`` persistence, and subprocess
output decoding.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from local_agent.utils.encoding import ensure_utf8_stdio

PERSIAN = "سلام دنیا — دستیار محلی ویندوز ۱۲۳۴۵۶"


# ---------------------------------------------------------------------------
# Layer 1: stdout with a legacy (Windows-like) encoding
# ---------------------------------------------------------------------------
def test_stdout_reconfigured_to_utf8_by_fix(monkeypatch) -> None:
    # Simulate a hostile Windows console that only knows cp1252.
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="replace")
    monkeypatch.setattr(sys, "stdout", stream)
    ensure_utf8_stdio()
    sys.stdout.write(PERSIAN)
    sys.stdout.flush()
    raw = stream.buffer.getvalue()
    # The fix reconfigures the stream to UTF-8, so the bytes must decode as UTF-8.
    assert raw.decode("utf-8", errors="replace") == PERSIAN
    assert "�" not in raw.decode("utf-8", errors="replace")


def test_stdout_without_fix_garbles_legacy_encoding() -> None:
    # Demonstrate the original bug so the regression test is meaningful.
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="replace")
    old = sys.stdout
    sys.stdout = stream
    try:
        sys.stdout.write(PERSIAN)
        sys.stdout.flush()
        raw = stream.buffer.getvalue()
    finally:
        sys.stdout = old
    garbled = raw.decode("cp1252", errors="replace")
    assert garbled != PERSIAN  # Persian is destroyed on a legacy console


# ---------------------------------------------------------------------------
# Layer 2: history.jsonl persistence
# ---------------------------------------------------------------------------
def test_history_jsonl_stays_utf8(tmp_path: Path) -> None:
    from local_agent.core.config import AssistantSettings
    from local_agent.core.context import ConversationMessage, RuntimeContext

    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    ctx = RuntimeContext(settings)
    ctx.append(ConversationMessage(role="user", content=PERSIAN))
    ctx.append(ConversationMessage(role="assistant", content="پاسخ سالم فارسی ✅"))

    raw = (tmp_path / "history.jsonl").read_bytes()
    decoded = raw.decode("utf-8")  # must not raise / must not contain mojibake
    lines = [json.loads(l) for l in decoded.splitlines() if l.strip()]
    assert lines[0]["content"] == PERSIAN
    assert "پاسخ سالم" in lines[1]["content"]
    assert "Ø" not in decoded and "�" not in decoded


# ---------------------------------------------------------------------------
# Layer 3: subprocess output decoding
# ---------------------------------------------------------------------------
def test_subprocess_decoded_as_utf8() -> None:
    code = f"import sys; sys.stdout.write({PERSIAN!r}); sys.stdout.flush()"
    good = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert good.stdout == PERSIAN


def test_subprocess_legacy_decoding_garble() -> None:
    code = f"import sys; sys.stdout.write({PERSIAN!r}); sys.stdout.flush()"
    bad = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="cp1252", errors="replace",
    )
    # This is the mojibake pattern seen on Windows without the fix.
    assert bad.stdout != PERSIAN
    assert "Ø" in bad.stdout or "?" in bad.stdout
