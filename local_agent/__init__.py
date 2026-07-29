"""Local Windows Assistant - A powerful, autonomous agent for your desktop.

Unlike the Telegram bot (which runs on a server), this agent lives on YOUR
Windows machine and has direct access to your GUI session: it can launch
apps, drive Photoshop / Chrome / Telegram Desktop with mouse + keyboard,
send real messages from your personal Telegram account (via Telethon user
session), and orchestrate everything via natural language.

Modules:
    core        - config, logging, runtime context, exception types
    llm         - provider-agnostic model client (Ollama / OpenAI-compatible)
    actions     - high-level desktop actions (open apps, manage windows, ...)
    automation  - GUI automation primitives (mouse, keyboard, screenshots)
    telegram    - personal-account Telegram user client (Telethon)
    cli         - rich terminal UI and the main REPL
    utils       - shared helpers (path safety, JSON, platform)
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
