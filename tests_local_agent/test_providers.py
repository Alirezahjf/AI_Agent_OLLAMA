"""Tests for provider auto-detection and billing normalisation."""

from __future__ import annotations

import pytest

from local_agent.llm.providers import (
    _normalise_billing,
    detect_provider,
    fetch_billing,
)


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


def test_normalise_avalai_credit() -> None:
    raw = {
        "limit": 0.0,
        "remaining_irt": 742927.85,
        "remaining_unit": 0.0,
        "total_unit": 6.44,
        "exchange_rate": 115250,
        "account_tier": 5,
        "credit_sources": {"packages": [{"end_date": "2025-11-27T00:00:00+00:00"}]},
    }
    info = detect_provider("https://api.avalai.ir/v1", "sk-x")
    out = _normalise_billing(info, raw)
    assert out["available"] is True
    assert out["balance"] == 742927.85
    assert out["currency"] == "IRT"
    assert out["expires"] == "2025-11-27T00:00:00+00:00"


def test_fetch_billing_unavailable_is_best_effort() -> None:
    # No network / invalid endpoint should degrade to available=False, not raise.
    out = fetch_billing("https://nonexistent-provider.invalid/v1", "bad", provider_hint="")
    assert out["available"] is False
    assert out["balance"] is None
