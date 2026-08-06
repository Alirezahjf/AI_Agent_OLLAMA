"""Offline tests — گ ۱: ارسال ایمیل HTML باید multipart/alternative شود.

قبلاً هر body با ``set_content`` به‌صورت text/plain می‌رفت و گیرنده کدِ HTML
را می‌دید. حالا خود برنامه تشخیص می‌دهد و برای HTML یک بخش text/plain
(سلب‌شده) + یک بخش text/html می‌سازد.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.gmail.client import (
    _build_mime,
    _build_mime_reply,
    _html_to_text,
    _looks_like_html,
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
