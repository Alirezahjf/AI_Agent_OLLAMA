"""BridgeHandlers: the actual implementation behind every Bridge request.

This is where the agent loop, tool registry, and event publisher live.
Both the in-process backend and the HTTP server delegate to it.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from queue import Queue
from typing import Any
from urllib.parse import urlparse

from ...actions import build_default_registry, describe_action, run_action
from ...actions.groups import TOOL_GROUPS, DEFAULT_GROUP_IDS
from ...actions.config_actions import register_config
from ...actions.gmail_actions import register_gmail
from ...actions.github_actions import register_github
from ...actions.registry import ActionContext, ConfirmationGate
from ...actions.scheduler_actions import register_scheduler
from ...actions.telegram_actions import register_telegram
from ...automation import is_gui_available, register_gui
from ...core.config import (
    AssistantSettings,
    ConfigError,
    GitHubAccount,
    GitHubSettings,
    TelegramAccount,
    TelegramSettings,
)
from ...core.context import ConversationMessage, RuntimeContext
from ...core.errors import ActionRefused, AssistantError, DependencyMissing
from ...core.logging_setup import get_logger
from ...core.notify import notify_desktop
from ...core.scheduler import ScheduledJob, Scheduler
from ...gmail import GmailClient
from ...gmail.client import GmailError
from ...github import GitHubClient
from ...github.client import GitHubError, PendingOAuth
from ...llm import create_client
from ...telegram import PersonalTelegram
from ...telegram.client import TelegramError
from ..protocol import (
    PROTOCOL_VERSION,
    ActionInvocation,
    ActionResult,
    ErrorPayload,
    Event,
    EventType,
    Hello,
    MessageType,
    Response,
    Welcome,
)

logger = get_logger("bridge.handlers")

# Multi-session limits (F4): cap live sessions and drop idle ones.
MAX_SESSIONS = 20
SESSION_TIMEOUT_SECONDS = 24 * 3600
_TOOL_CAP = 120


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------


class EventBus:
    """A simple pub/sub bus used to broadcast chat events to all frontends.

    Each chat run gets its own queue.  Subscribers receive every event
    of every run; if you want only one run, filter by ``run_id``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Event], None]] = []
        self._run_queues: dict[str, Queue[Event | None]] = {}

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def create_run_queue(self, run_id: str) -> Queue[Event | None]:
        with self._lock:
            existing = self._run_queues.get(run_id)
            if existing is not None:
                # The chat worker already created this queue when it started;
                # returning a *new* one would silently drop events published
                # between start and subscription.
                return existing
            q: Queue[Event | None] = Queue()
            self._run_queues[run_id] = q
            return q

    def destroy_run_queue(self, run_id: str) -> None:
        with self._lock:
            self._run_queues.pop(run_id, None)

    def publish(self, event: Event) -> None:
        with self._lock:
            listeners = list(self._listeners)
            q = self._run_queues.get(event.run_id)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("event listener raised")
        if q is not None:
            q.put(event)

    def end_run(self, run_id: str) -> None:
        with self._lock:
            q = self._run_queues.get(run_id)
        if q is not None:
            q.put(None)


# ---------------------------------------------------------------------------
# Main handlers
# ---------------------------------------------------------------------------


@dataclass
class BridgeHandlers:
    """The request handler for the Bridge.

    Owns the agent state, the tool registry, the LLM client, and the
    Telegram user client.  All public methods are thread-safe; long
    operations (chat runs) are dispatched to a worker thread so the
    request handler returns immediately.
    """

    settings: AssistantSettings
    runtime: RuntimeContext
    registry: Any
    context: ActionContext
    gate: ConfirmationGate
    event_bus: EventBus = field(default_factory=EventBus)
    # The *active* account's client (backward-compatible accessor).
    telegram: PersonalTelegram | None = None
    # Every enabled account's client, keyed by account name (F2).
    _telegram_accounts: dict[str, PersonalTelegram] = field(default_factory=dict)
    # The *active* GitHub account's client (backward-compatible accessor).
    github: GitHubClient | None = None
    # Every enabled GitHub account's client, keyed by account name.
    _github_accounts: dict[str, GitHubClient] = field(default_factory=dict)
    # In-flight OAuth flows: state -> PendingOAuth (CSRF + account routing).
    _github_pending: dict[str, PendingOAuth] = field(default_factory=dict)
    # Per-session runtimes keyed by session_id (F4); the default runtime is
    # ``self.runtime`` under the key "default".
    _sessions: dict[str, RuntimeContext] = field(default_factory=dict)
    _session_tool_groups: dict[str, frozenset[str]] = field(default_factory=dict)
    _sessions_lock: threading.RLock = field(default_factory=threading.RLock)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _active_runs: dict[str, threading.Event] = field(default_factory=dict)
    _run_threads: dict[str, threading.Thread] = field(default_factory=dict)
    _confirmation_lock: threading.Lock = field(default_factory=threading.Lock)
    _pending_confirms: dict[str, PendingConfirmation] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: AssistantSettings) -> BridgeHandlers:
        settings = _auto_select_provider(settings)
        runtime = RuntimeContext(settings)
        gate = ConfirmationGate(settings.safety)
        context = ActionContext(
            runtime=runtime,
            confirmation_gate=gate,
            work_dir=settings.work_dir,
        )
        registry = build_default_registry(context)
        # ``register_gui`` always registers ``screen_capture`` (it works
        # without pyautogui through the PIL/mss fallback); mouse/keyboard
        # tools are registered only when a real desktop is attached.
        register_gui(registry, context)
        register_telegram(registry, context)
        register_config(registry, context)
        register_gmail(registry, context)
        register_github(registry, context)
        register_scheduler(registry, context)
        context.extra["telegram"] = None
        context.extra["github"] = None
        context.extra["registry"] = registry
        gmail = _build_gmail_client(settings)
        context.extra["gmail"] = gmail
        handlers = cls(
            settings=settings,
            runtime=runtime,
            registry=registry,
            context=context,
            gate=gate,
            telegram=None,
        )
        # ``config_set`` (used when the user says «به تلگرامم وصل شو») needs
        # a way to persist + apply settings from inside the action layer.
        context.extra["settings_owner"] = handlers
        handlers._sync_telegram_accounts()
        handlers._start_telegram_auto_connect()
        handlers._sync_github_accounts()
        # Scheduled reminders/tasks: persisted in data_dir/scheduled.json,
        # fired by a daemon thread, streamed to every frontend.
        scheduler = Scheduler(settings.data_dir)
        context.extra["scheduler"] = scheduler
        scheduler.set_fire_callback(handlers._on_scheduled_fired)
        scheduler.start()
        runtime.set_system_prompt(_build_system_prompt(
            registry, settings, is_gui_available(), telegram_has_clients(handlers),
        ))
        return handlers

    # -----------------------------------------------------------------

    def welcome(self) -> Welcome:
        return Welcome(
            session_id=uuid.uuid4().hex[:12],
            server_version="1.0.0",
            protocol_version=PROTOCOL_VERSION,
            user=__import__("os").environ.get("USERNAME") or __import__("os").environ.get("USER") or "?",
            hostname=socket.gethostname(),
            platform=platform.platform(terse=True),
            capabilities=_capabilities(self),
        )

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Top-level dispatcher.  Returns a Response dict."""
        type_ = str(request.get("type", ""))
        request_id = str(request.get("id", ""))
        payload = dict(request.get("payload") or {})
        try:
            if type_ == MessageType.HELLO.value:
                return Response(id=request_id, ok=True, result=Hello().to_dict()).to_dict()
            if type_ == MessageType.LIST_ACTIONS.value:
                return Response(
                    id=request_id, ok=True,
                    result=[describe_action(a) for a in self.registry.all()],
                ).to_dict()
            if type_ == MessageType.LIST_MODELS.value:
                client = create_client(self.settings.llm)
                return Response(id=request_id, ok=True, result=client.list_models()).to_dict()
            if type_ == MessageType.INVOKE_ACTION.value:
                inv = ActionInvocation.from_dict(payload)
                result = self._invoke_action_sync(inv)
                return Response(id=request_id, ok=True, result=result.to_dict()).to_dict()
            if type_ == MessageType.GET_STATUS.value:
                return Response(id=request_id, ok=True, result=self._status()).to_dict()
            if type_ == MessageType.GET_HISTORY.value:
                runtime = self._runtime_for(payload.get("session_id"))
                return Response(
                    id=request_id, ok=True,
                    result=[m.to_openai() for m in runtime.snapshot()],
                ).to_dict()
            if type_ == MessageType.CLEAR_HISTORY.value:
                runtime = self._runtime_for(payload.get("session_id"))
                runtime.clear()
                return Response(id=request_id, ok=True, result={"cleared": True}).to_dict()
            if type_ == MessageType.SET_MODEL.value:
                return Response(id=request_id, ok=True, result=self._set_model(payload)).to_dict()
            if type_ == "SET_TOOL_GROUPS":
                return Response(id=request_id, ok=True, result=self.set_tool_groups(payload.get("session_id"), payload.get("groups"))).to_dict()
            if type_ == MessageType.CHAT.value:
                # Returns a run_id immediately; events flow over the bus.
                run_id = self._start_chat_run(
                    payload.get("message", ""), session_id=payload.get("session_id")
                )
                return Response(id=request_id, ok=True, result={"run_id": run_id}).to_dict()
            if type_ == MessageType.INTERRUPT.value:
                self._interrupt_run(payload.get("run_id", ""))
                return Response(id=request_id, ok=True, result={"interrupted": True}).to_dict()
            if type_ == MessageType.AUTH.value:
                # Auth is enforced at the transport level (HTTP middleware or
                # local-only access).  In-process calls are always trusted.
                return Response(id=request_id, ok=True, result={"ok": True}).to_dict()
            if type_ == MessageType.PING.value:
                return Response(id=request_id, ok=True, result={"pong": True}).to_dict()
            return self._fail(request_id, "unknown_type", f"unknown message type: {type_!r}")
        except AssistantError as exc:
            return self._fail(request_id, "assistant_error", str(exc))
        except Exception as exc:
            logger.exception("bridge handler crashed")
            return self._fail(request_id, "internal", f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _fail(request_id: str, code: str, message: str) -> dict[str, Any]:
        return Response(
            id=request_id,
            ok=False,
            error=ErrorPayload(code=code, message=message),
        ).to_dict()

    # ---------------------------------------------------------------- tool groups

    def tool_groups(self, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or "default"
        active = self._session_tool_groups.get(sid, frozenset(DEFAULT_GROUP_IDS))
        count = len([a for a in self.registry.all() if not a.unavailable and a.group in active])
        return {"groups": [{**g.__dict__} for g in TOOL_GROUPS], "active": sorted(active), "enabled_tool_count": min(count, _TOOL_CAP), "cap": _TOOL_CAP}

    def set_tool_groups(self, session_id: str | None, groups: Any) -> dict[str, Any]:
        sid = session_id or "default"
        valid = frozenset(str(g) for g in (groups or []) if any(x.id == str(g) for x in TOOL_GROUPS))
        self._session_tool_groups[sid] = valid
        return self.tool_groups(sid)

    def _visible_tools(self, session_id: str | None = None) -> list[Any]:
        active = self._session_tool_groups.get(session_id or "default", frozenset(DEFAULT_GROUP_IDS))
        tools = [a for a in self.registry.all() if not a.unavailable and a.group in active]
        if len(tools) > _TOOL_CAP:
            logger.warning("تعداد ابزارهای فعال بیش از سقف است؛ فقط %s ابزار به مدل ارسال شد.", _TOOL_CAP)
            tools = tools[:_TOOL_CAP]
        return tools

    # ---------------------------------------------------------------- status

    def _status(self) -> dict[str, Any]:
        from ...utils.platform import elevation_level

        telegram_state = self.telegram_status()
        return {
            "settings": {
                "data_dir": str(self.settings.data_dir),
                "work_dir": str(self.settings.work_dir),
                "llm_provider": self.settings.llm.provider,
                "llm_model": self.settings.llm.ollama_model or self.settings.llm.openai_model,
                "openai_base_url": self.settings.llm.openai_base_url,
                "openai_api_key_set": bool(self.settings.llm.openai_api_key),
                "telegram_enabled": telegram_state["feature_enabled"],
                "telegram_connected": telegram_state["connected"],
                "telegram_state": telegram_state["state"],
                "telegram_phone": telegram_state["phone"],
                "gmail_enabled": bool(self.settings.gmail.enabled),
                "gmail_connected": self.gmail_connected(),
                "github_enabled": bool(self.settings.github.enabled),
                "github_connected": bool(self.github and self.github.is_connected),
                "github_login": (self.github.login if self.github and self.github.is_connected else ""),
                "github_active_account": self.settings.github.active_account,
                "github_confirm_push": bool(self.settings.github.confirm_push),
                "full_system_access": bool(self.settings.safety.full_system_access),
                "elevation": elevation_level(),
                "confirm_mode": self.settings.safety.confirm_mode,
                "telegram_active_account": self.settings.telegram.active_account,
                "telegram_accounts": self.telegram_accounts_status(),
                "telegram_confirm_send": bool(
                    self.settings.telegram.account(None).confirm_send
                ),
                "gmail_confirm_send": bool(self.settings.gmail.confirm_send),
            },
            "warnings": self._warnings(),
            "actions": [a.name for a in self.registry.all()],
            "history": self.runtime.stats(),
            "sessions": len(self._sessions),
        }

    def gmail_connected(self) -> bool:
        client = self.context.extra.get("gmail")
        return bool(client and client.is_connected)

    def _warnings(self) -> list[str]:
        """Human-readable (Persian) warnings shown as a banner in the UI."""
        out: list[str] = []
        llm = self.settings.llm
        if llm.provider == "ollama" and not _ollama_reachable(llm.ollama_base_url):
            if llm.openai_api_key:
                out.append(
                    "Ollama در دسترس نیست. کلید API شما ثبت شده است؛ "
                    "در تنظیمات، ارائه‌دهنده را روی «سازگار با OpenAI» بگذارید."
                )
            else:
                out.append(
                    "Ollama در دسترس نیست. لطفاً در تنظیمات، ارائه‌دهنده را به "
                    "«سازگار با OpenAI» تغییر دهید و کلید API (مثلاً AvalAI) را وارد کنید."
                )
        if llm.provider == "openai_compatible" and not llm.openai_api_key:
            out.append("کلید API تنظیم نشده است. در تنظیمات، کلید AvalAI خود را وارد کنید.")
        if self.settings.gmail.enabled and self.context.extra.get("gmail") is None:
            out.append(
                "جیمیل فعال است ولی اتصالش کامل نیست. برای اتصال جیمیل، username و "
                "App Password (یا credentials.json) را در تنظیمات ست کنید و دکمهٔ "
                "«اتصال جیمیل» را بزنید."
            )
        return out

    def _set_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = payload.get("provider")
        model = payload.get("model")
        # Mutate settings through a fresh dataclass.
        llm_dict = dict(self.settings.llm.__dict__)
        if provider:
            llm_dict["provider"] = str(provider)
        if model:
            # Update both ollama_model and openai_model for simplicity
            llm_dict["ollama_model"] = str(model)
            llm_dict["openai_model"] = str(model)
        new_llm = type(self.settings.llm)(**llm_dict)
        self.settings = self.settings.with_overrides(llm=new_llm)
        self._persist_settings()
        client = create_client(new_llm)
        return {"provider": new_llm.provider, "model": client.model_name}

    def _persist_settings(self) -> bool:
        """Write the current settings back to ``config.json``.

        Without this, provider/model/API-key changes made from the web UI
        are lost on the next restart.  Failures are logged, never raised:
        a read-only config file must not break a running chat.

        The write is atomic (tmp + ``os.replace``) so a crash mid-write
        can never corrupt the file the next start reads.
        """
        path = self.settings.effective_config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.settings.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
            self.runtime.settings = self.settings
            return True
        except OSError as exc:
            logger.warning("could not persist settings to %s: %s", path, exc)
            return False

    # ------------------------------------------------- config_set support

    def apply_config_set(self, path: str, value: Any) -> AssistantSettings:
        """Persist one dotted-path setting (``telegram.api_hash``, ``work_dir``, ...).

        Used by the ``config_set`` action so the agent can save values
        (e.g. Telegram credentials) at the user's request.  The whole
        payload is re-validated through :class:`AssistantSettings` so a
        bad value can never leave a half-written config behind.
        """
        if path.startswith("telegram."):
            return self._apply_telegram_config_set(path, value)
        payload = self.settings.to_dict()
        parts = path.split(".")
        cursor = payload
        for part in parts[:-1]:
            if not isinstance(cursor, dict) or part not in cursor:
                raise AssistantError(f"تنظیم ناشناخته: {path}")
            cursor = cursor[part]
        last = parts[-1]
        if not isinstance(cursor, dict) or last not in cursor:
            raise AssistantError(f"تنظیم ناشناخته: {path}")
        cursor[last] = _coerce_setting_value(value, cursor[last])
        try:
            new_settings = AssistantSettings.from_dict(payload)
        except (ConfigError, TypeError, ValueError) as exc:
            raise AssistantError(f"مقدار تنظیم نامعتبر است: {exc}") from exc
        return self._apply_settings(new_settings)

    def _apply_telegram_config_set(self, path: str, value: Any) -> AssistantSettings:
        """Apply a ``telegram.<field>`` config_set to the active account.

        The modern config stores accounts in a list, so legacy dotted paths
        (``telegram.api_id``, ``telegram.confirm_send``, ...) are mapped onto
        the active account to keep the «به تلگرامم وصل شو» flow working.
        """
        leaf = path[len("telegram."):]
        tg = self.settings.telegram
        if leaf == "enabled":
            # Enabling the feature also enables the active account, so a
            # client actually gets created for it.
            val = bool(value)
            accounts = list(tg.accounts)
            if not accounts:
                accounts = [TelegramAccount(name=tg.active_account, enabled=val)]
            accounts = [
                replace(a, enabled=val) if a.name == tg.active_account else a
                for a in accounts
            ]
            new_tg = TelegramSettings(
                enabled=val, active_account=tg.active_account, accounts=tuple(accounts)
            )
        elif leaf == "active_account":
            new_tg = tg.updated({"active_account": str(value)})
        elif leaf == "api_id":
            new_tg = tg.updated({"api_id": int(str(value).strip() or 0)})
        elif leaf == "confirm_send":
            new_tg = tg.updated({"confirm_send": bool(value)})
        elif leaf in ("api_hash", "phone", "session_name"):
            new_tg = tg.updated({leaf: str(value)})
        else:
            raise AssistantError(f"تنظیم ناشناخته: {path}")
        return self._apply_settings(self.settings.with_overrides(telegram=new_tg))

    def _apply_settings(self, new_settings: AssistantSettings) -> AssistantSettings:
        """Swap in new settings and keep every dependent object in sync."""
        old = self.settings
        self.settings = new_settings
        self.runtime.settings = new_settings
        self.gate = ConfirmationGate(new_settings.safety)
        self.context.confirmation_gate = self.gate
        self.context.work_dir = new_settings.work_dir
        self._persist_settings()
        self._sync_telegram_accounts()
        self._sync_gmail_client(old)
        return new_settings

    def _start_telegram_auto_connect(self) -> None:
        """اتصال پس‌زمینه به سشن‌های موجود؛ راه‌اندازی وب را مسدود نمی‌کند."""
        candidates = [
            (name, client) for name, client in self._telegram_accounts.items()
            if client.session_path.is_file() and client.session_path.stat().st_size > 0
        ]
        if not candidates:
            return
        semaphore = threading.BoundedSemaphore(2)
        def worker(name: str, client: PersonalTelegram) -> None:
            with semaphore:
                try:
                    result = client.start_login()
                    if result.get("state") == "connected":
                        self._mark_account_enabled(name)
                    self._publish_telegram_state()
                except Exception as exc:  # noqa: BLE001 - status is user-facing
                    client._last_error = "اتصال خودکار تلگرام ناموفق بود؛ فیلترشکن/VPN و اینترنت را بررسی کنید"
                    logger.debug("telegram auto-connect failed for %s: %s", name, type(exc).__name__)
                    self._publish_telegram_state()
        for name, client in candidates:
            threading.Thread(target=worker, args=(name, client), name=f"telegram-auto-{name}", daemon=True).start()

    def _sync_telegram_accounts(self) -> None:
        """Create/drop one PersonalTelegram per enabled account (F2).

        The active account's client is also mirrored onto ``self.telegram``
        and ``context.extra["telegram"]`` so the existing single-account
        action code keeps working unchanged.
        """
        tg = self.settings.telegram
        desired: dict[str, TelegramAccount] = {}
        if tg.enabled:
            desired = {acc.name: acc for acc in tg.accounts if acc.enabled}

        # Drop accounts that were disabled / removed.
        for name in list(self._telegram_accounts):
            if name not in desired:
                client = self._telegram_accounts.pop(name)
                if client.is_connected:
                    try:
                        client.disconnect()
                    except Exception as exc:  # noqa: BLE001 - best-effort teardown
                        logger.debug("telegram disconnect failed: %s", exc)

        # Create missing enabled accounts.
        for name, acc in desired.items():
            if name in self._telegram_accounts:
                continue
            if acc.api_id and acc.api_hash and acc.phone:
                self._telegram_accounts[name] = PersonalTelegram(
                    api_id=acc.api_id,
                    api_hash=acc.api_hash,
                    phone=acc.phone,
                    session_path=self.settings.telegram_session_path_for(name),
                    account_name=name,
                )

        # Mirror the active account onto the single-client accessor.
        active = self._telegram_accounts.get(tg.active_account)
        self.telegram = active
        self.context.extra["telegram"] = active
        self.runtime.set_system_prompt(_build_system_prompt(
            self.registry, self.settings, is_gui_available(), telegram_has_clients(self),
        ))

    def _sync_gmail_client(self, old: AssistantSettings) -> None:
        """Create/drop the Gmail client as ``gmail.enabled`` changes."""
        gmail = self.settings.gmail
        if gmail.enabled and self.context.extra.get("gmail") is None:
            client = _build_gmail_client(self.settings)
            self.context.extra["gmail"] = client
        elif not gmail.enabled and self.context.extra.get("gmail") is not None:
            existing = self.context.extra["gmail"]
            try:
                existing.disconnect()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("gmail disconnect failed: %s", exc)
            self.context.extra["gmail"] = None

    # ---------------------------------------------------------- gmail flow

    def gmail_status(self) -> dict[str, Any]:
        client = self.context.extra.get("gmail")
        gmail = self.settings.gmail
        return {
            "enabled": bool(gmail.enabled),
            "connected": bool(client and client.is_connected),
            "username": gmail.username,
            "has_credentials_file": self.settings.gmail_credentials_path.is_file(),
            "has_token_file": self.settings.gmail_token_path.is_file(),
            "has_app_password": bool(gmail.app_password),
        }

    def connect_gmail(self) -> dict[str, Any]:
        """Connect the Gmail backend (OAuth browser flow or IMAP login)."""
        client = self.context.extra.get("gmail")
        if client is None:
            client = _build_gmail_client(self.settings, force=True)
            self.context.extra["gmail"] = client
        if client is None:
            raise AssistantError(
                "هیچ روش اتصال جیمیل پیکربندی نشده است. یا credentials.json (OAuth) را "
                "از Google Cloud Console بگذارید، یا gmail.username و gmail.app_password "
                "را در تنظیمات وب ثبت کنید."
            )
        try:
            message = client.connect()
        except GmailError as exc:
            raise AssistantError(str(exc)) from exc
        return {"connected": True, "message": message, **self.gmail_status()}

    def disconnect_gmail(self) -> dict[str, Any]:
        client = self.context.extra.get("gmail")
        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("gmail disconnect failed: %s", exc)
        return self.gmail_status()

    # ------------------------------------------------------- telegram flow

    def _account_names(self) -> list[str]:
        return [acc.name for acc in self.settings.telegram.accounts]

    def _account_client(self, account: str | None = None) -> tuple[str, PersonalTelegram]:
        """Resolve ``account`` (default: active) to its client.

        Raises a Persian error for an unknown account name (F2).
        """
        tg = self.settings.telegram
        name = (account or tg.active_account) or "اصلی"
        # Active-account fast path: honour the mirrored ``self.telegram``
        # (set by ``_sync_telegram_accounts`` and overridable in tests / by
        # the UI) instead of silently bypassing it for a freshly-built client.
        if account is None and self.telegram is not None:
            return name, self.telegram
        acc = tg.account(name)
        if name not in self._telegram_accounts:
            client = None
            if acc.api_id and acc.api_hash and acc.phone:
                client = PersonalTelegram(
                    api_id=acc.api_id,
                    api_hash=acc.api_hash,
                    phone=acc.phone,
                    session_path=self.settings.telegram_session_path_for(name),
                    account_name=name,
                )
                self._telegram_accounts[name] = client
            if client is None:
                raise AssistantError(
                    f"اکانت تلگرام «{name}» پیدا نشد. "
                    "از https://my.telegram.org یک app بسازید و api_id / api_hash / phone "
                    "را در تنظیمات وب یا config.json ثبت کنید."
                )
        return name, self._telegram_accounts[name]

    def telegram_status(self, account: str | None = None) -> dict[str, Any]:
        """Connection state for one account (default: active) — no secrets.

        ``enabled`` is the *per-account* flag; ``feature_enabled`` is the
        global Telegram toggle.  The two are deliberately separate so the
        UI can show «فعال» per account while the master switch stays global.
        """
        tg = self.settings.telegram
        name = (account or tg.active_account) or "اصلی"
        acc = tg.account(name)
        client = self._telegram_accounts.get(name)
        if client is not None:
            state = client.login_state
        elif acc.enabled and acc.api_id:
            state = "disconnected"
        else:
            state = "disabled"
        return {
            "account": name,
            "enabled": bool(acc.enabled),
            "feature_enabled": bool(tg.enabled),
            "connected": bool(client and client.is_connected),
            "state": state,
            "phone": acc.phone,
            "session_path": str(self.settings.telegram_session_path_for(name)),
            "session_file_exists": self.settings.telegram_session_path_for(name).is_file(),
            "auto_connect": bool(acc.enabled and self.settings.telegram_session_path_for(name).is_file()),
            "connected_at": (getattr(client, "connected_at", None).isoformat()
                             if getattr(client, "connected_at", None) else None),
            "last_error": getattr(client, "last_error", "") if client else "",
            "has_credentials": bool(acc.api_id and acc.api_hash and acc.phone),
            "confirm_send": bool(acc.confirm_send),
        }

    def telegram_accounts_status(self) -> dict[str, Any]:
        """Status of every account (no secrets) plus the active one.

        When settings were constructed directly (no ``accounts`` materialised
        yet), the active account is synthesised so the UI never renders an
        empty list.
        """
        tg = self.settings.telegram
        names = [a.name for a in tg.accounts]
        if not names:
            names = [tg.active_account or "اصلی"]
        accounts = [self.telegram_status(name) for name in names]
        return {
            "enabled": bool(tg.enabled),
            "active_account": tg.active_account,
            "accounts": accounts,
        }

    def set_telegram_account_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """Toggle one account's ``enabled`` flag (persisted, secrets untouched).

        The web UI switch sends only ``{name, enabled}`` — credentials stay
        in the stored config and are never round-tripped through the UI.
        """
        tg = self.settings.telegram
        if not any(a.name == name for a in tg.accounts):
            raise AssistantError(f"اکانت تلگرام «{name}» وجود ندارد")
        accounts = [
            replace(a, enabled=bool(enabled)) if a.name == name else a
            for a in tg.accounts
        ]
        new_tg = TelegramSettings(
            enabled=tg.enabled, active_account=tg.active_account,
            accounts=tuple(accounts),
        )
        self._apply_settings(self.settings.with_overrides(telegram=new_tg))
        return self.telegram_accounts_status()

    def _mark_account_enabled(self, name: str) -> None:
        """Persist ``enabled=True`` for one account (and the feature).

        Called the moment a login flow reaches ``connected`` so that after
        a restart :meth:`_sync_telegram_accounts` rebuilds a client for the
        account instead of leaving it in the "disabled" state.
        """
        tg = self.settings.telegram
        acc = replace(tg.account(name), enabled=True)
        accounts = [replace(a, enabled=True) if a.name == name else a for a in tg.accounts]
        if not any(a.name == name for a in accounts):
            accounts.append(acc)
        new_tg = TelegramSettings(
            enabled=True, active_account=tg.active_account, accounts=tuple(accounts),
        )
        self._apply_settings(self.settings.with_overrides(telegram=new_tg))

    def add_telegram_account(self, name: str, phone: str, session_name: str | None = None) -> dict[str, Any]:
        """ثبت اکانت بدون تلاش برای چاپ یا بازگرداندن رازهای تلگرام."""
        tg = self.settings.telegram
        if any(a.name == name for a in tg.accounts):
            raise AssistantError(f"اکانت تلگرام «{name}» از قبل وجود دارد")
        base = tg.account(tg.active_account or "اصلی")
        account = TelegramAccount(name=str(name), enabled=False, api_id=base.api_id,
                                  api_hash=base.api_hash, phone=str(phone),
                                  session_name=session_name or str(name))
        new = replace(tg, accounts=tuple(tg.accounts) + (account,))
        self._apply_settings(self.settings.with_overrides(telegram=new))
        return self.telegram_accounts_status()

    def remove_telegram_account(self, name: str, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise AssistantError("حذف اکانت خطرناک است؛ برای تأیید confirmed=true بفرستید")
        tg = self.settings.telegram
        account = tg.account(name)
        session = self.settings.telegram_session_path_for(name)
        client = self._telegram_accounts.pop(name, None)
        if client is not None:
            client.disconnect()
        if session.is_file():
            session.unlink()
        accounts = tuple(a for a in tg.accounts if a.name != name)
        active = tg.active_account if tg.active_account != name else (accounts[0].name if accounts else "اصلی")
        self._apply_settings(self.settings.with_overrides(telegram=replace(tg, accounts=accounts, active_account=active)))
        return self.telegram_accounts_status()

    def switch_telegram_account(self, name: str) -> dict[str, Any]:
        """Make ``name`` the active account and enable it (persisted).

        «فعال کن/تعویض» must also flip the account's ``enabled`` flag,
        otherwise ``_sync_telegram_accounts`` never builds a client for it
        after a restart and the account stays "disabled".
        """
        tg = self.settings.telegram
        if not any(a.name == name for a in tg.accounts):
            # No accounts materialised yet — activating the current active
            # account is still a valid enable operation.
            if name != tg.active_account:
                raise AssistantError(f"اکانت تلگرام «{name}» وجود ندارد")
            accounts = [replace(tg.account(name), enabled=True)]
        else:
            accounts = [replace(a, enabled=True) if a.name == name else a for a in tg.accounts]
        new_tg = TelegramSettings(
            enabled=tg.enabled, active_account=name, accounts=tuple(accounts),
        )
        self._apply_settings(self.settings.with_overrides(telegram=new_tg))
        return self.telegram_accounts_status()

    def start_telegram_login(self, account: str | None = None) -> dict[str, Any]:
        """Begin the SMS-code login flow (web UI state machine)."""
        name, client = self._account_client(account)
        try:
            result = client.start_login()
        except TelegramError as exc:
            raise AssistantError(str(exc)) from exc
        except Exception as exc:
            if _is_telegram_network_error(exc):
                logger.warning("telegram start_login network failure: %s", exc)
                raise AssistantError(_TELEGRAM_NETWORK_HINT) from exc
            logger.warning("telegram start_login failed: %s", exc)
            raise AssistantError(
                "اتصال به سرور تلگرام ممکن نشد؛ اتصال اینترنت را بررسی کنید "
                "(در صورت نیاز فیلترشکن) و دوباره تلاش کنید."
            ) from exc
        if result.get("state") == "connected":
            # A valid session file skipped the code step — persist enabled so
            # the client survives restarts.
            self._mark_account_enabled(name)
        self._publish_telegram_state()
        return {**result, **self.telegram_status(name)}

    def submit_telegram_code(self, code: str, account: str | None = None) -> dict[str, Any]:
        name, client = self._account_client(account)
        if client is None:
            raise AssistantError("اتصال تلگرام شروع نشده است؛ دوباره دکمهٔ اتصال را بزنید")
        try:
            result = client.submit_code(code)
        except TelegramError as exc:
            raise AssistantError(str(exc)) from exc
        except Exception as exc:
            if _is_telegram_network_error(exc):
                logger.warning("telegram submit_code network failure: %s", exc)
                raise AssistantError(_TELEGRAM_NETWORK_HINT) from exc
            logger.warning("telegram submit_code failed: %s", exc)
            raise AssistantError(
                "ارسال کد به سرور تلگرام ناموفق بود؛ اتصال اینترنت را بررسی کنید."
            ) from exc
        if result.get("state") == "connected":
            # No 2FA on the account — the code completed the login.
            self._mark_account_enabled(name)
        self._publish_telegram_state()
        return {**result, **self.telegram_status(name)}

    def submit_telegram_password(self, password: str, account: str | None = None) -> dict[str, Any]:
        name, client = self._account_client(account)
        if client is None:
            raise AssistantError("اتصال تلگرام شروع نشده است؛ دوباره دکمهٔ اتصال را بزنید")
        try:
            result = client.submit_password(password)
        except TelegramError as exc:
            raise AssistantError(str(exc)) from exc
        except Exception as exc:
            if _is_telegram_network_error(exc):
                logger.warning("telegram submit_password network failure: %s", exc)
                raise AssistantError(_TELEGRAM_NETWORK_HINT) from exc
            logger.warning("telegram submit_password failed: %s", exc)
            raise AssistantError(
                "ارسال رمز 2FA به سرور تلگرام ناموفق بود؛ اتصال اینترنت را بررسی کنید."
            ) from exc
        if result.get("state") == "connected":
            # Login complete — persist enabled=True for this account so the
            # session is rebuilt automatically after a restart.
            self._mark_account_enabled(name)
        self._publish_telegram_state()
        return {**result, **self.telegram_status(name)}

    def connect_telegram(
        self, *, code_callback=None, password_callback=None, account: str | None = None
    ) -> dict[str, Any]:
        """Blocking connect with callbacks — used by the CLI."""
        name, client = self._account_client(account)
        try:
            message = client.connect(code_callback=code_callback, password_callback=password_callback)
        except TelegramError as exc:
            raise AssistantError(str(exc)) from exc
        except Exception as exc:
            if _is_telegram_network_error(exc):
                logger.warning("telegram connect network failure: %s", exc)
                raise AssistantError(_TELEGRAM_NETWORK_HINT) from exc
            logger.warning("telegram connect failed: %s", exc)
            raise AssistantError(
                "اتصال به سرور تلگرام ممکن نشد؛ اتصال اینترنت را بررسی کنید "
                "(در صورت نیاز فیلترشکن) و دوباره تلاش کنید."
            ) from exc
        self._mark_account_enabled(name)
        self._publish_telegram_state()
        return {"state": "connected", "message": message, **self.telegram_status(name)}

    def disconnect_telegram(self, account: str | None = None) -> dict[str, Any]:
        name = (account or self.settings.telegram.active_account) or "اصلی"
        client = self._telegram_accounts.get(name)
        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("telegram disconnect failed: %s", exc)
        self._publish_telegram_state()
        return self.telegram_status(name)

    def _publish_telegram_state(self) -> None:
        self.event_bus.publish(Event(
            type=EventType.TELEGRAM_STATE.value,
            payload={"telegram": self.telegram_status(), "accounts": self.telegram_accounts_status()},
            run_id="",
        ))

    # ----------------------------------------------------------- github flow

    def _build_github_client(self, name: str, acc: GitHubAccount) -> GitHubClient:
        return GitHubClient(
            account_name=name,
            api_base=acc.api_base or "https://api.github.com",
            client_id=acc.client_id,
            client_secret=acc.client_secret,
            token_file=self.settings.github_token_path_for(name),
            default_scope=self.settings.github.default_scope,
            data_dir=self.settings.data_dir,
        )

    def _sync_github_accounts(self) -> None:
        """Build one GitHubClient per enabled account; auto-connect stored tokens.

        Mirrors the active account onto ``self.github`` and
        ``context.extra["github"]`` so the single-account action code keeps
        working unchanged.  ``connect()`` only validates an existing token
        file; it never starts the OAuth flow (that is UI-driven).
        """
        gh = self.settings.github
        desired = {a.name: a for a in gh.accounts if a.enabled}
        for name in list(self._github_accounts):
            if name not in desired:
                self._github_accounts.pop(name, None)
        for name, acc in desired.items():
            if name in self._github_accounts:
                continue
            self._github_accounts[name] = self._build_github_client(name, acc)
        active = self._github_accounts.get(gh.active_account)
        if active is not None and self.settings.github_token_path_for(gh.active_account).is_file():
            try:
                active.connect()  # validate stored token only
            except Exception as exc:  # noqa: BLE001 - status is user-facing
                active.last_error = str(exc)
        self.github = active
        self.context.extra["github"] = active
        self._publish_github_state()

    def github_status(self, account: str | None = None) -> dict[str, Any]:
        """Connection state for one GitHub account (default: active) — no secrets."""
        gh = self.settings.github
        name = (account or gh.active_account) or "اصلی"
        acc = gh.account(name)
        client = self._github_accounts.get(name)
        if client is not None:
            state = client.login_state
            login = client.login
            connected = client.is_connected
            last_error = client.last_error
        elif acc.enabled and (acc.client_id or acc.auth_mode == "pat"):
            state = "disconnected"
            login = ""
            connected = False
            last_error = ""
        else:
            state = "disabled"
            login = ""
            connected = False
            last_error = ""
        return {
            "account": name,
            "enabled": bool(acc.enabled),
            "feature_enabled": bool(gh.enabled),
            "connected": bool(connected),
            "state": state,
            "login": login,
            "auth_mode": acc.auth_mode,
            "has_client_id": bool(acc.client_id),
            "token_file_exists": self.settings.github_token_path_for(name).is_file(),
            "confirm_push": bool(acc.confirm_push),
            "last_error": last_error,
        }

    def github_accounts_status(self) -> dict[str, Any]:
        gh = self.settings.github
        names = [a.name for a in gh.accounts] or [gh.active_account or "اصلی"]
        return {
            "enabled": bool(gh.enabled),
            "active_account": gh.active_account,
            "accounts": [self.github_status(name) for name in names],
        }

    def switch_github_account(self, name: str) -> dict[str, Any]:
        gh = self.settings.github
        if not any(a.name == name for a in gh.accounts):
            if name != gh.active_account:
                raise AssistantError(f"اکانت گیتهاب «{name}» وجود ندارد")
            accounts = [replace(gh.account(name), enabled=True)]
        else:
            accounts = [replace(a, enabled=True) if a.name == name else a for a in gh.accounts]
        new_gh = GitHubSettings(enabled=gh.enabled, active_account=name, accounts=tuple(accounts))
        self._apply_settings(self.settings.with_overrides(github=new_gh))
        return self.github_accounts_status()

    def start_github_oauth(self, account: str | None = None, *, redirect_uri: str) -> dict[str, Any]:
        """Begin the OAuth redirect flow for one account; returns the authorize URL."""
        name = (account or self.settings.github.active_account) or "اصلی"
        acc = self.settings.github.account(name)
        client = self._github_accounts.get(name) or self._build_github_client(name, acc)
        self._github_accounts[name] = client
        try:
            url, state = client.authorize_url(redirect_uri, state_registry=self._github_pending)
        except GitHubError as exc:
            raise AssistantError(str(exc)) from exc
        return {"account": name, "authorize_url": url, "state": state, "redirect_uri": redirect_uri}

    def complete_github_oauth(self, code: str, state: str) -> dict[str, Any]:
        """Finish the OAuth flow: validate ``state``, exchange ``code``."""
        pending = self._github_pending.pop(state, None)
        if pending is None:
            raise AssistantError("درخواست OAuth نامعتبر یا منقضی است؛ دوباره دکمهٔ اتصال را بزنید.")
        client = self._github_accounts.get(pending.account)
        if client is None:
            acc = self.settings.github.account(pending.account)
            client = self._build_github_client(pending.account, acc)
            self._github_accounts[pending.account] = client
        try:
            result = client.exchange_code(code, client_secret=pending.client_secret)
        except GitHubError as exc:
            client.last_error = str(exc)
            self._publish_github_state()
            raise AssistantError(str(exc)) from exc
        self._mark_github_enabled(pending.account)
        self._publish_github_state()
        return {**result, **self.github_status(pending.account)}

    def connect_github_pat(self, token: str, account: str | None = None) -> dict[str, Any]:
        """Validate a Personal Access Token and store it for the account."""
        name = (account or self.settings.github.active_account) or "اصلی"
        acc = self.settings.github.account(name)
        client = self._github_accounts.get(name) or self._build_github_client(name, acc)
        self._github_accounts[name] = client
        try:
            result = client.connect_pat(token)
        except GitHubError as exc:
            client.last_error = str(exc)
            self._publish_github_state()
            raise AssistantError(str(exc)) from exc
        self._mark_github_enabled(name)
        self._publish_github_state()
        return {**result, **self.github_status(name)}

    def disconnect_github(self, account: str | None = None) -> dict[str, Any]:
        name = (account or self.settings.github.active_account) or "اصلی"
        client = self._github_accounts.get(name)
        if client is not None:
            try:
                client.forget_token()
            except Exception as exc:  # noqa: BLE001
                logger.debug("github disconnect failed: %s", exc)
        self._publish_github_state()
        return self.github_status(name)

    def _mark_github_enabled(self, name: str) -> None:
        gh = self.settings.github
        accounts = [replace(a, enabled=True) if a.name == name else a for a in gh.accounts]
        if not any(a.name == name for a in accounts):
            accounts.append(replace(gh.account(name), enabled=True))
        new_gh = GitHubSettings(enabled=True, active_account=gh.active_account, accounts=tuple(accounts))
        self._apply_settings(self.settings.with_overrides(github=new_gh))

    def _publish_github_state(self) -> None:
        self.event_bus.publish(Event(
            type=EventType.GITHUB_STATE.value,
            payload={"github": self.github_status(), "accounts": self.github_accounts_status()},
            run_id="",
        ))

    # ---------------------------------------------------------- scheduler

    def _on_scheduled_fired(self, job: ScheduledJob) -> None:
        """Callback از ریسمان زمان‌بند: اعلان/اجرای کار و انتشار رویداد.

        ``reminder`` → اعلان دسکتاپ + رویداد ``scheduled_fired``.
        ``task`` → اجرای اکشن (با auto_confirm، چون تأیید هنگام ثبت گرفته
        شده) و نتیجهٔ موفق/ناموفق همان رویداد می‌شود.
        """
        if job.type == "task":
            try:
                result = self._invoke_action_sync(ActionInvocation(
                    name=job.action_name, arguments=job.arguments or {}, auto_confirm=True,
                ))
                payload = {
                    "job": job.to_dict(),
                    "success": result.success,
                    "result": result.text,
                }
            except Exception as exc:
                logger.exception("scheduled task %s crashed", job.id)
                payload = {"job": job.to_dict(), "success": False, "result": f"خطا: {exc}"}
        else:
            payload = {"job": job.to_dict(), "success": True, "result": ""}
        notify_desktop("⏰ یادآوری" if job.type == "reminder" else f"⏰ کار زمان‌بندی‌شده: {job.action_name}",
                       job.message or payload.get("result") or "")
        self.event_bus.publish(Event(
            type=EventType.SCHEDULED_FIRED.value,
            payload=payload,
            run_id="",
        ))

    # ---------------------------------------------------------------- actions

    def _invoke_action_sync(self, inv: ActionInvocation) -> ActionResult:
        with self._lock:
            # Install a temporary auto-approve gate for this call
            previous = self.gate._auto_approve_all  # type: ignore[attr-defined]
            if inv.auto_confirm:
                self.gate.auto_approve()
            try:
                result_text = run_action(self.registry, inv.name, inv.arguments, self.context)
                return ActionResult(
                    name=inv.name,
                    text=result_text,
                    success=True,
                    artifacts=_collect_artifacts(result_text, self.settings),
                )
            except ActionRefused as exc:
                return ActionResult(
                    name=inv.name, text=str(exc), success=False, refused=True
                )
            except DependencyMissing as exc:
                return ActionResult(
                    name=inv.name,
                    text=str(exc),
                    success=False,
                    error=f"missing dependency: {exc.install_hint}" if exc.install_hint else str(exc),
                )
            except AssistantError as exc:
                return ActionResult(
                    name=inv.name, text=str(exc), success=False, error=str(exc)
                )
            finally:
                if inv.auto_confirm and not previous:
                    self.gate.reset()
                elif inv.auto_confirm and previous:
                    self.gate.auto_approve()

    # ---------------------------------------------------------------- chat

    def _runtime_for(self, session_id: str | None = None) -> RuntimeContext:
        """Get (or create) the runtime for a chat session (F4).

        ``None``/``"default"`` maps to the shared ``self.runtime``.  Any other
        session id gets its own runtime whose history lives at
        ``data_dir/history/<session_id>.jsonl``, so tabs never share history.
        Live sessions are capped (oldest unused are dropped) and unused
        sessions are closed after a quiet period.
        """
        sid = (session_id or "default") or "default"
        if sid == "default":
            return self.runtime
        with self._sessions_lock:
            runtime = self._sessions.get(sid)
            if runtime is None:
                self._close_stale_sessions()
                history = self.settings.data_dir / "history" / f"{sid}.jsonl"
                runtime = RuntimeContext(self.settings, history_path=history)
                self._sessions[sid] = runtime
                if len(self._sessions) > MAX_SESSIONS:
                    self._close_stale_sessions()
            runtime._last_used = time.monotonic()  # type: ignore[attr-defined]
            return runtime

    def _close_stale_sessions(self) -> None:
        """Drop the oldest sessions once we exceed the cap, and any that have
        been idle longer than the session timeout."""
        now = time.monotonic()
        with self._sessions_lock:
            stale = [
                sid for sid, rt in self._sessions.items()
                if now - getattr(rt, "_last_used", now) > SESSION_TIMEOUT_SECONDS
            ]
            for sid in stale:
                self._sessions.pop(sid, None)
            while len(self._sessions) > MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda s: getattr(self._sessions[s], "_last_used", 0))
                self._sessions.pop(oldest, None)

    def _start_chat_run(self, user_message: str, session_id: str | None = None) -> str:
        if not user_message:
            raise AssistantError("empty chat message")
        run_id = uuid.uuid4().hex[:12]
        stop_event = threading.Event()
        runtime = self._runtime_for(session_id)
        self.event_bus.create_run_queue(run_id)
        thread = threading.Thread(
            target=self._chat_worker,
            args=(run_id, user_message, stop_event, runtime),
            name=f"bridge-chat-{run_id}",
            daemon=True,
        )
        with self._lock:
            self._active_runs[run_id] = stop_event
            self._run_threads[run_id] = thread
        thread.start()
        return run_id

    def _interrupt_run(self, run_id: str) -> None:
        with self._lock:
            ev = self._active_runs.get(run_id)
        if ev is not None:
            ev.set()

    def _chat_worker(self, run_id: str, user_message: str, stop_event: threading.Event,
                     runtime: RuntimeContext) -> None:
        try:
            self._chat_loop(run_id, user_message, stop_event, runtime)
        except Exception as exc:
            logger.exception("chat run %s crashed", run_id)
            self.event_bus.publish(Event(
                type=EventType.CHAT_FAILED.value,
                payload={"error": str(exc)},
                run_id=run_id,
            ))
        finally:
            self.event_bus.end_run(run_id)
            with self._lock:
                self._active_runs.pop(run_id, None)
                self._run_threads.pop(run_id, None)

    def _chat_loop(self, run_id: str, user_message: str, stop_event: threading.Event,
                   runtime: RuntimeContext) -> None:
        self.event_bus.publish(Event(
            type=EventType.CHAT_STARTED.value,
            payload={"user_message": user_message},
            run_id=run_id,
        ))

        runtime.append(ConversationMessage(role="user", content=user_message))

        max_turns = max(1, self.settings.safety.max_agent_turns)
        session_id = next((sid for sid, rt in self._sessions.items() if rt is runtime), None)
        tools = [a.to_tool_definition() for a in self._visible_tools(session_id)]
        client = create_client(self.settings.llm)

        for turn in range(max_turns):
            if stop_event.is_set():
                self.event_bus.publish(Event(
                    type=EventType.CHAT_FAILED.value,
                    payload={"reason": "interrupted"},
                    run_id=run_id,
                ))
                return
            self.event_bus.publish(Event(
                type=EventType.TURN_STARTED.value,
                payload={"turn": turn + 1, "max_turns": max_turns},
                run_id=run_id,
            ))
            streamed = False

            def emit_delta(piece: str) -> None:
                """Push each token to the frontends as it arrives."""
                nonlocal streamed
                if stop_event.is_set() or not piece:
                    return
                streamed = True
                self.event_bus.publish(Event(
                    type=EventType.ASSISTANT_DELTA.value,
                    payload={"text": piece},
                    run_id=run_id,
                ))

            # ``complete_streaming`` is optional: any object exposing the
            # plain ``complete`` method (including test doubles and third
            # party clients) still works.
            stream = getattr(client, "complete_streaming", None)
            try:
                if callable(stream):
                    reply = stream(self._build_messages(runtime), tools, emit_delta)
                else:
                    reply = client.complete(self._build_messages(runtime), tools)
            except Exception as exc:  # noqa: BLE001
                # B5: surface a readable Persian message (network/4xx/5xx),
                # never a raw English traceback as an "internal error".
                self.event_bus.publish(Event(
                    type=EventType.CHAT_FAILED.value,
                    payload={"error": _friendly_llm_error(exc)},
                    run_id=run_id,
                ))
                return
            # --- UI streaming for assistant text (even when tool calls follow)
            if reply.content and not streamed:
                # Provider did not stream; emit the text in one go so
                # frontends still receive a delta before the final.
                self.event_bus.publish(Event(
                    type=EventType.ASSISTANT_DELTA.value,
                    payload={"text": reply.content},
                    run_id=run_id,
                ))
            if reply.content:
                self.event_bus.publish(Event(
                    type=EventType.ASSISTANT_FINAL.value,
                    payload={"text": reply.content},
                    run_id=run_id,
                ))

            if not reply.has_tool_calls:
                if reply.content:
                    runtime.append(ConversationMessage(role="assistant", content=reply.content))
                self.event_bus.publish(Event(type=EventType.CHAT_DONE.value, payload={}, run_id=run_id))
                return

            # OpenAI-compatible providers (AvalAI, OpenAI, ...) require every
            # ``tool`` message to follow a single ``assistant`` message that
            # carries the matching ``tool_calls`` entries.
            # Combine assistant text + tool_calls in ONE message to keep the
            # conversation valid (assistant -> tool -> assistant ...).
            call_ids: list[str] = []
            openai_tool_calls: list[dict[str, Any]] = []
            for call in reply.tool_calls:
                call_id = call.id or f"call_{uuid.uuid4().hex[:12]}"
                call_ids.append(call_id)
                openai_tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                })
            runtime.append(ConversationMessage(
                role="assistant",
                content=reply.content or "",
                tool_calls=openai_tool_calls,
            ))

            for call, call_id in zip(reply.tool_calls, call_ids):
                if stop_event.is_set():
                    # An interrupt that lands mid-turn must still surface as a
                    # clean ``chat_failed`` (reason=interrupted), never as a
                    # silent exit that leaves the UI hanging.
                    self.event_bus.publish(Event(
                        type=EventType.CHAT_FAILED.value,
                        payload={"reason": "interrupted"},
                        run_id=run_id,
                    ))
                    return
                self.event_bus.publish(Event(
                    type=EventType.TOOL_PROPOSED.value,
                    payload={
                        "name": call.name,
                        "arguments": call.arguments,
                        "call_id": call_id,
                    },
                    run_id=run_id,
                ))
                result = self._invoke_with_bridge_confirmation(call.name, call.arguments, run_id)
                if result.refused:
                    text = f"REFUSED: {result.text}"
                elif not result.success:
                    text = f"ERROR: {result.error or result.text}"
                else:
                    text = result.text
                artifacts = _collect_artifacts(text, self.settings) or list(result.artifacts)
                runtime.append(ConversationMessage(
                    role="tool",
                    name=call.name,
                    tool_call_id=call_id,
                    content=text,
                ))
                self.event_bus.publish(Event(
                    type=EventType.TOOL_RESULT.value,
                    payload={
                        "name": call.name,
                        "text": text,
                        "success": result.success,
                        "refused": result.refused,
                        "call_id": call_id,
                        "artifacts": artifacts,
                    },
                    run_id=run_id,
                ))
        # Turn cap
        self.event_bus.publish(Event(
            type=EventType.CHAT_FAILED.value,
            payload={"reason": "turn_cap"},
            run_id=run_id,
        ))

    def _invoke_with_bridge_confirmation(self, name: str, arguments: dict[str, Any], run_id: str) -> ActionResult:
        """Run an action, but route its confirmation through the event bus.

        Frontends see a TOOL_CONFIRM_REQUESTED event and respond with a
        follow-up INTERRUPT or by simply allowing the request.  To keep
        the model flowing, we ask the user via the bus and block on a
        short timeout.  In auto-confirm mode, we skip the wait.
        """
        action = self.registry.get(name)
        if not action.needs_confirmation(self.settings.safety, arguments):
            return self._invoke_action_sync(ActionInvocation(name=name, arguments=arguments))

        if self.gate._auto_approve_all:  # type: ignore[attr-defined]
            return self._invoke_action_sync(ActionInvocation(name=name, arguments=arguments))

        request_id = uuid.uuid4().hex[:12]
        pending = PendingConfirmation(
            request_id=request_id, name=name, arguments=arguments, run_id=run_id
        )
        with self._confirmation_lock:
            self._pending_confirms[request_id] = pending

        self.event_bus.publish(Event(
            type=EventType.TOOL_CONFIRM_REQUESTED.value,
            payload={
                "request_id": request_id,
                "name": name,
                "arguments": arguments,
                "risk": action.risk_level.value,
            },
            run_id=run_id,
        ))

        # Block for up to 120s waiting for a response.
        if pending.event.wait(timeout=120):
            with self._confirmation_lock:
                self._pending_confirms.pop(request_id, None)
            if pending.approved:
                return self._invoke_action_sync(
                    ActionInvocation(name=name, arguments=arguments, auto_confirm=True)
                )
            return ActionResult(
                name=name, text="user declined via bridge", success=False, refused=True
            )
        # Timeout = refuse for safety
        with self._confirmation_lock:
            self._pending_confirms.pop(request_id, None)
        return ActionResult(
            name=name,
            text="confirmation timed out (defaulting to refuse)",
            success=False,
            refused=True,
        )

    def resolve_confirmation(self, request_id: str, approved: bool) -> bool:
        with self._confirmation_lock:
            pending = self._pending_confirms.get(request_id)
        if pending is None:
            return False
        pending.approved = approved
        pending.event.set()
        # Let every frontend close the approval card once a decision is in.
        self.event_bus.publish(Event(
            type=EventType.TOOL_CONFIRM_RESOLVED.value,
            payload={"request_id": request_id, "approved": approved, "name": pending.name},
            run_id=pending.run_id,
        ))
        return True

    def _build_messages(self, runtime: RuntimeContext | None = None) -> list[dict[str, Any]]:
        runtime = runtime or self.runtime
        out: list[dict[str, Any]] = []
        if runtime.system_prompt:
            out.append({"role": "system", "content": runtime.system_prompt})
        for msg in runtime.snapshot():
            out.append(msg.to_openai())
        if len(out) > self.settings.llm.max_context_messages:
            out = [out[0]] + out[-self.settings.llm.max_context_messages + 1:]
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class PendingConfirmation:
    request_id: str
    name: str
    arguments: dict[str, Any]
    approved: bool = False
    run_id: str = ""
    event: threading.Event = field(default_factory=threading.Event)


_TELEGRAM_NETWORK_HINT = (
    "اتصال به سرور تلگرام برقرار نشد؛ اتصال اینترنت را بررسی کنید و در صورت "
    "نیاز از فیلترشکن/VPN استفاده کنید، سپس دوباره تلاش کنید."
)


def _is_telegram_network_error(exc: Exception) -> bool:
    """Is the failure a network/connectivity problem, not bad credentials?

    Telethon retries its connection and finally raises a plain
    ``ConnectionError`` ("Connection to Telegram failed 5 time(s)") — a
    completely different situation from «account not enabled/configured»,
    so it deserves its own readable Persian message.
    """
    name = type(exc).__name__
    text = str(exc).lower()
    return (
        isinstance(exc, ConnectionError)
        or "connection" in name.lower()
        or "connection to telegram failed" in text
        or "timed out" in text
        or "network" in text
        or "dns" in text
        # Telethon raises these when the socket is reset mid-handshake;
        # they are connectivity problems, not bad credentials.
        or "0 bytes read" in text
        or "bytes read on a total of" in text
        or "server closed the connection" in text
        or "eof occurred" in text
        or "reset by peer" in text
        or "handshake" in text
    )


def _coerce_setting_value(value: Any, current: Any) -> Any:
    """Coerce a raw ``config_set`` value to the type currently stored."""
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes", "on", "بله"}
    if isinstance(current, int):
        return int(str(value).strip())
    if isinstance(current, float):
        return float(str(value).strip())
    return str(value)


_ARTIFACT_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".md", ".txt", ".json", ".csv", ".log", ".pdf", ".zip",
}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_ARTIFACT_TOKEN_RE = re.compile(
    r"[^\s\"'<>|;]+\.(?:png|jpe?g|gif|webp|bmp|md|txt|json|csv|log|pdf|zip)",
    re.IGNORECASE,
)


def _collect_artifacts(text: str, settings: AssistantSettings) -> list[dict[str, Any]]:
    """Extract existing, in-scope files referenced by an action result.

    Tools like ``screen_capture`` save files (into ``data_dir/screenshots``)
    and only report them in the human-readable text.  This walks that text
    for ``*.png`` / ``*.md`` / ... tokens, checks each against the workspace
    and data directories, and returns a list of artifact descriptors:

    .. code-block:: json

        [{"name": "shot.png", "path": "screenshots/shot.png", "kind": "image"}]

    ``path`` is relative to the root it lives in so the web UI can serve it
    through ``/api/artifact``.  Anything outside the workspace/data dirs is
    ignored for safety.
    """
    if not text:
        return []
    work_dir = settings.work_dir.resolve()
    data_dir = settings.data_dir.resolve()
    screenshots = data_dir / "screenshots"
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _ARTIFACT_TOKEN_RE.finditer(str(text)):
        token = match.group(0).rstrip(",.;:)]}")
        if token.lower() in seen:
            continue
        seen.add(token.lower())
        candidate = Path(token)
        roots_to_try: list[tuple[Path, str, bool]] = []
        if candidate.is_absolute():
            roots_to_try.append((candidate, "", False))
        else:
            roots_to_try.append((work_dir / candidate, "", False))
            roots_to_try.append((data_dir / candidate, "", False))
            roots_to_try.append((screenshots / candidate.name, "screenshots", False))
        for raw, prefix, _relative in roots_to_try:
            try:
                resolved = raw.resolve()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            try:
                relative_to = resolved.relative_to(work_dir)
            except ValueError:
                pass
            else:
                suffix = resolved.suffix.lower()
                artifacts.append({
                    "name": resolved.name,
                    # Canonical forward slashes: the web layer accepts both
                    # separators, and stored conversations stay portable.
                    "path": relative_to.as_posix(),
                    "kind": "image" if suffix in _IMAGE_EXT else "file",
                })
                break
            try:
                relative_to = resolved.relative_to(data_dir)
            except ValueError:
                continue
            suffix = resolved.suffix.lower()
            artifacts.append({
                "name": resolved.name,
                "path": relative_to.as_posix(),
                "kind": "image" if suffix in _IMAGE_EXT else "file",
            })
            break
    return artifacts


def _friendly_llm_error(exc: Exception) -> str:
    """Translate provider/network failures into a readable Persian message.

    B5: a NameResolutionError / DNS failure or a down gateway must surface as
    «ارائه‌دهنده در دسترس نیست...» instead of a raw English traceback in the
    UI, and a transient 4xx/5xx must be retried (done upstream) rather than
    reported as an internal error.
    """
    name = type(exc).__name__
    text = str(exc) or ""
    lowered = text.lower()
    if (
        "NameResolutionError" in name
        or "CannotConnectError" in name
        or "ConnectionError" in name
        or "resolve" in lowered
        or "dns" in lowered
        or "connection" in lowered
    ):
        return "ارائه‌دهنده در دسترس نیست؛ اتصال اینترنت را بررسی کنید و دوباره تلاش کنید."
    if name == "LLMTimeout" or "timed out" in text.lower():
        return "دریافت پاسخ از مدل ناموفق بود (مهلت زمانی). دوباره تلاش کنید."
    if name == "LLMRateLimit":
        return "محدودیت نرخ ارائه‌دهنده فعال شد؛ چند لحظه صبر کنید و دوباره تلاش کنید."
    if "HTTP 400" in text or "400" in text and "stream" in text.lower():
        return "پاسخ مدل ناموفق بود (درخواست ناقص). دوباره تلاش کنید."
    if "401" in text or "403" in text:
        return "کلید API یا دسترسی نامعتبر است؛ اعتبار سنجی را بررسی کنید."
    return "پاسخ مدل ناموفق بود؛ دوباره تلاش کنید. (" + text[:120] + ")"


def _short(value: Any, limit: int = 120) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(value)
    return rendered[: limit - 3] + "..." if len(rendered) > limit else rendered


def telegram_has_clients(handlers: BridgeHandlers) -> bool:
    """Whether any account client exists (for the system-prompt banner)."""
    return bool(handlers._telegram_accounts)


def _capabilities(handlers: BridgeHandlers) -> list[str]:
    caps = [
        "actions",
        "chat_stream",
        "history",
        "telegram" if handlers.telegram else "telegram_disabled",
        "gui_automation" if is_gui_available() else "gui_automation_unavailable",
    ]
    return caps


def _build_system_prompt(registry, settings, gui_available: bool, telegram_enabled: bool) -> str:
    from ...cli.prompts import build_system_prompt
    return build_system_prompt(
        settings=settings,
        actions=registry.all(),
        gui_available=gui_available,
        telegram_enabled=telegram_enabled,
    )


def _ollama_reachable(base_url: str, timeout: float = 1.5) -> bool:
    """Cheap TCP probe so we never block startup on a dead Ollama."""
    try:
        parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 11434)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _build_gmail_client(
    settings: AssistantSettings, *, force: bool = False
) -> GmailClient | None:
    """Build the Gmail client when enabled (or when ``force``); never touches the network.

    Missing/incomplete config is expected while the user has not finished
    the settings form (e.g. no username yet), so it is logged at DEBUG —
    a WARNING here repeated on every startup only confused users.  The UI
    banner (:meth:`BridgeHandlers._warnings`) explains what to fill in.
    """
    if not settings.gmail.enabled and not force:
        return None
    try:
        return GmailClient.from_settings(settings.gmail, settings.data_dir)
    except GmailError as exc:
        logger.debug("gmail client not built: %s", exc)
        return None


def _auto_select_provider(settings: AssistantSettings) -> AssistantSettings:
    """Fall back to the OpenAI-compatible provider when Ollama is absent.

    Users of hosted gateways (AvalAI) keep the default ``ollama`` provider
    in their config and then hit connection errors on every request.  If a
    key and base URL are configured and Ollama is not listening, switch.
    """
    llm = settings.llm
    if llm.provider != "ollama":
        return settings
    if not (llm.openai_api_key and llm.openai_base_url):
        return settings
    if _ollama_reachable(llm.ollama_base_url):
        return settings
    logger.warning("Ollama unreachable; switching provider to openai_compatible")
    new_llm = type(llm)(**{**llm.__dict__, "provider": "openai_compatible"})
    return settings.with_overrides(llm=new_llm)
