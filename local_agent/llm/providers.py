"""Known LLM gateway providers: identity detection and billing endpoints.

The assistant talks to any OpenAI-compatible endpoint, but a handful of
Iranian/global gateways (AvalAI, GapGPT, OpenAI) have recognisable base
URLs and their own ``GET`` balance/usage endpoints.  This module centralises
that knowledge so the web UI can:

  * auto-detect which provider an API key / base URL belongs to, and
  * fetch a live credit / usage summary for the billing tab.

The billing calls are deliberately *best-effort*: every endpoint is tried
inside a timeout and a friendly ``available=False`` is returned when the
provider does not expose one (rather than an exception reaching the UI).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    label: str
    default_base_url: str
    host_patterns: tuple[str, ...] = ()
    key_prefixes: tuple[str, ...] = ()
    # Billing probe endpoints as (method, url_template). ``{base}`` is the
    # provider's OpenAI-compatible base URL, ``{root}`` its origin (scheme +
    # host).  The first endpoint that returns 2xx wins.
    billing_endpoints: tuple[tuple[str, str], ...] = ()


KNOWN_PROVIDERS: dict[str, ProviderInfo] = {
    "avalai": ProviderInfo(
        id="avalai",
        label="AvalAI",
        default_base_url="https://api.avalai.ir/v1",
        host_patterns=("api.avalai.ir", "avalai"),
        key_prefixes=("sk-", "aa-"),
        billing_endpoints=(
            ("GET", "{root}/user/v1/credit"),
        ),
    ),
    "gapgpt": ProviderInfo(
        id="gapgpt",
        label="GapGPT",
        default_base_url="https://api.gapgpt.app/v1",
        host_patterns=("gapgpt",),
        key_prefixes=("gg-", "sk-"),
        billing_endpoints=(
            ("GET", "{root}/api/v1/usage"),
            ("GET", "{root}/v1/usage"),
            ("GET", "{root}/api/v1/credit"),
        ),
    ),
    "openai": ProviderInfo(
        id="openai",
        label="OpenAI",
        default_base_url="https://api.openai.com/v1",
        host_patterns=("api.openai.com", "openai.com"),
        key_prefixes=("sk-",),
        billing_endpoints=(
            ("GET", "{root}/dashboard/billing/subscription"),
            ("GET", "{root}/dashboard/billing/usage"),
        ),
    ),
}

# The generic fallback used for any unrecognised gateway.
DEFAULT_PROVIDER = ProviderInfo(
    id="openai_compatible",
    label="سازگار با OpenAI",
    default_base_url="https://api.avalai.ir/v1",
    billing_endpoints=(),
)


def detect_provider(
    base_url: str = "", api_key: str = "", *, provider_hint: str = ""
) -> ProviderInfo:
    """Best-effort identification of the gateway behind ``base_url`` / key.

    Hostname patterns take precedence over key prefixes.  A caller that
    already knows the provider (e.g. it is persisted in config) can pass
    ``provider_hint`` to bypass detection for known ids.
    """
    if provider_hint in KNOWN_PROVIDERS:
        return KNOWN_PROVIDERS[provider_hint]
    if provider_hint == "ollama":
        return DEFAULT_PROVIDER
    base = (base_url or "").lower()
    key = (api_key or "").strip()
    for info in KNOWN_PROVIDERS.values():
        if info.host_patterns and any(p in base for p in info.host_patterns):
            return info
    if key:
        for info in KNOWN_PROVIDERS.values():
            if info.key_prefixes and key.startswith(info.key_prefixes):
                return info
    return DEFAULT_PROVIDER


def _origin(base_url: str) -> str:
    """Return ``scheme://host[:port]`` of an OpenAI-style base URL."""
    candidate = base_url.strip().rstrip("/")
    if "://" not in candidate:
        candidate = "https://" + candidate
    parts = candidate.split("://", 1)
    scheme, rest = parts[0], parts[1]
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _clean_base(base_url: str) -> str:
    candidate = base_url.strip().rstrip("/")
    if "://" not in candidate:
        candidate = "https://" + candidate
    return candidate


def fetch_billing(base_url: str, api_key: str, *, provider_hint: str = "") -> dict[str, Any]:
    """Fetch a normalised credit / usage summary for ``base_url`` + key.

    Returns a dict with keys ``provider``, ``label``, ``available``,
    ``balance``, ``currency``, ``usage``, ``expires``, ``raw`` and
    ``error``.  ``available`` is False (without raising) when the
    provider has no reachable billing endpoint or the key is invalid.
    """
    info = detect_provider(base_url, api_key, provider_hint=provider_hint)
    base = _clean_base(base_url)
    root = _origin(base_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    for method, template in info.billing_endpoints:
        url = template.format(base=base, root=root)
        try:
            if method.upper() == "POST":
                response = requests.post(url, headers=headers, timeout=10)
            else:
                response = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException as exc:
            continue
        if response.status_code >= 400:
            continue
        try:
            data = response.json()
        except ValueError:
            continue
        return _normalise_billing(info, data)
    return {
        "provider": info.id,
        "label": info.label,
        "available": False,
        "balance": None,
        "currency": None,
        "usage": None,
        "expires": None,
        "raw": None,
        "error": f"{info.label} درگاه مالی در دسترس نیست یا کلید نامعتبر است",
    }


def _normalise_billing(info: ProviderInfo, data: Any) -> dict[str, Any]:
    """Map the raw provider payload to our small uniform schema."""
    if not isinstance(data, dict):
        data = {}
    result: dict[str, Any] = {
        "provider": info.id,
        "label": info.label,
        "available": True,
        "balance": None,
        "currency": "IRT",
        "usage": None,
        "expires": None,
        "raw": data,
        "error": None,
    }
    if info.id == "avalai":
        result["balance"] = data.get("remaining_irt")
        result["currency"] = "IRT"
        result["usage"] = data.get("total_unit")
        packages = (data.get("credit_sources") or {}).get("packages") or []
        dates = [p.get("end_date") for p in packages if p.get("end_date")]
        result["expires"] = min(dates) if dates else None
    elif info.id == "gapgpt":
        result["balance"] = (
            data.get("remaining_credit")
            or data.get("balance")
            or data.get("remaining_credits")
            or data.get("credits")
        )
        result["usage"] = data.get("usage")
        result["expires"] = data.get("expires_at") or data.get("expiration")
    elif info.id == "openai":
        sub = data.get("data") if "usage" in (data or {}) else data
        result["balance"] = data.get("balance") or data.get("credit_granted")
        result["usage"] = data.get("total_usage")
        result["expires"] = data.get("expires_at") or data.get("hard_limit_usd")
    return result
