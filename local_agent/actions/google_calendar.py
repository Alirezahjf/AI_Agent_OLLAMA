"""Google Calendar integration actions.

Uses the Google Calendar API v3 with OAuth2 (installed-app flow)
or a simple API key for read-only access.

Auth methods:
  1. OAuth2: credentials.json (Desktop app) → calendar_token.json
  2. API Key: GOOGLE_CALENDAR_API_KEY for read-only public calendars

All read actions are SAFE; write actions are DESTRUCTIVE.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_google_calendar(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="calendar.status",
        description="وضعیت اتصال Google Calendar. SAFE.",
        parameters={},
    )(calendar_status)

    registry.decorator(
        name="calendar.list_events",
        description=(
            "لیست رویدادهای پیش‌رو در Google Calendar. "
            "فیلتر: تعداد روز آینده (پیش‌فرض 7). SAFE."
        ),
        parameters={
            "days": {"type": "integer", "description": "تعداد روز آینده (پیش‌فرض 7)"},
            "max_results": {"type": "integer"},
            "calendar_id": {"type": "string", "description": "شناسه تقویم (پیش‌فرض: primary)"},
        },
    )(calendar_list_events)

    registry.decorator(
        name="calendar.get_event",
        description="جزئیات یک رویداد خاص. SAFE.",
        parameters={
            "event_id": {"type": "string"},
            "calendar_id": {"type": "string"},
        },
        required=("event_id",),
    )(calendar_get_event)

    registry.decorator(
        name="calendar.create_event",
        description=(
            "ساخت رویداد جدید در Google Calendar. DESTRUCTIVE."
        ),
        parameters={
            "summary": {"type": "string", "description": "عنوان رویداد"},
            "start": {"type": "string", "description": "زمان شروع (ISO 8601: 2026-08-15T10:00:00)"},
            "end": {"type": "string", "description": "زمان پایان (ISO 8601)"},
            "description": {"type": "string"},
            "location": {"type": "string"},
            "calendar_id": {"type": "string"},
        },
        required=("summary", "start", "end"),
        risk_level=Risk.DESTRUCTIVE,
    )(calendar_create_event)

    registry.decorator(
        name="calendar.delete_event",
        description="حذف یک رویداد از تقویم. DESTRUCTIVE.",
        parameters={
            "event_id": {"type": "string"},
            "calendar_id": {"type": "string"},
        },
        required=("event_id",),
        risk_level=Risk.DESTRUCTIVE,
    )(calendar_delete_event)

    registry.decorator(
        name="calendar.list_calendars",
        description="لیست تقویم‌های کاربر. SAFE.",
        parameters={},
    )(calendar_list_calendars)

    registry.decorator(
        name="calendar.connect",
        description=(
            "شروع اتصال OAuth به Google Calendar. یک URL و کد نمایش می‌دهد. "
            "بعد از authorize در مرورگر، calendar.verify را صدا بزنید. DESTRUCTIVE."
        ),
        parameters={
            "client_id": {"type": "string", "description": "OAuth Client ID"},
            "client_secret": {"type": "string", "description": "OAuth Client Secret"},
        },
        required=("client_id", "client_secret"),
        risk_level=Risk.DESTRUCTIVE,
    )(calendar_connect)

    registry.decorator(
        name="calendar.verify",
        description="تکمیل اتصال OAuth با authorization code. DESTRUCTIVE.",
        parameters={
            "code": {"type": "string", "description": "Authorization code از مرورگر"},
            "client_id": {"type": "string"},
            "client_secret": {"type": "string"},
            "redirect_uri": {"type": "string", "description": "مثلاً urn:ietf:wg:oauth:2.0:oob"},
        },
        required=("code", "client_id", "client_secret"),
        risk_level=Risk.DESTRUCTIVE,
    )(calendar_verify)


# ===========================================================================
# Auth helpers
# ===========================================================================


def _get_calendar_token(context: ActionContext) -> str:
    """Get an access token for Google Calendar API."""
    # Check env first
    token = os.environ.get("GOOGLE_CALENDAR_TOKEN", "")
    if token:
        return token

    # Check API key (read-only)
    api_key = os.environ.get("GOOGLE_CALENDAR_API_KEY", "")
    if not api_key:
        api_key = context.runtime.settings.extra.get("google_calendar_api_key", "")
    if api_key:
        return f"key:{api_key}"

    # Check OAuth token file
    data_dir = context.runtime.settings.data_dir
    token_file = data_dir / "calendar_token.json"
    if token_file.is_file():
        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
            token = data.get("access_token", "")
            if token:
                return token
        except Exception:
            pass

    raise DependencyMissing(
        "Google Calendar is not configured",
        install_hint=(
            "یکی از این‌ها را تنظیم کنید:\n"
            "  1. GOOGLE_CALENDAR_TOKEN (access token)\n"
            "  2. GOOGLE_CALENDAR_API_KEY (فقط خواندنی)\n"
            "  3. فایل calendar_token.json در data_dir"
        ),
    )


def _cal_get(path: str, auth: str, params: dict | None = None) -> dict:
    headers: dict[str, str] = {}
    extra_params = dict(params or {})
    if auth.startswith("key:"):
        extra_params["key"] = auth[4:]
    else:
        headers["Authorization"] = f"Bearer {auth}"

    resp = requests.get(
        f"https://www.googleapis.com/calendar/v3{path}",
        headers=headers, params=extra_params, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _cal_post(path: str, auth: str, data: dict) -> dict:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    extra_params: dict[str, str] = {}
    if auth.startswith("key:"):
        extra_params["key"] = auth[4:]
    else:
        headers["Authorization"] = f"Bearer {auth}"

    resp = requests.post(
        f"https://www.googleapis.com/calendar/v3{path}",
        headers=headers, params=extra_params, json=data, timeout=15,
    )
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def _cal_delete(path: str, auth: str) -> None:
    headers: dict[str, str] = {}
    extra_params: dict[str, str] = {}
    if auth.startswith("key:"):
        extra_params["key"] = auth[4:]
    else:
        headers["Authorization"] = f"Bearer {auth}"

    resp = requests.delete(
        f"https://www.googleapis.com/calendar/v3{path}",
        headers=headers, params=extra_params, timeout=15,
    )
    resp.raise_for_status()


# ===========================================================================
# Implementations
# ===========================================================================


@risk(Risk.SAFE)
def calendar_status(*, context: ActionContext) -> str:
    auth = _get_calendar_token(context)
    try:
        data = _cal_get("/users/me/calendarList", auth)
        calendars = data.get("items", [])
        return (
            f"✅ Google Calendar وصل است\n"
            f"  تعداد تقویم‌ها: {len(calendars)}"
        )
    except Exception as exc:
        return f"❌ Google Calendar وصل نیست: {exc}"


@risk(Risk.SAFE)
def calendar_list_events(*, days: int = 7, max_results: int = 20,
                         calendar_id: str = "primary",
                         context: ActionContext) -> str:
    auth = _get_calendar_token(context)
    cal_id = str(calendar_id or "primary").strip()
    d = max(1, min(int(days or 7), 365))
    limit = max(1, min(int(max_results or 20), 250))

    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(days=d)).isoformat() + "Z"

    try:
        data = _cal_get(f"/calendars/{cal_id}/events", auth, {
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": limit,
            "singleEvents": "true",
            "orderBy": "startTime",
        })
    except Exception as exc:
        raise AssistantError(f"دریافت رویدادها ناموفق بود: {exc}")

    events = data.get("items", [])
    if not events:
        return f"رویدادی در {d} روز آینده نیست."

    lines = [f"📅 {len(events)} رویداد در {d} روز آینده:"]
    for e in events:
        summary = e.get("summary", "(بدون عنوان)")
        start = e.get("start", {})
        if "dateTime" in start:
            dt = start["dateTime"][:16].replace("T", " ")
        elif "date" in start:
            dt = start["date"] + " (تمام روز)"
        else:
            dt = "?"
        location = e.get("location", "")
        status = e.get("status", "")
        lines.append(f"  • {dt} — {summary}")
        if location:
            lines.append(f"    📍 {location}")
    return "\n".join(lines)


@risk(Risk.SAFE)
def calendar_get_event(*, event_id: str, calendar_id: str = "primary",
                       context: ActionContext) -> str:
    auth = _get_calendar_token(context)
    cal_id = str(calendar_id or "primary").strip()

    try:
        e = _cal_get(f"/calendars/{cal_id}/events/{event_id}", auth)
    except Exception as exc:
        raise AssistantError(f"رویداد پیدا نشد: {exc}")

    summary = e.get("summary", "(بدون عنوان)")
    start = e.get("start", {})
    end = e.get("end", {})
    description = e.get("description", "")
    location = e.get("location", "")
    status = e.get("status", "")
    organizer = e.get("organizer", {}).get("displayName", "")
    attendees = e.get("attendees", [])

    start_str = start.get("dateTime", start.get("date", "?"))
    end_str = end.get("dateTime", end.get("date", "?"))

    lines = [
        f"📅 {summary}",
        f"  شروع: {start_str}",
        f"  پایان: {end_str}",
        f"  وضعیت: {status}",
    ]
    if location:
        lines.append(f"  📍 مکان: {location}")
    if organizer:
        lines.append(f"  👤 برگزارکننده: {organizer}")
    if description:
        lines.append(f"  توضیحات: {description[:500]}")
    if attendees:
        att_names = [a.get("displayName", a.get("email", "?")) for a in attendees[:10]]
        lines.append(f"  شرکت‌کنندگان: {', '.join(att_names)}")
    lines.append(f"  ID: {e.get('id', '?')}")
    return "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def calendar_create_event(*, summary: str, start: str, end: str,
                          description: str = "", location: str = "",
                          calendar_id: str = "primary",
                          context: ActionContext) -> str:
    auth = _get_calendar_token(context)
    cal_id = str(calendar_id or "primary").strip()

    body: dict[str, Any] = {
        "summary": str(summary),
        "start": _parse_datetime(str(start)),
        "end": _parse_datetime(str(end)),
    }
    if description:
        body["description"] = str(description)
    if location:
        body["location"] = str(location)

    try:
        result = _cal_post(f"/calendars/{cal_id}/events", auth, body)
    except Exception as exc:
        raise AssistantError(f"ساخت رویداد ناموفق بود: {exc}")

    return (
        f"✅ رویداد ساخته شد: {summary}\n"
        f"  ID: {result.get('id', '?')}\n"
        f"  Link: {result.get('htmlLink', '?')}"
    )


@risk(Risk.DESTRUCTIVE)
def calendar_delete_event(*, event_id: str, calendar_id: str = "primary",
                          context: ActionContext) -> str:
    auth = _get_calendar_token(context)
    cal_id = str(calendar_id or "primary").strip()

    try:
        _cal_delete(f"/calendars/{cal_id}/events/{event_id}", auth)
    except Exception as exc:
        raise AssistantError(f"حذف رویداد ناموفق بود: {exc}")

    return f"✅ رویداد {event_id} حذف شد."


@risk(Risk.SAFE)
def calendar_list_calendars(*, context: ActionContext) -> str:
    auth = _get_calendar_token(context)
    try:
        data = _cal_get("/users/me/calendarList", auth)
    except Exception as exc:
        raise AssistantError(f"دریافت تقویم‌ها ناموفق بود: {exc}")

    calendars = data.get("items", [])
    if not calendars:
        return "تقویمی یافت نشد."

    lines = [f"📅 {len(calendars)} تقویم:"]
    for cal in calendars:
        name = cal.get("summary", "?")
        cal_id = cal.get("id", "?")
        primary = " ⭐" if cal.get("primary") else ""
        access = cal.get("accessRole", "?")
        lines.append(f"  • {name}{primary} (id={cal_id}, {access})")
    return "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def calendar_connect(*, client_id: str, client_secret: str,
                     context: ActionContext) -> str:
    """Start OAuth2 flow for Google Calendar."""
    import urllib.parse

    cid = str(client_id).strip()
    if not cid:
        raise AssistantError("client_id خالی است")

    redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    scope = "https://www.googleapis.com/auth/calendar"

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode({
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent",
        })
    )

    # Store client_secret temporarily for the verify step
    data_dir = context.runtime.settings.data_dir
    secret_file = data_dir / "_cal_oauth_pending.json"
    secret_file.write_text(json.dumps({
        "client_id": cid,
        "client_secret": str(client_secret).strip(),
        "redirect_uri": redirect_uri,
    }), encoding="utf-8")

    return (
        f"🔗 اتصال Google Calendar:\n"
        f"  ۱. این URL را در مرورگر باز کنید:\n"
        f"  {auth_url}\n\n"
        f"  ۲. Authorize کنید و کد دریافتی را کپی کنید\n"
        f"  ۳. calendar.verify(code='کد') را صدا بزنید"
    )


@risk(Risk.DESTRUCTIVE)
def calendar_verify(*, code: str, client_id: str, client_secret: str,
                    redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob",
                    context: ActionContext) -> str:
    """Complete OAuth2 flow and save token."""
    auth_code = str(code).strip()
    if not auth_code:
        raise AssistantError("code خالی است")

    # Try to load pending secrets
    data_dir = context.runtime.settings.data_dir
    secret_file = data_dir / "_cal_oauth_pending.json"
    cid = str(client_id).strip()
    csec = str(client_secret).strip()
    ruri = str(redirect_uri or "urn:ietf:wg:oauth:2.0:oob").strip()

    if (not cid or not csec) and secret_file.is_file():
        try:
            pending = json.loads(secret_file.read_text(encoding="utf-8"))
            cid = cid or pending.get("client_id", "")
            csec = csec or pending.get("client_secret", "")
            ruri = ruri or pending.get("redirect_uri", ruri)
        except Exception:
            pass

    if not cid or not csec:
        raise AssistantError("client_id و client_secret لازم است.")

    # Exchange code for tokens
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": auth_code,
                "client_id": cid,
                "client_secret": csec,
                "redirect_uri": ruri,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
        resp.raise_for_status()
        token_data = resp.json()
    except requests.RequestException as exc:
        raise AssistantError(f"Exchange code ناموفق: {exc}")

    # Save token
    token_file = data_dir / "calendar_token.json"
    token_file.write_text(json.dumps(token_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Clean up pending file
    secret_file.unlink(missing_ok=True)

    # Verify by listing calendars
    try:
        access_token = token_data.get("access_token", "")
        cal_data = _cal_get("/users/me/calendarList", access_token)
        count = len(cal_data.get("items", []))
    except Exception:
        count = "?"

    return (
        f"✅ Google Calendar وصل شد!\n"
        f"  Token ذخیره شد: {token_file}\n"
        f"  تقویم‌ها: {count}\n"
        f"  حالا می‌توانید از calendar.list_events و بقیه ابزارها استفاده کنید."
    )


# ===========================================================================
# Helpers
# ===========================================================================


def _parse_datetime(value: str) -> dict[str, str]:
    """Parse a datetime string into Google Calendar format."""
    value = value.strip()
    # Already ISO format with T
    if "T" in value:
        if not value.endswith("Z") and "+" not in value:
            # Assume Tehran timezone
            value = value + "+03:30"
        return {"dateTime": value, "timeZone": "Asia/Tehran"}
    # Date only (all-day event)
    if len(value) == 10:
        return {"date": value}
    # Try to parse common formats
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            dt = datetime.strptime(value, fmt)
            return {"dateTime": dt.isoformat() + "+03:30", "timeZone": "Asia/Tehran"}
        except ValueError:
            continue
    # Last resort: return as-is
    return {"dateTime": value, "timeZone": "Asia/Tehran"}
