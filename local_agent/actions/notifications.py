"""Push notification and smart home actions.

  * ntfy: Free push notification service (ntfy.sh)
  * Pushbullet: Push notifications to phone/desktop
  * Home Assistant: Smart home device control
"""

from __future__ import annotations

import os
from typing import Any

import requests

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_notifications(registry: ActionRegistry, context: ActionContext) -> None:
    # ---- Push Notifications ----
    registry.decorator(
        name="push_notification",
        description=(
            "ارسال push notification به گوشی/دسکتاپ از طریق ntfy.sh (رایگان، بدون API key). "
            "اختیاری: topic برای فیلتر. SAFE (فقط ارسال)."
        ),
        parameters={
            "message": {"type": "string"},
            "title": {"type": "string"},
            "topic": {"type": "string", "description": "موضوع ntfy (پیش‌فرض از config)"},
            "priority": {"type": "string", "enum": ["min", "low", "default", "high", "urgent"]},
        },
        required=("message",),
    )(push_notification)

    registry.decorator(
        name="pushbullet_send",
        description="ارسال push به Pushbullet (نیاز به API key). DESTRUCTIVE.",
        parameters={
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        required=("body",),
        risk_level=Risk.DESTRUCTIVE,
    )(pushbullet_send)

    # ---- Home Assistant ----
    registry.decorator(
        name="hass_status",
        description="وضعیت اتصال Home Assistant و لیست domain ها. SAFE.",
        parameters={},
    )(hass_status)

    registry.decorator(
        name="hass_list_entities",
        description="لیست entity های Home Assistant (با فیلتر domain). SAFE.",
        parameters={
            "domain": {"type": "string", "description": "فیلتر domain (light, switch, sensor, ...)"},
            "limit": {"type": "integer"},
        },
    )(hass_list_entities)

    registry.decorator(
        name="hass_get_state",
        description="گرفتن وضعیت یک entity از Home Assistant. SAFE.",
        parameters={
            "entity_id": {"type": "string", "description": "مثلاً light.bedroom یا switch.tv"},
        },
        required=("entity_id",),
    )(hass_get_state)

    registry.decorator(
        name="hass_call_service",
        description="اجرای یک service در Home Assistant (مثلاً روشن/خاموش کردن). DESTRUCTIVE.",
        parameters={
            "domain": {"type": "string"},
            "service": {"type": "string"},
            "entity_id": {"type": "string"},
            "data": {"type": "object", "description": "پارامترهای اضافی (اختیاری)"},
        },
        required=("domain", "service", "entity_id"),
        risk_level=Risk.DESTRUCTIVE,
    )(hass_call_service)


# ===========================================================================
# ntfy (free push)
# ===========================================================================


@risk(Risk.SAFE)
def push_notification(*, message: str, title: str = "",
                      topic: str = "", priority: str = "default",
                      context: ActionContext) -> str:
    """Send a push notification via ntfy.sh (free, no API key needed)."""
    text = str(message).strip()
    if not text:
        raise AssistantError("متن پیام خالی است")

    # Topic: from arg > env > config > default
    t = str(topic or "").strip()
    if not t:
        t = os.environ.get("NTFY_TOPIC", "")
    if not t:
        t = context.runtime.settings.extra.get("ntfy_topic", "")
    if not t:
        t = "local-assistant"

    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

    headers: dict[str, str] = {}
    ttl = str(title or "").strip()
    if ttl:
        headers["Title"] = ttl
    prio = str(priority or "default").strip()
    if prio in ("min", "low", "default", "high", "urgent"):
        headers["Priority"] = prio

    # Auth token (optional, for private topics)
    ntfy_token = os.environ.get("NTFY_TOKEN", "")
    if not ntfy_token:
        ntfy_token = context.runtime.settings.extra.get("ntfy_token", "")
    if ntfy_token:
        headers["Authorization"] = f"Bearer {ntfy_token}"

    try:
        resp = requests.post(
            f"{server.rstrip('/')}/{t}",
            data=text.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        raise AssistantError(f"ارسال notification ناموفق بود: {exc}")

    return f"🔔 Notification ارسال شد (topic: {t})\n  {ttl + ': ' if ttl else ''}{text[:200]}"


# ===========================================================================
# Pushbullet
# ===========================================================================


def _pushbullet_token(context: ActionContext) -> str:
    token = os.environ.get("PUSHBULLET_API_KEY", "")
    if not token:
        token = context.runtime.settings.extra.get("pushbullet_api_key", "")
    if not token:
        raise DependencyMissing(
            "Pushbullet API key is not configured",
            install_hint="PUSHBULLET_API_KEY را تنظیم کنید. از pushbullet.com/account بگیرید.",
        )
    return token


@risk(Risk.DESTRUCTIVE)
def pushbullet_send(*, title: str = "", body: str, context: ActionContext) -> str:
    token = _pushbullet_token(context)
    data: dict[str, Any] = {
        "type": "note",
        "body": str(body),
    }
    ttl = str(title or "").strip()
    if ttl:
        data["title"] = ttl

    try:
        resp = requests.post(
            "https://api.pushbullet.com/v2/pushes",
            headers={"Access-Token": token},
            json=data,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        raise AssistantError(f"Pushbullet ناموفق بود: {exc}")

    return f"🔔 Push ارسال شد: {ttl + ': ' if ttl else ''}{body[:200]}"


# ===========================================================================
# Home Assistant
# ===========================================================================


def _hass_config(context: ActionContext) -> tuple[str, str]:
    """Return (base_url, token) for Home Assistant."""
    base = os.environ.get("HASS_BASE_URL", "")
    if not base:
        base = context.runtime.settings.extra.get("hass_base_url", "")
    token = os.environ.get("HASS_TOKEN", "")
    if not token:
        token = context.runtime.settings.extra.get("hass_token", "")
    if not base or not token:
        raise DependencyMissing(
            "Home Assistant is not configured",
            install_hint="HASS_BASE_URL و HASS_TOKEN را تنظیم کنید. "
                         "Token از Home Assistant → Profile → Long-Lived Access Tokens.",
        )
    return base.rstrip("/"), token


def _hass_get(path: str, base: str, token: str) -> Any:
    resp = requests.get(
        f"{base}/api/{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _hass_post(path: str, base: str, token: str, data: dict) -> Any:
    resp = requests.post(
        f"{base}/api/{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=data,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json() if resp.text else {}


@risk(Risk.SAFE)
def hass_status(*, context: ActionContext) -> str:
    base, token = _hass_config(context)
    try:
        config = _hass_get("config", base, token)
        states = _hass_get("states", base, token)
    except Exception as exc:
        return f"❌ Home Assistant وصل نیست: {exc}"

    domains: dict[str, int] = {}
    for state in states:
        domain = state["entity_id"].split(".")[0]
        domains[domain] = domains.get(domain, 0) + 1

    lines = [
        f"🏠 Home Assistant وصل است",
        f"  نسخه: {config.get('version', '?')}",
        f"  مکان: {config.get('location_name', '?')}",
        f"  منطقه زمانی: {config.get('time_zone', '?')}",
        f"  Entities: {len(states)}",
        f"  Domains: {', '.join(f'{d}:{c}' for d, c in sorted(domains.items()))}",
    ]
    return "\n".join(lines)


@risk(Risk.SAFE)
def hass_list_entities(*, domain: str = "", limit: int = 50,
                       context: ActionContext) -> str:
    base, token = _hass_config(context)
    states = _hass_get("states", base, token)

    filt = str(domain or "").strip().lower()
    if filt:
        states = [s for s in states if s["entity_id"].startswith(f"{filt}.")]

    max_items = max(1, min(int(limit or 50), 200))
    lines = [f"🏠 {len(states)} entity{' (فیلتر: ' + filt + ')' if filt else ''}:"]
    for s in states[:max_items]:
        eid = s["entity_id"]
        name = s.get("attributes", {}).get("friendly_name", eid)
        state = s.get("state", "?")
        unit = s.get("attributes", {}).get("unit_of_measurement", "")
        state_str = f"{state} {unit}".strip()
        lines.append(f"  • {name} ({eid}) = {state_str}")
    if len(states) > max_items:
        lines.append(f"  … و {len(states) - max_items} entity دیگر")
    return "\n".join(lines)


@risk(Risk.SAFE)
def hass_get_state(*, entity_id: str, context: ActionContext) -> str:
    base, token = _hass_config(context)
    try:
        state = _hass_get(f"states/{entity_id}", base, token)
    except Exception as exc:
        raise AssistantError(f"Entity یافت نشد: {exc}")

    name = state.get("attributes", {}).get("friendly_name", entity_id)
    value = state.get("state", "?")
    attrs = state.get("attributes", {})
    unit = attrs.get("unit_of_measurement", "")

    lines = [f"🏠 {name} ({entity_id}):"]
    lines.append(f"  وضعیت: {value} {unit}".strip())
    # Show interesting attributes
    skip = {"friendly_name", "icon", "supported_features", "unit_of_measurement"}
    for key, val in attrs.items():
        if key not in skip and val is not None and str(val).strip():
            lines.append(f"  {key}: {str(val)[:100]}")
    return "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def hass_call_service(*, domain: str, service: str, entity_id: str,
                      data: dict | None = None, context: ActionContext) -> str:
    base, token = _hass_config(context)
    payload: dict[str, Any] = {"entity_id": entity_id}
    if data:
        payload.update(data)

    try:
        _hass_post(f"services/{domain}/{service}", base, token, payload)
    except Exception as exc:
        raise AssistantError(f"Service call ناموفق بود: {exc}")

    return f"✅ {domain}.{service} اجرا شد روی {entity_id}"
