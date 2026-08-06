"""Offline tests for the Gmail integration (fake backend, no network).

Covers the client facade, the gmail.* actions, the web endpoints and
the settings round-trip for the gmail section.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.actions import run_action
from local_agent.actions.registry import Risk
from local_agent.bridge.api.handlers import BridgeHandlers
from local_agent.core.config import AssistantSettings
from local_agent.core.errors import ActionRefused, AssistantError
from local_agent.gmail.client import GmailBackend, GmailClient, GmailMessage


class FakeGmailBackend(GmailBackend):
    """In-memory Gmail backend for tests."""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.messages = [
            GmailMessage(
                id="1",
                subject="گزارش هفتگی",
                sender="boss@example.com",
                snippet="متن ایمیل…",
                date="Tue, 1 Jan 2024 10:00:00 +0000",
                is_unread=True,
            ),
            GmailMessage(
                id="2",
                subject="فاکتور",
                sender="billing@example.com",
                snippet="فاکتور این ماه…",
                date="Wed, 2 Jan 2024 09:00:00 +0000",
                is_unread=True,
            ),
        ]
        self.sent: list[tuple[str, str, str]] = []
        self.sent_attachments: list[list[str]] = []
        self.replies: list[tuple[str, str, list[str]]] = []
        self.downloads: list[tuple[str, str, str]] = []

    def connect(self) -> str:
        self._connected = True
        return "connected as test@gmail.com (fake)"

    def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def list_unread(self, limit: int) -> list[GmailMessage]:
        return self.messages[:limit]

    def search(self, query: str, limit: int) -> list[GmailMessage]:
        return [m for m in self.messages if query.lower() in m.subject.lower()][:limit]

    def read(self, msg_id: str) -> GmailMessage:
        for m in self.messages:
            if m.id == msg_id:
                return m
        raise AssistantError(f"ایمیلی با شناسهٔ {msg_id} پیدا نشد")

    def send(self, to: str, subject: str, body: str, attachments: list[str] | None = None) -> str:
        self.sent.append((to, subject, body))
        self.sent_attachments.append(list(attachments or []))
        return "sent-fake"

    def reply(self, msg_id: str, body: str, attachments: list[str] | None = None) -> str:
        self.replies.append((msg_id, body, list(attachments or [])))
        return "reply-fake"

    def download_attachment(self, msg_id: str, filename: str, save_dir: Path) -> Path:
        save_dir.mkdir(parents=True, exist_ok=True)
        target = save_dir / (filename or "att.bin")
        target.write_bytes(b"attachment-bytes")
        self.downloads.append((msg_id, filename, str(target)))
        return target


@pytest.fixture
def handlers(tmp_path: Path) -> BridgeHandlers:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    return BridgeHandlers.build(settings)


@pytest.fixture
def connected_handlers(handlers: BridgeHandlers, fake_backend: FakeGmailBackend) -> BridgeHandlers:
    handlers.context.extra["gmail"] = GmailClient(backend=fake_backend)
    return handlers


@pytest.fixture
def fake_backend() -> FakeGmailBackend:
    return FakeGmailBackend()


# ---------------------------------------------------------------------------
# client facade
# ---------------------------------------------------------------------------


def test_client_facade_delegates_to_backend(fake_backend: FakeGmailBackend) -> None:
    client = GmailClient(backend=fake_backend)
    assert client.connect() == "connected as test@gmail.com (fake)"
    assert client.is_connected
    assert len(client.list_unread(5)) == 2
    assert client.search("فاکتور", 5)[0].id == "2"
    assert "متن" in client.read("1").snippet
    assert client.send("x@y.com", "s", "b") == "sent-fake"
    assert fake_backend.sent == [("x@y.com", "s", "b")]


def test_from_settings_without_any_backend_raises_helpful_error(tmp_path: Path) -> None:
    from local_agent.core.config import GmailSettings

    settings = GmailSettings(enabled=True)
    from local_agent.gmail.client import GmailError

    with pytest.raises(GmailError):
        GmailClient.from_settings(settings, tmp_path)


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


def test_gmail_actions_are_registered(handlers: BridgeHandlers) -> None:
    names = {a.name for a in handlers.registry.all()}
    for expected in ("gmail.list_unread", "gmail.search", "gmail.read", "gmail.send"):
        assert expected in names, expected


def test_gmail_risk_levels(handlers: BridgeHandlers) -> None:
    by_name = {a.name: a for a in handlers.registry.all()}
    assert by_name["gmail.list_unread"].risk_level == Risk.SAFE
    assert by_name["gmail.search"].risk_level == Risk.SAFE
    assert by_name["gmail.read"].risk_level == Risk.SAFE
    assert by_name["gmail.send"].risk_level == Risk.DESTRUCTIVE


def test_gmail_actions_without_client_hint(handlers: BridgeHandlers) -> None:
    from local_agent.core.errors import DependencyMissing

    with pytest.raises(DependencyMissing) as excinfo:
        run_action(handlers.registry, "gmail.list_unread", {}, handlers.context)
    assert "جیمیل" in excinfo.value.install_hint


def test_gmail_list_and_search(connected_handlers: BridgeHandlers) -> None:
    ctx = connected_handlers.context
    result = run_action(connected_handlers.registry, "gmail.list_unread", {"limit": 5}, ctx)
    assert "گزارش هفتگی" in result
    assert "فاکتور" in result
    found = run_action(connected_handlers.registry, "gmail.search", {"query": "فاکتور"}, ctx)
    assert "فاکتور" in found
    assert "گزارش" not in found


def test_gmail_read_returns_body(connected_handlers: BridgeHandlers) -> None:
    result = run_action(connected_handlers.registry, "gmail.read", {"id": "1"}, connected_handlers.context)
    assert "گزارش هفتگی" in result
    assert "boss@example.com" in result


def test_gmail_send_is_refused_without_approval(connected_handlers: BridgeHandlers) -> None:
    connected_handlers.gate.auto_deny()
    with pytest.raises(ActionRefused):
        run_action(
            connected_handlers.registry,
            "gmail.send",
            {"to": "x@y.com", "subject": "s", "body": "b"},
            connected_handlers.context,
        )


def test_gmail_send_succeeds_with_approval(
    connected_handlers: BridgeHandlers, fake_backend: FakeGmailBackend
) -> None:
    connected_handlers.gate.auto_approve()
    result = run_action(
        connected_handlers.registry,
        "gmail.send",
        {"to": "x@y.com", "subject": "سلام", "body": "متن"},
        connected_handlers.context,
    )
    assert "ارسال شد" in result
    assert fake_backend.sent == [("x@y.com", "سلام", "متن")]


def test_gmail_confirm_send_honoured_even_in_never_mode(handlers: BridgeHandlers) -> None:
    by_name = {a.name: a for a in handlers.registry.all()}
    action = by_name["gmail.send"]
    safety = type(handlers.settings.safety)(**{**handlers.settings.safety.__dict__, "confirm_mode": "never"})
    assert action.needs_confirmation(safety) is True
    handlers.apply_config_set("gmail.confirm_send", False)
    action2 = by_name["gmail.send"]
    assert action2.needs_confirmation(safety) is False


# ---------------------------------------------------------------------------
# web endpoints
# ---------------------------------------------------------------------------


def test_gmail_connect_without_configuration_returns_guidance(web_server) -> None:
    import requests

    base = f"http://127.0.0.1:{web_server.port}"
    r = requests.post(base + "/api/gmail/connect", timeout=5)
    assert r.status_code == 400
    body = r.json()
    assert "credentials" in body.get("detail", "") or "App Password" in body.get("detail", "")


def test_gmail_connect_uses_configured_client(web_server, monkeypatch) -> None:
    """With a backend injected into the handlers, connect works end-to-end."""
    import requests

    import local_agent.bridge.api.handlers as handlers_mod
    from local_agent.gmail.client import GmailClient

    monkeypatch.setattr(handlers_mod, "_build_gmail_client", lambda settings, **kwargs: GmailClient(backend=FakeGmailBackend()))

    base = f"http://127.0.0.1:{web_server.port}"
    r = requests.post(base + "/api/gmail/connect", timeout=5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] is True

    status = requests.get(base + "/api/status", timeout=5).json()
    assert status["settings"]["settings"]["gmail_connected"] is True

    r2 = requests.post(base + "/api/gmail/disconnect", timeout=5)
    assert r2.status_code == 200
    assert r2.json()["connected"] is False


# ---------------------------------------------------------------------------
# F3 — professional Gmail (attachments, download, reply)
# ---------------------------------------------------------------------------


def test_gmail_f3_actions_registered(handlers: BridgeHandlers) -> None:
    names = {a.name for a in handlers.registry.all()}
    for expected in ("gmail.download_attachment", "gmail.reply", "gmail.send"):
        assert expected in names, expected


def test_gmail_send_with_attachments(connected_handlers, fake_backend, tmp_path) -> None:
    att = tmp_path / "file.txt"
    att.write_text("hello", encoding="utf-8")
    connected_handlers.gate.auto_approve()
    result = run_action(
        connected_handlers.registry,
        "gmail.send",
        {"to": "x@y.com", "subject": "s", "body": "b", "attachments": [str(att)]},
        connected_handlers.context,
    )
    assert "ارسال شد" in result
    assert fake_backend.sent == [("x@y.com", "s", "b")]
    assert fake_backend.sent_attachments == [[str(att)]]


def test_gmail_reply_action(connected_handlers, fake_backend) -> None:
    connected_handlers.gate.auto_approve()
    result = run_action(
        connected_handlers.registry,
        "gmail.reply",
        {"id": "1", "body": "پاسخ"},
        connected_handlers.context,
    )
    assert "ارسال شد" in result
    assert fake_backend.replies == [("1", "پاسخ", [])]


def test_gmail_download_attachment_action(connected_handlers, fake_backend, tmp_path) -> None:
    result = run_action(
        connected_handlers.registry,
        "gmail.download_attachment",
        {"id": "1", "filename": "att.bin"},
        connected_handlers.context,
    )
    assert "پیوست دانلود شد" in result
    assert (tmp_path / "gmail" / "att.bin").is_file()
    assert fake_backend.downloads[0][0] == "1"
