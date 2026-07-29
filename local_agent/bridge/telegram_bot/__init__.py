"""Telegram bot that drives the local Bridge.

This is *not* a bot like the original ``agent.bot`` in this
repository — that bot is fully autonomous and lives on the server.
This bot is a thin client: it sends every user message to the
Bridge (which lives on the user's Windows machine) and renders the
streamed events back to the chat.

The bot supports both messengers that speak the Telegram Bot API:
  * Telegram (tapi.bots.telegram.org)
  * Bale (tapi.bale.ai)

Authentication mirrors the original bot's allow-list model.
"""

from .bot import BridgeTelegramBot, BridgeBaleBot, run_telegram, run_bale

__all__ = [
    "BridgeTelegramBot",
    "BridgeBaleBot",
    "run_telegram",
    "run_bale",
]
