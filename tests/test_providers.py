from typing import Any

from agent.providers import OpenAICompatibleClient, ProviderError, clean_base_url


class FakeResponse:
    def __init__(
        self, payload: dict[str, Any], status_code: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            error = requests.HTTPError("bad request")
            error.response = self  # type: ignore[assignment]
            raise error

    def json(self) -> dict[str, Any]:
        return self.payload


def test_clean_base_url_repairs_markdown_mangled_url() -> None:
    assert clean_base_url("https://[api.avalai.ir](http://api.avalai.ir)/v1") == "https://api.avalai.ir/v1"
    assert clean_base_url("[https://api.avalai.ir/v1](https://api.avalai.ir/v1)") == "https://api.avalai.ir/v1"
    assert clean_base_url("api.avalai.ir/v1") == "https://api.avalai.ir/v1"


def test_openai_compatible_client_parses_native_function_call(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "list_files",
                                        "arguments": '{"path":".","depth":2}',
                                    }
                                }
                            ],
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("agent.providers.requests.post", fake_post)
    client = OpenAICompatibleClient("avalai", "https://example.test/v1", "never-log-me", "model", 10)
    reply = client.complete([{"role": "user", "content": "سلام"}], [{"type": "function"}])

    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer never-log-me"
    assert reply.content == ""
    assert reply.tool_calls[0].name == "list_files"
    assert reply.tool_calls[0].arguments == {"path": ".", "depth": 2}


def test_avalai_default_service_tier_is_sent(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        captured.update(kwargs["json"])
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("agent.providers.requests.post", fake_post)
    client = OpenAICompatibleClient(
        "avalai", "https://example.test/v1", "key", "model", 10, service_tier="default"
    )

    client.complete([{"role": "user", "content": "hi"}], [])

    assert captured["service_tier"] == "default"


def test_rate_limit_retries_then_succeeds(monkeypatch) -> None:
    calls = 0

    def fake_sleep(seconds: float) -> None:
        return None

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse({}, status_code=429, headers={"Retry-After": "1"})
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("agent.providers.time.sleep", fake_sleep)
    monkeypatch.setattr("agent.providers.random.uniform", lambda a, b: 0)
    monkeypatch.setattr("agent.providers.requests.post", fake_post)
    client = OpenAICompatibleClient("avalai", "https://example.test/v1", "key", "model", 10)

    reply = client.complete([{"role": "user", "content": "hi"}], [])

    assert calls == 2
    assert reply.content == "ok"


def test_avalai_flex_falls_back_to_default(monkeypatch) -> None:
    tiers: list[str] = []

    def fake_sleep(seconds: float) -> None:
        return None

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        tier = kwargs["json"].get("service_tier", "")
        tiers.append(tier)
        if tier == "flex":
            return FakeResponse({}, status_code=503)
        return FakeResponse({"choices": [{"message": {"content": "default ok"}}]})

    monkeypatch.setattr("agent.providers.time.sleep", fake_sleep)
    monkeypatch.setattr("agent.providers.random.uniform", lambda a, b: 0)
    monkeypatch.setattr("agent.providers.requests.post", fake_post)
    client = OpenAICompatibleClient(
        "avalai", "https://example.test/v1", "key", "model", 10, service_tier="flex", max_retries=0
    )

    reply = client.complete([{"role": "user", "content": "hi"}], [])

    assert tiers == ["flex", "default"]
    assert reply.content == "default ok"


def test_rate_limit_failure_message_is_actionable(monkeypatch) -> None:
    def fake_sleep(seconds: float) -> None:
        return None

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(
            {},
            status_code=429,
            headers={"Retry-After": "30", "x-ratelimit-remaining-requests": "0"},
        )

    monkeypatch.setattr("agent.providers.time.sleep", fake_sleep)
    monkeypatch.setattr("agent.providers.random.uniform", lambda a, b: 0)
    monkeypatch.setattr("agent.providers.requests.post", fake_post)
    client = OpenAICompatibleClient("avalai", "https://example.test/v1", "key", "model", 10, max_retries=0)

    try:
        client.complete([{"role": "user", "content": "hi"}], [])
    except ProviderError as exc:
        message = str(exc)
    else:
        raise AssertionError("ProviderError was not raised")

    assert "محدودیت نرخ" in message
    assert "Retry-After" in message
    assert "30" in message


def test_text_only_model_retries_without_tools(monkeypatch) -> None:
    payloads: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        payloads.append(kwargs["json"].copy())
        if len(payloads) == 1:
            return FakeResponse({}, status_code=400)
        return FakeResponse({"choices": [{"message": {"content": "{" + '"tool":"list_files","args":{}' + "}"}}]})

    monkeypatch.setattr("agent.providers.requests.post", fake_post)
    client = OpenAICompatibleClient("gapgpt", "https://example.test/v1", "key", "model", 10)
    reply = client.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])

    assert len(payloads) == 2
    assert "tools" in payloads[0]
    assert "tools" not in payloads[1]
    assert reply.content.startswith('{"tool"')
