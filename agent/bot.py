"""Telegram and Bale UIs: approvals, provider setup, history, and terminal screenshots."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
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
from .providers import PROFILES, ProviderError, ProviderRouter
from .storage import Storage
from .tools import LocalTools, ToolResult


@dataclass
class SetupState:
    kind: str  # key, detected_key, model
    provider: str | None = None
    api_key: str = ""  # process memory only; never passed to Storage


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ گفتگوی جدید", callback_data="new"),
                InlineKeyboardButton("🧹 پاک‌کردن حافظه", callback_data="reset"),
            ],
            [
                InlineKeyboardButton("⚙️ مدل و API", callback_data="cfg"),
                InlineKeyboardButton("📜 تاریخچه", callback_data="history"),
            ],
            [InlineKeyboardButton("📌 وضعیت", callback_data="status")],
        ]
    )


def terminal_image(content: str) -> BytesIO:
    """Create a Telegram-friendly PNG terminal capture without desktop access."""
    lines: list[str] = []
    for line in content[:12000].splitlines() or ["(بدون خروجی)"]:
        lines.extend(textwrap.wrap(line, width=105, replace_whitespace=False, drop_whitespace=False) or [""])
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
    for index, line in enumerate(lines):
        draw.text((18, 46 + index * 15), line, font=font, fill="#d1fae5")
    data = BytesIO()
    image.save(data, "PNG")
    data.name = "terminal-output.png"
    data.seek(0)
    return data


_MD_LINK = re.compile(r"\[([^\[\]]{1,300})\]\(([^\s()\[\]]{1,600})\)")
_MD_INLINE_CODE = re.compile(r"`([^`\n]{1,500})`")
_MD_BOLD_ITALIC = re.compile(r"(\*\*|__|~~)(.*?)\1")
_MD_ITALIC = re.compile(r"(?<!\w)(\*|_)([^\n*_]{1,300})\1(?!\w)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_ORDERED_LIST = re.compile(r"^\s*\d+[.)]\s+")
_MD_UNORDERED_LIST = re.compile(r"^\s*[-*+]\s+")


def clean_chat_text(text: str) -> str:
    """Render model reports as clean plain Persian text for Telegram/Bale.

    The model may still emit Markdown despite instructions. Bale also applies
    Markdown-like rendering to all messages, so visible report text should not
    contain #, **, backticks, fenced blocks, or noisy Markdown list syntax.
    Command output and file previews are intentionally not passed through this.
    """
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"^\s*```[A-Za-z0-9_-]*\s*$", "", value, flags=re.MULTILINE)
    value = _MD_LINK.sub(lambda match: f"{match.group(1)} ({match.group(2)})", value)
    value = _MD_INLINE_CODE.sub(lambda match: match.group(1), value)
    for _ in range(4):
        changed = _MD_BOLD_ITALIC.sub(lambda match: match.group(2), value)
        changed = _MD_ITALIC.sub(lambda match: match.group(2), changed)
        if changed == value:
            break
        value = changed

    clean_lines: list[str] = []
    for raw_line in value.split("\n"):
        line = _MD_HEADING.sub("", raw_line).strip()
        if not line:
            clean_lines.append("")
            continue
        if _MD_ORDERED_LIST.match(line):
            line = "• " + _MD_ORDERED_LIST.sub("", line).strip()
        elif _MD_UNORDERED_LIST.match(line):
            line = "• " + _MD_UNORDERED_LIST.sub("", line).strip()
        line = re.sub(r"\s{2,}", " ", line)
        clean_lines.append(line)

    compact: list[str] = []
    blank_count = 0
    for line in clean_lines:
        if line:
            blank_count = 0
            compact.append(line)
        else:
            blank_count += 1
            if blank_count <= 1:
                compact.append("")
    return "\n".join(compact).strip() or text.strip()


class TelegramBot:
    storage_filename = "agent.sqlite3"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = Storage(settings.data_dir / self.storage_filename)
        self.tools = LocalTools(settings.workspace_root, settings.command_timeout, settings.max_output_chars)
        self.providers = ProviderRouter(settings)
        self.agent = OllamaAgent(settings, self.storage, self.tools, self.providers)
        self.setup: dict[int, SetupState] = {}

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

    def _selection(self, chat_id: int) -> tuple[str, str]:
        provider, model = self.storage.preference(chat_id, self.settings.default_provider)
        return provider, model or self.providers.default_model(provider)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_message and update.effective_user
        provider, model = self._selection(update.effective_chat.id)  # type: ignore[union-attr]
        warning = (
            "\n⚠️ ALLOWED_TELEGRAM_USER_IDS خالی است؛ قبل از استفادهٔ واقعی allow-list را تنظیم کنید."
            if not self.settings.allowed_user_ids
            else ""
        )
        await update.effective_message.reply_text(
            "سلام. من عامل حرفه‌ای محلی شما هستم: درخواست را تحلیل می‌کنم، ساختار و فایل‌ها را واقعاً بررسی می‌کنم، "
            "تغییر را فقط با تأیید انجام می‌دهم، سپس تست و نتیجه را گزارش می‌دهم.\n\n"
            "برای API ابری از «مدل و API» استفاده کنید؛ کلیدی که در ربات وارد می‌کنید فقط تا زمان اجرای همین برنامه در حافظه می‌ماند و در SQLite ذخیره نمی‌شود.\n\n"
            f"شناسهٔ تلگرام شما: {update.effective_user.id}\n"
            f"Workspace: {self.settings.workspace_root}\n"
            f"مدل فعال: {PROFILES[provider].title} / {model}{warning}",
            reply_markup=menu(),
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_chat and update.effective_message
        provider, model = self._selection(update.effective_chat.id)
        source = self.providers.key_source(update.effective_chat.id, provider)
        await update.effective_message.reply_text(
            "📌 وضعیت عامل\n\n"
            f"ارائه‌دهنده: {PROFILES[provider].title}\n"
            f"مدل: {model}\n"
            f"منبع کلید: {source}\n"
            f"Workspace: {self.settings.workspace_root}\n"
            f"تأیید خودکار تغییرات: {self.settings.auto_approve_mutations}\n"
            f"حداکثر مراحل هر workflow: {self.settings.max_agent_turns}\n"
            "فایل‌های secret و .env حتی در workspace نیز محافظت می‌شوند.",
            reply_markup=menu(),
        )

    async def show_config(self, message, chat_id: int) -> None:
        provider, model = self._selection(chat_id)
        await message.reply_text(
            "⚙️ انتخاب مدل و API\n\n"
            f"فعلی: {PROFILES[provider].title} / {model}\n"
            "Ollama محلی کلید نمی‌خواهد. برای GapGPT و AvalAI می‌توانید کلید را در .env بگذارید یا موقتاً در همین جلسه وارد کنید. "
            "تشخیص خودکار با endpoint مدل‌ها انجام می‌شود؛ اگر تشخیص اشتباه بود، ارائه‌دهنده را دستی انتخاب کنید.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🖥 Ollama", callback_data="p:ollama"),
                        InlineKeyboardButton("☁️ GapGPT", callback_data="p:gapgpt"),
                    ],
                    [
                        InlineKeyboardButton("☁️ AvalAI", callback_data="p:avalai"),
                        InlineKeyboardButton("🔎 تشخیص API", callback_data="p:auto"),
                    ],
                    [InlineKeyboardButton("🤖 انتخاب مدل", callback_data="m:show")],
                    [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")],
                ]
            ),
        )

    async def show_models(self, message, chat_id: int) -> None:
        provider, current = self._selection(chat_id)
        profile = PROFILES[provider]
        rows: list[list[InlineKeyboardButton]] = []
        for index, (model, label) in enumerate(profile.recommended_models):
            title = f"{'✅ ' if model == current else ''}{model}"
            rows.append([InlineKeyboardButton(title, callback_data=f"m:{provider}:{index}")])
        rows.append([InlineKeyboardButton("✍️ مدل دلخواه", callback_data="m:custom")])
        rows.append([InlineKeyboardButton("🔄 دریافت مدل‌های قابل‌دسترسی", callback_data="m:refresh")])
        rows.append([InlineKeyboardButton("⬅️ تنظیمات", callback_data="cfg")])
        recommended = "\n".join(f"• {name}: {description}" for name, description in profile.recommended_models)
        await message.reply_text(
            f"🤖 مدل‌های پیشنهادی {profile.title}\n{profile.description}\n\n{recommended}\n\n"
            "مدل‌های frontier می‌توانند هزینهٔ قابل‌توجه داشته باشند؛ پیش از اجرای workflow بزرگ، قیمت/اعتبار حساب را بررسی کنید.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def show_history(self, message, chat_id: int) -> None:
        transcript = self.storage.transcript(chat_id)
        events = self.storage.recent_audit(chat_id)
        if not transcript and not events:
            text = "📜 هنوز پیامی یا رخدادی در گفتگوی فعلی ثبت نشده است."
        else:
            lines = ["📜 آخرین رویدادهای گفتگوی فعلی"]
            for event in events[-8:]:
                lines.append(f"• [{event['created_at']}] {event['event_type']}: {event['detail'][:250]}")
            if transcript:
                lines.append("\nآخرین پیام‌ها:")
                for item in transcript[-6:]:
                    content = item["content"].replace("\n", " ")[:300]
                    lines.append(f"• {item['role']}: {content}")
            text = "\n".join(lines)
        await message.reply_text(text[:4000], reply_markup=menu())

    async def _run(self, update: Update, user_text: str | None, resume: dict | None = None) -> None:
        assert update.effective_chat and update.effective_message
        chat_id = update.effective_chat.id
        status = await update.effective_message.reply_text("⏳ درخواست دریافت شد…")
        loop = asyncio.get_running_loop()

        def progress(text: str) -> None:
            future = asyncio.run_coroutine_threadsafe(status.edit_text(text), loop)
            # Telegram can reject a repeated identical edit; that must not abort the agent thread.
            future.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            final, pending, output = await asyncio.to_thread(self.agent.run, chat_id, user_text, progress, resume)
        except (ProviderError, RuntimeError) as exc:
            await status.edit_text(f"❌ {exc}")
            return
        except Exception as exc:  # Do not expose a stack trace or secret-bearing request objects to Telegram.
            await status.edit_text(f"❌ خطای پیش‌بینی‌نشده در workflow: {type(exc).__name__}: {exc}")
            return

        if pending:
            action_id = uuid.uuid4().hex[:16]
            self.storage.put_pending(action_id, chat_id, {"tool": pending.tool, "args": pending.args})
            await status.edit_text("⚠️ workflow در مرحلهٔ نیازمند تأیید متوقف شده است.")
            await update.effective_message.reply_text(
                self._preview(pending),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ تأیید و اجرا", callback_data=f"ok:{action_id}"),
                            InlineKeyboardButton("✖️ لغو", callback_data=f"no:{action_id}"),
                        ]
                    ]
                ),
            )
            return

        await status.edit_text("✅ مرحلهٔ workflow انجام شد.")
        terminal_text = ""
        if output:
            terminal_text = output.terminal_text or (output.text if output.text.startswith("$") else "")
        if terminal_text:
            await update.effective_message.reply_photo(
                terminal_image(terminal_text), caption="🖥️ اسکرین‌شات خروجی دستورها/تست‌ها"
            )
        if output:
            await self._send_artifacts(update.effective_message, output)
        if final:
            clean_final = clean_chat_text(final)
            for chunk in [
                clean_final[index : index + 4000] for index in range(0, len(clean_final), 4000)
            ]:
                await update.effective_message.reply_text(chunk, reply_markup=menu())

    def _validated_image_artifact(self, artifact: object) -> Path | None:
        """Only an existing PNG below WORKSPACE_ROOT may be sent to Telegram."""
        try:
            path = Path(artifact).resolve()  # type: ignore[arg-type]
            if path.suffix.lower() != ".png" or not path.is_file():
                return None
            path.relative_to(self.settings.workspace_root.resolve())
        except (OSError, TypeError, ValueError):
            return None
        return path

    async def _send_artifacts(self, message, output: ToolResult) -> None:
        """Send real files produced by the workflow (e.g. web screenshots).

        A Telegram failure here must never crash the workflow; the user still gets
        the text report and the file stays in the workspace.
        """
        for artifact in output.artifacts:
            path = self._validated_image_artifact(artifact)
            if path is None:
                await message.reply_text("⚠️ یکی از فایل‌های خروجی عامل معتبر نبود و ارسال نشد.")
                continue
            try:
                with path.open("rb") as handle:
                    await message.reply_photo(handle, caption="📸 اسکرین‌شات ساخته‌شده توسط عامل")
            except Exception as exc:  # network/API failures are operational, not fatal
                await message.reply_text(
                    f"⚠️ ارسال تصویر به تلگرام ناموفق بود ({type(exc).__name__}). "
                    f"فایل در workspace ذخیره شده است: {path.name}"
                )

    @staticmethod
    def _preview(pending: Pending) -> str:
        args = pending.args
        plan = f"📋 برنامهٔ عامل: {pending.note}\n\n" if pending.note else ""
        if pending.tool == "run_command":
            return plan + f"⚠️ اجرای دستور نیاز به تأیید دارد\n\nدستور:\n{args.get('command', '')}\n\nپوشه: {args.get('cwd', '.')}"
        if pending.tool == "write_file":
            content = str(args.get("content", ""))
            sample = content[:700] + ("\n… [پیش‌نمایش کوتاه شد]" if len(content) > 700 else "")
            return plan + f"⚠️ نوشتن فایل نیاز به تأیید دارد\n\nمسیر: {args.get('path', '')}\nحجم: {len(content)} نویسه\n\nپیش‌نمایش:\n{sample}"
        if pending.tool == "patch_file":
            return plan + (
                "⚠️ اعمال patch نیاز به تأیید دارد\n\n"
                f"مسیر: {args.get('path', '')}\n"
                f"قطعهٔ جست‌وجو: {len(str(args.get('expected_text', '')))} نویسه\n"
                f"جایگزین: {len(str(args.get('replacement', '')))} نویسه"
            )
        if pending.tool == "create_directory":
            return plan + f"⚠️ ساخت پوشه نیاز به تأیید دارد\n\nمسیر: {args.get('path', '')}"
        if pending.tool == "organize_files":
            return plan + f"⚠️ جابه‌جایی و دسته‌بندی فایل‌ها نیاز به تأیید دارد\n\nپوشه: {args.get('path', '.')}\nبدون overwrite انجام می‌شود."
        if pending.tool == "capture_screenshot":
            return plan + f"⚠️ ساخت فایل اسکرین‌شات نیاز به تأیید دارد\n\nURL: {args.get('url', '')}\nخروجی: {args.get('output_path', '')}"
        return plan + f"⚠️ ابزار {pending.tool} نیاز به تأیید دارد.\nپارامترها: {args}"

    async def _consume_setup_text(self, update: Update) -> bool:
        """Consume an API key/custom model before it reaches the language model or SQLite."""
        assert update.effective_chat and update.effective_message
        chat_id, message = update.effective_chat.id, update.effective_message
        state = self.setup.get(chat_id)
        if not state:
            return False
        value = (message.text or "").strip()
        if state.kind == "model":
            if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", value):
                await message.reply_text("نام مدل نامعتبر است. فقط حروف/عدد و . _ : / - مجاز است.")
                return True
            provider, _ = self._selection(chat_id)
            self.storage.set_preference(chat_id, provider, value)
            self.setup.pop(chat_id, None)
            await message.reply_text(f"✅ مدل فعال شد: {value}", reply_markup=menu())
            return True

        if len(value) < 10 or len(value) > 1024 or any(char.isspace() for char in value):
            await message.reply_text("کلید API نامعتبر به نظر می‌رسد. کلید را بدون فاصله وارد کنید یا از تنظیمات خارج شوید.")
            return True
        # Best-effort deletion limits visible retention in chats where Telegram permits it.
        try:
            await message.delete()
        except Exception:
            pass
        progress = await update.effective_chat.send_message(  # type: ignore[union-attr]
            "🔐 کلید دریافت شد؛ در حال اعتبارسنجی امن بدون ذخیره‌سازی…"
        )

        if state.kind == "key" and state.provider:
            await self._validate_and_select(chat_id, state.provider, value, progress)
            return True

        provider, diagnostics = await asyncio.to_thread(self.providers.detect_provider, value)
        if provider:
            self.providers.set_session_key(chat_id, provider, value)
            self.setup[chat_id] = SetupState("detected_key", provider, value)
            await progress.edit_text(
                f"🔎 کلید با {PROFILES[provider].title} تشخیص داده شد. درست است؟",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ درست است", callback_data=f"d:yes:{provider}"),
                            InlineKeyboardButton("↩️ اشتباه است", callback_data="d:no"),
                        ]
                    ]
                ),
            )
        else:
            # Keep it only in RAM so a manual provider selection can retry it.
            self.setup[chat_id] = SetupState("detected_key", None, value)
            await progress.edit_text(
                "تشخیص خودکار قطعی نبود. ارائه‌دهنده را دستی انتخاب کنید؛ کلید همچنان فقط در حافظهٔ همین process است.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("GapGPT", callback_data="p:gapgpt"),
                            InlineKeyboardButton("AvalAI", callback_data="p:avalai"),
                        ],
                        [InlineKeyboardButton("لغو و پاک‌کردن کلید", callback_data="d:cancel")],
                    ]
                ),
            )
        return True

    async def _validate_and_select(self, chat_id: int, provider: str, api_key: str, progress) -> None:
        try:
            models = await asyncio.to_thread(self.providers.validate_key, provider, api_key)
        except ProviderError as exc:
            self.setup[chat_id] = SetupState("key", provider)
            await progress.edit_text(f"❌ اعتبارسنجی {PROFILES[provider].title} ناموفق بود: {exc}\nکلید را دوباره وارد کنید یا ارائه‌دهنده را تغییر دهید.")
            return
        self.providers.set_session_key(chat_id, provider, api_key)
        default = self.providers.default_model(provider)
        self.storage.set_preference(chat_id, provider, default)
        self.setup.pop(chat_id, None)
        model_hint = f" ({len(models)} مدل قابل‌دسترسی شناسایی شد)" if models else ""
        await progress.edit_text(
            f"✅ {PROFILES[provider].title} فعال شد{model_hint}.\nمدل پیشنهادی: {default}",
            reply_markup=menu(),
        )

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_message
        if await self._consume_setup_text(update):
            return
        await self._run(update, update.effective_message.text)

    async def set_model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_chat and update.effective_message
        value = " ".join(context.args).strip()
        if not value:
            await update.effective_message.reply_text("استفاده: /model نام-مدل", reply_markup=menu())
            return
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", value):
            await update.effective_message.reply_text("نام مدل نامعتبر است.", reply_markup=menu())
            return
        provider, _ = self._selection(update.effective_chat.id)
        self.storage.set_preference(update.effective_chat.id, provider, value)
        await update.effective_message.reply_text(f"✅ مدل فعال شد: {value}", reply_markup=menu())

    async def models_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_chat and update.effective_message
        provider, model = self._selection(update.effective_chat.id)
        try:
            client = self.providers.client_for(update.effective_chat.id, provider, model)
            models = await asyncio.to_thread(client.list_models)
        except ProviderError as exc:
            await update.effective_message.reply_text(f"❌ {exc}", reply_markup=menu())
            return
        text = "\n".join(f"• {item}" for item in models[:100]) or "مدلی برنگشت."
        await update.effective_message.reply_text(f"مدل‌های {PROFILES[provider].title}:\n{text}"[:4000], reply_markup=menu())

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        query = update.callback_query
        assert query and query.message and update.effective_chat
        try:
            await query.answer()
        except Exception:
            # Callback answering only clears the client's loading state. Older Bale clients may not
            # support it, and a transient API failure must not prevent handling the actual action.
            pass
        data, chat_id = query.data or "", update.effective_chat.id
        if data == "home":
            await query.message.reply_text("منوی اصلی", reply_markup=menu())
            return
        if data == "cfg":
            await self.show_config(query.message, chat_id)
            return
        if data == "history":
            await self.show_history(query.message, chat_id)
            return
        if data == "new":
            self.setup.pop(chat_id, None)
            thread = self.storage.new_chat(chat_id)
            await query.message.reply_text(f"✅ گفتگوی جدید شمارهٔ {thread} ساخته شد.", reply_markup=menu())
            return
        if data == "reset":
            self.storage.reset(chat_id)
            await query.message.reply_text("🧹 حافظهٔ گفتگوی فعلی پاک شد.", reply_markup=menu())
            return
        if data == "status":
            # Callback messages are valid Update effective messages, so use a direct response.
            provider, model = self._selection(chat_id)
            await query.message.reply_text(
                f"مدل: {PROFILES[provider].title} / {model}\nمنبع کلید: {self.providers.key_source(chat_id, provider)}\n"
                f"Workspace: {self.settings.workspace_root}",
                reply_markup=menu(),
            )
            return
        if data.startswith("p:"):
            provider = data.split(":", 1)[1]
            if provider == "ollama":
                self.setup.pop(chat_id, None)
                self.storage.set_preference(chat_id, "ollama", self.providers.default_model("ollama"))
                await query.message.reply_text("✅ Ollama محلی فعال شد.", reply_markup=menu())
                return
            if provider == "auto":
                self.setup[chat_id] = SetupState("key")
                await query.message.reply_text(
                    "کلید API را در یک پیام جداگانه بفرستید. تلاش می‌کنم GapGPT یا AvalAI را با /models تشخیص دهم. "
                    "پیام کلید پس از دریافت، در صورت امکان حذف می‌شود و خود کلید در دیتابیس ذخیره نمی‌شود."
                )
                return
            if provider in {"gapgpt", "avalai"}:
                old = self.setup.get(chat_id)
                if old and old.kind == "detected_key" and old.api_key:
                    progress = await query.message.reply_text(f"🔐 در حال بررسی کلید برای {PROFILES[provider].title}…")
                    await self._validate_and_select(chat_id, provider, old.api_key, progress)
                else:
                    self.setup[chat_id] = SetupState("key", provider)
                    await query.message.reply_text(
                        f"کلید {PROFILES[provider].title} را در یک پیام جداگانه بفرستید. کلید فقط در RAM همین process نگهداری می‌شود؛ "
                        "برای نگهداری امن‌تر و پایدار، آن را در متغیر محیطی قرار دهید."
                    )
                return
        if data.startswith("d:"):
            parts = data.split(":")
            verb = parts[1] if len(parts) > 1 else ""
            if verb == "cancel":
                self.setup.pop(chat_id, None)
                self.providers.clear_session_key(chat_id)
                await query.message.reply_text("کلید موقت از حافظهٔ process پاک شد.", reply_markup=menu())
                return
            if verb == "no":
                state = self.setup.get(chat_id)
                if state:
                    state.provider = None
                await self.show_config(query.message, chat_id)
                return
            if verb == "yes" and len(parts) == 3 and parts[2] in PROFILES:
                provider = parts[2]
                state = self.setup.get(chat_id)
                if not state or not state.api_key:
                    await query.message.reply_text("کلید موقت منقضی شده؛ دوباره واردش کنید.", reply_markup=menu())
                    return
                self.storage.set_preference(chat_id, provider, self.providers.default_model(provider))
                self.setup.pop(chat_id, None)
                await query.message.reply_text(
                    f"✅ {PROFILES[provider].title} و مدل پیشنهادی فعال شد.", reply_markup=menu()
                )
                return
        if data.startswith("m:"):
            parts = data.split(":")
            if data == "m:show":
                await self.show_models(query.message, chat_id)
                return
            if data == "m:custom":
                self.setup[chat_id] = SetupState("model")
                await query.message.reply_text("نام دقیق مدل را بفرستید (مثال: claude-sonnet-5 یا qwen2.5:7b).")
                return
            if data == "m:refresh":
                provider, model = self._selection(chat_id)
                try:
                    client = self.providers.client_for(chat_id, provider, model)
                    models = await asyncio.to_thread(client.list_models)
                except ProviderError as exc:
                    await query.message.reply_text(f"❌ {exc}")
                    return
                listing = "\n".join(f"• {item}" for item in models[:80]) or "مدلی برنگشت."
                await query.message.reply_text(f"مدل‌های قابل‌دسترسی:\n{listing}"[:4000], reply_markup=menu())
                return
            if len(parts) == 3 and parts[1] in PROFILES and parts[2].isdigit():
                provider, index = parts[1], int(parts[2])
                options = PROFILES[provider].recommended_models
                if index >= len(options):
                    await query.message.reply_text("انتخاب مدل معتبر نیست.")
                    return
                model = self.providers.default_model("ollama") if options[index][0] == "local" else options[index][0]
                self.storage.set_preference(chat_id, provider, model)
                await query.message.reply_text(f"✅ مدل فعال شد: {PROFILES[provider].title} / {model}", reply_markup=menu())
                return
        if data.startswith(("ok:", "no:")):
            verb, action_id = data.split(":", 1)
            pending = self.storage.pop_pending(action_id, chat_id)
            if not pending:
                await query.message.reply_text("این درخواست منقضی شده یا قبلاً رسیدگی شده است.")
                return
            if verb == "no":
                self.storage.add(chat_id, "user", "کاربر اجرای مرحلهٔ پیشنهادی را رد کرد.")
                self.storage.audit(chat_id, "action_rejected", str(pending.get("tool", "unknown")))
                await query.message.reply_text("✖️ لغو شد. می‌توانید روش دیگری بخواهید.", reply_markup=menu())
                return
            self.storage.audit(chat_id, "action_approved", str(pending.get("tool", "unknown")))
            await query.message.reply_text("✅ تأیید شد؛ workflow ادامه پیدا می‌کند.")
            await self._run(update, None, pending)
            return
        await query.message.reply_text("دکمهٔ ناشناخته یا منقضی‌شده است.", reply_markup=menu())

    def application(self) -> Application:
        app = Application.builder().token(self.settings.telegram_token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("models", self.models_command))
        app.add_handler(CommandHandler("model", self.set_model_command))
        app.add_handler(CallbackQueryHandler(self.callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text))
        return app


class BaleBot(TelegramBot):
    """Bale messenger UI with the same handlers and agent workflow as Telegram.

    Bale's Bot API is intentionally Telegram-compatible for the methods this
    project uses (long polling, commands, inline keyboards, callback queries,
    message edits/deletes, chat actions, and photo uploads). Therefore the
    safest way to keep feature parity is to run the exact same handler methods
    through python-telegram-bot, but point its Bot API base URLs at Bale.
    """

    storage_filename = "agent_bale.sqlite3"

    def authorized(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user) and (
            not self.settings.allowed_bale_user_ids
            or user.id in self.settings.allowed_bale_user_ids
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_message and update.effective_user
        provider, model = self._selection(update.effective_chat.id)  # type: ignore[union-attr]
        warning = (
            "\n⚠️ ALLOWED_BALE_USER_IDS خالی است؛ قبل از استفادهٔ واقعی allow-list را تنظیم کنید."
            if not self.settings.allowed_bale_user_ids
            else ""
        )
        await update.effective_message.reply_text(
            "سلام. من عامل حرفه‌ای محلی شما در بله هستم: درخواست را تحلیل می‌کنم، "
            "ساختار و فایل‌ها را واقعاً بررسی می‌کنم، تغییر را فقط با تأیید انجام می‌دهم، "
            "سپس تست و نتیجه را گزارش می‌دهم.\n\n"
            "برای API ابری از «مدل و API» استفاده کنید؛ کلیدی که در ربات وارد می‌کنید "
            "فقط تا زمان اجرای همین برنامه در حافظه می‌ماند و در SQLite ذخیره نمی‌شود.\n\n"
            f"شناسهٔ بله شما: {update.effective_user.id}\n"
            f"Workspace: {self.settings.workspace_root}\n"
            f"مدل فعال: {PROFILES[provider].title} / {model}{warning}",
            reply_markup=menu(),
        )

    async def _send_artifacts(self, message, output: ToolResult) -> None:
        for artifact in output.artifacts:
            path = self._validated_image_artifact(artifact)
            if path is None:
                await message.reply_text("⚠️ یکی از فایل‌های خروجی عامل معتبر نبود و ارسال نشد.")
                continue
            try:
                with path.open("rb") as handle:
                    await message.reply_photo(handle, caption="📸 اسکرین‌شات ساخته‌شده توسط عامل")
            except Exception as exc:  # network/API failures are operational, not fatal
                await message.reply_text(
                    f"⚠️ ارسال تصویر به بله ناموفق بود ({type(exc).__name__}). "
                    f"فایل در workspace ذخیره شده است: {path.name}"
                )

    @staticmethod
    def _endpoint_with_bot_prefix(base_url: str) -> str:
        base = base_url.rstrip("/")
        return base if base.endswith("/bot") else f"{base}/bot"

    def application(self) -> Application:
        bale_api = self._endpoint_with_bot_prefix(self.settings.bale_base_url)
        bale_file_api = self._endpoint_with_bot_prefix(self.settings.bale_base_file_url)
        app = (
            Application.builder()
            .token(self.settings.bale_token)
            .base_url(bale_api)
            .base_file_url(bale_file_api)
            .build()
        )
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("models", self.models_command))
        app.add_handler(CommandHandler("model", self.set_model_command))
        app.add_handler(CallbackQueryHandler(self.callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text))
        return app


def main() -> None:
    settings = Settings.from_env(require_messenger="telegram")
    TelegramBot(settings).application().run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
