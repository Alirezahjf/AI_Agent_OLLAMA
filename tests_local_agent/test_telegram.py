"""Tests for the personal Telegram (Telethon) wrapper.

Telethon is an optional dependency, so these tests are skipped if it is
not importable.  The wrapper itself is exercised with a fake
TelegramClient to avoid needing a real account.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

telethon = pytest.importorskip("telethon")

from local_agent.telegram.client import (
    PersonalTelegram,
    TelegramError,
    _chat_from_dialog,
    _entity_summary,
    _get_full_entity,
    _message_from_telethon,
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
            for contact in self.contacts:
                if contact.id == target:
                    return contact
        cleaned = str(target).lstrip("@").lower()
        for d in self.dialogs:
            title = getattr(d.entity, "title", None) or getattr(d.entity, "first_name", "")
            username = getattr(d.entity, "username", "") or ""
            phone = getattr(d.entity, "phone", "") or ""
            if cleaned in {str(title).lower(), str(username).lower(), str(phone).lower()}:
                return d.entity
        for contact in self.contacts:
            if cleaned in {contact.username.lower(), contact.phone.lower()}:
                return contact
        raise RuntimeError(f"cannot find {target}")

    async def iter_dialogs(self, limit=None):
        dialogs = self.dialogs if limit is None else self.dialogs[:limit]
        for dialog in dialogs:
            yield dialog

    async def contacts_request(self, request):
        if type(request).__name__ == "GetContactsRequest":
            self.contact_requests += 1
            return type("ContactsResult", (), {"users": list(self.contacts)})()
        if type(request).__name__ == "GetFullUserRequest":
            return type(
                "FullUserResult", (),
                {"full_user": type("FullUser", (), {"about": "زندگی‌نامهٔ زنده"})()},
            )()
        raise AssertionError(f"unexpected Telegram request: {type(request).__name__}")

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

    async def download_media(self, message, file: str):
        target_dir = Path(file)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"message_{message.id}.bin"
        target.write_bytes(b"media")
        return str(target)


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


def test_resolver_supports_id_username_phone_name_and_saved_messages(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    alice = patched_telethon.dialogs[0].entity
    alice.username = "alice_user"
    alice.phone = "+989121111111"
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")

    assert client.resolve_target("10")["name"] == "Alice"
    assert client.resolve_target("@alice_user")["raw_id"] == 10
    assert client.resolve_target("09121111111")["raw_id"] == 10
    assert client.resolve_target("+989121111111")["raw_id"] == 10
    assert client.resolve_target("Alice")["raw_id"] == 10
    assert client.resolve_target("خودم")["raw_id"] == 1
    assert client.resolve_target("پیام های ذخیره شده")["raw_id"] == 1


def test_resolver_normalizes_persian_and_refreshes_live_dialogs(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    patched_telethon.dialogs = [
        _FakeDialog(_FakeEntity(88, "توسعه\u200cدهندگان كاربردی")),
    ]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    assert client.resolve_target("توسعه دهندگان کاربردی")["raw_id"] == 88

    # Resolver scans current dialogs each time; no old result snapshot is reused.
    patched_telethon.dialogs.insert(0, _FakeDialog(_FakeEntity(99, "گفتگوی همین لحظه")))
    assert client.resolve_target("گفتگوی همین لحظه")["raw_id"] == 99


def test_resolver_prefers_exact_username_over_same_display_title(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    by_title = _FakeEntity(11, "unique_target")
    by_username = _FakeEntity(12, "Other", username="unique_target")
    patched_telethon.dialogs = [_FakeDialog(by_title), _FakeDialog(by_username)]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    assert client.resolve_target("unique_target")["raw_id"] == 12


def test_resolver_rejects_ambiguous_display_names(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    patched_telethon.dialogs = [
        _FakeDialog(_FakeEntity(71, "علی رضایی")),
        _FakeDialog(_FakeEntity(72, "علی رضایی")),
    ]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    with pytest.raises(TelegramError) as excinfo:
        client.resolve_target("علی رضایی")
    message = str(excinfo.value)
    assert "مبهم" in message
    assert "71" in message and "72" in message


def test_resolver_accepts_one_unique_partial_match(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    patched_telethon.dialogs = [
        _FakeDialog(_FakeEntity(81, "گروه تخصصی پایتون تهران", is_group=True)),
        _FakeDialog(_FakeEntity(82, "گفتگوی جاوا", is_group=True)),
    ]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    assert client.resolve_target("پایتون تهران")["raw_id"] == 81


def test_resolver_rejects_ambiguous_partial_match(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    patched_telethon.dialogs = [
        _FakeDialog(_FakeEntity(91, "تیم توسعه آلفا", is_group=True)),
        _FakeDialog(_FakeEntity(92, "تیم توسعه بتا", is_group=True)),
    ]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    with pytest.raises(TelegramError, match="مبهم"):
        client.resolve_target("تیم توسعه")


def test_profile_reads_full_user_about_live(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    person = _FakeUser(501, "Full", "full_user", "Person")
    patched_telethon.dialogs = [_FakeDialog(person)]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    profile = client.get_profile("Full Person", tmp_path / "media")
    assert profile["kind"] == "private"
    assert profile["bio"] == "زندگی‌نامهٔ زنده"
    assert profile["id"] == 501


def test_rich_message_metadata_and_media_types() -> None:
    msg = _FakeMessage(44, "caption", sender="alice", out=False)
    msg.sender_id = 123
    msg.reply_to = type("Reply", (), {"reply_to_msg_id": 40})()
    msg.forwards = 7
    msg.views = 99
    msg.media = object()
    msg.photo = object()
    result = _message_from_telethon(msg, chat_id=900)
    assert result.to_dict() == {
        "id": 44,
        "chat_id": 900,
        "sender_id": 123,
        "sender": "alice",
        "text": "caption",
        "date": "2024-01-01T12:00:00+00:00",
        "is_outgoing": False,
        "message_type": "photo",
        "reply_to_msg_id": 40,
        "forwards": 7,
        "views": 99,
        "has_media": True,
    }


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        ("sticker", "sticker"), ("gif", "gif"), ("voice", "voice"),
        ("video_note", "video_note"), ("video", "video"), ("audio", "audio"),
        ("document", "document"), ("geo", "location"), ("contact", "contact"),
        ("poll", "poll"),
    ],
)
def test_rich_message_detects_supported_media_types(attribute: str, expected: str) -> None:
    msg = _FakeMessage(1, "")
    msg.media = object()
    setattr(msg, attribute, object())
    assert _message_from_telethon(msg, chat_id=1).message_type == expected


def test_real_telethon_entities_have_stable_kinds_and_marked_ids() -> None:
    from telethon.tl import types

    user = types.User(id=7, access_hash=1, first_name="Alice", username="alice")
    group = types.Chat(
        id=8, title="Group", photo=types.ChatPhotoEmpty(), participants_count=12,
        date=datetime(2024, 1, 1, tzinfo=UTC), version=1,
    )
    channel = types.Channel(
        id=9, title="Channel", photo=types.ChatPhotoEmpty(),
        date=datetime(2024, 1, 1, tzinfo=UTC), broadcast=True,
        megagroup=False, access_hash=2,
    )
    supergroup = types.Channel(
        id=10, title="Supergroup", photo=types.ChatPhotoEmpty(),
        date=datetime(2024, 1, 1, tzinfo=UTC), broadcast=False,
        megagroup=True, access_hash=3,
    )

    assert _entity_summary(user)["kind"] == "private"
    assert _entity_summary(group)["id"] == -8
    assert _entity_summary(group)["kind"] == "group"
    assert _entity_summary(channel)["id"] == -1000000000009
    assert _entity_summary(channel)["kind"] == "channel"
    assert _entity_summary(supergroup)["kind"] == "supergroup"
    chat = _chat_from_dialog(_FakeDialog(supergroup, _FakeMessage(1, "live")))
    assert chat.kind == "supergroup"
    assert chat.to_dict()["last_message_date"] == "2024-01-01T12:00:00+00:00"


def test_full_entity_requests_cover_users_groups_and_channels() -> None:
    from telethon.tl import types

    requests = []

    async def requester(request):
        requests.append(type(request).__name__)
        attribute = "full_user" if type(request).__name__ == "GetFullUserRequest" else "full_chat"
        return type("FullResult", (), {attribute: type("Full", (), {"about": "full"})()})()

    user = types.User(id=1, access_hash=1, first_name="User")
    group = types.Chat(
        id=2, title="Group", photo=types.ChatPhotoEmpty(), participants_count=2,
        date=datetime(2024, 1, 1, tzinfo=UTC), version=1,
    )
    channel = types.Channel(
        id=3, title="Channel", photo=types.ChatPhotoEmpty(),
        date=datetime(2024, 1, 1, tzinfo=UTC), broadcast=True,
        megagroup=False, access_hash=3,
    )
    assert asyncio.run(_get_full_entity(requester, user)).about == "full"
    assert asyncio.run(_get_full_entity(requester, group)).about == "full"
    assert asyncio.run(_get_full_entity(requester, channel)).about == "full"
    assert requests == ["GetFullUserRequest", "GetFullChatRequest", "GetFullChannelRequest"]


def test_chat_pagination_archive_and_unread_filters_are_applied_live(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    patched_telethon.dialogs = [
        _FakeDialog(_FakeEntity(100 + i, f"Person {i}"), unread=i, folder_id=1 if i % 2 else None)
        for i in range(6)
    ]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    page = client.list_chats(limit=2, kind="private", offset=2)
    assert [chat.title for chat in page] == ["Person 2", "Person 3"]
    archived = client.list_chats(limit=10, archived=True)
    assert [chat.title for chat in archived] == ["Person 1", "Person 3", "Person 5"]
    unread = client.list_chats(limit=10, unread_only=True)
    assert all(chat.unread_count > 0 for chat in unread)


def test_live_statistics_unread_and_refresh(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    patched_telethon.dialogs = [
        _FakeDialog(_FakeEntity(1, "Private"), unread=3),
        _FakeDialog(_FakeEntity(2, "Group", is_group=True), unread=0),
        _FakeDialog(_FakeUser(3, "Bot", bot=True), unread=1),
    ]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    stats = client.get_statistics()
    assert stats["total_chats"] == 3
    assert stats["private_chats"] == 1 and stats["bot_chats"] == 1
    assert stats["unread_chats"] == 2 and stats["total_unread"] == 4
    assert [chat.unread_count for chat in client.list_unread_chats()] == [3, 1]
    refreshed = client.refresh_summary()
    assert refreshed["total_contacts"] == 2
    assert refreshed["source"] == "live"


def test_chat_statistics_and_export_are_rich_and_live(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    first = _FakeMessage(10, "hello", out=False)
    first.sender_id = 50
    second = _FakeMessage(11, "photo", out=True)
    second.sender_id = 1
    second.media = object()
    second.photo = object()
    patched_telethon.search_results = [first, second]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    stats = client.get_chat_statistics("Alice", 100)
    assert stats["sampled_messages"] == 2
    assert stats["message_types"] == {"photo": 1, "text": 1}
    assert stats["incoming"] == 1 and stats["outgoing"] == 1

    output = client.export_chat("Alice", tmp_path / "exports", fmt="json", limit=100)
    payload = __import__("json").loads(output.read_text(encoding="utf-8"))
    assert payload["message_count"] == 2
    assert payload["messages"][1]["message_type"] == "photo"


def test_batch_media_download_filters_types(
    patched_telethon: _FakeTelegramClient, tmp_path: Path
) -> None:
    photo = _FakeMessage(20, "photo")
    photo.media = object()
    photo.photo = object()
    document = _FakeMessage(21, "doc")
    document.media = object()
    document.document = object()
    patched_telethon.search_results = [photo, document, _FakeMessage(22, "text")]
    client = PersonalTelegram(api_id=1, api_hash="hash", phone="+1", session_path=tmp_path / "tg.session")
    client.connect(code_callback=lambda: "12345")
    paths = client.download_media_batch(
        "Alice", tmp_path / "media", limit=100, media_types=["photo"]
    )
    assert [path.name for path in paths] == ["message_20.bin"]
    assert paths[0].is_file()


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
