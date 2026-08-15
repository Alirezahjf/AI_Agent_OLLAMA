"""Personal-account Telegram integration (Telethon user client).

This is *not* a bot. It logs in as the user through
``api_id``/``api_hash``/phone, stores a separate local session per account,
and exposes live dialogs, contacts, rich messages, profiles and safe target
resolution to the assistant.
"""

from .client import Chat, Contact, Message, PersonalTelegram, TelegramError

__all__ = ["Chat", "Contact", "Message", "PersonalTelegram", "TelegramError"]
