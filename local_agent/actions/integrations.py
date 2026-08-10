"""Discord, Slack, and Notion integration actions.

Category 1: Third-party service integrations.

All use simple REST APIs with token-based auth:
  * Discord: Bot token (from Discord Developer Portal)
  * Slack: Bot/User token (from api.slack.com)
  * Notion: Integration token (from notion.so/my-integrations)
"""

from __future__ import annotations

import os
from typing import Any

import requests

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_integrations(registry: ActionRegistry, context: ActionContext) -> None:
    # ---- Discord ----
    for name, func, desc, params, required, rsk in (
        ("discord.status", discord_status, "وضعیت اتصال Discord bot. SAFE.", {}, (), Risk.SAFE),
        ("discord.list_guilds", discord_list_guilds, "لیست سرورهای Discord bot. SAFE.", {}, (), Risk.SAFE),
        ("discord.list_channels", discord_list_channels, "لیست کانال‌های یک سرور Discord. SAFE.",
         {"guild_id": {"type": "string"}}, ("guild_id",), Risk.SAFE),
        ("discord.get_messages", discord_get_messages, "خواندن پیام‌های یک کانال Discord. SAFE.",
         {"channel_id": {"type": "string"}, "limit": {"type": "integer"}}, ("channel_id",), Risk.SAFE),
        ("discord.send_message", discord_send_message, "ارسال پیام در کانال Discord. DESTRUCTIVE.",
         {"channel_id": {"type": "string"}, "text": {"type": "string"}}, ("channel_id", "text"), Risk.DESTRUCTIVE),
        ("discord.delete_message", discord_delete_message, "حذف پیام از کانال Discord. DESTRUCTIVE.",
         {"channel_id": {"type": "string"}, "message_id": {"type": "string"}}, ("channel_id", "message_id"), Risk.DESTRUCTIVE),
    ):
        registry.decorator(name=name, description=desc, parameters=params,
                           required=required, risk_level=rsk)(func)

    # ---- Slack ----
    for name, func, desc, params, required, rsk in (
        ("slack.status", slack_status, "وضعیت اتصال Slack. SAFE.", {}, (), Risk.SAFE),
        ("slack.list_channels", slack_list_channels, "لیست کانال‌های Slack. SAFE.",
         {"limit": {"type": "integer"}}, (), Risk.SAFE),
        ("slack.get_messages", slack_get_messages, "خواندن پیام‌های کانال Slack. SAFE.",
         {"channel": {"type": "string"}, "limit": {"type": "integer"}}, ("channel",), Risk.SAFE),
        ("slack.send_message", slack_send_message, "ارسال پیام در کانال Slack. DESTRUCTIVE.",
         {"channel": {"type": "string"}, "text": {"type": "string"}}, ("channel", "text"), Risk.DESTRUCTIVE),
    ):
        registry.decorator(name=name, description=desc, parameters=params,
                           required=required, risk_level=rsk)(func)

    # ---- Notion ----
    for name, func, desc, params, required, rsk in (
        ("notion.status", notion_status, "وضعیت اتصال Notion. SAFE.", {}, (), Risk.SAFE),
        ("notion.search", notion_search, "جست‌وجو در Notion workspace (pages/databases). SAFE.",
         {"query": {"type": "string"}, "limit": {"type": "integer"}}, ("query",), Risk.SAFE),
        ("notion.get_page", notion_get_page, "خواندن محتوای یک صفحه Notion. SAFE.",
         {"page_id": {"type": "string"}}, ("page_id",), Risk.SAFE),
        ("notion.create_page", notion_create_page, "ساخت صفحه جدید در Notion. DESTRUCTIVE.",
         {"parent_id": {"type": "string"}, "title": {"type": "string"},
          "content": {"type": "string"}}, ("parent_id", "title"), Risk.DESTRUCTIVE),
        ("notion.list_databases", notion_list_databases, "لیست دیتابیس‌های Notion. SAFE.",
         {"limit": {"type": "integer"}}, (), Risk.SAFE),
    ):
        registry.decorator(name=name, description=desc, parameters=params,
                           required=required, risk_level=rsk)(func)


# ===========================================================================
# Discord
# ===========================================================================


def _discord_token(context: ActionContext) -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        token = context.runtime.settings.extra.get("discord_bot_token", "")
    if not token:
        raise DependencyMissing(
            "Discord bot token is not configured",
            install_hint="DISCORD_BOT_TOKEN را در environment یا config.json (extra.discord_bot_token) تنظیم کنید.",
        )
    return token


def _discord_get(path: str, token: str, params: dict | None = None) -> Any:
    resp = requests.get(
        f"https://discord.com/api/v10{path}",
        headers={"Authorization": f"Bot {token}"},
        params=params, timeout=15,
    )
    if resp.status_code == 429:
        retry = resp.json().get("retry_after", 1)
        raise AssistantError(f"Discord rate limit. {retry} ثانیه بعد دوباره تلاش کنید.")
    resp.raise_for_status()
    return resp.json()


def _discord_post(path: str, token: str, data: dict) -> Any:
    resp = requests.post(
        f"https://discord.com/api/v10{path}",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        json=data, timeout=15,
    )
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def _discord_delete(path: str, token: str) -> None:
    resp = requests.delete(
        f"https://discord.com/api/v10{path}",
        headers={"Authorization": f"Bot {token}"},
        timeout=15,
    )
    resp.raise_for_status()


@risk(Risk.SAFE)
def discord_status(*, context: ActionContext) -> str:
    token = _discord_token(context)
    try:
        user = _discord_get("/users/@me", token)
        return (
            f"✅ Discord وصل است\n"
            f"  Bot: {user.get('username', '?')}#{user.get('discriminator', '')}\n"
            f"  ID: {user.get('id', '?')}"
        )
    except Exception as exc:
        return f"❌ Discord وصل نیست: {exc}"


@risk(Risk.SAFE)
def discord_list_guilds(*, context: ActionContext) -> str:
    token = _discord_token(context)
    guilds = _discord_get("/users/@me/guilds", token)
    if not guilds:
        return "سروری یافت نشد."
    lines = [f"🎮 {len(guilds)} سرور Discord:"]
    for g in guilds[:30]:
        lines.append(f"  • {g['name']} (id={g['id']}, members={g.get('approximate_member_count', '?')})")
    return "\n".join(lines)


@risk(Risk.SAFE)
def discord_list_channels(*, guild_id: str, context: ActionContext) -> str:
    token = _discord_token(context)
    channels = _discord_get(f"/guilds/{guild_id}/channels", token)
    text_channels = [c for c in channels if c.get("type") == 0]
    if not text_channels:
        return "کانال متنی یافت نشد."
    lines = [f"💬 {len(text_channels)} کانال متنی:"]
    for c in text_channels[:30]:
        lines.append(f"  • #{c['name']} (id={c['id']})")
    return "\n".join(lines)


@risk(Risk.SAFE)
def discord_get_messages(*, channel_id: str, limit: int = 20, context: ActionContext) -> str:
    token = _discord_token(context)
    msgs = _discord_get(f"/channels/{channel_id}/messages", token,
                        params={"limit": max(1, min(int(limit or 20), 50))})
    if not msgs:
        return "پیامی یافت نشد."
    lines = [f"💬 {len(msgs)} پیام:"]
    for m in msgs:
        author = m.get("author", {}).get("username", "?")
        text = (m.get("content", "") or "")[:200]
        ts = m.get("timestamp", "")[:16]
        lines.append(f"  [{ts}] @{author}: {text}")
    return "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def discord_send_message(*, channel_id: str, text: str, context: ActionContext) -> str:
    token = _discord_token(context)
    result = _discord_post(f"/channels/{channel_id}/messages", token,
                           {"content": str(text)})
    return f"✅ پیام ارسال شد (id={result.get('id', '?')})"


@risk(Risk.DESTRUCTIVE)
def discord_delete_message(*, channel_id: str, message_id: str, context: ActionContext) -> str:
    token = _discord_token(context)
    _discord_delete(f"/channels/{channel_id}/messages/{message_id}", token)
    return f"✅ پیام {message_id} حذف شد."


# ===========================================================================
# Slack
# ===========================================================================


def _slack_token(context: ActionContext) -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        token = context.runtime.settings.extra.get("slack_bot_token", "")
    if not token:
        raise DependencyMissing(
            "Slack token is not configured",
            install_hint="SLACK_BOT_TOKEN را در environment یا config.json تنظیم کنید.",
        )
    return token


def _slack_api(method: str, token: str, data: dict | None = None) -> dict:
    resp = requests.post(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=data or {}, timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise AssistantError(f"Slack error: {result.get('error', 'unknown')}")
    return result


@risk(Risk.SAFE)
def slack_status(*, context: ActionContext) -> str:
    token = _slack_token(context)
    try:
        result = _slack_api("auth.test", token)
        return (
            f"✅ Slack وصل است\n"
            f"  Team: {result.get('team', '?')}\n"
            f"  User: {result.get('user', '?')}\n"
            f"  URL: {result.get('url', '?')}"
        )
    except Exception as exc:
        return f"❌ Slack وصل نیست: {exc}"


@risk(Risk.SAFE)
def slack_list_channels(*, limit: int = 30, context: ActionContext) -> str:
    token = _slack_token(context)
    result = _slack_api("conversations.list", token, {
        "types": "public_channel,private_channel",
        "limit": max(1, min(int(limit or 30), 200)),
    })
    channels = result.get("channels", [])
    if not channels:
        return "کانالی یافت نشد."
    lines = [f"💬 {len(channels)} کانال Slack:"]
    for c in channels:
        icon = "🔒" if c.get("is_private") else "#"
        members = c.get("num_members", "?")
        lines.append(f"  {icon} {c['name']} (id={c['id']}, members={members})")
    return "\n".join(lines)


@risk(Risk.SAFE)
def slack_get_messages(*, channel: str, limit: int = 20, context: ActionContext) -> str:
    token = _slack_token(context)
    result = _slack_api("conversations.history", token, {
        "channel": channel,
        "limit": max(1, min(int(limit or 20), 100)),
    })
    messages = result.get("messages", [])
    if not messages:
        return "پیامی یافت نشد."
    lines = [f"💬 {len(messages)} پیام:"]
    for m in reversed(messages):
        user = m.get("user", "?")
        text = (m.get("text", "") or "")[:200]
        ts = m.get("ts", "")
        lines.append(f"  [{ts}] <@{user}>: {text}")
    return "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def slack_send_message(*, channel: str, text: str, context: ActionContext) -> str:
    token = _slack_token(context)
    _slack_api("chat.postMessage", token, {
        "channel": channel,
        "text": str(text),
    })
    return f"✅ پیام به {channel} ارسال شد."


# ===========================================================================
# Notion
# ===========================================================================


def _notion_token(context: ActionContext) -> str:
    token = os.environ.get("NOTION_API_KEY", "")
    if not token:
        token = context.runtime.settings.extra.get("notion_api_key", "")
    if not token:
        raise DependencyMissing(
            "Notion API key is not configured",
            install_hint="NOTION_API_KEY را تنظیم کنید. از notion.so/my-integrations بگیرید.",
        )
    return token


_NOTION_HEADERS_BASE = {
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def _notion_get(path: str, token: str, data: dict | None = None) -> dict:
    headers = {**_NOTION_HEADERS_BASE, "Authorization": f"Bearer {token}"}
    if data is not None:
        resp = requests.post(
            f"https://api.notion.com/v1{path}",
            headers=headers, json=data, timeout=15,
        )
    else:
        resp = requests.get(
            f"https://api.notion.com/v1{path}",
            headers=headers, timeout=15,
        )
    resp.raise_for_status()
    return resp.json()


def _notion_post(path: str, token: str, data: dict) -> dict:
    headers = {**_NOTION_HEADERS_BASE, "Authorization": f"Bearer {token}"}
    resp = requests.post(
        f"https://api.notion.com/v1{path}",
        headers=headers, json=data, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


@risk(Risk.SAFE)
def notion_status(*, context: ActionContext) -> str:
    token = _notion_token(context)
    try:
        result = _notion_get("/users/me", token)
        bot = result.get("name", "?")
        workspace = result.get("workspace_name", "?")
        return f"✅ Notion وصل است\n  Bot: {bot}\n  Workspace: {workspace}"
    except Exception as exc:
        return f"❌ Notion وصل نیست: {exc}"


@risk(Risk.SAFE)
def notion_search(*, query: str, limit: int = 10, context: ActionContext) -> str:
    token = _notion_token(context)
    result = _notion_get("/search", token, {
        "query": str(query),
        "page_size": max(1, min(int(limit or 10), 100)),
    })
    items = result.get("results", [])
    if not items:
        return f"نتیجه‌ای برای «{query}» یافت نشد."
    lines = [f"🔍 {len(items)} نتیجه:"]
    for item in items:
        obj_type = item.get("object", "?")
        title = ""
        if obj_type == "page":
            props = item.get("properties", {})
            for v in props.values():
                if v.get("type") == "title" and v.get("title"):
                    title = v["title"][0].get("plain_text", "")
                    break
            if not title:
                title = item.get("url", "?")
        elif obj_type == "database":
            title_parts = item.get("title", [])
            title = title_parts[0].get("plain_text", "") if title_parts else "?"
        lines.append(f"  • [{obj_type}] {title}\n    id={item.get('id', '?')}")
    return "\n".join(lines)


@risk(Risk.SAFE)
def notion_get_page(*, page_id: str, context: ActionContext) -> str:
    token = _notion_token(context)
    # Get page metadata
    page = _notion_get(f"/pages/{page_id}", token)
    # Get page content (blocks)
    blocks = _notion_get(f"/blocks/{page_id}/children", token)

    lines = [f"📄 صفحه Notion (id={page_id}):"]

    # Extract title
    props = page.get("properties", {})
    for v in props.values():
        if v.get("type") == "title" and v.get("title"):
            title = v["title"][0].get("plain_text", "")
            lines.append(f"  عنوان: {title}")
            break

    lines.append(f"  URL: {page.get('url', '?')}")
    lines.append(f"  ایجاد: {page.get('created_time', '?')[:10]}")

    # Extract text from blocks
    block_list = blocks.get("results", [])
    text_parts = []
    for block in block_list[:50]:
        btype = block.get("type", "")
        bdata = block.get(btype, {})
        if "rich_text" in bdata:
            texts = [rt.get("plain_text", "") for rt in bdata["rich_text"]]
            text = "".join(texts)
            if text.strip():
                prefix = {"paragraph": "", "heading_1": "# ", "heading_2": "## ",
                          "heading_3": "### ", "bulleted_list_item": "• ",
                          "numbered_list_item": "• ", "quote": "> ",
                          "to_do": "☐ " if not bdata.get("checked") else "☑ "
                          }.get(btype, "")
                text_parts.append(f"  {prefix}{text}")

    if text_parts:
        lines.append("\n" + "\n".join(text_parts[:100]))
    else:
        lines.append("  (محتوای متنی یافت نشد)")

    return "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def notion_create_page(*, parent_id: str, title: str,
                       content: str = "", context: ActionContext) -> str:
    token = _notion_token(context)

    # Determine parent type (page or database)
    parent_type = "page_id"  # default
    try:
        check = _notion_get(f"/databases/{parent_id}", token)
        if check.get("object") == "database":
            parent_type = "database_id"
    except Exception:
        pass

    body: dict[str, Any] = {
        "parent": {parent_type: parent_id},
        "properties": {},
    }

    if parent_type == "database_id":
        body["properties"]["title"] = {
            "title": [{"text": {"content": str(title)}}]
        }
    else:
        body["properties"]["title"] = {
            "title": [{"text": {"content": str(title)}}]
        }

    # Add content blocks
    children = []
    if content:
        for paragraph in str(content).split("\n\n"):
            paragraph = paragraph.strip()
            if paragraph:
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": paragraph[:2000]}}]
                    }
                })
    if children:
        body["children"] = children

    result = _notion_post("/pages", token, body)
    page_url = result.get("url", "?")
    return f"✅ صفحه ساخته شد: {title}\n   {page_url}"


@risk(Risk.SAFE)
def notion_list_databases(*, limit: int = 20, context: ActionContext) -> str:
    token = _notion_token(context)
    result = _notion_get("/search", token, {
        "filter": {"value": "database", "property": "object"},
        "page_size": max(1, min(int(limit or 20), 100)),
    })
    dbs = result.get("results", [])
    if not dbs:
        return "دیتابیسی یافت نشد."
    lines = [f"🗃️ {len(dbs)} دیتابیس Notion:"]
    for db in dbs:
        title_parts = db.get("title", [])
        title = title_parts[0].get("plain_text", "?") if title_parts else "?"
        props = list(db.get("properties", {}).keys())[:5]
        lines.append(f"  • {title} (id={db.get('id', '?')})")
        if props:
            lines.append(f"    ستون‌ها: {', '.join(props)}")
    return "\n".join(lines)
