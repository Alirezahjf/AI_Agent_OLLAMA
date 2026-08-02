"""Main entrypoint for the CLI: builds the agent loop and runs the REPL.

The CLI can run in two modes:

  * **Direct** (the default): spins up a Bridge in-process and uses
    it directly.  This is what most users want for desktop use.
  * **Client** (``--connect URL``): connects to an already-running
    Bridge daemon over HTTP.  Use this when the Bridge is running as
    a service or when another frontend (e.g. the Telegram bot on a
    different machine) is the source of truth.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..actions.registry import ActionContext, ConfirmationGate, Risk
from ..actions import build_default_registry, describe_action, run_action
from ..automation import is_gui_available, register_gui
from ..automation.screenshot import take_screenshot
from ..bridge import BridgeClient, BridgeConnectionError
from ..bridge.api.handlers import EventType
from ..bridge.protocol import ActionResult, Event
from ..core.config import AssistantSettings, load_settings
from ..core.context import ConversationMessage, RuntimeContext
from ..core.errors import ActionRefused, AssistantError, DependencyMissing
from ..core.logging_setup import get_logger, setup_logging
from ..llm import LLMClient, create_client
from ..llm.client import ToolDefinition
from ..telegram import PersonalTelegram
from .render import Renderer
from .prompts import build_system_prompt


logger = get_logger("cli")


HELP_TEXT = """
Available commands (type /<command> or just chat normally):

  /help               show this help
  /status             show model, status, working directory
  /doctor             run the installation self-check (بررسی سلامت)
  /actions            list every action the agent can call
  /model NAME         switch the model at runtime
  /provider NAME      switch provider (ollama | openai_compatible | auto)
  /approve            toggle auto-approve for destructive actions
  /confirm MODE       set confirm mode: destructive | always | never
  /reset              clear conversation history (shared across frontends)
  /undo               pop the last user message and resend
  /screenshot         capture the primary screen now
  /telegram           connect / status for the personal Telegram client
  /send NAME TEXT     (telegram) quick send without going through the agent
  /history            show the last 20 conversation messages
  /purge              پاک‌سازی کامل داده‌ها/تنظیمات ایجنت و لغو اجرای خودکار
  /quit               exit the assistant

When this CLI is connected to a Bridge daemon, every command above
and every free-form message goes through the shared bridge.  Other
frontends (the Telegram bot, the web UI) see the same history.
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_cli(argv: list[str] | None = None) -> int:
    from ..utils.encoding import ensure_utf8_stdio

    ensure_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    settings = load_settings()
    setup_logging(settings.data_dir, verbose=_has_flag(argv, "--verbose", "-v"))

    # ``--purge`` wipes the app's footprint and exits before any server or
    # bridge starts; ``--yes`` is the required safety switch for unattended
    # use (otherwise a typed confirmation is requested).
    if _has_flag(argv, "--purge"):
        from ..core.cleanup import purge_with_confirmation

        return purge_with_confirmation(
            settings,
            assume_yes=_has_flag(argv, "--yes", "-y"),
            extra_kwargs={"close_logging": True},
        )

    renderer = Renderer()

    bridge_url = _bridge_url_from_argv(argv)
    if bridge_url:
        return _run_remote_client(bridge_url, settings, renderer)

    # --- Direct (in-process) mode ---------------------------------------
    renderer.banner(
        title="Local Windows Assistant",
        subtitle=platform.platform(terse=True),
        extra=f"data dir: {settings.data_dir}",
    )
    renderer.info("starting in-process Bridge (use --connect to attach to a daemon instead)")

    client = BridgeClient.start_in_process(settings)
    if client.info:
        renderer.info(
            f"Bridge: session={client.info.session_id} host={client.info.hostname} "
            f"capabilities={','.join(client.info.capabilities)}"
        )

    repl = _REPL(renderer=renderer, settings=settings, client=client)
    return repl.run()


def _bridge_url_from_argv(argv: list[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg in {"--connect", "-c"} and index + 1 < len(argv):
            return argv[index + 1]
    if "BRIDGE_URL" in os.environ:
        return os.environ["BRIDGE_URL"]
    return None


def _run_remote_client(url: str, settings: AssistantSettings, renderer: Renderer) -> int:
    token = os.environ.get("LOCAL_AGENT_BRIDGE_TOKEN", "")
    token_file = settings.data_dir / "bridge.token"
    if not token and token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        renderer.warn(f"no token found; set LOCAL_AGENT_BRIDGE_TOKEN or {token_file}")
        return 1
    try:
        client = BridgeClient.connect(base_url=url, token=token)
    except BridgeConnectionError as exc:
        renderer.warn(f"could not connect to {url}: {exc}")
        return 1
    renderer.banner(
        title="Local Windows Assistant (remote bridge)",
        subtitle=str(client.info) if client.info else url,
        extra=f"connected to {url}",
    )
    repl = _REPL(renderer=renderer, settings=settings, client=client)
    return repl.run()


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------


class _REPL:
    def __init__(self, *, renderer: Renderer, settings: AssistantSettings, client: BridgeClient) -> None:
        self.renderer = renderer
        self.settings = settings
        self.client = client
        self._stop = threading.Event()
        self._print_welcome()

    def _print_welcome(self) -> None:
        self.renderer.section("Welcome")
        self.renderer.info(
            "Type a request in plain Persian / English. Press Ctrl-C to interrupt, "
            "Ctrl-D or /quit to exit."
        )
        self.renderer.print(HELP_TEXT.strip())

    # ---------------------------------------------------------------- main

    def run(self) -> int:
        try:
            while not self._stop.is_set():
                try:
                    line = self.renderer.prompt("you")
                except (EOFError, KeyboardInterrupt):
                    self.renderer.info("\nbye.")
                    return 0
                if not line:
                    continue
                if line.startswith("/"):
                    self._handle_command(line)
                    continue
                self._handle_message(line)
        except KeyboardInterrupt:
            self.renderer.info("\nbye.")
            return 0
        return 0

    # ------------------------------------------------------------- commands

    def _handle_command(self, line: str) -> None:
        parts = shlex.split(line)
        cmd = parts[0].lower()
        rest = parts[1:]
        if cmd in {"/help", "/h", "/?"}:
            self.renderer.print(HELP_TEXT.strip())
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/doctor":
            self._cmd_doctor()
        elif cmd == "/actions":
            self._cmd_actions()
        elif cmd == "/model":
            self._cmd_model(rest)
        elif cmd == "/provider":
            self._cmd_provider(rest)
        elif cmd == "/approve":
            self._cmd_approve()
        elif cmd == "/confirm":
            self._cmd_confirm(rest)
        elif cmd == "/reset":
            self.client.clear_history()
            self.renderer.info("conversation cleared.")
        elif cmd == "/undo":
            self._cmd_undo()
        elif cmd == "/screenshot":
            self._cmd_screenshot()
        elif cmd == "/telegram":
            self._cmd_telegram(rest)
        elif cmd == "/send":
            self._cmd_send(rest)
        elif cmd == "/history":
            self._cmd_history()
        elif cmd == "/purge":
            self._cmd_purge()
        elif cmd in {"/quit", "/exit"}:
            self._stop.set()
        else:
            self.renderer.warn(f"unknown command: {cmd}; type /help for the list.")

    def _cmd_status(self) -> None:
        try:
            status = self.client.get_status()
        except AssistantError as exc:
            self.renderer.warn(f"could not fetch status: {exc}")
            return
        self.renderer.section("Status")
        settings = status.get("settings", {})
        history = status.get("history", {})
        for key, value in settings.items():
            self.renderer.info(f"  {key}: {value}")
        for key, value in history.items():
            self.renderer.info(f"  history.{key}: {value}")
        if status.get("actions"):
            self.renderer.info(f"  actions: {len(status['actions'])} registered")

    def _cmd_doctor(self) -> None:
        from ..diagnostics import run_checks

        self.renderer.info("در حال بررسی سلامت…")
        try:
            report = run_checks(self.settings)
        except Exception as exc:  # noqa: BLE001
            self.renderer.warn(f"self-check failed: {exc}")
            return
        self.renderer.print(report.render())

    def _cmd_actions(self) -> None:
        try:
            descriptions = self.client.list_actions()
        except AssistantError as exc:
            self.renderer.warn(f"could not list actions: {exc}")
            return
        self.renderer.section("Actions")
        for line in descriptions:
            self.renderer.info("  " + line)

    def _cmd_model(self, args: list[str]) -> None:
        if not args:
            self.renderer.warn("usage: /model MODEL_NAME")
            return
        name = " ".join(args).strip()
        try:
            result = self.client.set_model(model=name)
        except AssistantError as exc:
            self.renderer.warn(f"could not switch model: {exc}")
            return
        self.renderer.info(f"model switched to: {result.get('model')}")

    def _cmd_provider(self, args: list[str]) -> None:
        if not args:
            self.renderer.warn("usage: /provider ollama | openai_compatible | auto")
            return
        try:
            result = self.client.set_model(provider=args[0])
        except AssistantError as exc:
            self.renderer.warn(f"could not switch provider: {exc}")
            return
        self.renderer.info(f"provider switched: {result.get('provider')} / {result.get('model')}")

    def _cmd_approve(self) -> None:
        # The local CLI doesn't own the gate any more; it lives in the
        # Bridge.  This command is a hint to the user; the real switch
        # is at the Bridge level (e.g. ``bridge-cli set confirm never``).
        self.renderer.info(
            "auto-approve is a Bridge-level setting. "
            "Edit config.json (safety.confirm_mode) and restart the Bridge."
        )

    def _cmd_confirm(self, args: list[str]) -> None:
        if not args or args[0] not in {"destructive", "always", "never"}:
            self.renderer.warn("usage: /confirm destructive | always | never")
            return
        self.renderer.info(
            "confirm mode is a Bridge-level setting. "
            f"Set safety.confirm_mode = {args[0]!r} in config.json and restart."
        )

    def _cmd_undo(self) -> None:
        history = self.client.get_history(limit=200)
        for index in range(len(history) - 1, -1, -1):
            if history[index].get("role") == "user":
                history.pop(index)
                break
        self.client.clear_history()
        # Re-append the trimmed history through the model completion is
        # not exposed; in practice the user will just continue and the
        # next model call will see the trimmed context.
        self.renderer.info("last user message removed (history cleared; resend if needed).")

    def _cmd_screenshot(self) -> None:
        image = take_screenshot()
        target = self.settings.data_dir / "screenshots" / f"cli_{int(time.time())}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, "PNG")
        self.renderer.info(f"screenshot saved: {target}  ({image.width}x{image.height})")

    def _cmd_telegram(self, args: list[str]) -> None:
        if not args or args[0] == "status":
            status = self.client.get_status()
            tg = status.get("settings", {}).get("telegram_enabled")
            self.renderer.info(f"telegram enabled: {tg}")
            return
        self.renderer.warn("telegram interactions go through chat; ask the agent to send a message.")

    def _cmd_send(self, args: list[str]) -> None:
        if len(args) < 2:
            self.renderer.warn("usage: /send <chat name or id> <text>")
            return
        target = args[0]
        text = " ".join(args[1:])
        try:
            result = self.client.invoke_action(
                "telegram_send_message", {"chat": target, "text": text}
            )
        except AssistantError as exc:
            self.renderer.warn(f"send failed: {exc}")
            return
        if not result.success:
            self.renderer.warn(f"send failed: {result.error or result.text}")
            return
        self.renderer.info("message sent.")

    def _cmd_history(self) -> None:
        try:
            history = self.client.get_history(limit=20)
        except AssistantError as exc:
            self.renderer.warn(f"could not fetch history: {exc}")
            return
        for message in history:
            content = str(message.get("content", "")).replace("\n", " ")[:240]
            self.renderer.info(f"  [{message.get('role')}] {content}")

    def _cmd_purge(self) -> None:
        """Wipe every trace of the app and exit — the CLI's «پاک‌سازی کامل»."""
        from ..core.cleanup import PURGE_CONFIRM_WORD, purge_all

        self.renderer.warn("⚠️  پاک‌سازی کامل: همهٔ داده‌ها، تنظیمات، تاریخچه، لاگ‌ها، اسکرین‌شات‌ها")
        self.renderer.warn("   و توکن‌ها حذف و ثبت «اجرای خودکار» لغو می‌شود. (کتابخانه‌ها باقی می‌مانند)")
        try:
            answer = self.renderer.prompt(
                f"برای تأیید، عبارت «{PURGE_CONFIRM_WORD}» را بنویسید (Enter = لغو)"
            )
        except (EOFError, KeyboardInterrupt):
            self.renderer.info("لغو شد — چیزی پاک نشد.")
            return
        if answer.strip() not in {PURGE_CONFIRM_WORD, "بله", "yes", "y"}:
            self.renderer.info("تأیید نشد — چیزی پاک نشد.")
            return
        report = purge_all(self.settings, close_logging=True)
        self.renderer.info(report["message"])
        for failure in report["failed"]:
            self.renderer.warn(f"  نشد: {failure['path']} — {failure['error']}")
        # Exit afterwards: the just-wiped data directory must not be
        # recreated by a half-alive session.
        self._stop.set()

    # ------------------------------------------------------------- message

    def _handle_message(self, user_text: str) -> None:
        # Stream the chat run so the user sees events as they happen.
        assistant_buffer: list[str] = []
        try:
            for event in self.client.chat(user_text):
                self._render_event(event, assistant_buffer)
        except AssistantError as exc:
            self.renderer.warn(f"chat failed: {exc}")

    def _render_event(self, event: Event, assistant_buffer: list[str]) -> None:
        if event.type == EventType.CHAT_STARTED.value:
            self.renderer.thinking("chat started")
        elif event.type == EventType.TURN_STARTED.value:
            payload = event.payload
            self.renderer.thinking(
                f"turn {payload.get('turn', '?')}/{payload.get('max_turns', '?')}"
            )
        elif event.type == EventType.ASSISTANT_FINAL.value:
            text = event.payload.get("text", "")
            assistant_buffer.append(text)
            self.renderer.assistant(text)
        elif event.type == EventType.TOOL_PROPOSED.value:
            name = event.payload.get("name", "?")
            args = event.payload.get("arguments", {})
            self.renderer.action_result(name, f"proposed: {_short(args)}")
        elif event.type == EventType.TOOL_CONFIRM_REQUESTED.value:
            name = event.payload.get("name", "?")
            self.renderer.info(f"approval required for {name} (use Bridge confirm endpoint)")
        elif event.type == EventType.TOOL_RESULT.value:
            name = event.payload.get("name", "?")
            text = event.payload.get("text", "")
            self.renderer.action_result(name, text[:300])
        elif event.type == EventType.CHAT_DONE.value:
            self.renderer.info("done.")
        elif event.type == EventType.CHAT_FAILED.value:
            reason = event.payload.get("reason") or event.payload.get("error") or "unknown"
            self.renderer.warn(f"chat failed: {reason}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_flag(argv: list[str], *names: str) -> bool:
    return any(name in argv for name in names)


def _short(value: Any, limit: int = 120) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) > limit:
        return rendered[: limit - 3] + "..."
    return rendered
