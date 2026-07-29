"""HTTP + Server-Sent Events server for the Bridge.

The server is intentionally minimal: stdlib ``http.server`` and a tiny
SSE writer.  No third-party web framework is required.  The Bridge
is meant to be a localhost service; for production exposure you would
reverse-proxy through nginx with TLS.

Usage::

    server = BridgeServer(settings)
    server.start_in_thread()       # for tests
    server.start_foreground()      # for production

A second entry point ``start_daemon()`` detaches from the parent
process and is suitable for a Windows service wrapper.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from ...core.config import AssistantSettings
from ...core.errors import AssistantError
from ...core.logging_setup import get_logger, setup_logging
from ..protocol import MessageType, PROTOCOL_VERSION, Request, Response
from ..api.handlers import BridgeHandlers


logger = get_logger("bridge.server")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 0  # 0 = pick a free port
    token: str = ""
    allow_remote: bool = False  # when False, only 127.0.0.1 connections accepted

    @classmethod
    def from_settings(cls, settings: AssistantSettings, *, host: str, port: int) -> "ServerConfig":
        return cls(
            host=host or "127.0.0.1",
            port=int(port or 0),
            token=_resolve_token(settings),
            allow_remote=host not in {"", "127.0.0.1", "localhost"},
        )


def _resolve_token(settings: AssistantSettings) -> str:
    """Return the bearer token, generating and persisting one if needed."""
    explicit = os.environ.get("LOCAL_AGENT_BRIDGE_TOKEN", "").strip()
    if explicit:
        return explicit
    token_path = settings.data_dir / "bridge.token"
    if token_path.is_file():
        try:
            stored = token_path.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        except OSError:
            pass
    new_token = secrets.token_urlsafe(32)
    try:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(new_token, encoding="utf-8")
        try:
            # POSIX-only, ignored on Windows.
            os.chmod(token_path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.warning("could not persist bridge token: %s", exc)
    return new_token


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class BridgeServer:
    """HTTP + SSE server exposing the Bridge API.

    The server is split into two layers:

    * :class:`BridgeServer` (this class) — lifecycle, configuration,
      and a thin wrapper around :class:`BridgeHandlers`.
    * A custom :class:`BaseHTTPRequestHandler` subclass that dispatches
      requests to the handlers.

    The server runs in either *foreground* mode (blocks the calling
    thread) or *thread* mode (returns immediately; useful for tests
    and in-process usage).  See ``start_foreground`` and
    ``start_in_thread``.
    """

    def __init__(self, settings: AssistantSettings) -> None:
        self.settings = settings
        self.handlers = BridgeHandlers.build(settings)
        self.config = ServerConfig.from_settings(
            settings,
            host=os.environ.get("LOCAL_AGENT_BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("LOCAL_AGENT_BRIDGE_PORT", "0") or 0),
        )
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._in_process = False
        self._stop = threading.Event()
        self.event_bus = self.handlers.event_bus

    # ----------------------------------------------------------- lifecycle

    def start_in_thread(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._httpd = _make_server(self.config, self.handlers, self._stop)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="bridge-http", daemon=True)
        self._thread.start()
        return self._thread

    def start_foreground(self) -> None:
        self._httpd = _make_server(self.config, self.handlers, self._stop)
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self.telegram and self.telegram.is_connected:
            try:
                self.telegram.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------- introspection

    @property
    def actual_port(self) -> int:
        if self._httpd is None:
            return self.config.port
        return int(self._httpd.server_address[1])

    @property
    def actual_host(self) -> str:
        return self.config.host

    @property
    def token(self) -> str:
        return self.config.token

    @property
    def telegram(self):
        return self.handlers.telegram

    def welcome(self):
        return self.handlers.welcome()

    # ------------------------------------------------------------ in-process

    def start_in_process(self) -> None:
        """Used by the in-process client."""
        self._in_process = True
        # No thread is needed; the BridgeClient calls ``handle_request``
        # directly in the calling thread.

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.handlers.handle(request)

    def stream_request(self, request: dict[str, Any]):
        type_ = str(request.get("type", ""))
        payload = dict(request.get("payload") or {})
        if type_ != MessageType.CHAT.value:
            yield self.handlers.handle(request)
            return
        run_id = self.handlers._start_chat_run(payload.get("message", ""))
        queue = self.event_bus.create_run_queue(run_id)
        # We rely on the worker thread to also publish through the bus;
        # the queue is a side-channel so we can yield events as they come.
        try:
            # Subscribe a one-shot listener that pushes into our local queue
            local: Queue[Any] = Queue()

            def listener(event) -> None:
                local.put(event)

            self.event_bus.subscribe(listener)
            try:
                while True:
                    try:
                        event = local.get(timeout=300)
                    except Empty:
                        yield {
                            "type": "event",
                            "event_type": "chat_failed",
                            "payload": {"error": "timeout"},
                            "run_id": run_id,
                        }
                        return
                    yield {
                        "type": "event",
                        "event_type": event.type,
                        "payload": event.payload,
                        "run_id": event.run_id,
                        "seq": event.seq,
                    }
                    if event.type in {"chat_done", "chat_failed"}:
                        return
            finally:
                self.event_bus.unsubscribe(listener)
        finally:
            self.event_bus.destroy_run_queue(run_id)


# ---------------------------------------------------------------------------
# HTTP server factory
# ---------------------------------------------------------------------------


def _make_server(config: ServerConfig, handlers: BridgeHandlers, stop_event: threading.Event) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((config.host, config.port), _BridgeHandler)
    server.bridge_handlers = handlers  # type: ignore[attr-defined]
    server.bridge_token = config.token  # type: ignore[attr-defined]
    server.bridge_allow_remote = config.allow_remote  # type: ignore[attr-defined]
    return server


def _check_token(headers, token: str) -> bool:
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    candidate = auth[len("Bearer "):].strip()
    return bool(candidate) and secrets.compare_digest(candidate, token)


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "LocalAgentBridge/1.0"

    # Silence the default per-request logging; we use the assistant logger.
    def log_message(self, *_args, **_kwargs) -> None:  # noqa: D401
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self) -> bool:
        return _check_token(self.headers, self.server.bridge_token)  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"ok": True, "protocol_version": PROTOCOL_VERSION})
            return
        if self.path == "/welcome":
            if not self._check_token():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            welcome = self.server.bridge_handlers.welcome()  # type: ignore[attr-defined]
            self._send_json(200, {"ok": True, "result": welcome.to_dict()})
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_token():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            request = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return

        if self.path == "/rpc":
            response = self.server.bridge_handlers.handle(request)  # type: ignore[attr-defined]
            self._send_json(200, response)
            return

        if self.path == "/stream":
            self._stream_events(request)
            return

        if self.path == "/confirm":
            payload = dict(request.get("payload") or {})
            request_id = str(payload.get("request_id", ""))
            approved = bool(payload.get("approved", False))
            ok = self.server.bridge_handlers.resolve_confirmation(request_id, approved)  # type: ignore[attr-defined]
            self._send_json(200, {"ok": True, "result": {"resolved": ok}})
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    # ---------------------------------------------------------------- SSE

    def _stream_events(self, request: dict[str, Any]) -> None:
        type_ = str(request.get("type", ""))
        if type_ != MessageType.CHAT.value:
            # Streaming is only meaningful for chat. Fall back to RPC.
            response = self.server.bridge_handlers.handle(request)  # type: ignore[attr-defined]
            self._send_json(200, response)
            return
        payload = dict(request.get("payload") or {})
        message = str(payload.get("message", ""))

        bus = self.server.bridge_handlers.event_bus  # type: ignore[attr-defined]

        # Buffer events between subscription and the moment we send
        # the SSE response headers.
        buffer: list[Any] = []
        ready = threading.Event()

        def listener(event) -> None:
            if ready.is_set():
                self._write_sse(event)
            else:
                buffer.append(event)
            if event.type in {"chat_done", "chat_failed"}:
                ready.set()

        bus.subscribe(listener)

        # Open the SSE response BEFORE starting the run so the client
        # can receive data immediately. We do not start the run yet.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        # Flush any events that arrived between subscription and headers
        for event in buffer:
            self._write_sse(event)
        buffer.clear()
        ready.set()

        # Now start the run
        run_id = self.server.bridge_handlers._start_chat_run(message)  # type: ignore[attr-defined]

        try:
            # Wait for the run queue to deliver the terminal sentinel
            queue = bus.create_run_queue(run_id)
            try:
                while True:
                    try:
                        event = queue.get(timeout=300)
                    except Empty:
                        return
                    if event is None:
                        return
                    # already written by the listener; just check terminal
                    if event.type in {"chat_done", "chat_failed"}:
                        return
            finally:
                bus.destroy_run_queue(run_id)
        finally:
            bus.unsubscribe(listener)

    def _write_sse(self, event) -> None:
        try:
            payload_out = {
                "type": "event",
                "event_type": event.type,
                "payload": event.payload,
                "run_id": event.run_id,
                "seq": event.seq,
            }
            line = "data: " + json.dumps(payload_out, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass
