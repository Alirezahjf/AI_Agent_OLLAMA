"""Tests for the LLM client and provider routing."""

from __future__ import annotations

import json
from typing import Any

import pytest

from local_agent.core.config import LLMSettings
from local_agent.core.errors import ConfigError
from local_agent.llm import create_client
from local_agent.llm.client import (
    OpenAICompatibleClient,
    ToolDefinition,
    _clean_base_url,
    _content_to_text,
    _extract_tool_calls,
)
from local_agent.llm.errors import LLMRateLimit, LLMTimeout


def test_clean_base_url_repairs_markdown() -> None:
    assert _clean_base_url("https://[api.avalai.ir](http://api.avalai.ir)/v1") == "https://api.avalai.ir/v1"
    assert _clean_base_url("api.avalai.ir/v1") == "https://api.avalai.ir/v1"
    assert _clean_base_url("  https://x.test/v1  ") == "https://x.test/v1"


def test_clean_base_url_requires_nonempty() -> None:
    with pytest.raises(ConfigError):
        _clean_base_url("")


def test_content_to_text_handles_string_and_list() -> None:
    assert _content_to_text("hello") == "hello"
    assert _content_to_text([{"text": "a"}, {"text": "b"}]) == "a\nb"
    assert _content_to_text(None) == ""


def test_extract_tool_calls_parses_native_calls() -> None:
    raw = [
        {
            "function": {
                "name": "open_app",
                "arguments": json.dumps({"name": "chrome"}),
            }
        }
    ]
    calls = list(_extract_tool_calls(raw))
    assert len(calls) == 1
    assert calls[0].name == "open_app"
    assert calls[0].arguments == {"name": "chrome"}


def test_extract_tool_calls_skips_malformed() -> None:
    raw = [{"function": {"name": "x", "arguments": "not json"}}, {"junk": True}]
    assert list(_extract_tool_calls(raw)) == []


def test_tool_definition_to_openai_shape() -> None:
    definition = ToolDefinition(
        name="echo",
        description="returns text",
        parameters={"text": {"type": "string"}},
        required=("text",),
    )
    rendered = definition.to_openai()
    assert rendered["type"] == "function"
    assert rendered["function"]["name"] == "echo"
    assert rendered["function"]["parameters"]["additionalProperties"] is False
    assert rendered["function"]["parameters"]["required"] == ["text"]


def test_create_client_routes_to_ollama() -> None:
    client = create_client(LLMSettings(provider="ollama"))
    assert client.provider_name == "ollama"


def test_create_client_routes_to_openai_compatible() -> None:
    client = create_client(
        LLMSettings(
            provider="openai_compatible",
            openai_base_url="https://example.test/v1",
            openai_api_key="key",
        )
    )
    assert client.provider_name == "openai_compatible"


def test_create_client_auto_picks_ollama_without_key() -> None:
    client = create_client(LLMSettings(provider="auto"))
    assert client.provider_name == "ollama"


def test_create_client_auto_picks_openai_when_key() -> None:
    client = create_client(
        LLMSettings(
            provider="auto",
            openai_base_url="https://x.test/v1",
            openai_api_key="key",
        )
    )
    assert client.provider_name == "openai_compatible"


def test_create_client_rejects_unknown() -> None:
    with pytest.raises(ConfigError):
        create_client(LLMSettings(provider="unknown"))


def test_openai_client_requires_credentials() -> None:
    with pytest.raises(ConfigError):
        OpenAICompatibleClient(LLMSettings(provider="openai_compatible"))


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self.payload = payload or {"choices": [{"message": {"content": "ok"}}]}
        self.status_code = status_code
        self.text = text
        self.headers = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self  # type: ignore[attr-defined]
            raise err

    def json(self) -> dict[str, Any]:
        return self.payload


def test_openai_client_native_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["json"] = kwargs.get("json", {})
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "open_app",
                                        "arguments": json.dumps({"name": "chrome"}),
                                    }
                                }
                            ],
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("local_agent.llm.client.requests.post", fake_post)
    client = OpenAICompatibleClient(
        LLMSettings(
            provider="openai_compatible",
            openai_base_url="https://x.test/v1",
            openai_api_key="key",
        )
    )
    reply = client.complete([{"role": "user", "content": "hi"}], [])
    assert captured["url"] == "https://x.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer key"
    assert reply.tool_calls[0].name == "open_app"


def test_openai_client_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_post(*args: Any, **kwargs: Any) -> _FakeResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(status_code=429, text="rate limit")
        return _FakeResponse()

    monkeypatch.setattr("local_agent.llm.client.time.sleep", lambda *_: None)
    monkeypatch.setattr("local_agent.llm.client.requests.post", fake_post)
    client = OpenAICompatibleClient(
        LLMSettings(
            provider="openai_compatible",
            openai_base_url="https://x.test/v1",
            openai_api_key="key",
            max_retries=2,
        )
    )
    reply = client.complete([{"role": "user", "content": "hi"}], [])
    assert calls["n"] == 2
    assert reply.content == "ok"


def test_openai_client_falls_back_without_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_post(*args: Any, **kwargs: Any) -> _FakeResponse:
        body = kwargs.get("json", {})
        seen.append(body)
        if "tools" in body:
            return _FakeResponse(status_code=400, text="bad")
        return _FakeResponse()

    monkeypatch.setattr("local_agent.llm.client.requests.post", fake_post)
    client = OpenAICompatibleClient(
        LLMSettings(
            provider="openai_compatible",
            openai_base_url="https://x.test/v1",
            openai_api_key="key",
            max_retries=0,
        )
    )
    reply = client.complete(
        [{"role": "user", "content": "hi"}],
        [ToolDefinition(name="x", description="x", parameters={})],
    )
    assert "tools" in seen[0]
    assert "tools" not in seen[1]
    assert reply.content == "ok"


def test_openai_client_raises_rate_limit_after_exhausting(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(status_code=429, text="rate limit")

    monkeypatch.setattr("local_agent.llm.client.time.sleep", lambda *_: None)
    monkeypatch.setattr("local_agent.llm.client.requests.post", fake_post)
    client = OpenAICompatibleClient(
        LLMSettings(
            provider="openai_compatible",
            openai_base_url="https://x.test/v1",
            openai_api_key="key",
            max_retries=1,
        )
    )
    with pytest.raises(LLMRateLimit):
        client.complete([{"role": "user", "content": "hi"}], [])


def test_openai_client_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def fake_post(*args: Any, **kwargs: Any) -> _FakeResponse:
        raise requests.Timeout("slow")

    monkeypatch.setattr("local_agent.llm.client.time.sleep", lambda *_: None)
    monkeypatch.setattr("local_agent.llm.client.requests.post", fake_post)
    client = OpenAICompatibleClient(
        LLMSettings(
            provider="openai_compatible",
            openai_base_url="https://x.test/v1",
            openai_api_key="key",
            max_retries=0,
        )
    )
    with pytest.raises(LLMTimeout):
        client.complete([{"role": "user", "content": "hi"}], [])
