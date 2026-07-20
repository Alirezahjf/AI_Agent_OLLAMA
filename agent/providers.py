"""Model-provider adapters.

All three supported providers expose a chat-shaped API, but only Ollama is local.  This
module keeps provider-specific HTTP details out of the agent loop and, importantly,
never writes runtime API keys to SQLite or the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import random
import re
import time
from typing import Any, Iterable

import requests

from .config import Settings


class ProviderError(RuntimeError):
    """A safe-to-show model-provider error (it never includes an API key)."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    title: str
    description: str
    recommended_models: tuple[tuple[str, str], ...]


_MARKDOWN_LINK_RE = re.compile(r"\[([^\[\]]{1,500})\]\((https?://[^\s()\[\]]{1,800})\)")
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def clean_base_url(value: str) -> str:
    """Return a safe HTTPS-ish base URL even if a chat client mangled it.

    Users often copy rendered Markdown such as
    ``https://[api.avalai.ir](http://api.avalai.ir)/v1`` into .env.  Without this
    cleanup requests sees the literal host ``[api.avalai.ir](http://...)`` and TLS
    fails before the provider can answer.  We keep the visible link text so an
    existing ``https://`` prefix remains intact.
    """
    text = html.unescape(str(value or "").strip())
    for _ in range(4):
        replaced = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), text)
        if replaced == text:
            break
        text = replaced
    text = text.strip().rstrip("/")
    if text and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", text):
        text = "https://" + text
    return text.rstrip("/")


PROFILES: dict[str, ProviderProfile] = {
    "ollama": ProviderProfile(
        "ollama",
        "Ollama محلی",
        "بدون ارسال کد به سرویس ابری؛ کیفیت به مدل نصب‌شده وابسته است.",
        (("local", "مدل تنظیم‌شده در OLLAMA_MODEL"),),
    ),
    "gapgpt": ProviderProfile(
        "gapgpt",
        "GapGPT",
        "API سازگار با OpenAI. برای کارهای بزرگ، هزینهٔ مدل را در نظر بگیرید.",
        (
            ("claude-sonnet-5", "پیشنهادی برای کدنویسی و عامل‌ها"),
            ("gpt-5.6-sol", "بیشترین کیفیت/استدلال؛ گران‌تر"),
            ("gpt-5.6-terra", "تعادل کیفیت و هزینه"),
            ("gapgpt-qwen-3.6-thinking", "اقتصادی برای تحلیل"),
        ),
    ),
    "avalai": ProviderProfile(
        "avalai",
        "AvalAI",
        "API سازگار با OpenAI با مدل‌های متنوع و endpoint استاندارد /v1.",
        (
            ("claude-sonnet-5", "پیشنهادی برای کدنویسی عاملی"),
            ("gpt-5.6-sol", "بیشترین استدلال؛ گران‌تر"),
            ("kimi-k2.7-code", "کدنویسی با زمینهٔ بلند"),
            ("deepseek-v4-pro", "تعادل قدرتمند و اقتصادی"),
        ),
    ),
}


class ModelClient:
    provider_id: str
    model: str

    def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> ModelReply:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError


class OllamaClient(ModelClient):
    provider_id = "ollama"

    def __init__(self, base_url: str, model: str, timeout: int) -> None:
        self.base_url, self.model, self.timeout = base_url, model, timeout

    def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0.1, "num_ctx": 32768},
        }
        try:
            response = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
            # Some locally installed/older models reject the tools field. The system
            # prompt includes a strict one-JSON fallback, so retry once without it.
            if response.status_code in {400, 422} and tools:
                payload.pop("tools")
                response = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
            response.raise_for_status()
            message = response.json().get("message", {})
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(
                f"ارتباط با Ollama ناموفق بود ({exc}). آیا `ollama serve` و مدل {self.model} آماده‌اند؟"
            ) from exc
        return ModelReply(
            content=str(message.get("content") or "").strip(),
            tool_calls=tuple(_extract_tool_calls(message.get("tool_calls", []))),
        )

    def list_models(self) -> list[str]:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=min(20, self.timeout))
            response.raise_for_status()
            return [str(item["name"]) for item in response.json().get("models", []) if item.get("name")]
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"دریافت فهرست مدل‌های Ollama ناموفق بود: {exc}") from exc


class OpenAICompatibleClient(ModelClient):
    """Chat Completions client shared by GapGPT and AvalAI.

    The documented endpoints both support this conservative API surface.  We use
    native function calls when the selected model supports them and the agent has a
    strict JSON fallback for models that return plain text.
    """

    def __init__(
        self,
        provider_id: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int,
        service_tier: str = "",
        max_retries: int = 4,
    ) -> None:
        self.provider_id = provider_id
        self.base_url = clean_base_url(base_url)
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self.service_tier = service_tier.strip().lower()
        self.max_retries = max(0, min(int(max_retries), 8))

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _retry_after_seconds(response: requests.Response | Any | None) -> float:
        headers = getattr(response, "headers", {}) or {}
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            return 0.0
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _rate_limit_hint(response: requests.Response | Any | None) -> str:
        headers = getattr(response, "headers", {}) or {}
        interesting = {
            key: headers.get(key)
            for key in (
                "Retry-After",
                "x-ratelimit-remaining-requests",
                "x-ratelimit-reset-requests",
                "x-ratelimit-remaining-tokens",
                "x-ratelimit-reset-tokens",
            )
            if headers.get(key) is not None
        }
        if not interesting:
            return ""
        return " | " + "، ".join(f"{key}: {value}" for key, value in interesting.items())

    @staticmethod
    def _transient_exception(exc: requests.RequestException) -> bool:
        return isinstance(
            exc,
            (
                requests.Timeout,
                requests.ConnectionError,
                requests.exceptions.SSLError,
            ),
        )

    def _sleep_before_retry(self, attempt: int, response: requests.Response | Any | None = None) -> None:
        retry_after = self._retry_after_seconds(response)
        base_delay = min(30.0, 1.5 * (2**attempt))
        delay = max(retry_after, base_delay)
        time.sleep(delay + random.uniform(0, min(delay * 0.25, 3.0)))

    def _post_chat(self, body: dict[str, Any]) -> requests.Response:
        last_error: requests.RequestException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json=body,
                    timeout=self.timeout,
                )
                if response.status_code in _TRANSIENT_STATUS_CODES and attempt < self.max_retries:
                    self._sleep_before_retry(attempt, response)
                    continue
                return response
            except requests.RequestException as exc:
                last_error = exc
                if not self._transient_exception(exc) or attempt >= self.max_retries:
                    raise
                self._sleep_before_retry(attempt)
        assert last_error is not None
        raise last_error

    def _complete_once(self, body: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        response = self._post_chat(body)
        # A provider may expose a text-only model. Retry once with the strict JSON
        # fallback protocol from the system prompt rather than failing the workflow.
        if response.status_code in {400, 422} and tools and "tools" in body:
            body = dict(body)
            body.pop("tools", None)
            body.pop("tool_choice", None)
            response = self._post_chat(body)
        response.raise_for_status()
        return response.json()

    def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.1,
        }
        if self.provider_id == "avalai" and self.service_tier:
            body["service_tier"] = self.service_tier
        try:
            try:
                payload = self._complete_once(body, tools)
            except (requests.HTTPError, requests.RequestException):
                # AvalAI recommends falling back from flex to default for production
                # interactive workflows when flex is unavailable or too slow.
                if self.provider_id != "avalai" or body.get("service_tier") != "flex":
                    raise
                fallback = dict(body)
                fallback["service_tier"] = "default"
                payload = self._complete_once(fallback, tools)
            message = payload["choices"][0]["message"]
        except requests.HTTPError as exc:
            response = exc.response
            status = response.status_code if response is not None else "?"
            if status == 429:
                raise ProviderError(
                    f"محدودیت نرخ {PROFILES[self.provider_id].title} فعال شد (HTTP 429). "
                    "ربات چند بار با backoff تلاش کرد اما هنوز ظرفیت آزاد نشده است؛ "
                    "چند لحظه بعد دوباره امتحان کنید."
                    f"{self._rate_limit_hint(response)}"
                ) from exc
            if status in {500, 502, 503, 504}:
                raise ProviderError(
                    f"{PROFILES[self.provider_id].title} موقتاً در دسترس نیست (HTTP {status}). "
                    "ربات با backoff تلاش مجدد انجام داد؛ کمی بعد دوباره امتحان کنید."
                ) from exc
            raise ProviderError(
                f"پاسخ ناموفق از {PROFILES[self.provider_id].title} (HTTP {status}). "
                "کلید، مدل، base URL و اعتبار حساب را بررسی کنید."
            ) from exc
        except requests.exceptions.SSLError as exc:
            raise ProviderError(
                f"ارتباط TLS با {PROFILES[self.provider_id].title} بعد از چند تلاش ناموفق بود. "
                "اگر base URL را از چت کپی کرده‌اید مطمئن شوید دقیقاً این باشد: "
                f"{self.base_url} . خطای SSL: {exc}"
            ) from exc
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            raise ProviderError(
                f"ارتباط با {PROFILES[self.provider_id].title} بعد از چند تلاش ناموفق بود: {exc}"
            ) from exc
        return ModelReply(
            content=_content_to_text(message.get("content")),
            tool_calls=tuple(_extract_tool_calls(message.get("tool_calls", []))),
        )

    def list_models(self) -> list[str]:
        try:
            response = None
            for attempt in range(min(self.max_retries, 3) + 1):
                response = requests.get(
                    f"{self.base_url}/models", headers=self._headers, timeout=min(30, self.timeout)
                )
                if response.status_code in _TRANSIENT_STATUS_CODES and attempt < min(self.max_retries, 3):
                    self._sleep_before_retry(attempt, response)
                    continue
                break
            assert response is not None
            response.raise_for_status()
            payload = response.json()
            return [str(item["id"]) for item in payload.get("data", []) if item.get("id")]
        except requests.HTTPError as exc:
            response = exc.response
            status = response.status_code if response is not None else "?"
            if status == 429:
                raise ProviderError(
                    "دریافت مدل‌ها به محدودیت نرخ خورد (HTTP 429). "
                    f"کمی بعد دوباره امتحان کنید.{self._rate_limit_hint(response)}"
                ) from exc
            raise ProviderError(f"دریافت مدل‌ها ناموفق بود (HTTP {status}).") from exc
        except requests.exceptions.SSLError as exc:
            raise ProviderError(
                "دریافت مدل‌ها به خطای TLS خورد. base URL را دقیق و بدون Markdown وارد کنید: "
                f"{self.base_url}. خطا: {exc}"
            ) from exc
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"دریافت مدل‌ها ناموفق بود: {exc}") from exc


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        ).strip()
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


class ProviderRouter:
    """Creates clients from persistent non-secret preferences and ephemeral keys."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session_keys: dict[tuple[int, str], str] = {}

    def set_session_key(self, chat_id: int, provider: str, api_key: str) -> None:
        if provider not in {"gapgpt", "avalai"} or not api_key.strip():
            raise ValueError("کلید یا ارائه‌دهنده نامعتبر است.")
        self._session_keys[(chat_id, provider)] = api_key.strip()

    def clear_session_key(self, chat_id: int, provider: str | None = None) -> None:
        if provider:
            self._session_keys.pop((chat_id, provider), None)
            return
        for key in [key for key in self._session_keys if key[0] == chat_id]:
            self._session_keys.pop(key, None)

    def key_source(self, chat_id: int, provider: str) -> str:
        if provider == "ollama":
            return "محلی"
        if (chat_id, provider) in self._session_keys:
            return "جلسهٔ فعلی (در حافظه، ذخیره‌نشده)"
        return "ENV" if self._env_key(provider) else "تنظیم نشده"

    def _env_key(self, provider: str) -> str:
        return self.settings.gapgpt_api_key if provider == "gapgpt" else self.settings.avalai_api_key

    def _key(self, chat_id: int, provider: str) -> str:
        return self._session_keys.get((chat_id, provider), self._env_key(provider))

    def default_model(self, provider: str) -> str:
        if self.settings.default_model:
            return self.settings.default_model
        if provider == "ollama":
            return self.settings.ollama_model
        return PROFILES[provider].recommended_models[0][0]

    def client_for(self, chat_id: int, provider: str, model: str | None = None) -> ModelClient:
        provider = provider.lower()
        if provider not in PROFILES:
            raise ProviderError("ارائه‌دهندهٔ انتخاب‌شده معتبر نیست.")
        selected_model = model or self.default_model(provider)
        if provider == "ollama":
            return OllamaClient(self.settings.ollama_base_url, selected_model, self.settings.model_timeout)
        key = self._key(chat_id, provider)
        if not key:
            title = PROFILES[provider].title
            raise ProviderError(
                f"کلید API برای {title} تنظیم نشده است. از دکمهٔ «مدل و API» یا متغیر محیطی استفاده کنید."
            )
        base_url = self.settings.gapgpt_base_url if provider == "gapgpt" else self.settings.avalai_base_url
        service_tier = self.settings.avalai_service_tier if provider == "avalai" else ""
        return OpenAICompatibleClient(
            provider,
            base_url,
            key,
            selected_model,
            self.settings.model_timeout,
            service_tier=service_tier,
            max_retries=self.settings.provider_max_retries,
        )

    def validate_key(self, provider: str, api_key: str) -> list[str]:
        """Validate a key with GET /models; the key is never persisted by this call."""
        if provider not in {"gapgpt", "avalai"}:
            raise ProviderError("فقط کلید API ابری قابل اعتبارسنجی است.")
        base_url = self.settings.gapgpt_base_url if provider == "gapgpt" else self.settings.avalai_base_url
        service_tier = self.settings.avalai_service_tier if provider == "avalai" else ""
        client = OpenAICompatibleClient(
            provider,
            base_url,
            api_key.strip(),
            self.default_model(provider),
            self.settings.model_timeout,
            service_tier=service_tier,
            max_retries=self.settings.provider_max_retries,
        )
        return client.list_models()

    def detect_provider(self, api_key: str) -> tuple[str | None, list[str]]:
        """Try both documented /models endpoints without storing the submitted key.

        A successful authenticated models endpoint is the only reliable detection;
        prefix heuristics are deliberately not trusted.  The second result is a
        diagnostic suitable for a UI, never a raw HTTP body.
        """
        found: list[str] = []
        diagnostics: list[str] = []
        for provider in ("avalai", "gapgpt"):
            try:
                self.validate_key(provider, api_key)
                found.append(provider)
            except ProviderError as exc:
                diagnostics.append(f"{PROFILES[provider].title}: {exc}")
        return (found[0] if len(found) == 1 else None), diagnostics
