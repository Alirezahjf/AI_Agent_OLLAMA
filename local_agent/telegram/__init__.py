"""Personal-account Telegram integration (Telethon user client).

This is *not* a bot.  It logs in as you (via api_id/api_hash/phone),
stores the session in a local file, and can:
  * list your recent dialogs
  * send text messages
  * send files / photos
  * mark messages as read
  * search messages in a chat

The session is reused across restarts; the assistant never asks for
the SMS code more than once.
"""

from .client import PersonalTelegram, TelegramError, Chat, Message

__all__ = ["PersonalTelegram", "TelegramError", "Chat", "Message"]
