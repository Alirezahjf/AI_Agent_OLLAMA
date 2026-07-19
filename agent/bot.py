from __future__ import annotations

import asyncio
from io import BytesIO
import textwrap
import uuid

from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .brain import OllamaAgent, Pending
from .config import Settings
from .storage import Storage
from .tools import LocalTools


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ گفتگوی جدید", callback_data="new"),
                InlineKeyboardButton("🧹 پاک‌کردن حافظه", callback_data="reset"),
            ],
            [InlineKeyboardButton("📌 وضعیت", callback_data="status")],
        ]
    )


def terminal_image(content: str) -> BytesIO:
    """Make a Telegram-friendly terminal screenshot without requiring a desktop session."""
    lines: list[str] = []
    for line in content[:12000].splitlines() or ["(بدون خروجی)"]:
        lines.extend(
            textwrap.wrap(line, width=105, replace_whitespace=False, drop_whitespace=False) or [""]
        )
    lines = lines[:120]
    font = ImageFont.load_default()
    width, height = 1050, max(150, 58 + len(lines) * 15)
    image = Image.new("RGB", (width, height), "#111827")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 38), fill="#1f2937")
    draw.ellipse((18, 13, 28, 23), fill="#ef4444")
    draw.ellipse((34, 13, 44, 23), fill="#f59e0b")
    draw.ellipse((50, 13, 60, 23), fill="#22c55e")
    draw.text((78, 12), "Agent terminal output", font=font, fill="#d1d5db")
    for i, line in enumerate(lines):
        draw.text((18, 46 + i * 15), line, font=font, fill="#d1fae5")
    data = BytesIO()
    image.save(data, "PNG")
    data.name = "terminal-output.png"
    data.seek(0)
    return data


class TelegramBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = Storage(settings.data_dir / "agent.sqlite3")
        self.tools = LocalTools(
            settings.workspace_root, settings.command_timeout, settings.max_output_chars
        )
        self.agent = OllamaAgent(settings, self.storage, self.tools)

    def authorized(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user) and (
            not self.settings.allowed_user_ids or user.id in self.settings.allowed_user_ids
        )

    async def deny_if_needed(self, update: Update) -> bool:
        if self.authorized(update):
            return False
        if update.effective_message:
            await update.effective_message.reply_text("⛔ شما مجاز به استفاده از این عامل نیستید.")
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_message and update.effective_user
        warning = (
            "\n⚠️ ALLOWED_TELEGRAM_USER_IDS خالی است؛ پیش از استفادهٔ واقعی آن را تنظیم کنید."
            if not self.settings.allowed_user_ids
            else ""
        )
        await update.effective_message.reply_text(
            "سلام. من عامل کدنویسی محلی شما هستم. درخواستت را طبیعی و فارسی بنویس؛ "
            "فایل‌ها را بررسی می‌کنم، برای تغییر/اجرای دستور از تو تأیید می‌گیرم و خروجی ترمینال را تصویر می‌فرستم.\n\n"
            f"شناسهٔ تلگرام شما: `{update.effective_user.id}`\nWorkspace: `{self.settings.workspace_root}`{warning}",
            parse_mode="Markdown",
            reply_markup=menu(),
        )

    async def _run(self, update: Update, user_text: str | None, resume: dict | None = None) -> None:
        assert update.effective_chat and update.effective_message
        chat_id = update.effective_chat.id
        status = await update.effective_message.reply_text("⏳ درخواست دریافت شد…")
        loop = asyncio.get_running_loop()

        def progress(text: str) -> None:
            asyncio.run_coroutine_threadsafe(status.edit_text(text), loop)

        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            final, pending, output = await asyncio.to_thread(
                self.agent.run, chat_id, user_text, progress, resume
            )
        except RuntimeError as exc:
            await status.edit_text(f"❌ {exc}")
            return
        if pending:
            action_id = uuid.uuid4().hex[:16]
            self.storage.put_pending(
                action_id, chat_id, {"tool": pending.tool, "args": pending.args}
            )
            preview = self._preview(pending)
            await status.edit_text("⚠️ این مرحله نیاز به تأیید شما دارد:\n\n" + preview)
            await update.effective_message.reply_text(
                "اجرا شود؟",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ اجرا", callback_data=f"ok:{action_id}"),
                            InlineKeyboardButton("✖️ لغو", callback_data=f"no:{action_id}"),
                        ]
                    ]
                ),
            )
            return
        await status.edit_text("✅ انجام شد.")
        if output and output.text.startswith("$"):
            await update.effective_message.reply_photo(
                terminal_image(output.text), caption="🖥️ تصویر خروجی آخرین دستور"
            )
        if final:
            # Telegram's limit is 4096 chars.
            for chunk in [final[i : i + 4000] for i in range(0, len(final), 4000)]:
                await update.effective_message.reply_text(chunk, reply_markup=menu())

    @staticmethod
    def _preview(pending: Pending) -> str:
        if pending.tool == "run_command":
            return f"ابزار: اجرای دستور\n`{pending.args.get('command', '')}`\nدر: `{pending.args.get('cwd', '.')}`"
        if pending.tool == "write_file":
            return f"ابزار: نوشتن فایل\nمسیر: `{pending.args.get('path', '')}`\nحجم: {len(str(pending.args.get('content', '')))} نویسه"
        return f"ابزار: {pending.tool}\nپارامترها: {pending.args}"

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_message
        await self._run(update, update.effective_message.text)

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        query = update.callback_query
        assert query and update.effective_chat
        await query.answer()
        data, chat_id = query.data, update.effective_chat.id
        if data == "new":
            thread = self.storage.new_chat(chat_id)
            await query.message.reply_text(
                f"✅ گفتگوی جدید شمارهٔ {thread} ساخته شد.", reply_markup=menu()
            )
            return
        if data == "reset":
            self.storage.reset(chat_id)
            await query.message.reply_text("🧹 حافظهٔ گفتگوی فعلی پاک شد.", reply_markup=menu())
            return
        if data == "status":
            await query.message.reply_text(
                f"مدل: `{self.settings.ollama_model}`\nWorkspace: `{self.settings.workspace_root}`\nتأیید خودکار تغییرات: `{self.settings.auto_approve_mutations}`",
                parse_mode="Markdown",
                reply_markup=menu(),
            )
            return
        verb, action_id = data.split(":", 1)
        pending = self.storage.pop_pending(action_id, chat_id)
        if not pending:
            await query.message.reply_text("این درخواست منقضی شده یا قبلاً رسیدگی شده است.")
            return
        if verb == "no":
            self.storage.add(chat_id, "user", "کاربر اجرای تغییر را رد کرد.")
            await query.message.reply_text(
                "✖️ لغو شد. می‌توانید دستور دیگری بدهید.", reply_markup=menu()
            )
            return
        await query.message.reply_text("✅ تأیید شد.")
        # Construct a small Update-compatible execution entry using the callback message.
        await self._run(update, None, pending)

    def application(self) -> Application:
        app = Application.builder().token(self.settings.telegram_token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CallbackQueryHandler(self.callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text))
        return app


def main() -> None:
    settings = Settings.from_env()
    TelegramBot(settings).application().run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
