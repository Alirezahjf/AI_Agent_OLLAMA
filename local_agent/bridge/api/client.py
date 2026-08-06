"""BridgeClient: typed access to the Bridge from any frontend.

Two modes are supported:

  * **In-process** (``start_in_process``): the Bridge runs in a
    background thread inside the same Python interpreter.  This is
    what the CLI and tests use, and what the desktop app uses when
    it does not need a separate daemon.

  * **HTTP** (``connect``): the Bridge runs as a separate daemon
    process and speaks a tiny JSON protocol over HTTP.  This is what
    the Telegram bot uses when it lives on a different machine.

Both modes present the same :class:`BridgeClient` interface so that
frontends can be written once and target either mode.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from ...core.errors import AssistantError
from ...core.logging_setup import get_logger
from ..protocol import (
    ActionInvocation,
    ActionResult,
    Event,
    Hello,
    PROTOCOL_VERSION,
    Welcome,
    decode_message,
    encode_message,
    is_welcome,
)


logger = get_logger("bridge.client")


class BridgeConnectionError(AssistantError):
    """The Bridge is unreachable or refused the connection."""


@dataclass
class BridgeInfo:
    """The welcome payload from the Bridge, plus the connection id."""

    session_id: str
    server_version: str
    protocol_version: int
    user: str
    hostname: str
    platform: str
    capabilities: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "server_version": self.server_version,
            "protocol_version": self.protocol_version,
            "user": self.user,
            "hostname": self.hostname,
            "platform": self.platform,
            "capabilities": list(self.capabilities),
        }


# ---------------------------------------------------------------------------
# In-process backend
# ---------------------------------------------------------------------------


class _InProcessBackend:
    """Runs the Bridge in a background thread inside the same process."""

    def __init__(self, server: Any) -> None:
        self._server = server

    def start(self) -> None:
        self._server.start_in_process()

    def stop(self) -> None:
        self._server.stop()

    def is_running(self) -> bool:
        return self._server.is_running()

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._server.handle_request(request)

    def stream(self, request: dict[str, Any]) -> Iterable[dict[str, Any]]:
        yield from self._server.stream_request(request)


# ---------------------------------------------------------------------------
# HTTP backend
# ---------------------------------------------------------------------------


class _HttpBackend:
    """Talks to the Bridge over HTTP.  ``base_url`` like ``http://127.0.0.1:7823``."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {self._token}"})

    def start(self) -> None:
        # The remote daemon is already running; we just verify reachability.
        try:
            self._session.get(f"{self._base}/health", timeout=5).raise_for_status()
        except requests.RequestException as exc:
            raise BridgeConnectionError(f"bridge not reachable at {self._base}: {exc}") from exc

    def stop(self) -> None:
        self._session.close()

    def is_running(self) -> bool:
        try:
            r = self._session.get(f"{self._base}/health", timeout=3)
            return r.ok
        except requests.RequestException:
            return False

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._session.post(
                f"{self._base}/rpc",
                json=request,
                timeout=600,
            )
        except requests.RequestException as exc:
            raise BridgeConnectionError(f"bridge call failed: {exc}") from exc
        if response.status_code >= 400:
            raise BridgeConnectionError(
                f"bridge returned HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise BridgeConnectionError(f"bridge returned non-JSON: {exc}") from exc

    def stream(self, request: dict[str, Any]):
        # The HTTP backend uses Server-Sent Events to stream a chat run.
        try:
            with self._session.post(
                f"{self._base}/stream",
                json=request,
                stream=True,
                timeout=None,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data:"):
                        try:
                            yield json.loads(line[len("data:"):].strip())
                        except ValueError:
                            continue
        except requests.RequestException as exc:
            raise BridgeConnectionError(f"bridge stream failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


EventCallback = Callable[[Event], None]


class BridgeClient:
    """A typed client to the Bridge.  See module docstring for usage."""

    def __init__(self, backend: Any, info: BridgeInfo | None = None) -> None:
        self._backend = backend
        self._info = info
        self._lock = threading.Lock()
        self._listeners: list[EventCallback] = []
        self._id_counter = 0

    # ---------------------------------------------------------------- I/O

    @property
    def info(self) -> BridgeInfo | None:
        return self._info

    def on_event(self, callback: EventCallback) -> None:
        with self._lock:
            self._listeners.append(callback)

    def _emit(self, event: Event) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                logger.exception("event listener raised")

    def _next_id(self) -> str:
        with self._lock:
            self._id_counter += 1
            return f"req-{self._id_counter}"

    # ----------------------------------------------------------- Generic

    def request(self, type_: str, payload: dict[str, Any] | None = None) -> Any:
        """Send a typed request and return the ``result`` (or raise)."""
        request = {"id": self._next_id(), "type": type_, "payload": payload or {}}
        response = self._backend.call(request)
        if not response.get("ok"):
            err = response.get("error") or {}
            raise AssistantError(f"bridge {type_} failed: {err.get('message', '?')}")
        return response.get("result")

    def stream(self, type_: str, payload: dict[str, Any] | None = None) -> Iterable[Event]:
        """Send a request that streams events.

        Yields :class:`Event` objects as they arrive.  The final event
        is always :data:`EventType.CHAT_DONE` (or ``CHAT_FAILED``).
        """
        request = {"id": self._next_id(), "type": type_, "payload": payload or {}}
        for raw in self._backend.stream(request):
            if raw.get("type") == "event":
                event = Event(
                    type=str(raw.get("event_type", raw.get("type", "?"))),
                    payload=dict(raw.get("payload") or {}),
                    run_id=str(raw.get("run_id", "")),
                    seq=int(raw.get("seq", 0)),
                )
                self._emit(event)
                yield event

    # -------------------------------------------------------- Typed calls

    def list_actions(self) -> list[dict[str, Any]]:
        return list(self.request("list_actions") or [])

    def invoke_action(self, name: str, arguments: dict[str, Any], *, auto_confirm: bool = False) -> ActionResult:
        payload = ActionInvocation(
            name=name, arguments=arguments, auto_confirm=auto_confirm
        ).to_dict()
        result = self.request("invoke_action", payload) or {}
        return ActionResult.from_dict(result)

    def list_models(self) -> list[str]:
        return list(self.request("list_models") or [])

    def set_model(self, *, provider: str | None = None, model: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model
        return dict(self.request("set_model", payload) or {})

    def get_status(self) -> dict[str, Any]:
        return dict(self.request("get_status") or {})

    def get_history(self, limit: int = 200, session_id: str | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"limit": limit}
        if session_id:
            payload["session_id"] = session_id
        return list(self.request("get_history", payload) or [])

    def clear_history(self, session_id: str | None = None) -> None:
        payload: dict[str, Any] = {}
        if session_id:
            payload["session_id"] = session_id
        self.request("clear_history", payload)

    def chat(self, message: str, session_id: str | None = None) -> Iterable[Event]:
        """Send a free-form user message and stream events back."""
        payload: dict[str, Any] = {"message": message}
        if session_id:
            payload["session_id"] = session_id
        return self.stream("chat", payload)

    # ------------------------------------------------------------ Factory

    @classmethod
    def start_in_process(cls, settings) -> "BridgeClient":
        """Start the Bridge inside this process and return a connected client."""
        from ..server.server import BridgeServer

        server = BridgeServer(settings)
        backend = _InProcessBackend(server)
        backend.start()
        info = _welcome_to_info(server.welcome())
        return cls(backend, info)

    @classmethod
    def connect(cls, *, base_url: str, token: str) -> "BridgeClient":
        """Connect to an already-running Bridge daemon."""
        backend = _HttpBackend(base_url, token)
        backend.start()
        # Fetch welcome so the client has a BridgeInfo
        try:
            response = backend.call({"id": "hello", "type": "hello", "payload": {"protocol_version": PROTOCOL_VERSION, "client": "client", "client_version": "1.0.0"}})
            welcome = Welcome(**response.get("result", {}))
        except Exception:  # noqa: BLE001
            welcome = None  # type: ignore[assignment]
        return cls(backend, _welcome_to_info(welcome) if welcome else None)


def _welcome_to_info(welcome: Welcome) -> BridgeInfo:
    return BridgeInfo(
        session_id=welcome.session_id,
        server_version=welcome.server_version,
        protocol_version=welcome.protocol_version,
        user=welcome.user,
        hostname=welcome.hostname,
        platform=welcome.platform,
        capabilities=list(welcome.capabilities),
    )
