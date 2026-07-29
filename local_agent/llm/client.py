"""Provider-agnostic chat-completions client.

Two implementations live here:

  * :class:`OllamaClient` — talks to a local Ollama server using the
    ``/api/chat`` endpoint.  Handles streaming-less requests with
    optional native function calling.
  * :class:`OpenAICompatibleClient` — works against any OpenAI-style
    endpoint (AvalAI, GapGPT, OpenAI, vLLM, llama.cpp, etc.) with
    bearer auth, native tool calls, transient retry, and a graceful
    fallback to plain JSON when the server rejects the tools field.

The :func:`create_client` factory picks one based on settings; the
:class:`LLMClient` ABC exposes a uniform ``complete`` method that the
agent loop calls regardless of provider.
"""

from __future__ import annotations

import json
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

from ..core.config import LLMSettings
from ..core.errors import ConfigError
from ..core.logging_setup import get_logger
from .errors import LLMRateLimit, LLMTimeout


logger = get_logger("llm")

_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class ToolDefinition:
    """A function tool the model can call.

    The shape is the OpenAI function-calling schema.  Ollama accepts
    the same shape via its ``tools`` field.
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object
    required: tuple[str, ...] = ()

    def to_openai(self) -> dict[str, Any]:
        properties = dict(self.parameters)
        required = list(self.required)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class LLMClient(ABC):
    """Uniform interface for the agent loop."""

    provider_name: str = "abstract"
    model_name: str = "unknown"

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> ModelReply:
        """Return a model reply given the conversation and available tools."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Return the list of models this provider exposes (best-effort)."""


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


class OllamaClient(LLMClient):
    provider_name = "ollama"

    def __init__(self, settings: LLMSettings) -> None:
        self._base = settings.ollama_base_url.rstrip("/")
        self._model = settings.ollama_model
        self._timeout = settings.timeout_seconds
        self._retries = settings.max_retries
        self._temperature = settings.temperature
        self.model_name = self._model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self._temperature, "num_ctx": 32768},
        }
        if tools:
            payload["tools"] = [tool.to_openai() for tool in tools]

        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = requests.post(
                    f"{self._base}/api/chat",
                    json=payload,
                    timeout=self._timeout,
                )
            except requests.Timeout as exc:
                last_exc = exc
                if attempt >= self._retries:
                    raise LLMTimeout(f"Ollama timed out after {self._timeout}s") from exc
                self._sleep_backoff(attempt)
                continue
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self._retries:
                    raise
                self._sleep_backoff(attempt)
                continue

            # Some local models reject the tools field with 400/422; retry
            # without it. The agent's system prompt already encodes a
            # strict-JSON fallback protocol.
            if response.status_code in {400, 422} and tools:
                payload.pop("tools", None)
                continue

            if response.status_code in _TRANSIENT_STATUS:
                if attempt >= self._retries:
                    raise LLMRateLimit(
                        f"Ollama returned {response.status_code} after {self._retries} retries"
                    )
                self._sleep_backoff(attempt, response)
                continue

            try:
                data = response.json()
            except ValueError as exc:
                last_exc = exc
                if attempt >= self._retries:
                    raise
                self._sleep_backoff(attempt)
                continue

            message = data.get("message") or {}
            content = str(message.get("content") or "").strip()
            tool_calls = tuple(_extract_tool_calls(message.get("tool_calls")))
            return ModelReply(content=content, tool_calls=tool_calls, raw=data)

        if last_exc:
            raise last_exc
        raise RuntimeError("Ollama request failed without an exception")

    def list_models(self) -> list[str]:
        try:
            response = requests.get(f"{self._base}/api/tags", timeout=min(20, self._timeout))
            response.raise_for_status()
            return [str(item["name"]) for item in response.json().get("models", []) if item.get("name")]
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Ollama /api/tags failed: %s", exc)
            return []

    @staticmethod
    def _sleep_backoff(attempt: int, response: requests.Response | None = None) -> None:
        retry_after = 0.0
        if response is not None:
            try:
                retry_after = float(response.headers.get("Retry-After", "0") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
        base = min(15.0, 1.0 * (2**attempt))
        delay = max(retry_after, base)
        time.sleep(delay + random.uniform(0, min(0.5, delay * 0.25)))


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------


class OpenAICompatibleClient(LLMClient):
    provider_name = "openai_compatible"

    def __init__(self, settings: LLMSettings) -> None:
        if not settings.openai_base_url:
            raise ConfigError("openai_base_url is required for openai_compatible provider")
        if not settings.openai_api_key:
            raise ConfigError("openai_api_key is required for openai_compatible provider")
        self._base = _clean_base_url(settings.openai_base_url)
        self._key = settings.openai_api_key
        self._model = settings.openai_model or "gpt-4o-mini"
        self._timeout = settings.timeout_seconds
        self._retries = settings.max_retries
        self._temperature = settings.temperature
        self.model_name = self._model

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> ModelReply:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
        }
        if tools:
            body["tools"] = [tool.to_openai() for tool in tools]
            body["tool_choice"] = "auto"

        try:
            data = self._post_with_retry(body)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in {400, 422} and tools:
                # Server doesn't support tools; strip and retry once.
                fallback = dict(body)
                fallback.pop("tools", None)
                fallback.pop("tool_choice", None)
                data = self._post_with_retry(fallback)
            else:
                raise

        choices = data.get("choices") or []
        if not choices:
            return ModelReply(content="", tool_calls=())
        message = choices[0].get("message") or {}
        content = _content_to_text(message.get("content"))
        tool_calls = tuple(_extract_tool_calls(message.get("tool_calls")))
        return ModelReply(content=content, tool_calls=tool_calls, raw=data)

    def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = requests.post(
                    f"{self._base}/chat/completions",
                    headers=self._headers,
                    json=body,
                    timeout=self._timeout,
                )
            except requests.Timeout as exc:
                last_exc = exc
                if attempt >= self._retries:
                    raise LLMTimeout(f"provider timed out after {self._timeout}s") from exc
                self._sleep_backoff(attempt)
                continue
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self._retries:
                    raise
                self._sleep_backoff(attempt)
                continue

            if response.status_code in _TRANSIENT_STATUS:
                if attempt >= self._retries:
                    raise LLMRateLimit(
                        f"provider returned {response.status_code} after {self._retries} retries"
                    )
                self._sleep_backoff(attempt, response)
                continue

            if response.status_code >= 400:
                err: requests.HTTPError = requests.HTTPError(
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )
                err.response = response  # type: ignore[attr-defined]
                raise err

            try:
                return response.json()
            except ValueError as exc:
                last_exc = exc
                if attempt >= self._retries:
                    raise
                self._sleep_backoff(attempt)

        if last_exc:
            raise last_exc
        raise RuntimeError("provider request failed without an exception")

    def list_models(self) -> list[str]:
        try:
            response = requests.get(
                f"{self._base}/models",
                headers=self._headers,
                timeout=min(20, self._timeout),
            )
            response.raise_for_status()
            payload = response.json()
            return [str(item["id"]) for item in payload.get("data", []) if item.get("id")]
        except (requests.RequestException, ValueError) as exc:
            logger.warning("provider /models failed: %s", exc)
            return []

    @staticmethod
    def _sleep_backoff(attempt: int, response: requests.Response | None = None) -> None:
        retry_after = 0.0
        if response is not None:
            try:
                retry_after = float(response.headers.get("Retry-After", "0") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
        base = min(30.0, 1.5 * (2**attempt))
        delay = max(retry_after, base)
        time.sleep(delay + random.uniform(0, min(1.0, delay * 0.25)))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_client(settings: LLMSettings) -> LLMClient:
    """Pick the right client based on the settings."""
    provider = (settings.provider or "ollama").lower()
    if provider == "ollama":
        return OllamaClient(settings)
    if provider == "openai_compatible":
        return OpenAICompatibleClient(settings)
    if provider == "auto":
        # Prefer the cloud provider if a key is configured, otherwise Ollama.
        if settings.openai_api_key and settings.openai_base_url:
            return OpenAICompatibleClient(settings)
        return OllamaClient(settings)
    raise ConfigError(f"unknown LLM provider: {provider}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MARKDOWN_LINK_RE = re.compile(r"\[([^\[\]]{1,500})\]\((?:https?://)?[^\s()\[\]]{1,800}\)")


def _clean_base_url(raw: str) -> str:
    """Unwrap markdown-mangled URLs and ensure a scheme is present."""
    text = raw.strip()
    for _ in range(4):
        new = _MARKDOWN_LINK_RE.sub(lambda m: m.group(1), text)
        if new == text:
            break
        text = new
    text = text.strip().rstrip("/")
    if not text:
        raise ConfigError("openai_base_url is empty")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\\-]*://", text):
        text = "https://" + text
    return text.rstrip("/")


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return ""


def _extract_tool_calls(raw_calls: Any) -> Iterable[ToolCall]:
    if not isinstance(raw_calls, list):
        return ()
    calls: list[ToolCall] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function", raw)
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                continue
        if isinstance(name, str) and isinstance(arguments, dict):
            calls.append(ToolCall(name=name, arguments=arguments))
    return calls
