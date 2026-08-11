"""Wire protocol for the Bridge.

The protocol is a small JSON message format that is identical over
HTTP request/response and WebSocket.  Every message has a ``type``
field; the rest of the payload depends on the type.  Messages are
versioned: the ``protocol_version`` field is bumped whenever a
breaking change is made.

Frontends should treat unknown ``type`` values as errors rather than
silently ignoring them.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

PROTOCOL_VERSION = 1


class MessageType(str, Enum):
    """All known message types exchanged with the Bridge."""

    # --- Handshake ---------------------------------------------------
    HELLO = "hello"  # client -> server: announce protocol version
    WELCOME = "welcome"  # server -> client: session_id + server info
    AUTH = "auth"  # client -> server: token
    AUTH_OK = "auth_ok"
    AUTH_FAIL = "auth_fail"

    # --- Commands (client -> server) ---------------------------------
    LIST_ACTIONS = "list_actions"
    INVOKE_ACTION = "invoke_action"
    CHAT = "chat"  # send a free-form user message; server streams events
    INTERRUPT = "interrupt"  # abort the current chat run
    GET_HISTORY = "get_history"
    GET_STATUS = "get_status"
    CLEAR_HISTORY = "clear_history"
    SET_MODEL = "set_model"
    LIST_MODELS = "list_models"

    # --- Server -> client (responses and events) --------------------
    RESPONSE = "response"
    EVENT = "event"  # streaming event from a chat run
    ERROR = "error"
    PING = "ping"
    PONG = "pong"

    # --- Subscriptions -----------------------------------------------
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"


# Event subtypes used inside the EVENT message payload.
class EventType(str, Enum):
    CHAT_STARTED = "chat_started"
    TURN_STARTED = "turn_started"
    ASSISTANT_DELTA = "assistant_delta"
    ASSISTANT_FINAL = "assistant_final"
    TOOL_PROPOSED = "tool_proposed"
    TOOL_CONFIRM_REQUESTED = "tool_confirm_requested"
    TOOL_CONFIRM_RESOLVED = "tool_confirm_resolved"
    TOOL_RESULT = "tool_result"
    CHAT_DONE = "chat_done"
    CHAT_FAILED = "chat_failed"
    LOG = "log"
    TELEGRAM_STATE = "telegram_state"
    GITHUB_STATE = "github_state"
    SCHEDULED_FIRED = "scheduled_fired"


@dataclass
class Hello:
    protocol_version: int = PROTOCOL_VERSION
    client: str = "local-agent-frontend"
    client_version: str = "1.0.0"
    auth_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Welcome:
    session_id: str
    server_version: str
    protocol_version: int
    user: str
    hostname: str
    platform: str
    capabilities: list[str] = field(default_factory=list)
    server_time: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Auth:
    token: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActionInvocation:
    """A request to run an action through the Bridge.

    The action name and arguments are exactly what the LLM would have
    sent; the Bridge performs validation, confirmation, and execution.
    """

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    auto_confirm: bool = False  # True skips the confirmation gate

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActionInvocation:
        return cls(
            name=str(payload.get("name", "")),
            arguments=dict(payload.get("arguments") or {}),
            auto_confirm=bool(payload.get("auto_confirm", False)),
        )


@dataclass
class ActionResult:
    name: str
    text: str
    success: bool
    refused: bool = False
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActionResult:
        return cls(
            name=str(payload.get("name", "")),
            text=str(payload.get("text", "")),
            success=bool(payload.get("success", True)),
            refused=bool(payload.get("refused", False)),
            error=payload.get("error"),
            artifacts=list(payload.get("artifacts") or []),
        )


@dataclass
class Event:
    """A streaming event from a chat run."""

    type: str  # one of EventType
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ErrorPayload:
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Request:
    """A typed request envelope."""

    id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Response:
    """A typed response envelope."""

    id: str
    ok: bool
    result: Any = None
    error: ErrorPayload | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "ok": self.ok, "type": MessageType.RESPONSE.value}
        if self.ok:
            out["result"] = self.result
        else:
            out["error"] = self.error.to_dict() if self.error else {"code": "?", "message": "?"}
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Response:
        error_payload = payload.get("error")
        return cls(
            id=str(payload.get("id", "")),
            ok=bool(payload.get("ok", False)),
            result=payload.get("result"),
            error=ErrorPayload(**error_payload) if isinstance(error_payload, dict) else None,
        )


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def encode_message(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_message(raw: bytes | str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in bridge message: {exc}") from exc


def is_welcome(message: dict[str, Any]) -> bool:
    return message.get("type") == MessageType.WELCOME.value
