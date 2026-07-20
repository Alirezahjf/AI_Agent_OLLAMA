from typing import Any

from agent.providers import OpenAICompatibleClient


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            error = requests.HTTPError("bad request")
            error.response = self  # type: ignore[assignment]
            raise error

    def json(self) -> dict[str, Any]:
        return self.payload


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
