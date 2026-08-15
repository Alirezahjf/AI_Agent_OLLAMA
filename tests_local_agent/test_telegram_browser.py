"""Direct Web/Desktop Telegram browser endpoints and UI contract."""

from __future__ import annotations

from datetime import UTC, datetime

import requests

from local_agent.telegram.client import Chat, Message


class _LiveTelegram:
    is_connected = True

    def list_chats(self, limit=50, kind="all", query="", sort="recent", *, offset=0,
                   archived=None, unread_only=False):
        items = [
            Chat(
                id=10, title="Alice", username="alice", is_group=False,
                is_private=True, unread_count=2,
                last_message_date=datetime(2026, 8, 15, tzinfo=UTC),
            ),
            Chat(id=-10020, title="Python", username="python", is_group=True,
                 is_supergroup=True),
        ]
        if kind != "all":
            items = [item for item in items if item.kind == kind or (kind == "group" and item.is_group)]
        if query:
            items = [item for item in items if query.lower() in item.title.lower()]
        if archived is not None:
            items = [item for item in items if item.archived is archived]
        if unread_only:
            items = [item for item in items if item.unread_count > 0]
        return items[offset:offset + limit]

    def list_contacts(self, limit=100):
        return [{"id": 10, "name": "Alice", "first_name": "Alice", "last_name": "",
                 "username": "alice", "phone": "+100", "is_mutual_contact": True}][:limit]

    def search_contacts(self, query, limit=100):
        return self.list_contacts(limit) if "ali" in query.lower() else []

    def refresh_summary(self):
        return {"total_chats": 2, "private_chats": 1, "group_chats": 0,
                "supergroup_chats": 1, "channel_chats": 0, "bot_chats": 0,
                "unread_chats": 1, "total_unread": 2, "total_contacts": 1,
                "source": "live", "refreshed_at": "2026-08-15T12:00:00+00:00"}

    def get_chat_history(self, target, limit=50, offset_id=0):
        return [Message(id=5, chat_id=10, sender="alice", text="hello",
                        date=datetime(2026, 8, 15, tzinfo=UTC), is_outgoing=False,
                        sender_id=10)]

    def resolve_target(self, target):
        return {"id": 10, "raw_id": 10, "name": "Alice", "username": "alice",
                "phone": "+100", "kind": "private", "is_bot": False,
                "verified": False, "deleted": False}


def _install_fake(web_server):
    handlers = web_server.client._backend._server.handlers
    fake = _LiveTelegram()
    handlers._telegram_accounts["اصلی"] = fake
    handlers.telegram = fake
    handlers.context.extra["telegram"] = fake
    return fake


def test_live_telegram_browser_endpoints(web_server) -> None:
    _install_fake(web_server)
    base = f"http://127.0.0.1:{web_server.port}"

    chats = requests.get(base + "/api/telegram/chats", params={"kind": "all", "limit": 1}, timeout=5)
    assert chats.status_code == 200
    assert chats.json()["source"] == "live"
    assert chats.json()["items"][0]["title"] == "Alice"
    assert chats.json()["has_more"] is True
    assert chats.json()["next_offset"] == 1
    second = requests.get(
        base + "/api/telegram/chats", params={"kind": "all", "limit": 1, "offset": 1}, timeout=5,
    ).json()
    assert second["items"][0]["title"] == "Python"

    contacts = requests.get(base + "/api/telegram/contacts", params={"query": "Ali"}, timeout=5)
    assert contacts.status_code == 200
    assert contacts.json()["items"][0]["id"] == 10

    stats = requests.get(base + "/api/telegram/stats", timeout=5).json()
    assert stats["total_contacts"] == 1 and stats["total_unread"] == 2

    history = requests.get(
        base + "/api/telegram/history", params={"target": "10", "limit": 20}, timeout=5,
    ).json()
    assert history["chat"]["id"] == 10
    assert history["items"][0]["sender_id"] == 10

    resolved = requests.post(
        base + "/api/telegram/resolve", json={"target": "Alice"}, timeout=5,
    ).json()
    assert resolved["kind"] == "private" and resolved["source"] == "live"


def test_telegram_browser_requires_connected_account(web_server) -> None:
    fake = _install_fake(web_server)
    fake.is_connected = False
    response = requests.get(
        f"http://127.0.0.1:{web_server.port}/api/telegram/chats", timeout=5,
    )
    assert response.status_code == 409
    assert "متصل نیست" in response.text


def test_telegram_browser_ui_contract() -> None:
    from local_agent.utils.paths import web_static_dir, web_templates_dir

    html = (web_templates_dir() / "index.html").read_text(encoding="utf-8")
    js = (web_static_dir() / "app.js").read_text(encoding="utf-8")
    css = (web_static_dir() / "style.css").read_text(encoding="utf-8")
    for token in ("telegramBrowserOpen", "telegramChatKind", "telegramChatScope",
                  "loadTelegramHistory", "loadTelegramBrowser(true)", "چت‌ها و مخاطبین",
                  "تازه‌سازی زنده", "نمایش موارد بیشتر"):
        assert token in html
    for endpoint in ("/api/telegram/chats", "/api/telegram/contacts",
                     "/api/telegram/stats", "/api/telegram/history"):
        assert endpoint in js
    assert ".telegram-browser__grid" in css
