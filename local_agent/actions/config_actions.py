"""Configuration actions: persist settings from inside a chat run.

``config_set`` lets the agent write values (e.g. Telegram credentials)
into ``config.json`` at the user's request — the same place the web UI
and CLI write.  Values are validated through :class:`AssistantSettings`
so a bad payload can never corrupt the file, and the write is atomic.

Security rules enforced here:

* the API key / hash / password values are never echoed back in the
  tool output (only the field name);
* secret-looking paths (``*.api_key``, ``api_hash``, ``*password*``,
  ``*token*``) are logged/returned redacted;
* the action only touches the assistant's own ``config.json``.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import AssistantError
from .registry import ActionContext, ActionRegistry, risk, Risk

# Field names whose values must never appear in action output or logs.
_SECRET_SUFFIXES = ("api_key", "api_hash", "password", "token", "secret")


def register_config(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="config_set",
        description=(
            "مقدار یک تنظیم را در config.json ذخیره می‌کند و بلافاصله اعمال می‌کند. "
            "path به شکل نقطه‌چین است، مثل: telegram.api_id ، telegram.api_hash ، "
            "telegram.phone ، telegram.enabled ، work_dir ، llm.provider ، llm.openai_api_key. "
            "مقادیر محرمانه (api_key/api_hash) هرگز در خروجی چاپ نمی‌شوند. SAFE."
        ),
        parameters={
            "path": {"type": "string", "description": "مسیر نقطه‌چین تنظیم"},
            "value": {"description": "مقدار جدید (رشته، عدد یا بولین)"},
        },
        required=("path", "value"),
    )(config_set)


def _is_secret(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(suffix) or f".{suffix}" in lowered for suffix in _SECRET_SUFFIXES)


def _coerce(path: str, value: Any, current: Any) -> Any:
    """Coerce a raw value to the type of the currently stored one."""
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes", "on", "بله"}
    if isinstance(current, int):
        return int(str(value).strip())
    if isinstance(current, float):
        return float(str(value).strip())
    return str(value)


@risk(Risk.SAFE)
def config_set(*, path: str, value: Any, context: ActionContext) -> str:
    if not isinstance(path, str) or not path.strip():
        raise AssistantError("path must be a non-empty string")
    normalized = path.strip().strip(".")
    if not normalized or any(part == "" for part in normalized.split(".")):
        raise AssistantError(f"مسیر تنظیم نامعتبر است: {path!r}")

    owner = context.extra.get("settings_owner")
    if owner is None:
        raise AssistantError(
            "ذخیرهٔ تنظیمات در این محیط در دسترس نیست (settings_owner یافت نشد)"
        )

    owner.apply_config_set(normalized, value)
    if _is_secret(normalized):
        return f"✅ تنظیم «{normalized}» ذخیره شد (مقدار محرمانه است و نمایش داده نمی‌شود)."
    return f"✅ تنظیم «{normalized}» ذخیره شد و در config.json ثبت گردید."
