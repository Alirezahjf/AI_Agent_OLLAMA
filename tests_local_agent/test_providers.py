"""Tests for provider auto-detection and billing normalisation.

The AvalAI payloads in this file are taken **verbatim from the official
User API reference** (``docs.avalai.ir/en/api-reference/user``) so the
normaliser is verified against the documented schema, offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from local_agent.llm.providers import (
    KNOWN_PROVIDERS,
    _normalise_billing,
    _normalise_summary,
    _normalise_transactions,
    detect_provider,
    fetch_billing,
)

# --- Official sample of GET https://api.avalai.ir/user/v1/credit -----------
AVALAI_CREDIT_DOC_SAMPLE: dict[str, Any] = {
    "limit": 0.0,
    "remaining_irt": 742927.85,
    "remaining_unit": 0.0,
    "total_unit": 6.44622863340564,
    "exchange_rate": 115250,
    "account_tier": 5,
    "credit_sources": {
        "grants": [
            {
                "id": "7",
                "description": "اعتبار هدیهٔ خوش‌آمد",
                "amount_irt": "10000.00",
                "remaining_irt": "8000.00",
                "end_date": "2025-10-01T00:00:00+00:00",
                "allowed_services": ["api"],
                "scope_details": {},
            }
        ],
        "packages": [
            {
                "id": "25",
                "template_id": "d-o3050s",
                "name": "مدل‌های زبانی منتخب OpenAI روزانه پایه",
                "description": "٪۴۰ تخفیف در مدل‌های منتخب OpenAI",
                "amount_irt": "500000.00",
                "remaining_irt": "498447.15",
                "end_date": "2025-11-27T15:05:07.404882+00:00",
                "allowed_services": ["api"],
                "scope_details": {"api": ["gpt-5-chat", "gpt-5-mini"]},
            }
        ],
    },
}

# --- Official sample of GET .../transactions/summary?group_by=model --------
AVALAI_SUMMARY_DOC_SAMPLE: dict[str, Any] = {
    "period": {
        "start": "2025-11-26T09:09:31.342Z",
        "end": "2025-11-27T09:09:31.342Z",
    },
    "totals": {
        "transactions": 1011,
        "tokens": {
            "total": 31863,
            "prompt": 11362,
            "completion": 20501,
            "reasoning": 294,
            "cached": 0,
        },
        "cost": {
            "unit": "0.01466890",
            "paid_unit": "0.01466890",
            "paid_irt": "131.73",
            "paid_grant_irt": "1552.85",
        },
    },
    "by_model": [
        {"model": "gpt-5.4-mini", "transactions": 1000, "tokens": 30000, "cost_unit": "0.01350000"},
        {"model": "gemini-2.5-flash", "transactions": 3, "tokens": 1529, "cost_unit": "0.00097790"},
    ],
}

# --- Official sample of GET .../transactions -------------------------------
AVALAI_TRANSACTIONS_DOC_SAMPLE: dict[str, Any] = {
    "transactions": [
        {
            "id": "019ac1c0-9ff3-7663-b0b9-fbcf2461939a",
            "created_at": "2025-11-26T20:06:23.442Z",
            "requested_at": "2025-11-26T20:00:18.031Z",
            "safety_identifier": None,
            "model": "gpt-5.4-mini",
            "provider": "openai",
            "status_code": 200,
            "stream": False,
            "tokens": {"total": 30, "prompt": 10, "completion": 20, "reasoning": 0, "cached": 0},
        }
    ],
    "total": 100,
    "page": 1,
    "page_size": 100,
    "has_more": False,
}


def test_detect_provider_by_host() -> None:
    assert detect_provider("https://api.avalai.ir/v1", "sk-x").id == "avalai"
    assert detect_provider("https://api.gapgpt.app/v1", "gg-x").id == "gapgpt"
    assert detect_provider("https://api.openai.com/v1", "sk-x").id == "openai"


def test_detect_provider_by_key_prefix_when_host_unknown() -> None:
    assert detect_provider("https://proxy.example.com/v1", "gg-abc").id == "gapgpt"
    # sk- is shared by AvalAI and OpenAI; on an unknown host the gateway
    # checked first (AvalAI, the flagship default) wins.
    assert detect_provider("https://proxy.example.com/v1", "sk-abc").id == "avalai"


def test_detect_provider_defaults_to_openai_compatible() -> None:
    info = detect_provider("https://custom.example.com/v1", "abcdef")
    assert info.id == "openai_compatible"


def test_detect_provider_respects_hint() -> None:
    assert detect_provider("https://whatever/v1", "x", provider_hint="ollama").id == "openai_compatible"


def test_normalise_avalai_credit_matches_official_docs() -> None:
    info = detect_provider("https://api.avalai.ir/v1", "sk-x")
    out = _normalise_billing(info, AVALAI_CREDIT_DOC_SAMPLE)
    assert out["available"] is True
    # موجودی به تومان + واحد
    assert out["balance"] == 742927.85
    assert out["balance_unit"] == 0.0
    assert out["currency"] == "IRT"
    # فیلدهای تکمیلیِ سند رسمی
    assert out["limit"] == 0.0
    assert out["total_credit_unit"] == pytest.approx(6.44622863340564)
    assert out["exchange_rate"] == 115250.0
    assert out["account_tier"] == 5
    # total_unit *کل* اعتبار است نه مصرف — usage نباید از آن پر شود.
    assert out["usage"] is None
    # نزدیک‌ترین انقضا بین گرنت و بسته (گرنت زودتر می‌سوزد)
    assert out["expires"] == "2025-10-01T00:00:00+00:00"
    # بسته‌ها: مبالغِ رشته‌ایِ سند به عدد تبدیل شده‌اند
    assert out["packages"] == [
        {
            "kind": "package",
            "id": "25",
            "name": "مدل‌های زبانی منتخب OpenAI روزانه پایه",
            "description": "٪۴۰ تخفیف در مدل‌های منتخب OpenAI",
            "amount_irt": 500000.0,
            "remaining_irt": 498447.15,
            "end_date": "2025-11-27T15:05:07.404882+00:00",
        }
    ]
    assert out["grants"][0]["remaining_irt"] == 8000.0
    # امنیت: کلید / payload خام در خروجی هست نباشد
    assert "raw" not in out
    assert out["error"] is None
    assert out["fetched_at"] is None  # فقط در مسیر fetch_billing پر می‌شود


def test_normalise_avalai_credit_tolerates_empty_sources() -> None:
    info = KNOWN_PROVIDERS["avalai"]
    out = _normalise_billing(info, {"credit_sources": {"grants": [], "packages": []}})
    assert out["available"] is True
    assert out["packages"] == [] and out["grants"] == []
    assert out["expires"] is None
    # اشتباه‌تایپ هم نباید ترک بزند
    out_ot = _normalise_billing(info, {"credit_sources": "not-a-dict"})
    assert out_ot["packages"] == []


def test_normalise_summary_matches_official_docs() -> None:
    out = _normalise_summary(AVALAI_SUMMARY_DOC_SAMPLE)
    assert out is not None
    assert out["transactions"] == 1011.0
    assert out["tokens_total"] == 31863.0
    assert out["cost_unit"] == pytest.approx(0.0146689)
    assert out["cost_paid_irt"] == pytest.approx(131.73)
    assert out["cost_paid_grant_irt"] == pytest.approx(1552.85)
    assert out["period_start"] == "2025-11-26T09:09:31.342Z"
    assert out["by_model"][0] == {
        "model": "gpt-5.4-mini",
        "transactions": 1000.0,
        "tokens": 30000.0,
        "cost_unit": pytest.approx(0.0135),
    }


def test_normalise_transactions_matches_official_docs() -> None:
    rows, total = _normalise_transactions(AVALAI_TRANSACTIONS_DOC_SAMPLE)
    assert total == 100.0
    assert rows == [
        {
            "id": "019ac1c0-9ff3-7663-b0b9-fbcf2461939a",
            "created_at": "2025-11-26T20:06:23.442Z",
            "model": "gpt-5.4-mini",
            "provider": "openai",
            "status_code": 200,
            "stream": False,
            "tokens_total": 30.0,
            "tokens_prompt": 10.0,
            "tokens_completion": 20.0,
        }
    ]


class _FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_fetch_billing_end_to_end_avalai(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full pipeline against mocked official-doc payloads, no network."""
    calls: list[str] = []

    def fake_get(url: str, *, headers: dict[str, str], **kwargs: Any) -> _FakeResponse:
        calls.append(url)
        assert headers["Authorization"] == "Bearer secret-key"
        if url.endswith("/user/v1/credit"):
            return _FakeResponse(
                AVALAI_CREDIT_DOC_SAMPLE,
                headers={
                    "x-ratelimit-limit-requests": "750",
                    "x-ratelimit-remaining-requests": "742",
                },
            )
        if url.endswith("/transactions/summary"):
            assert kwargs.get("params", {}).get("group_by") == "model"
            return _FakeResponse(AVALAI_SUMMARY_DOC_SAMPLE)
        if url.endswith("/transactions"):
            return _FakeResponse(AVALAI_TRANSACTIONS_DOC_SAMPLE)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("local_agent.llm.providers.requests.get", fake_get)
    out = fetch_billing("https://api.avalai.ir/v1", "secret-key")
    assert out["available"] is True
    assert out["balance"] == 742927.85
    assert out["account_tier"] == 5
    # Enrichment: مصرف ۲۴ ساعت + تراکنش‌های اخیر
    assert out["usage_24h"]["tokens_total"] == 31863.0
    assert out["usage_24h"]["by_model"][0]["model"] == "gpt-5.4-mini"
    assert out["transactions"][0]["tokens_total"] == 30.0
    assert out["transactions_total"] == 100.0
    assert out["rate_limit"] == {"limit": 750.0, "remaining": 742.0, "reset_seconds": None}
    assert out["fetched_at"] is not None
    assert out["error"] is None
    # کلید API هرگز در خروجی بازتاب داده نشود
    import json

    assert "secret-key" not in json.dumps(out, ensure_ascii=False)


def test_fetch_billing_telemetry_failure_does_not_break_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, *, headers: dict[str, str], **kwargs: Any) -> _FakeResponse:
        if url.endswith("/user/v1/credit"):
            return _FakeResponse(AVALAI_CREDIT_DOC_SAMPLE)
        return _FakeResponse({"error": "boom"}, status_code=500)

    monkeypatch.setattr("local_agent.llm.providers.requests.get", fake_get)
    out = fetch_billing("https://api.avalai.ir/v1", "key")
    assert out["available"] is True
    assert out["usage_24h"] is None
    assert out["transactions"] is None


@pytest.mark.parametrize(
    ("status", "expected_fragment"),
    [
        (401, "کلید API"),
        (403, "معلق یا غیرفعال"),
        (429, "محدودیت تعداد درخواست"),
        (500, "خطای 500"),
    ],
)
def test_fetch_billing_http_errors_are_readable_persian(
    monkeypatch: pytest.MonkeyPatch, status: int, expected_fragment: str
) -> None:
    def fake_get(url: str, *, headers: dict[str, str], **kwargs: Any) -> _FakeResponse:
        return _FakeResponse({"error": "x", "message": "y"}, status_code=status)

    monkeypatch.setattr("local_agent.llm.providers.requests.get", fake_get)
    out = fetch_billing("https://api.avalai.ir/v1", "bad-key")
    assert out["available"] is False
    assert expected_fragment in out["error"]


def test_fetch_billing_unavailable_is_best_effort() -> None:
    # No network / invalid endpoint should degrade to available=False, not raise.
    out = fetch_billing("https://nonexistent-provider.invalid/v1", "bad", provider_hint="")
    assert out["available"] is False
    assert out["balance"] is None
    assert "در دسترس نیست" in out["error"]
