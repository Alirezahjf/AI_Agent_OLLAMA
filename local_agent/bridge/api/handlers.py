"""BridgeHandlers: the actual implementation behind every Bridge request.

This is where the agent loop, tool registry, and event publisher live.
Both the in-process backend and the HTTP server delegate to it.
"""

from __future__ import annotations

import json
import platform
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urlparse
from typing import Any, Callable, Iterable

from ...actions import build_default_registry, run_action, describe_action
from ...actions.registry import ActionContext, ConfirmationGate
from ...automation import is_gui_available, register_gui
from ...core.config import AssistantSettings
from ...core.context import ConversationMessage, RuntimeContext
from ...core.errors import ActionRefused, AssistantError, DependencyMissing
from ...core.logging_setup import get_logger
from ...llm import create_client
from ...llm.client import ToolDefinition
from ...telegram import PersonalTelegram
from ..protocol import (
    ActionInvocation,
    ActionResult,
    ErrorPayload,
    Event,
    EventType,
    Hello,
    MessageType,
    PROTOCOL_VERSION,
    Request,
    Response,
    Welcome,
)


logger = get_logger("bridge.handlers")


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
        q: Queue[Event | None] = Queue()
        with self._lock:
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
            except Exception:  # noqa: BLE001
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
    telegram: PersonalTelegram | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _active_runs: dict[str, threading.Event] = field(default_factory=dict)
    _run_threads: dict[str, threading.Thread] = field(default_factory=dict)
    _confirmation_lock: threading.Lock = field(default_factory=threading.Lock)
    _pending_confirms: dict[str, "PendingConfirmation"] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: AssistantSettings) -> "BridgeHandlers":
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
        telegram = None
        if settings.telegram.enabled:
            telegram = PersonalTelegram(
                api_id=settings.telegram.api_id,
                api_hash=settings.telegram.api_hash,
                phone=settings.telegram.phone,
                session_path=settings.telegram_session_path,
            )
        runtime.set_system_prompt(_build_system_prompt(registry, settings, is_gui_available(), telegram is not None))
        return cls(
            settings=settings,
            runtime=runtime,
            registry=registry,
            context=context,
            gate=gate,
            telegram=telegram,
        )

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
                return Response(
                    id=request_id, ok=True,
                    result=[m.to_openai() for m in self.runtime.snapshot()],
                ).to_dict()
            if type_ == MessageType.CLEAR_HISTORY.value:
                self.runtime.clear()
                return Response(id=request_id, ok=True, result={"cleared": True}).to_dict()
            if type_ == MessageType.SET_MODEL.value:
                return Response(id=request_id, ok=True, result=self._set_model(payload)).to_dict()
            if type_ == MessageType.CHAT.value:
                # Returns a run_id immediately; events flow over the bus.
                run_id = self._start_chat_run(payload.get("message", ""))
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("bridge handler crashed")
            return self._fail(request_id, "internal", f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _fail(request_id: str, code: str, message: str) -> dict[str, Any]:
        return Response(
            id=request_id,
            ok=False,
            error=ErrorPayload(code=code, message=message),
        ).to_dict()

    # ---------------------------------------------------------------- status

    def _status(self) -> dict[str, Any]:
        return {
            "settings": {
                "data_dir": str(self.settings.data_dir),
                "work_dir": str(self.settings.work_dir),
                "llm_provider": self.settings.llm.provider,
                "llm_model": self.settings.llm.ollama_model or self.settings.llm.openai_model,
                "telegram_enabled": bool(self.telegram),
                "telegram_connected": bool(self.telegram and self.telegram.is_connected),
                "confirm_mode": self.settings.safety.confirm_mode,
            },
            "warnings": self._warnings(),
            "actions": [a.name for a in self.registry.all()],
            "history": self.runtime.stats(),
        }

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
        """
        path = self.settings.config_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.settings.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.runtime.settings = self.settings
            return True
        except OSError as exc:  # noqa: BLE001
            logger.warning("could not persist settings to %s: %s", path, exc)
            return False

    # ---------------------------------------------------------------- actions

    def _invoke_action_sync(self, inv: ActionInvocation) -> ActionResult:
        with self._lock:
            # Install a temporary auto-approve gate for this call
            previous = self.gate._auto_approve_all  # type: ignore[attr-defined]
            if inv.auto_confirm:
                self.gate.auto_approve()
            try:
                result_text = run_action(self.registry, inv.name, inv.arguments, self.context)
                return ActionResult(name=inv.name, text=result_text, success=True)
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

    def _start_chat_run(self, user_message: str) -> str:
        if not user_message:
            raise AssistantError("empty chat message")
        run_id = uuid.uuid4().hex[:12]
        stop_event = threading.Event()
        self.event_bus.create_run_queue(run_id)
        thread = threading.Thread(
            target=self._chat_worker,
            args=(run_id, user_message, stop_event),
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

    def _chat_worker(self, run_id: str, user_message: str, stop_event: threading.Event) -> None:
        try:
            self._chat_loop(run_id, user_message, stop_event)
        except Exception as exc:  # noqa: BLE001
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

    def _chat_loop(self, run_id: str, user_message: str, stop_event: threading.Event) -> None:
        self.event_bus.publish(Event(
            type=EventType.CHAT_STARTED.value,
            payload={"user_message": user_message},
            run_id=run_id,
        ))

        self.runtime.append(ConversationMessage(role="user", content=user_message))

        max_turns = max(1, self.settings.safety.max_agent_turns)
        tools = [a.to_tool_definition() for a in self.registry.all()]
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
            try:
                reply = client.complete(self._build_messages(), tools)
            except Exception as exc:  # noqa: BLE001
                self.event_bus.publish(Event(
                    type=EventType.CHAT_FAILED.value,
                    payload={"error": f"LLM error: {exc}"},
                    run_id=run_id,
                ))
                return
            if reply.content:
                # Emit assistant_delta for streaming frontends
                self.event_bus.publish(Event(
                    type=EventType.ASSISTANT_DELTA.value,
                    payload={"text": reply.content},
                    run_id=run_id,
                ))
                self.runtime.append(ConversationMessage(role="assistant", content=reply.content))
                self.event_bus.publish(Event(
                    type=EventType.ASSISTANT_FINAL.value,
                    payload={"text": reply.content},
                    run_id=run_id,
                ))

            if not reply.has_tool_calls:
                self.event_bus.publish(Event(type=EventType.CHAT_DONE.value, payload={}, run_id=run_id))
                return

            # OpenAI-compatible providers (AvalAI, OpenAI, ...) require every
            # ``tool`` message to follow a single ``assistant`` message that
            # carries the matching ``tool_calls`` entries.
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
            self.runtime.append(ConversationMessage(
                role="assistant",
                content="",
                tool_calls=openai_tool_calls,
            ))

            for call, call_id in zip(reply.tool_calls, call_ids):
                if stop_event.is_set():
                    return
                self.event_bus.publish(Event(
                    type=EventType.TOOL_PROPOSED.value,
                    payload={"name": call.name, "arguments": call.arguments},
                    run_id=run_id,
                ))
                result = self._invoke_with_bridge_confirmation(call.name, call.arguments, run_id)
                if result.refused:
                    text = f"REFUSED: {result.text}"
                elif not result.success:
                    text = f"ERROR: {result.error or result.text}"
                else:
                    text = result.text
                self.runtime.append(ConversationMessage(
                    role="tool",
                    name=call.name,
                    tool_call_id=call_id,
                    content=text,
                ))
                self.event_bus.publish(Event(
                    type=EventType.TOOL_RESULT.value,
                    payload={"name": call.name, "text": text, "success": result.success, "refused": result.refused},
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
        if not action.needs_confirmation(self.settings.safety):
            return self._invoke_action_sync(ActionInvocation(name=name, arguments=arguments))

        if self.gate._auto_approve_all:  # type: ignore[attr-defined]
            return self._invoke_action_sync(ActionInvocation(name=name, arguments=arguments))

        request_id = uuid.uuid4().hex[:12]
        pending = PendingConfirmation(request_id=request_id, name=name, arguments=arguments)
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
        return True

    def _build_messages(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.runtime.system_prompt:
            out.append({"role": "system", "content": self.runtime.system_prompt})
        for msg in self.runtime.snapshot():
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
    event: threading.Event = field(default_factory=threading.Event)


def _short(value: Any, limit: int = 120) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(value)
    return rendered[: limit - 3] + "..." if len(rendered) > limit else rendered


def _capabilities(handlers: "BridgeHandlers") -> list[str]:
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
