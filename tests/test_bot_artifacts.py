"""TelegramBot artifact delivery: real PNGs go out with reply_photo, safely."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock


from agent.bot import TelegramBot, clean_chat_text
from agent.config import Settings
from agent.tools import ToolResult


def make_settings(root: Path) -> Settings:
    (root / "data").mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_token="token",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model="test-model",
        default_provider="ollama",
        default_model="",
        gapgpt_api_key="",
        gapgpt_base_url="https://gap.example/v1",
        avalai_api_key="",
        avalai_base_url="https://aval.example/v1",
        allowed_user_ids=frozenset(),
        workspace_root=root,
        data_dir=root / "data",
        command_timeout=5,
        max_output_chars=2000,
        max_agent_turns=4,
        model_timeout=10,
        auto_approve_mutations=False,
    )


def make_update(chat_id: int = 1) -> tuple[MagicMock, MagicMock]:
    status = MagicMock()
    status.edit_text = AsyncMock(return_value=None)
    message = MagicMock()
    message.reply_text = AsyncMock(return_value=status)
    message.reply_photo = AsyncMock(return_value=None)
    chat = MagicMock()
    chat.id = chat_id
    chat.send_action = AsyncMock(return_value=None)
    update = MagicMock()
    update.effective_chat = chat
    update.effective_message = message
    return update, message


def run_workflow(bot: TelegramBot, update: MagicMock, result: ToolResult, final: str = "گزارش") -> None:
    def fake_run(chat_id: int, user_text: str | None, progress: Any, resume: Any = None) -> tuple:
        return final, None, result

    bot.agent.run = fake_run  # type: ignore[method-assign]
    asyncio.run(bot._run(update, "یک اسکرین‌شات بگیر"))


def test_png_artifact_is_sent_with_reply_photo(tmp_path: Path) -> None:
    bot = TelegramBot(make_settings(tmp_path))
    png = tmp_path / "screenshot_example.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
    update, message = make_update()

    run_workflow(bot, update, ToolResult("اسکرین‌شات ذخیره شد", changed=True, artifacts=(png,)))

    message.reply_photo.assert_called_once()
    kwargs = message.reply_photo.call_args.kwargs
    assert kwargs["caption"] == "📸 اسکرین‌شات ساخته‌شده توسط عامل"
    # The real file (opened handle) is what gets sent, not a claim about it.
    handle = message.reply_photo.call_args.args[0]
    assert Path(handle.name).name == "screenshot_example.png"


def test_artifact_outside_workspace_is_never_sent(tmp_path: Path) -> None:
    bot = TelegramBot(make_settings(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(exist_ok=True)
    outside = tmp_path / "outside.png"  # exists, but outside WORKSPACE_ROOT
    outside.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
    update, message = make_update()

    run_workflow(bot, update, ToolResult("done", artifacts=(outside,)))

    message.reply_photo.assert_not_called()
    warning_texts = " ".join(str(call.args[0]) for call in message.reply_text.call_args_list if call.args)
    assert "معتبر نبود" in warning_texts


def test_non_png_or_missing_artifact_is_never_sent(tmp_path: Path) -> None:
    bot = TelegramBot(make_settings(tmp_path))
    text_file = tmp_path / "notes.txt"
    text_file.write_text("hello", encoding="utf-8")
    missing = tmp_path / "missing.png"
    update, message = make_update()

    run_workflow(bot, update, ToolResult("done", artifacts=(text_file, missing, "garbage")))  # type: ignore[arg-type]

    message.reply_photo.assert_not_called()


def test_reply_photo_failure_does_not_crash_workflow(tmp_path: Path) -> None:
    bot = TelegramBot(make_settings(tmp_path))
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
    update, message = make_update()
    message.reply_photo = AsyncMock(side_effect=RuntimeError("telegram is down"))

    # Must not raise even though Telegram rejected the photo.
    run_workflow(bot, update, ToolResult("done", artifacts=(png,)), final="گزارش نهایی")

    warning_texts = " ".join(str(call.args[0]) for call in message.reply_text.call_args_list if call.args)
    assert "ارسال تصویر به تلگرام ناموفق بود" in warning_texts
    assert "shot.png" in warning_texts
    # The final report still reaches the user.
    assert "گزارش نهایی" in warning_texts


def test_terminal_screenshot_flow_is_preserved(tmp_path: Path) -> None:
    bot = TelegramBot(make_settings(tmp_path))
    update, message = make_update()
    command_output = ToolResult("$ dir\n\nfile.txt\n\n[exit code: 0]")

    run_workflow(bot, update, command_output)

    message.reply_photo.assert_called_once()
    kwargs = message.reply_photo.call_args.kwargs
    assert kwargs["caption"] == "🖥️ اسکرین‌شات خروجی دستورها/تست‌ها"


def test_artifact_validation_accepts_only_workspace_png(tmp_path: Path) -> None:
    bot = TelegramBot(make_settings(tmp_path))
    png = tmp_path / "ok.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nf")
    assert bot._validated_image_artifact(png) == png.resolve()
    assert bot._validated_image_artifact(tmp_path / "ok.jpg") is None
    assert bot._validated_image_artifact(tmp_path / "nope.png") is None
    assert bot._validated_image_artifact(png.parent) is None
    assert bot._validated_image_artifact(None) is None


def test_clean_chat_text_removes_noisy_markdown() -> None:
    raw = """
# انجام شد ✅
طبق دسته‌بندی پیشنهادی، **۲۹ فایل** که در ریشه‌ی `Desktop\\Downloads` بودند منتقل شدند.

- `*.jpg / تصاویر` → `Downloads\\Images`
- [مستندات](https://example.com) بررسی شد
```text
خلاصه بدون قاب کد
```
"""

    cleaned = clean_chat_text(raw)

    assert "#" not in cleaned
    assert "**" not in cleaned
    assert "`" not in cleaned
    assert "```" not in cleaned
    assert "۲۹ فایل" in cleaned
    assert "Desktop\\Downloads" in cleaned
    assert "• *.jpg / تصاویر" in cleaned
    assert "مستندات (https://example.com)" in cleaned
