"""Entry point for running the same agent on Bale messenger."""

from __future__ import annotations

from .bot import BaleBot
from .config import Settings


def main() -> None:
    settings = Settings.from_env(require_messenger="bale")
    # Bale documents deleteWebhook without Telegram's optional drop_pending_updates flag.
    # Passing None lets python-telegram-bot omit that optional parameter while keeping polling behavior.
    BaleBot(settings).application().run_polling(drop_pending_updates=None)


if __name__ == "__main__":
    main()
