"""Tests for the personal Telegram (Telethon) wrapper.

Telethon is an optional dependency, so these tests are skipped if it is
not importable.  The wrapper itself is exercised with a fake
TelegramClient to avoid needing a real account.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

telethon = pytest.importorskip("telethon")

from local_agent.telegram.client import (
    PersonalTelegram,
    TelegramError,
)

# ---------------------------------------------------------------------------
# Fake telethon client
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, id: int, first_name: str, username: str = "me", last_name: str = "",
                 phone: str = "+10000000000", *, bot: bool = False) -> None:
        self.id = id
        self.first_name = first_name
        self.username = username
        self.last_name = last_name
        self.phone = phone
        self.bot = bot
        self.contact = True
        self.mutual_contact = False
        self.verified = False
        self.deleted = False
        self.status = None


class _FakeDialog:
    def __init__(self, entity, message=None, unread: int = 0, *, folder_id=None,
                 pinned: bool = False) -> None:
        self.entity = entity
        self.id = getattr(entity, "id", 0)
        self.message = message
        self.unread_count = unread
        self.name = getattr(entity, "title", None) or getattr(entity, "first_name", "?")
        self.folder_id = folder_id
        self.pinned = pinned
        self.notify_settings = None
        self.date = getattr(message, "date", None)


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
        self.date = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
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
        self.password_prompted = False
        self.contacts = [
            _FakeUser(101, "علی", "ali", "رضایی", "+989121234567"),
            _FakeUser(102, "Sara", "sara", "Ahmadi", "+989351111111"),
        ]
        self.contact_requests = 0

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

    async def iter_dialogs(self, limit=None):
        dialogs = self.dialogs if limit is None else self.dialogs[:limit]
        for dialog in dialogs:
            yield dialog

    async def contacts_request(self, request):
        self.contact_requests += 1
        return type("ContactsResult", (), {"users": list(self.contacts)})()

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


class _FakeTelegramClient2FA(_FakeTelegramClient):
    """Same fake, but the account has 2FA enabled."""

    async def sign_in(self, *args, **kwargs) -> Any:
        if not kwargs.get("password"):
            from telethon.errors import SessionPasswordNeededError

            self.password_prompted = True
            raise SessionPasswordNeededError(request=None)
        self.authorized = True
        return self.user


def _patch_telethon(monkeypatch: pytest.MonkeyPatch, fake: _FakeTelegramClient) -> _FakeTelegramClient:
    """Replace telethon.TelegramClient with our fake."""
    import sys

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

        async def __call__(self, request):
            return await self._fake.contacts_request(request)

    telethon_module = sys.modules.get("telethon") or telethon
    monkeypatch.setattr(telethon_module, "TelegramClient", _FakeTelegramClientClass)
    return fake


@pytest.fixture
def patched_telethon(monkeypatch: pytest.MonkeyPatch) -> _FakeTelegramClient:
    """Replace telethon.TelegramClient with our fake."""
    return _patch_telethon(monkeypatch, _FakeTelegramClient())


@pytest.fixture
def patched_telethon_2fa(monkeypatch: pytest.MonkeyPatch) -> _FakeTelegramClient:
    """Same, but the fake account requires a 2FA password."""
    return _patch_telethon(monkeypatch, _FakeTelegramClient2FA())


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


def test_contacts_use_live_get_contacts_request(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")

    first = client.list_contacts(limit=100)
    assert {row["id"] for row in first} == {101, 102}
    assert first[0].keys() >= {"id", "name", "username", "phone", "is_contact", "is_mutual_contact"}

    # A second call must hit Telegram again, not return an application cache.
    patched_telethon.contacts.append(_FakeUser(103, "New", "new_user"))
    second = client.list_contacts(limit=100)
    assert {row["id"] for row in second} == {101, 102, 103}
    assert patched_telethon.contact_requests == 2


def test_contact_search_normalizes_persian_username_and_phone(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")

    assert client.search_contacts("علي رضايي")[0]["id"] == 101
    assert client.search_contacts("@ALI")[0]["id"] == 101
    assert client.search_contacts("09121234567")[0]["id"] == 101
    assert patched_telethon.contact_requests == 3


def test_private_filter_applies_before_limit_and_reads_live_dialogs(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    groups = [_FakeDialog(_FakeEntity(1000 + i, f"Group {i}", is_group=True)) for i in range(40)]
    privates = [_FakeDialog(_FakeEntity(2000 + i, f"Person {i}")) for i in range(5)]
    patched_telethon.dialogs = groups + privates
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")

    result = client.list_chats(limit=3, kind="private")
    assert [chat.title for chat in result] == ["Person 0", "Person 1", "Person 2"]
    assert all(chat.is_private and chat.kind == "private" for chat in result)

    # Every call reads the current Telegram dialogs, so new activity is visible.
    patched_telethon.dialogs.insert(0, _FakeDialog(_FakeEntity(3000, "Newest Person")))
    assert client.list_chats(limit=1, kind="private")[0].title == "Newest Person"


def test_chat_query_searches_beyond_initial_limit(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    patched_telethon.dialogs = [
        _FakeDialog(_FakeEntity(i, f"گروه {i}", is_group=True)) for i in range(50)
    ] + [_FakeDialog(_FakeEntity(999, "توسعه\u200cدهندگان پایتون", username="PythonDevs"))]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")

    by_name = client.list_chats(limit=5, kind="private", query="توسعه دهندگان")
    by_username = client.list_chats(limit=5, query="@pythondevs")
    assert [chat.id for chat in by_name] == [999]
    assert [chat.id for chat in by_username] == [999]


def test_chat_classification_and_live_metadata(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    supergroup = _FakeEntity(10, "Super", is_group=False)
    supergroup.megagroup = True
    channel = _FakeEntity(20, "News")
    channel.broadcast = True
    bot = _FakeUser(30, "Helper", "helper_bot", bot=True)
    patched_telethon.dialogs = [
        _FakeDialog(supergroup, _FakeMessage(1, "sg"), unread=2),
        _FakeDialog(channel, _FakeMessage(2, "news"), folder_id=1, pinned=True),
        _FakeDialog(bot, _FakeMessage(3, "bot")),
    ]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")

    assert client.list_chats(kind="supergroup")[0].kind == "supergroup"
    channel_result = client.list_chats(kind="channel")[0]
    assert channel_result.archived and channel_result.pinned
    assert channel_result.last_message_date is not None
    assert client.list_chats(kind="bot")[0].kind == "bot"
    # group is intentionally inclusive of supergroups for backward compatibility.
    assert client.list_chats(kind="group")[0].title == "Super"


def test_unread_sort_scans_all_matching_dialogs(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    patched_telethon.dialogs = [
        _FakeDialog(_FakeEntity(i, f"Person {i}"), unread=i) for i in range(1, 8)
    ]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    result = client.list_chats(limit=3, kind="private", sort="unread")
    assert [chat.unread_count for chat in result] == [7, 6, 5]


# ---------------------------------------------------------------------------
# Stepwise login (web UI state machine): await_code -> await_2fa -> connected
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> PersonalTelegram:
    return PersonalTelegram(
        api_id=12345,
        api_hash="hash",
        phone="+10000000000",
        session_path=tmp_path / "tg.session",
    )


def test_stepwise_login_code_then_connected(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    client = _client(tmp_path)
    first = client.start_login()
    assert first["state"] == "await_code"
    assert client.login_state == "await_code"

    second = client.submit_code("12345")
    assert second["state"] == "connected"
    assert client.is_connected
    assert client.login_state == "connected"
    assert "user" in second


def test_stepwise_login_with_2fa(patched_telethon_2fa: _FakeTelegramClient, tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.start_login()["state"] == "await_code"
    # Code accepted, but Telegram asks for the 2FA password next.
    second = client.submit_code("12345")
    assert second["state"] == "await_2fa"
    assert client.login_state == "await_2fa"
    assert not client.is_connected

    third = client.submit_password("secret-password")
    assert third["state"] == "connected"
    assert client.is_connected
    assert patched_telethon_2fa.password_prompted


def test_submit_code_without_started_login_raises(tmp_path: Path) -> None:
    client = _client(tmp_path)
    with pytest.raises(TelegramError):
        client.submit_code("12345")


def test_wrong_2fa_password_raises_and_stays_await_2fa(
    patched_telethon_2fa: _FakeTelegramClient, tmp_path: Path
) -> None:
    from telethon.errors import PasswordHashInvalidError

    real_sign_in = patched_telethon_2fa.sign_in

    async def reject_password(*args, **kwargs) -> Any:
        if kwargs.get("password"):
            raise PasswordHashInvalidError(request=None)
        return await real_sign_in(*args, **kwargs)

    patched_telethon_2fa.sign_in = reject_password
    client = _client(tmp_path)
    client.start_login()
    client.submit_code("12345")
    with pytest.raises(TelegramError):
        client.submit_password("wrong")
    assert client.login_state == "await_2fa"


def test_cancel_login_aborts_flow(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.start_login()
    assert client.login_state == "await_code"
    client.cancel_login()
    assert client.login_state == "disconnected"
    with pytest.raises(TelegramError):
        client.submit_code("12345")


def test_session_reuse_skips_code(patched_telethon: _FakeTelegramClient, tmp_path: Path) -> None:
    """A valid session file must reconnect without any login round-trip."""
    patched_telethon.authorized = True
    client = _client(tmp_path)
    result = client.start_login()
    assert result["state"] == "connected"
    assert client.is_connected
    # send_code_request was never called (session file was reused).
    assert not hasattr(patched_telethon, "_phone")


def test_connect_callback_flow_with_2fa(
    patched_telethon_2fa: _FakeTelegramClient, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    calls: list[str] = []

    def code_cb() -> str:
        calls.append("code")
        return "12345"

    def password_cb() -> str:
        calls.append("password")
        return "p4ss"

    status = client.connect(code_callback=code_cb, password_callback=password_cb)
    assert "connected" in status.lower()
    assert calls == ["code", "password"]
