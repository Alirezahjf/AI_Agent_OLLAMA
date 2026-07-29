"""Tests for the personal Telegram (Telethon) wrapper.

Telethon is an optional dependency, so these tests are skipped if it is
not importable.  The wrapper itself is exercised with a fake
TelegramClient to avoid needing a real account.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

telethon = pytest.importorskip("telethon")

from local_agent.core.errors import AssistantError  # noqa: E402
from local_agent.telegram.client import (  # noqa: E402
    Chat,
    Message,
    PersonalTelegram,
    TelegramError,
)


# ---------------------------------------------------------------------------
# Fake telethon client
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, id: int, first_name: str, username: str = "me", last_name: str = "") -> None:
        self.id = id
        self.first_name = first_name
        self.username = username
        self.last_name = last_name
        self.phone = "+10000000000"


class _FakeDialog:
    def __init__(self, entity, message=None, unread: int = 0) -> None:
        self.entity = entity
        self.id = getattr(entity, "id", 0)
        self.message = message
        self.unread_count = unread
        self.name = getattr(entity, "title", None) or getattr(entity, "first_name", "?")


class _FakeEntity:
    def __init__(self, id: int, name: str, username: str | None = None, is_group: bool = False) -> None:
        self.id = id
        self.title = name
        self.first_name = name
        self.username = username
        self.megagroup = is_group
        self.gigagroup = False
        self.is_group = is_group


class _FakeMessage:
    def __init__(self, id: int, text: str, sender: str = "alice", out: bool = True) -> None:
        self.id = id
        self.message = text
        self.date = datetime(2024, 1, 1, 12, 0, 0)
        self.out = out
        self.sender = _FakeSender(sender) if sender else None


class _FakeSender:
    def __init__(self, name: str) -> None:
        self.username = name
        self.first_name = name


class _FakeTelegramClient:
    """In-memory stand-in for telethon.TelegramClient."""

    def __init__(self, *args, **kwargs) -> None:
        self.connected = False
        self.authorized = False
        self.user = _FakeUser(1, "Test", "tester")
        self.dialogs = [
            _FakeDialog(_FakeEntity(10, "Alice"), _FakeMessage(1, "hi"), unread=2),
            _FakeDialog(_FakeEntity(20, "Family Group", is_group=True), _FakeMessage(2, "dinner?"), unread=0),
        ]
        self.search_results: list[_FakeMessage] = []
        self.sent: list[tuple[Any, str]] = []
        self.uploaded: list[tuple[Any, str]] = []

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def send_code_request(self, phone: str) -> Any:
        self._phone = phone
        return type("Sent", (), {"phone_code_hash": "hash"})()

    async def sign_in(self, *args, **kwargs) -> Any:
        self.authorized = True
        return self.user

    async def disconnect(self) -> None:
        self.connected = False

    async def get_me(self) -> _FakeUser:
        return self.user

    async def get_entity(self, target) -> _FakeEntity:
        if isinstance(target, int):
            for d in self.dialogs:
                if d.id == target:
                    return d.entity
        for d in self.dialogs:
            if d.entity.title == str(target) or d.entity.username == str(target):
                return d.entity
        raise RuntimeError(f"cannot find {target}")

    async def iter_dialogs(self, limit: int):
        for dialog in self.dialogs[:limit]:
            yield dialog

    async def send_message(self, entity, text: str) -> _FakeMessage:
        self.sent.append((entity, text))
        return _FakeMessage(99, text, out=True)

    async def upload_file(self, path: str) -> str:
        self.uploaded.append((None, path))
        return path

    async def send_file(self, entity, file, caption: str = "", force_document: bool = False) -> _FakeMessage:
        self.sent.append((entity, f"[file] {caption}"))
        return _FakeMessage(100, caption or "", out=True)

    async def iter_messages(self, entity, search: str | None = None, limit: int = 30):
        for message in self.search_results[:limit]:
            yield message


@pytest.fixture
def patched_telethon(monkeypatch: pytest.MonkeyPatch) -> _FakeTelegramClient:
    """Replace telethon.TelegramClient with our fake."""
    import sys
    fake = _FakeTelegramClient()

    class _FakeTelegramClientClass:
        def __init__(self, *args, **kwargs) -> None:
            # Re-bind every method on the underlying fake as an instance
            # method on self so async call sites see real methods.
            self._fake = fake
            for name in dir(fake):
                if name.startswith("_"):
                    continue
                attr = getattr(fake, name)
                if callable(attr):
                    setattr(self, name, attr.__get__(fake, type(fake)))

    telethon_module = sys.modules.get("telethon") or telethon
    monkeypatch.setattr(telethon_module, "TelegramClient", _FakeTelegramClientClass)
    return fake


def test_connect_succeeds(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    client = PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )
    status = client.connect(code_callback=lambda: "12345")
    assert "connected" in status.lower()
    assert client.is_connected
    assert patched_telethon.authorized is True


def test_list_chats_returns_dialogs(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    client = PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )
    client.connect(code_callback=lambda: "12345")
    chats = client.list_chats(limit=10)
    assert len(chats) == 2
    assert any(c.title == "Alice" for c in chats)
    assert any(c.is_group for c in chats)


def test_send_message_records_send(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    client = PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )
    client.connect(code_callback=lambda: "12345")
    client.send_message("Alice", "سلام!")
    assert patched_telethon.sent[0][1] == "سلام!"


def test_send_photo_requires_existing_file(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    client = PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )
    client.connect(code_callback=lambda: "12345")
    with pytest.raises(TelegramError):
        client.send_photo("Alice", tmp_path / "nonexistent.png")


def test_send_photo_uploads(tmp_path: Path, patched_telethon: _FakeTelegramClient) -> None:
    image = tmp_path / "test.png"
    image.write_bytes(b"\x89PNG fake")
    client = PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )
    client.connect(code_callback=lambda: "12345")
    msg = client.send_photo("Alice", image, caption="see this")
    assert msg.is_outgoing
    assert patched_telethon.uploaded  # we uploaded something


def test_get_me_returns_identity(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    client = PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )
    client.connect(code_callback=lambda: "12345")
    me = client.get_me()
    assert me["first_name"] == "Test"
    assert me["username"] == "tester"


def test_disconnect_clears_state(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    client = PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )
    client.connect(code_callback=lambda: "12345")
    assert client.is_connected
    client.disconnect()
    assert not client.is_connected


def test_missing_credentials_raise(tmp_path: Path) -> None:
    with pytest.raises(TelegramError):
        PersonalTelegram(
            api_id=0, api_hash="", phone="", session_path=tmp_path / "x.session"
        )


def test_operations_require_connection(tmp_path: Path, patched_telethon: _FakeTelegramClient) -> None:
    client = PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )
    with pytest.raises(TelegramError):
        client.list_chats()
    with pytest.raises(TelegramError):
        client.send_message("Alice", "x")
    # close the loop so the coroutine warnings don't fire
    client.disconnect()


def test_search_messages_returns_results(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    patched_telethon.search_results = [
        _FakeMessage(1, "first hit"),
        _FakeMessage(2, "second hit"),
    ]
    client = PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )
    client.connect(code_callback=lambda: "12345")
    messages = client.search_messages("Alice", "hit", limit=10)
    assert len(messages) == 2
    assert messages[0].text == "first hit"
