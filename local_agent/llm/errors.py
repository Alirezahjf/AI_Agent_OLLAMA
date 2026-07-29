"""LLM-layer exceptions.

User-facing messages are intentionally short and actionable; the
underlying cause is preserved on ``__cause__`` for logging.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMTimeout(LLMError):
    """The provider took longer than the configured timeout."""


class LLMRateLimit(LLMError):
    """Provider asked us to slow down (HTTP 429)."""
