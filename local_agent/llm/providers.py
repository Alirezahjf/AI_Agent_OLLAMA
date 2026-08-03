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

AvalAI schema notes (verified against the official User API reference at
``docs.avalai.ir/en/api-reference/user``, base URL
``https://api.avalai.ir/user/v1``):

* ``GET /credit`` returns ``limit``, ``remaining_irt`` (اعتبار باقی‌مانده به
  تومان), ``remaining_unit`` (باقی‌مانده به واحد/دلار), ``total_unit``
  (**کل** اعتبارهای شارژشده به واحد، *نه* مصرف), ``exchange_rate``,
  ``account_tier`` (۰ تا ۵) and ``credit_sources`` with ``grants`` and
  ``packages`` arrays.  Package / grant amounts (``amount_irt``,
  ``remaining_irt``) arrive as **strings** and each entry carries an ISO
  ``end_date``.
* ``GET /transactions`` lists recent API calls with per-call token counts.
* ``GET /transactions/summary?group_by=model`` aggregates the last ≤24h of
  usage into ``totals`` (transactions / tokens / cost) plus a per-model
  breakdown — that is the only place "مصرف" (actual consumption) exists.

Every field the provider may omit degrades to ``None`` instead of raising,
and nothing sensitive (most importantly the API key) is ever echoed back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
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
    # Optional usage-telemetry endpoints (AvalAI User API).  Empty strings
    # mean the provider exposes no such endpoint; fetching them is always
    # best-effort and failures collapse the fields to ``None``.
    transactions_endpoint: str = ""
    summary_endpoint: str = ""


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
        transactions_endpoint="{root}/user/v1/transactions",
        summary_endpoint="{root}/user/v1/transactions/summary",
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


# ---------------------------------------------------------------------------
# Number / date helpers
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """Coerce ``value`` to a finite float; ``None`` when impossible.

    AvalAI returns package amounts (``amount_irt`` / ``remaining_irt``) as
    decimal *strings* like ``"500000.00"`` while scalar fields are floats —
    this helper normalises both without ever raising.
    """
    if value is None or isinstance(value, bool):
        return None
    candidate: float | None = None
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("٬", "")
        if not text:
            return None
        try:
            candidate = float(text)
        except ValueError:
            return None
    if candidate is None or not math.isfinite(candidate):
        return None
    return candidate


def _iso_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))  # noqa: FURB162
    except ValueError:
        return None


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------


def _unavailable(info: ProviderInfo, error: str) -> dict[str, Any]:
    return {
        "provider": info.id,
        "label": info.label,
        "available": False,
        "balance": None,
        "balance_unit": None,
        "currency": None,
        "limit": None,
        "total_credit_unit": None,
        "exchange_rate": None,
        "account_tier": None,
        "usage": None,
        "usage_24h": None,
        "transactions": None,
        "transactions_total": None,
        "expires": None,
        "packages": [],
        "grants": [],
        "rate_limit": None,
        "fetched_at": None,
        "error": error,
    }


def _billing_error_message(info: ProviderInfo, status: int | None) -> str:
    """A readable Persian message for why the billing endpoint failed."""
    label = info.label
    if status == 401:
        return f"کلید API برای درگاه مالی {label} نامعتبر است (احراز هویت ناموفق ۴۰۱)"
    if status == 403:
        return f"حساب {label} معلق یا غیرفعال است (۴۰۳)"
    if status == 429:
        return (
            f"محدودیت تعداد درخواست به درگاه مالی {label}؛"
            " چند لحظهٔ دیگر دوباره تلاش کنید (۴۲۹)"
        )
    if status:
        return f"درگاه مالی {label} خطای {status} برگرداند"
    return f"درگاه مالی {label} در دسترس نیست یا کلید نامعتبر است"


def _rate_limit_payload(headers: Any) -> dict[str, Any] | None:
    """Extract ``x-ratelimit-*`` headers when the provider publishes them."""
    try:
        limit = headers.get("x-ratelimit-limit-requests")
        remaining = headers.get("x-ratelimit-remaining-requests")
        reset = headers.get("x-ratelimit-reset-requests")
    except AttributeError:
        return None
    if limit is None and remaining is None and reset is None:
        return None
    return {
        "limit": _num(limit),
        "remaining": _num(remaining),
        "reset_seconds": _num(reset),
    }


def fetch_billing(base_url: str, api_key: str, *, provider_hint: str = "") -> dict[str, Any]:
    """Fetch a normalised credit / usage summary for ``base_url`` + key.

    Returns a dict with keys ``provider``, ``label``, ``available``,
    ``balance`` (IRT تومان), ``balance_unit`` (واحد/دلار), ``currency``,
    ``limit``, ``total_credit_unit``, ``exchange_rate``, ``account_tier``,
    ``usage``, ``usage_24h``, ``transactions``, ``transactions_total``,
    ``expires``, ``packages``, ``grants``, ``rate_limit``, ``fetched_at``
    and ``error``.  ``available`` is False (without raising) when the
    provider has no reachable billing endpoint or the key is invalid, and
    every optional field is ``None`` — never an exception — when the
    provider does not publish it.  The API key is only used for the
    ``Authorization`` header and never appears in the result.
    """
    info = detect_provider(base_url, api_key, provider_hint=provider_hint)
    base = _clean_base(base_url)
    root = _origin(base_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    last_status: int | None = None
    saw_response = False
    for method, template in info.billing_endpoints:
        url = template.format(base=base, root=root)
        try:
            if method.upper() == "POST":
                response = requests.post(url, headers=headers, timeout=10)
            else:
                response = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException:
            continue
        if response.status_code >= 400:
            saw_response = True
            last_status = response.status_code
            continue
        try:
            data = response.json()
        except ValueError:
            saw_response = True
            continue
        result = _normalise_billing(info, data)
        result["rate_limit"] = _rate_limit_payload(response.headers)
        result["fetched_at"] = datetime.now(UTC).isoformat()
        _enrich_usage_telemetry(info, headers, root, base, result)
        return result
    status = last_status if saw_response else None
    return _unavailable(info, _billing_error_message(info, status))


def _enrich_usage_telemetry(
    info: ProviderInfo,
    headers: dict[str, str],
    root: str,
    base: str,
    result: dict[str, Any],
) -> None:
    """Attach recent transactions and the 24h usage summary (AvalAI).

    Completely best-effort and rate-limit friendly (two small GETs): any
    failure leaves the fields at ``None`` rather than breaking the whole
    billing card.
    """
    if info.summary_endpoint:
        url = info.summary_endpoint.format(base=base, root=root)
        payload = _get_json(url, headers, params={"hours_ago": 24, "group_by": "model"})
        if payload is not None:
            result["usage_24h"] = _normalise_summary(payload)
    if info.transactions_endpoint:
        url = info.transactions_endpoint.format(base=base, root=root)
        payload = _get_json(url, headers, params={"hours_ago": 24, "page": 1, "page_size": 10})
        if payload is not None:
            transactions, total = _normalise_transactions(payload)
            result["transactions"] = transactions
            result["transactions_total"] = total


def _get_json(url: str, headers: dict[str, str], *, params: dict[str, Any]) -> Any | None:
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _normalise_summary(data: Any) -> dict[str, Any] | None:
    """Map ``GET /transactions/summary`` to a compact usage dict."""
    if not isinstance(data, dict):
        return None
    totals = data.get("totals") or {}
    if not isinstance(totals, dict):
        totals = {}
    tokens = totals.get("tokens") or {}
    cost = totals.get("cost") or {}
    period = data.get("period") or {}
    by_model = []
    for item in data.get("by_model") or []:
        if not isinstance(item, dict):
            continue
        by_model.append(
            {
                "model": item.get("model"),
                "transactions": _num(item.get("transactions")),
                "tokens": _num(item.get("tokens")),
                "cost_unit": _num(item.get("cost_unit")),
            }
        )
    return {
        "period_start": period.get("start"),
        "period_end": period.get("end"),
        "transactions": _num(totals.get("transactions")),
        "tokens_total": _num(tokens.get("total")),
        "tokens_prompt": _num(tokens.get("prompt")),
        "tokens_completion": _num(tokens.get("completion")),
        "cost_unit": _num(cost.get("unit")),
        "cost_paid_irt": _num(cost.get("paid_irt")),
        "cost_paid_grant_irt": _num(cost.get("paid_grant_irt")),
        "by_model": by_model,
    }


def _normalise_transactions(data: Any) -> tuple[list[dict[str, Any]], float | None]:
    """Map ``GET /transactions`` to (rows, total_count)."""
    if not isinstance(data, dict):
        return [], None
    rows: list[dict[str, Any]] = []
    for item in data.get("transactions") or []:
        if not isinstance(item, dict):
            continue
        tokens = item.get("tokens") or {}
        rows.append(
            {
                "id": item.get("id"),
                "created_at": item.get("created_at") or item.get("requested_at"),
                "model": item.get("model"),
                "provider": item.get("provider"),
                "status_code": item.get("status_code"),
                "stream": item.get("stream"),
                "tokens_total": _num(tokens.get("total")),
                "tokens_prompt": _num(tokens.get("prompt")),
                "tokens_completion": _num(tokens.get("completion")),
            }
        )
    return rows, _num(data.get("total"))


def _normalise_billing(info: ProviderInfo, data: Any) -> dict[str, Any]:
    """Map the raw provider payload to our uniform schema.

    Per-provider field notes live in the module docstring above.  Every
    field that is missing on the provider's side becomes ``None`` instead
    of raising, and the raw payload itself is **not** echoed back.
    """
    if not isinstance(data, dict):
        data = {}
    result: dict[str, Any] = {
        "provider": info.id,
        "label": info.label,
        "available": True,
        "balance": None,
        "balance_unit": None,
        "currency": None,
        "limit": None,
        "total_credit_unit": None,
        "exchange_rate": None,
        "account_tier": None,
        "usage": None,
        "usage_24h": None,
        "transactions": None,
        "transactions_total": None,
        "expires": None,
        "packages": [],
        "grants": [],
        "rate_limit": None,
        "fetched_at": None,
        "error": None,
    }
    if info.id == "avalai":
        # https://docs.avalai.ir/en/api-reference/user — GET /user/v1/credit
        result["balance"] = _num(data.get("remaining_irt"))
        result["balance_unit"] = _num(data.get("remaining_unit"))
        result["currency"] = "IRT"
        result["limit"] = _num(data.get("limit"))
        result["total_credit_unit"] = _num(data.get("total_unit"))
        result["exchange_rate"] = _num(data.get("exchange_rate"))
        tier = _num(data.get("account_tier"))
        result["account_tier"] = int(tier) if tier is not None else None
        sources = data.get("credit_sources") or {}
        if not isinstance(sources, dict):
            sources = {}
        packages = [_normalise_credit_source(p, kind="package") for p in sources.get("packages") or []]
        grants = [_normalise_credit_source(g, kind="grant") for g in sources.get("grants") or []]
        result["packages"] = [p for p in packages if p]
        result["grants"] = [g for g in grants if g]
        result["expires"] = _earliest_expiry(
            [entry.get("end_date") for entry in result["packages"] + result["grants"]]
        )
        # Note: ``total_unit`` is *کل اعتبارهای شارژشده*, not consumption —
        # real usage only exists in /transactions/summary (usage_24h), so
        # ``usage`` intentionally stays None here.
    elif info.id == "gapgpt":
        result["balance"] = _first_not_none(
            _num(data.get("remaining_credit")),
            _num(data.get("balance")),
            _num(data.get("remaining_credits")),
            _num(data.get("credits")),
        )
        result["usage"] = _num(data.get("usage"))
        result["expires"] = _first_not_none(data.get("expires_at"), data.get("expiration"))
    elif info.id == "openai":
        result["balance"] = _first_not_none(
            _num(data.get("balance")), _num(data.get("credit_granted"))
        )
        result["currency"] = "USD"
        result["usage"] = _num(data.get("total_usage"))
        result["expires"] = _first_not_none(data.get("expires_at"), data.get("access_until"))
        result["limit"] = _first_not_none(
            _num(data.get("hard_limit_usd")), _num(data.get("limit"))
        )
    return result


def _normalise_credit_source(entry: Any, *, kind: str) -> dict[str, Any] | None:
    """Normalise one ``credit_sources.packages[]`` / ``grants[]`` record."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name") or entry.get("description")
    return {
        "kind": kind,
        "id": entry.get("id"),
        "name": name,
        "description": entry.get("description"),
        "amount_irt": _num(entry.get("amount_irt")),
        "remaining_irt": _num(entry.get("remaining_irt")),
        "end_date": entry.get("end_date"),
    }


def _earliest_expiry(candidates: list[Any]) -> str | None:
    """Pick the soonest parseable end_date; fall back to the smallest string."""
    parsed = []
    for value in candidates:
        moment = _iso_datetime(value)
        if moment is not None:
            parsed.append((moment, str(value).strip()))
    if parsed:
        return min(parsed, key=lambda pair: pair[0])[1]
    strings = [str(value).strip() for value in candidates if value]
    return min(strings) if strings else None
