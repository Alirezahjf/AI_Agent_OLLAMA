"""Regression tests for the Persian mojibake in *streamed* assistant replies.

Root cause
----------

``requests.Response.iter_lines(decode_unicode=True)`` decodes the body with
the ``charset`` from the ``Content-Type`` header, and — when the header has
no charset (very common for SSE served by AvalAI / GapGPT / Gemini-style
gateways) — falls back to **ISO-8859-1**.  UTF-8 Persian text streamed by the
model then surfaced in the UI and in ``history.jsonl`` as mojibake::

    "لیست فایل‌ها"  →  "Ù\x84Û\x8cØ³Øª Ù\x81Ø§Û\x8cÙ\x84â\x80\x8cÙ\x87Ø§"

The fix (``local_agent/llm/client.py::_iter_stream_lines``) requests the raw
byte lines and decodes them explicitly as UTF-8 with ``errors="replace"``.

Everything here runs offline: the fakes replay raw UTF-8 bytes exactly the
way a charset-less SSE/NDJSON server would, including ``requests``' Latin-1
fallback behaviour for ``decode_unicode=True`` (test 0 documents the bug so
the other tests are meaningful).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from local_agent.core.config import LLMSettings
from local_agent.llm.client import (
    OllamaClient,
    OpenAICompatibleClient,
    _iter_stream_lines,
)

PERSIAN_REPLY = "دارای این‌ها هستم: لیست فایل‌ها را برگرداندم ✅"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _CharsetLessStream:
    """Stand-in for a streamed ``requests`` response **without** a charset.

    Real ``requests`` decodes ``iter_lines(decode_unicode=True)`` as
    ISO-8859-1 for ``text/*`` responses lacking an explicit charset.  The
    fake mirrors that so the suite proves the client never depends on it.
    """

    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        content_type: str = "text/event-stream",
    ) -> None:
        # iter_lines splits on \n / \r\n; keeping \r would not matter here.
        self._lines = body.split(b"\n")
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        # Exactly what requests infers for "text/event-stream" w/o charset.
        self.encoding = "ISO-8859-1"
        self.text = body.decode("utf-8", errors="replace")

    def iter_lines(self, decode_unicode: bool = False):
        for line in self._lines:
            if decode_unicode:
                # The historical bug: Latin-1 decoding of UTF-8 bytes.
                yield line.decode(self.encoding or "ISO-8859-1", errors="replace")
            else:
                yield line

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return {}


def _sse_lines(*deltas: str) -> bytes:
    """Build a charset-less UTF-8 SSE body containing ``data:`` lines."""
    lines: list[str] = []
    for delta in deltas:
        payload = {"choices": [{"delta": {"content": delta}}]}
        lines.append("data: " + json.dumps(payload, ensure_ascii=False))
    lines.append("data: [DONE]")
    return "\n".join(lines).encode("utf-8")


def _ndjson_lines(*chunks: str) -> bytes:
    """Build an Ollama-style NDJSON body with Persian content."""
    lines = [
        json.dumps({"message": {"content": chunk}, "done": False}, ensure_ascii=False)
        for chunk in chunks
    ]
    lines.append(json.dumps({"message": {"content": ""}, "done": True}))
    return "\n".join(lines).encode("utf-8")


def _openai_client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        LLMSettings(
            provider="openai_compatible",
            openai_base_url="https://gateway.test/v1",
            openai_api_key="key",
        )
    )


def _ollama_client() -> OllamaClient:
    return OllamaClient(LLMSettings(provider="ollama"))


# ---------------------------------------------------------------------------
# 0) Document the bug: the fake (like real requests) garbles on Latin-1
# ---------------------------------------------------------------------------


def test_fakes_reproduce_the_requests_latin1_corner() -> None:
    stream = _CharsetLessStream(_sse_lines("لیست فایل‌ها"))
    # What the old code did — and why the user saw mojibake:
    old_style = list(stream.iter_lines(decode_unicode=True))
    assert any("Ù" in line for line in old_style), "fake must reproduce the bug"
    # What the fixed helper yields from the very same bytes:
    fixed = list(_iter_stream_lines(stream))
    assert any("فایل" in line for line in fixed)


# ---------------------------------------------------------------------------
# 1) OpenAI-compatible SSE
# ---------------------------------------------------------------------------


def test_openai_complete_streaming_persian_survives_charsetless_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _sse_lines(PERSIAN_REPLY[:20], PERSIAN_REPLY[20:])
    monkeypatch.setattr(
        "local_agent.llm.client.requests.post",
        lambda *a, **k: _CharsetLessStream(body),
    )
    seen: list[str] = []
    reply = _openai_client().complete_streaming([], [], seen.append)
    assert reply.content == PERSIAN_REPLY
    assert "".join(seen) == PERSIAN_REPLY
    assert "Ø" not in reply.content and "�" not in reply.content


def test_openai_stream_complete_persian_yields_clean_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = [PERSIAN_REPLY[:10], PERSIAN_REPLY[10:35], PERSIAN_REPLY[35:]]
    body = _sse_lines(*parts)
    monkeypatch.setattr(
        "local_agent.llm.client.requests.post",
        lambda *a, **k: _CharsetLessStream(body),
    )
    events = list(_openai_client().stream_complete([], []))
    deltas = [text for kind, text in events if kind == "assistant_delta"]
    assert deltas == parts
    assert events[-1] == ("done", "")


# ---------------------------------------------------------------------------
# 2) Ollama NDJSON
# ---------------------------------------------------------------------------


def test_ollama_complete_streaming_persian_survives_charsetless_ndjson(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _ndjson_lines(PERSIAN_REPLY[:15], PERSIAN_REPLY[15:])
    client = _ollama_client()
    monkeypatch.setattr(client._session, "post", lambda *a, **k: _CharsetLessStream(body))
    seen: list[str] = []
    reply = client.complete_streaming([], [], seen.append)
    assert reply.content == PERSIAN_REPLY
    assert "".join(seen) == PERSIAN_REPLY
    assert "Ø" not in reply.content


def test_ollama_stream_complete_persian_yields_clean_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = [PERSIAN_REPLY[:12], PERSIAN_REPLY[12:]]
    body = _ndjson_lines(*parts)
    client = _ollama_client()
    monkeypatch.setattr(client._session, "post", lambda *a, **k: _CharsetLessStream(body))
    events = list(client.stream_complete([], []))
    deltas = [text for kind, text in events if kind == "assistant_delta"]
    assert "".join(deltas) == PERSIAN_REPLY


# ---------------------------------------------------------------------------
# 3) Robustness: one corrupt chunk must not kill the stream
# ---------------------------------------------------------------------------


def test_corrupt_chunk_is_replaced_but_stream_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = "data: " + json.dumps(
        {"choices": [{"delta": {"content": "متن سالم"}}]}, ensure_ascii=False
    )
    body = b"\xff\xfe binary junk\n" + good.encode("utf-8") + b"\ndata: [DONE]"
    monkeypatch.setattr(
        "local_agent.llm.client.requests.post",
        lambda *a, **k: _CharsetLessStream(body),
    )
    reply = _openai_client().complete_streaming([], [], None)
    assert "متن سالم" in reply.content


# ---------------------------------------------------------------------------
# 4) Non-streaming path must not regress
# ---------------------------------------------------------------------------


def test_non_streaming_complete_still_decodes_persian(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _JsonResponse:
        status_code = 200
        headers: ClassVar = {"Content-Type": "application/json"}
        text = ""

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": PERSIAN_REPLY}}]}

    monkeypatch.setattr(
        "local_agent.llm.client.requests.post", lambda *a, **k: _JsonResponse()
    )
    reply = _openai_client().complete([], [])
    assert reply.content == PERSIAN_REPLY
