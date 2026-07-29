"""Telegram / Bale bot that drives the local Bridge.

The bot is intentionally thin.  Its responsibilities are:

  1. Translate Telegram / Bale messages into Bridge ``chat`` calls.
  2. Stream Bridge events back as chat messages, edits, or photos.
  3. Convert inline approval requests into keyboard callbacks.
  4. Handle ``/status``, ``/actions``, ``/model``, ``/reset`` as
     direct RPC calls.

It never executes a tool by itself.  The Bridge owns the desktop
session; the bot is a remote control.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ...core.config import AssistantSettings
from ...core.errors import AssistantError
from ...core.logging_setup import get_logger, setup_logging
from ...bridge import BridgeClient, BridgeConnectionError
from ...bridge.api.handlers import EventType
from ...bridge.protocol import ActionResult, Event


logger = get_logger("bridge.bot")


# ---------------------------------------------------------------------------
# Markdown cleanup (Bale applies Markdown to all messages)
# ---------------------------------------------------------------------------


_MD_LINK = re.compile(r"\[([^\[\]]{1,300})\]\((?:https?://)?[^\s()\[\]]{1,600}\)")
_MD_CODE = re.compile(r"`([^`\n]{1,500})`")
_MD_BOLD_ITALIC = re.compile(r"(\*\*|__|~~)(.*?)\1")
_MD_ITALIC = re.compile(r"(?<!\w)(\*|_)([^\n*_]{1,300})\1(?!\w)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s+")


def clean_chat_text(text: str) -> str:
    """Strip noisy Markdown from a model report for safe rendering."""
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    for _ in range(4):
        new = _MD_LINK.sub(lambda m: m.group(1), value)
        new = _MD_CODE.sub(lambda m: m.group(1), new)
        new = _MD_BOLD_ITALIC.sub(lambda m: m.group(2), new)
        new = _MD_ITALIC.sub(lambda m: m.group(2), new)
        if new == value:
            break
        value = new
    cleaned_lines: list[str] = []
    for raw_line in value.split("\n"):
        line = _MD_HEADING.sub("", raw_line).strip()
        if not line:
            cleaned_lines.append("")
            continue
        line = _MD_LIST.sub("• ", line)
        cleaned_lines.append(re.sub(r"\s{2,}", " ", line))
    return "\n".join(cleaned_lines).strip() or text.strip()


# ---------------------------------------------------------------------------
# Bot base class
# ---------------------------------------------------------------------------


@dataclass
class _PendingApproval:
    request_id: str
    name: str
    arguments: dict[str, Any]
    chat_id: int
    run_id: str


class _BaseBot:
    """Shared logic.  Subclasses set ``app_factory`` and ``run_polling``."""

    storage_filename = "agent.sqlite3"

    def __init__(self, settings: AssistantSettings, client: BridgeClient) -> None:
        self.settings = settings
        self.client = client
        self._pending: dict[str, _PendingApproval] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # --------------------------------------------------------- permission

    def authorized(self, update: Update) -> bool:
        user = update.effective_user
        if not user:
            return False
        allowed = self._allowed_ids()
        return not allowed or user.id in allowed

    def _allowed_ids(self) -> frozenset[int]:
        return self.settings.allowed_user_ids

    async def deny_if_needed(self, update: Update) -> bool:
        if self.authorized(update):
            return False
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ شما مجاز به استفاده از این عامل نیستید."
            )
        return True

    # ----------------------------------------------------------- commands

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_user
        info = self.client.info
        if info is None:
            await update.effective_message.reply_text(
                "❌ Bridge info not available; check the connection."
            )
            return
        await update.effective_message.reply_text(
            f"🤖 سلام! من رابط ربات Bridge روی ماشین شما هستم.\n"
            f"• session: {info.session_id}\n"
            f"• host: {info.hostname}\n"
            f"• user: {info.user}\n"
            f"• capabilities: {', '.join(info.capabilities)}\n\n"
            f"پیام خود را بنویسید تا به Bridge ارسال شود. هر پیامی که در این "
            f"گفتگو می‌نویسید در حافظهٔ Bridge ثبت می‌شود و از هر رابط دیگری "
            f"(وب، CLI) نیز قابل مشاهده است.",
            reply_markup=self._menu(),
        )

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        try:
            status = self.client.get_status()
        except AssistantError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")
            return
        settings = status.get("settings", {})
        history = status.get("history", {})
        lines = ["📌 وضعیت Bridge"]
        for key, value in settings.items():
            lines.append(f"  • {key}: {value}")
        for key, value in history.items():
            lines.append(f"  history.{key}: {value}")
        await update.effective_message.reply_text("\n".join(lines))

    async def actions_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        try:
            descriptions = self.client.list_actions()
        except AssistantError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")
            return
        text = "ابزارها:\n" + "\n".join("  " + d for d in descriptions)
        for chunk in [text[i : i + 3500] for i in range(0, len(text), 3500)]:
            await update.effective_message.reply_text(chunk)

    async def reset_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        self.client.clear_history()
        await update.effective_message.reply_text("🧹 حافظهٔ Bridge پاک شد.")

    async def set_model_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        value = " ".join(context.args or []).strip()
        if not value:
            await update.effective_message.reply_text("استفاده: /model NAME")
            return
        try:
            result = self.client.set_model(model=value)
        except AssistantError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")
            return
        await update.effective_message.reply_text(
            f"✅ مدل فعال شد: {result.get('model')}"
        )

    async def history_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        try:
            history = self.client.get_history(limit=30)
        except AssistantError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")
            return
        text = "📜 تاریخچه\n" + "\n".join(
            f"  [{m.get('role')}] {str(m.get('content', ''))[:200]}" for m in history
        )
        await update.effective_message.reply_text(text[:4000] or "(empty)")

    # --------------------------------------------------------------- chat

    async def text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        assert update.effective_message and update.effective_chat
        user_text = update.effective_message.text or ""
        chat_id = update.effective_chat.id

        # Send "typing" action continuously while the chat runs.
        async def keep_typing() -> None:
            while True:
                try:
                    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
                except Exception:  # noqa: BLE001
                    return
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(keep_typing())
        status_msg = await update.effective_message.reply_text("⏳ در حال ارسال به Bridge…")
        assistant_buffer: list[str] = []
        last_status_text = ""

        try:
            for event in self.client.chat(user_text):
                if typing_task.done():
                    pass  # may have completed; ok
                update_result = await self._render_event(
                    event, chat_id, status_msg, assistant_buffer, last_status_text
                )
                if update_result is not None:
                    last_status_text = update_result
        except AssistantError as exc:
            await status_msg.edit_text(f"❌ {exc}")
            return
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except (asyncio.CancelledError, Exception):
                pass

        # Final assistant text
        if assistant_buffer:
            final = "\n\n".join(assistant_buffer)
            for chunk in [
                clean_chat_text(final)[i : i + 4000]
                for i in range(0, len(clean_chat_text(final)), 4000)
            ]:
                await update.effective_message.reply_text(chunk)

    async def _render_event(
        self,
        event: Event,
        chat_id: int,
        status_msg,
        assistant_buffer: list[str],
        last_text: str,
    ) -> str | None:
        """Edit the status message in place to reflect progress.  Returns the
        new text or None when no edit is needed."""
        if event.type == EventType.TURN_STARTED.value:
            new_text = f"🧠 turn {event.payload.get('turn')}/{event.payload.get('max_turns')}"
        elif event.type == EventType.ASSISTANT_FINAL.value:
            assistant_buffer.append(event.payload.get("text", ""))
            return None  # will be sent at the end
        elif event.type == EventType.TOOL_PROPOSED.value:
            new_text = (
                f"🔧 در حال اجرای {event.payload.get('name')}…\n"
                f"    args: {_short(event.payload.get('arguments', {}))}"
            )
        elif event.type == EventType.TOOL_CONFIRM_REQUESTED.value:
            request_id = event.payload.get("request_id", "")
            name = event.payload.get("name", "?")
            self._pending[request_id] = _PendingApproval(
                request_id=request_id,
                name=name,
                arguments=event.payload.get("arguments", {}),
                chat_id=chat_id,
                run_id=event.run_id,
            )
            args_preview = _short(event.payload.get("arguments", {}), limit=600)
            await status_msg.edit_text(
                f"⚠️ تأیید لازم است: {name}\n\n{args_preview}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ تأیید", callback_data=f"ok:{request_id}"),
                            InlineKeyboardButton("✖️ لغو", callback_data=f"no:{request_id}"),
                        ]
                    ]
                ),
            )
            return None
        elif event.type == EventType.TOOL_RESULT.value:
            preview = str(event.payload.get("text", ""))[:400]
            new_text = f"🔧 {event.payload.get('name')}: {preview}"
        elif event.type == EventType.CHAT_DONE.value:
            return None
        elif event.type == EventType.CHAT_FAILED.value:
            reason = event.payload.get("reason") or event.payload.get("error") or "?"
            new_text = f"❌ chat failed: {reason}"
        else:
            return None
        if new_text == last_text:
            return None
        try:
            await status_msg.edit_text(new_text)
        except Exception:  # noqa: BLE001 - Telegram may reject identical edits
            pass
        return new_text

    # ---------------------------------------------------------- callback

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.deny_if_needed(update):
            return
        query = update.callback_query
        assert query and query.data
        try:
            await query.answer()
        except Exception:  # noqa: BLE001
            pass
        if not query.data or ":" not in query.data:
            return
        verb, request_id = query.data.split(":", 1)
        pending = self._pending.pop(request_id, None)
        if pending is None:
            await query.message.reply_text("این درخواست منقضی شده است.")
            return
        approved = verb == "ok"
        # Send a follow-up RPC to the bridge.  We POST to /confirm; for the
        # in-process backend this is exposed via the resolve_confirmation
        # call on the handlers.  The HTTP client doesn't expose it directly,
        # so we fall back to a chat message if the dedicated call is missing.
        try:
            self._resolve(request_id, approved)
        except Exception as exc:  # noqa: BLE001
            await query.message.reply_text(f"❌ {exc}")
            return
        await query.message.reply_text(
            "✅ تأیید شد" if approved else "✖️ لغو شد",
        )

    def _resolve(self, request_id: str, approved: bool) -> None:
        """Resolve a pending confirmation.  Subclasses may override for HTTP."""
        # The in-process BridgeClient doesn't expose resolve_confirmation
        # directly; we go through the handlers.  For HTTP, the bot should
        # use the /confirm endpoint (see bot_http.py in the same package).
        raise AssistantError("this bot does not support confirmations")

    # ------------------------------------------------------------- menu

    def _menu(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📌 وضعیت", callback_data="menu:status"),
                    InlineKeyboardButton("🧹 پاک‌کردن", callback_data="menu:reset"),
                ],
            ]
        )


# ---------------------------------------------------------------------------
# Concrete bots
# ---------------------------------------------------------------------------


class BridgeTelegramBot(_BaseBot):
    """Drives the Bridge from a Telegram bot."""

    def __init__(self, settings: AssistantSettings, client: BridgeClient, token: str) -> None:
        super().__init__(settings, client)
        self.token = token

    def _resolve(self, request_id: str, approved: bool) -> None:
        # In-process backend: the client wraps a BridgeServer; we can
        # resolve directly via the handlers.
        backend = getattr(self.client, "_backend", None)
        server = getattr(backend, "_server", None) if backend else None
        if server is not None and hasattr(server, "handlers"):
            ok = server.handlers.resolve_confirmation(request_id, approved)
            if not ok:
                raise AssistantError("request not found or already resolved")
            return
        # HTTP backend: POST to /confirm
        backend = getattr(self.client, "_backend", None)
        if backend is not None and hasattr(backend, "_base"):
            try:
                backend._session.post(  # type: ignore[attr-defined]
                    f"{backend._base}/confirm",  # type: ignore[attr-defined]
                    json={"payload": {"request_id": request_id, "approved": approved}},
                    timeout=10,
                )
                return
            except Exception:  # noqa: BLE001
                pass
        raise AssistantError("confirmation transport not available")

    def application(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("status", self.status_cmd))
        app.add_handler(CommandHandler("actions", self.actions_cmd))
        app.add_handler(CommandHandler("reset", self.reset_cmd))
        app.add_handler(CommandHandler("model", self.set_model_cmd))
        app.add_handler(CommandHandler("history", self.history_cmd))
        app.add_handler(CallbackQueryHandler(self.callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_handler))
        return app


class BridgeBaleBot(BridgeTelegramBot):
    """Bale variant — same logic, different API base URLs."""

    def application(self) -> Application:
        bale_api = self.settings.bale_base_url.rstrip("/")
        if not bale_api.endswith("/bot"):
            bale_api = f"{bale_api}/bot"
        app = (
            Application.builder()
            .token(self.token)
            .base_url(bale_api)
            .build()
        )
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("status", self.status_cmd))
        app.add_handler(CommandHandler("actions", self.actions_cmd))
        app.add_handler(CommandHandler("reset", self.reset_cmd))
        app.add_handler(CommandHandler("model", self.set_model_cmd))
        app.add_handler(CommandHandler("history", self.history_cmd))
        app.add_handler(CallbackQueryHandler(self.callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_handler))
        return app


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------


def _connect_bridge(settings: AssistantSettings) -> BridgeClient:
    """Build a BridgeClient for the bot.  Prefer in-process if the bot is
    on the same machine; otherwise expect a daemon URL + token."""
    explicit = os.environ.get("BRIDGE_URL", "").strip()
    if explicit:
        token = os.environ.get("LOCAL_AGENT_BRIDGE_TOKEN", "").strip()
        if not token:
            token_file = settings.data_dir / "bridge.token"
            if token_file.is_file():
                token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise AssistantError("no bridge token; set LOCAL_AGENT_BRIDGE_TOKEN")
        return BridgeClient.connect(base_url=explicit, token=token)
    return BridgeClient.start_in_process(settings)


def run_telegram(argv: list[str] | None = None) -> int:
    from ...core.config import load_settings

    settings = load_settings()
    setup_logging(settings.data_dir)
    client = _connect_bridge(settings)
    token = settings.telegram_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return 1
    bot = BridgeTelegramBot(settings, client, token)
    bot.application().run_polling(drop_pending_updates=False)
    return 0


def run_bale(argv: list[str] | None = None) -> int:
    from ...core.config import load_settings

    settings = load_settings()
    setup_logging(settings.data_dir)
    client = _connect_bridge(settings)
    token = settings.bale_token or os.environ.get("BALE_BOT_TOKEN", "")
    if not token:
        print("BALE_BOT_TOKEN not set", file=sys.stderr)
        return 1
    bot = BridgeBaleBot(settings, client, token)
    bot.application().run_polling(drop_pending_updates=False)
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short(value: Any, limit: int = 120) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(value)
    return rendered[: limit - 3] + "..." if len(rendered) > limit else rendered
