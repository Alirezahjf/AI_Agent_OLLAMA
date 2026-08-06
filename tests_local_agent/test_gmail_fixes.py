"""Offline tests — رفع‌های باقی‌ماندهٔ جیمیل (HTML، RFC 2047، گیرندهٔ markdown و...).

گ ۱: ارسال ایمیل HTML باید multipart/alternative شود — قبلاً هر body با
``set_content`` به‌صورت text/plain می‌رفت و گیرنده کدِ HTML را می‌دید.
گ ۲: هدرهای RFC 2047 باید دیکد شوند تا موضوع فارسی مُخ نشود.
"""

from __future__ import annotations

import email
import email.message
import email.policy
from pathlib import Path

import pytest

from local_agent.core.errors import AssistantError
from local_agent.gmail.client import (
    _build_mime,
    _build_mime_reply,
    _decode_header_value,
    _html_to_text,
    _looks_like_html,
    _message_from_rfc822,
)


def test_plain_body_stays_text_plain() -> None:
    message = _build_mime("a@b.com", "s", "فقط متن ساده")
    assert message.get_content_type() == "text/plain"
    assert message.get_content_charset() == "utf-8"


@pytest.mark.parametrize(
    "body",
    [
        "<html><body><h1>سلام</h1></body></html>",
        "<!DOCTYPE html><html><head></head><body>تست</body></html>",
        "<div style='color:red'>متن رنگی</div><p>پاراگراف</p>",
        "<table><tr><td>سلول</td></tr></table>",
    ],
)
def test_html_body_becomes_multipart_alternative(body: str) -> None:
    message = _build_mime("a@b.com", "s", body)
    assert message.is_multipart()
    types = [part.get_content_type() for part in message.iter_parts()]
    assert "text/plain" in types
    assert "text/html" in types
    html_part = next(p for p in message.iter_parts() if p.get_content_type() == "text/html")
    # The email library normalises line endings (trailing \n) on write.
    assert html_part.get_content().rstrip("\n") == body


def test_html_plain_fallback_has_stripped_text() -> None:
    message = _build_mime("a@b.com", "s", "<html><body><h1>عنوان</h1><p>متن بدنه</p></body></html>")
    plain = next(p for p in message.iter_parts() if p.get_content_type() == "text/plain")
    text = plain.get_content()
    assert "عنوان" in text
    assert "متن بدنه" in text
    assert "<" not in text


def test_html_reply_also_detected() -> None:
    message = _build_mime_reply("Re: s", "<html><body>پاسخ HTML</body></html>")
    assert message.is_multipart()
    assert "text/html" in [p.get_content_type() for p in message.iter_parts()]


def test_attachments_still_work_with_html_body(tmp_path: Path) -> None:
    payload = tmp_path / "file.txt"
    payload.write_text("hello", encoding="utf-8")
    message = _build_mime("a@b.com", "s", "<html><body>x</body></html>", [str(payload)])
    assert message.is_multipart()
    assert any(p.get_filename() == "file.txt" for p in message.iter_parts())


def test_looks_like_html_heuristics() -> None:
    assert _looks_like_html("<html><body>x</body></html>")
    assert _looks_like_html("<!DOCTYPE html><html>x</html>")
    assert _looks_like_html("سلام <b>عزیز</b> جان")
    assert not _looks_like_html("فقط متن ساده")
    assert not _looks_like_html("a < b و c > d")
    assert not _looks_like_html("")


def test_html_to_text_strips_tags_and_keeps_meaning() -> None:
    out = _html_to_text("<p>خط اول</p><p>خط دوم</p><br>سوم")
    assert "خط اول" in out
    assert "خط دوم" in out
    assert "سوم" in out
    assert "<" not in out


# ===========================================================================
# گ ۲) هدرهای RFC 2047 باید به متن فارسی واقعی دیکد شوند
# ===========================================================================


def test_decode_header_value_base64_persian() -> None:
    # دقیقاً همان رشتهٔ مُخ دیده‌شده در خروجی کاربر (لاگ قبلی).
    raw = "=?UTF-8?B?2YfYtNiv2KfYsSDYp9mF2YbbjNiq24w=?="
    decoded = _decode_header_value(raw)
    assert decoded == "هشدار امنیتی"
    assert "?" not in decoded and "=" not in decoded


def test_decode_header_value_plain_text_passthrough() -> None:
    assert _decode_header_value("سلام دنیا") == "سلام دنیا"
    assert _decode_header_value(None) == ""
    assert _decode_header_value("") == ""


def test_decode_header_value_q_encoded() -> None:
    raw = "=?utf-8?q?=D8=B3=D9=84=D8=A7=D9=85?="
    assert _decode_header_value(raw) == "سلام"


def test_rfc822_message_subject_and_sender_decoded() -> None:
    msg = email.message.Message(policy=email.policy.default)
    msg["Subject"] = "=?UTF-8?B?2YfYtNiv2KfYsSDYp9mF2YbbjNiq24w=?="
    msg["From"] = "=?UTF-8?B?2LnZhNuM?= <boss@example.com>"
    parsed = _message_from_rfc822("1", msg)
    assert parsed.subject == "هشدار امنیتی"
    assert "علی" in parsed.sender


def test_rfc822_bad_encoding_does_not_crash() -> None:
    msg = email.message.Message(policy=email.policy.default)
    msg["Subject"] = "=?UTF-8?B?%%%invalid%%%?="
    parsed = _message_from_rfc822("1", msg)
    # نباید خطا بدهد؛ متن best-effort کافی است.
    assert isinstance(parsed.subject, str)


def test_imap_message_from_bytes_decoded() -> None:
    raw = (
        b"Subject: =?UTF-8?B?2YfYtNiv2KfYsSDYp9mF2YbbjNiq24w=?=\r\n"
        b"From: x@example.com\r\n\r\nbody\r\n"
    )
    parsed = email.message_from_bytes(raw)
    assert _decode_header_value(parsed.get("Subject")) == "هشدار امنیتی"


# ===========================================================================
# گ ۳) گیرندهٔ markdown باید تمیز و اعتبارسنجی شود
# ===========================================================================


def test_extract_email_cleans_markdown_link() -> None:
    from local_agent.actions.gmail_actions import _extract_email

    assert _extract_email("[sajjadbul313@gmail.com](mailto:sajjadbul313@gmail.com)") == (
        "sajjadbul313@gmail.com"
    )


def test_extract_email_plain_address() -> None:
    from local_agent.actions.gmail_actions import _extract_email

    assert _extract_email("a@b.com") == "a@b.com"
    assert _extract_email("  Name <x@y.com> ") == "x@y.com"


@pytest.mark.parametrize("bad", ["not-an-email", "", "a@", "@b.com", "a b c"])
def test_extract_email_rejects_invalid(bad: str) -> None:
    from local_agent.actions.gmail_actions import _extract_email

    with pytest.raises(AssistantError):
        _extract_email(bad)


def test_gmail_send_uses_cleaned_markdown_recipient(tmp_path: Path) -> None:
    from local_agent.actions import run_action
    from local_agent.bridge.api.handlers import BridgeHandlers
    from local_agent.core.config import AssistantSettings
    from local_agent.core.errors import AssistantError
    from local_agent.gmail.client import GmailClient

    seen: dict[str, object] = {}

    class _Backend:
        is_connected = True

        def send(self, to, subject, body, attachments=None):
            seen["to"] = to
            return "sent"

    handlers = BridgeHandlers.build(AssistantSettings(data_dir=tmp_path, work_dir=tmp_path))
    handlers.context.extra["gmail"] = GmailClient(backend=_Backend())
    handlers.gate.auto_approve()

    run_action(
        handlers.registry,
        "gmail.send",
        {"to": "[sajjadbul313@gmail.com](mailto:sajjadbul313@gmail.com)",
         "subject": "s", "body": "b"},
        handlers.context,
    )
    assert seen["to"] == "sajjadbul313@gmail.com"

    with pytest.raises(AssistantError) as exc:
        run_action(
            handlers.registry,
            "gmail.send",
            {"to": "not-an-email", "subject": "s", "body": "b"},
            handlers.context,
        )
    assert "نامعتبر" in str(exc.value)


# ===========================================================================
# گ ۴) شناسهٔ غیرعددی ایمیل → خطای فارسی، نه «FETCH command error»
# ===========================================================================


class _FakeImap:
    """حداقل جایگزین imaplib: شناسه‌های fetch را ضبط می‌کند و می‌تواند خطا بدهد."""

    def __init__(self, fetch_result=None, raise_on_fetch: Exception | None = None) -> None:
        self.fetch_result = fetch_result or ("OK", [(b"1", b"Subject: s\r\n\r\nbody")])
        self.raise_on_fetch = raise_on_fetch
        self.fetch_calls: list[str] = []

    def select(self, mailbox: str) -> None:
        return None

    def search(self, *args):
        return "OK", [b"1 2"]

    def fetch(self, msg_id, spec):
        self.fetch_calls.append(str(msg_id))
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        return self.fetch_result

    def logout(self) -> None:
        return None


def test_imap_require_numeric_id_rejects_filenames(tmp_path: Path) -> None:
    """read/reply/download با id غیرعددی → GmailError فارسی و بدون fetch."""
    from local_agent.gmail import client as gm

    imap = _FakeImap()
    backend = gm._ImapGmailBackend(username="u@gmail.com", app_password="p" * 16)
    backend._imap = imap

    for method, args in (
        ("read", ("content-bottom_1.png",)),
        ("download_attachment", ("content-bottom_1.png", "x.png", Path("/tmp"))),
        ("reply", ("content-bottom_1.png", "body")),
    ):
        with pytest.raises(gm.GmailError) as exc:
            getattr(backend, method)(*args)
        assert "عددی" in str(exc.value)
    assert imap.fetch_calls == []


def test_imap_numeric_id_fetch_ok() -> None:
    from local_agent.gmail import client as gm

    raw = b"Subject: test\r\nFrom: x@example.com\r\n\r\nbody\r\n"
    imap = _FakeImap(fetch_result=("OK", [(b"1", raw)]))
    backend = gm._ImapGmailBackend(username="u@gmail.com", app_password="p" * 16)
    backend._imap = imap
    message = backend.read("42")
    assert message.id == "42"
    assert imap.fetch_calls == ["42"]


def test_imap_fetch_error_is_friendly_persian() -> None:
    from local_agent.gmail import client as gm

    imap = _FakeImap(raise_on_fetch=__import__("imaplib").IMAP4.error("boom"))
    backend = gm._ImapGmailBackend(username="u@gmail.com", app_password="p" * 16)
    backend._imap = imap
    with pytest.raises(gm.GmailError):
        backend.read("1")


def test_download_attachment_action_non_numeric_id_is_clean_error(
    tmp_path: Path, caplog
) -> None:
    """اکشن download با id غیرعددی → خطای فارسی و بدون لاگ ERROR (کرش)."""
    import logging

    from local_agent.actions import run_action
    from local_agent.bridge.api.handlers import BridgeHandlers
    from local_agent.core.config import AssistantSettings
    from local_agent.gmail.client import GmailClient, GmailError

    class _Backend:
        is_connected = True

        def download_attachment(self, msg_id, filename, save_dir):
            # رفتار واقعی بکند IMAP: اعتبارسنجی قبل از fetch
            raise GmailError("شناسهٔ ایمیل باید عددی باشد (مثلاً ۱۲۳). «id» شناسهٔ خود ایمیل است، نه نام فایل پیوست.")

    handlers = BridgeHandlers.build(AssistantSettings(data_dir=tmp_path, work_dir=tmp_path))
    handlers.context.extra["gmail"] = GmailClient(backend=_Backend())

    with caplog.at_level(logging.ERROR, logger="actions"):
        with pytest.raises(AssistantError) as exc:
            run_action(
                handlers.registry,
                "gmail.download_attachment",
                {"id": "content-bottom_1.png", "filename": "content-bottom_1.png"},
                handlers.context,
            )
    assert "عددی" in str(exc.value)
    assert "crashed" not in caplog.text


# ===========================================================================
# گ ۵) مسیر نسبی پیوست باید از پوشهٔ کاری (workspace) باز شود
# ===========================================================================


def test_gmail_send_resolves_relative_attachment_from_work_dir(tmp_path: Path) -> None:
    from local_agent.actions import run_action
    from local_agent.bridge.api.handlers import BridgeHandlers
    from local_agent.core.config import AssistantSettings
    from local_agent.gmail.client import GmailClient

    attached = tmp_path / "tokpypl.txt"
    attached.write_text("data", encoding="utf-8")

    seen: dict[str, object] = {}

    class _Backend:
        is_connected = True

        def send(self, to, subject, body, attachments=None):
            seen["to"] = to
            seen["attachments"] = list(attachments or [])
            return "sent"

    handlers = BridgeHandlers.build(AssistantSettings(data_dir=tmp_path, work_dir=tmp_path))
    handlers.context.extra["gmail"] = GmailClient(backend=_Backend())
    handlers.gate.auto_approve()

    run_action(
        handlers.registry,
        "gmail.send",
        {"to": "a@b.com", "subject": "s", "body": "b", "attachments": ["tokpypl.txt"]},
        handlers.context,
    )
    assert seen["to"] == "a@b.com"
    assert seen["attachments"] == [str(attached)]


def test_gmail_send_absolute_attachment_used_as_is(tmp_path: Path) -> None:
    from local_agent.actions import run_action
    from local_agent.bridge.api.handlers import BridgeHandlers
    from local_agent.core.config import AssistantSettings
    from local_agent.gmail.client import GmailClient

    outside = tmp_path / "abs.txt"
    outside.write_text("x", encoding="utf-8")

    seen: dict[str, object] = {}

    class _Backend:
        is_connected = True

        def send(self, to, subject, body, attachments=None):
            seen["attachments"] = list(attachments or [])
            return "sent"

    handlers = BridgeHandlers.build(AssistantSettings(data_dir=tmp_path, work_dir=tmp_path))
    handlers.context.extra["gmail"] = GmailClient(backend=_Backend())
    handlers.gate.auto_approve()

    run_action(
        handlers.registry,
        "gmail.send",
        {"to": "a@b.com", "subject": "s", "body": "b", "attachments": [str(outside)]},
        handlers.context,
    )
    assert seen["attachments"] == [str(outside)]


# ===========================================================================
# گ ۱۰) هشدار «gmail client not built» نباید اسپم و گمراه‌کننده باشد
# ===========================================================================


def test_build_gmail_client_incomplete_config_logs_debug_not_warning(caplog) -> None:
    import logging

    from local_agent.bridge.api import handlers as h
    from local_agent.core.config import AssistantSettings, GmailSettings

    settings = AssistantSettings(
        data_dir=Path("/tmp/la_x"), work_dir=Path("/tmp/la_x"),
    ).with_overrides(gmail=GmailSettings(enabled=True))
    with caplog.at_level(logging.WARNING, logger="bridge.handlers"):
        client = h._build_gmail_client(settings)
    assert client is None
    assert "gmail client not built" not in caplog.text


def test_status_warnings_explain_gmail_missing_config(tmp_path: Path) -> None:
    from local_agent.bridge.api.handlers import BridgeHandlers
    from local_agent.core.config import AssistantSettings, GmailSettings

    settings = AssistantSettings(
        data_dir=tmp_path, work_dir=tmp_path,
    ).with_overrides(gmail=GmailSettings(enabled=True))
    handlers = BridgeHandlers.build(settings)
    warnings = handlers._warnings()
    assert any("جیمیل" in w and "username" in w for w in warnings)
