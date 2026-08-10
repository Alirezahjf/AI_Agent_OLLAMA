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
from typing import Any, Callable, Iterable

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
    id: str | None = None


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

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> Iterable[tuple[str, str]]:
        """Yield (event_type, content) tuples as the model generates.

        The default implementation falls back to the non-streaming
        ``complete`` and emits a single ``assistant_delta`` followed by
        ``done``.  Subclasses should override this for true streaming.
        """
        reply = self.complete(messages, tools)
        if reply.content:
            yield ("assistant_delta", reply.content)
        yield ("done", "")

    def complete_streaming(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        on_delta: Callable[[str], None] | None = None,
    ) -> ModelReply:
        """Return a full :class:`ModelReply` while emitting text deltas.

        This is what the agent loop wants: the incremental text for a
        live-typing UI *and* the structured ``tool_calls`` needed to keep
        the loop going.  The default implementation simply defers to
        :meth:`complete`; providers that support Server-Sent Events
        override it for real token-by-token output.
        """
        reply = self.complete(messages, tools)
        if on_delta and reply.content:
            on_delta(reply.content)
        return reply

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
        # Session reuse for connection pooling and lower latency
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

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
                response = self._session.post(
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
                # Retry without tools via same session
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
            response = self._session.get(f"{self._base}/api/tags", timeout=min(20, self._timeout))
            response.raise_for_status()
            return [str(item["name"]) for item in response.json().get("models", []) if item.get("name")]
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Ollama /api/tags failed: %s", exc)
            return []

    def complete_streaming(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        on_delta: Callable[[str], None] | None = None,
    ) -> ModelReply:
        """Stream Ollama's NDJSON response and rebuild the final reply."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": self._temperature, "num_ctx": 32768},
        }
        if tools:
            payload["tools"] = [tool.to_openai() for tool in tools]

        try:
            response = self._session.post(
                f"{self._base}/api/chat", json=payload, timeout=self._timeout, stream=True
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Ollama streaming unavailable (%s); falling back", exc)
            return self.complete(messages, tools)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        try:
            for line in _iter_stream_lines(response):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = data.get("message") or {}
                piece = _delta_to_text(message.get("content"))
                if piece:
                    text_parts.append(piece)
                    if on_delta:
                        on_delta(piece)
                if message.get("tool_calls"):
                    calls.extend(_extract_tool_calls(message["tool_calls"]))
                if data.get("done"):
                    break
        except requests.RequestException as exc:
            logger.warning("Ollama stream interrupted (%s); falling back", exc)
            return self.complete(messages, tools)

        if not text_parts and not calls:
            return self.complete(messages, tools)
        return ModelReply(content="".join(text_parts).strip(), tool_calls=tuple(calls))

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> Iterable[tuple[str, str]]:
        """Stream the Ollama response, yielding ``assistant_delta`` chunks."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": self._temperature, "num_ctx": 32768},
        }
        if tools:
            payload["tools"] = [tool.to_openai() for tool in tools]

        try:
            response = self._session.post(
                f"{self._base}/api/chat",
                json=payload,
                timeout=self._timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise LLMTimeout(f"Ollama streaming failed: {exc}") from exc

        if response.status_code >= 400:
            # Fall back to non-streaming
            yield ("assistant_delta", self.complete(messages, tools).content)
            yield ("done", "")
            return

        content_parts: list[str] = []
        for line in _iter_stream_lines(response):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = data.get("message") or {}
            chunk = message.get("content", "")
            if chunk:
                content_parts.append(chunk)
                yield ("assistant_delta", chunk)
            # If the response is done, break
            if data.get("done", False):
                break

        yield ("done", "")

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
        # Session for pooling + keep-alive
        self._session = requests.Session()
        self._session.headers.update(self._headers)

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

    def complete_streaming(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        on_delta: Callable[[str], None] | None = None,
    ) -> ModelReply:
        """Stream tokens over SSE and rebuild the final reply.

        Tool calls arrive in fragments: the first chunk carries the ``id``
        and function name, later chunks append to ``arguments`` one string
        piece at a time.  We accumulate them by index and only parse the
        JSON once the stream finishes, falling back to the blocking call
        if anything about the stream looks wrong.

        When *tools* are present, many providers (AvalAI, some GapGPT
        models) reject ``stream=true`` with HTTP 400 because they cannot
        combine function calling with streaming.  In that case we skip
        streaming entirely — tool-call results are structural and do not
        benefit from token-by-token output.
        """
        if tools:
            # Streaming + function calling is unreliable across providers;
            # fall back to the blocking endpoint which already handles the
            # tools/no-tools retry correctly.
            return self.complete(messages, tools)

        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": True,
        }

        try:
            response = requests.post(
                f"{self._base}/chat/completions",
                headers=self._headers,
                json=body,
                timeout=self._timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            logger.warning("streaming unavailable (%s); falling back", exc)
            return self.complete(messages, tools)

        if response.status_code >= 400:
            logger.warning("streaming rejected with HTTP %s; falling back", response.status_code)
            return self.complete(messages, tools)

        text_parts: list[str] = []
        partial: dict[int, dict[str, Any]] = {}
        try:
            for line in _iter_stream_lines(response):
                if not line or not line.startswith("data: "):
                    continue
                chunk = line[6:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = _delta_to_text(delta.get("content"))
                if piece:
                    text_parts.append(piece)
                    if on_delta:
                        on_delta(piece)
                for raw in delta.get("tool_calls") or []:
                    if not isinstance(raw, dict):
                        continue
                    index = int(raw.get("index", 0))
                    slot = partial.setdefault(index, {"id": None, "name": "", "arguments": ""})
                    if raw.get("id"):
                        slot["id"] = str(raw["id"])
                    function = raw.get("function") or {}
                    if function.get("name"):
                        slot["name"] = str(function["name"])
                    if function.get("arguments"):
                        slot["arguments"] += str(function["arguments"])
        except requests.RequestException as exc:
            logger.warning("stream interrupted (%s); falling back", exc)
            return self.complete(messages, tools)

        calls: list[ToolCall] = []
        for index in sorted(partial):
            slot = partial[index]
            if not slot["name"]:
                continue
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                logger.warning("could not parse streamed arguments for %s", slot["name"])
                continue
            if isinstance(arguments, dict):
                calls.append(ToolCall(name=slot["name"], arguments=arguments, id=slot["id"]))

        if not text_parts and not calls:
            # An empty stream usually means the provider silently refused
            # to stream; the blocking endpoint still works.
            return self.complete(messages, tools)
        return ModelReply(content="".join(text_parts).strip(), tool_calls=tuple(calls))

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
    ) -> Iterable[tuple[str, str]]:
        """Stream the OpenAI-compatible response, yielding ``assistant_delta`` chunks."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = [tool.to_openai() for tool in tools]
            body["tool_choice"] = "auto"

        try:
            response = requests.post(
                f"{self._base}/chat/completions",
                headers=self._headers,
                json=body,
                timeout=self._timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise LLMTimeout(f"streaming failed: {exc}") from exc

        if response.status_code >= 400:
            # Fall back to non-streaming
            yield ("assistant_delta", self.complete(messages, tools).content)
            yield ("done", "")
            return

        for line in _iter_stream_lines(response):
            if not line:
                continue
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content", "")
            if content:
                yield ("assistant_delta", content)

        yield ("done", "")

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


def _iter_stream_lines(response: requests.Response) -> Iterable[str]:
    """Yield decoded text lines from a streamed HTTP response, strictly as UTF-8.

    ``response.iter_lines(decode_unicode=True)`` decodes the body with the
    encoding advertised in the ``Content-Type`` header — and when the header
    carries **no charset** (extremely common for SSE from AvalAI / GapGPT /
    Gemini-style gateways), ``requests`` falls back to **ISO-8859-1**, so
    every UTF-8 Persian byte sequence turns into mojibake
    (``"لیست فایل‌ها"`` → ``"Ù\x84Û\x8cØ³Øª Ù\x81Ø§Û\x8cÙ\x84\xe2\x80\x8cÙ\x87Ø§"``).

    Requesting the raw byte lines (``decode_unicode=False``) and decoding
    them ourselves with UTF-8 makes the stream header-independent.  Two
    defensive details:

    * ``errors="replace"`` — one corrupt chunk must not kill the stream;
    * ``str`` lines are passed through untouched — the real ``requests``
      always yields ``bytes`` here, but simple test doubles may not.

    Splitting on ``\\n`` is safe for UTF-8: newline bytes (`0x0A`/`0x0D`)
    can never appear inside a multi-byte sequence, so no character is ever
    cut in half at a line boundary.
    """
    for raw in response.iter_lines(decode_unicode=False):
        if isinstance(raw, bytes):
            yield raw.decode("utf-8", errors="replace")
        else:
            yield str(raw)


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


def _delta_to_text(content: Any) -> str:
    """Like :func:`_content_to_text` but keeps whitespace.

    Streaming chunks arrive as ``"محتوای "``, ``"فایل "``, ...  Stripping
    each one glues the words together, so deltas must never be trimmed.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "".join(parts)
    return ""


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
    """Robust extraction handling string/dict args, int ids, and nested shapes."""
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
        # Handle arguments as JSON string, dict, or None
        if arguments is None:
            arguments = {}
        elif isinstance(arguments, str):
            s = arguments.strip()
            if not s:
                arguments = {}
            else:
                try:
                    arguments = json.loads(s)
                except json.JSONDecodeError:
                    # Try to fix single quotes or trailing commas
                    try:
                        # Replace single quotes with double (naive but helps some models)
                        fixed = s.replace("'", '"')
                        arguments = json.loads(fixed)
                    except Exception:
                        logger.warning("failed to parse tool arguments for %s: %s", name, s[:200])
                        continue
        if not isinstance(arguments, dict):
            continue
        raw_id = raw.get("id")
        # id can be int, str, or missing
        call_id = None
        if raw_id is not None:
            try:
                call_id = str(raw_id).strip() or None
            except Exception:
                call_id = None
        if isinstance(name, str) and name.strip() and isinstance(arguments, dict):
            calls.append(ToolCall(name=name.strip(), arguments=arguments, id=call_id))
    return calls
