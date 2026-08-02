"""FastAPI app that exposes the Bridge over HTTP and a tiny WebSocket.

The HTTP surface is deliberately small and stable so that the browser UI
(``templates/index.html`` + ``static/app.js``) and the desktop wrapper
(``local_agent.desktop``) can share exactly the same front-end.

Endpoints
---------

``GET  /``                    the single-page UI
``GET  /api/status``          bridge info + settings snapshot
``GET  /api/doctor``          self-check health report
``GET  /api/actions``         action descriptions (legacy, plain strings)
``GET  /api/actions/detail``  structured actions (name / risk / args)
``GET  /api/models``          models exposed by the active provider
``GET  /api/history``         shared conversation history
``POST /api/clear``           clear the shared history
``POST /api/chat``            start a chat run (events flow over /ws)
``POST /api/invoke``          run a single action
``POST /api/settings``        update provider / model / confirm mode
``POST /api/upload``          drop a file into the workspace
``GET  /api/file``            fetch a workspace artifact
``WS   /ws``                  chat + confirmation stream
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..bridge import BridgeClient
from ..core.config import AssistantSettings
from ..core.logging_setup import get_logger, setup_logging
from ..utils.paths import web_static_dir, web_templates_dir


logger = get_logger("web")


TEMPLATES = web_templates_dir()
STATIC = web_static_dir()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    auto_confirm: bool = False


class InvokeRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}
    auto_confirm: bool = False


class SettingsRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    confirm_mode: str | None = None


class UploadRequest(BaseModel):
    name: str
    content_base64: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ACTION_LINE = re.compile(
    r"^(?P<name>\S+)\s+\[risk=(?P<risk>[^\]]+)\]\s+args=\((?P<args>[^)]*)\)\s*(?P<description>.*)$"
)


def parse_action_line(line: str) -> dict[str, Any]:
    """Turn ``describe_action`` output into a structured record.

    The Bridge speaks in human-readable strings for backwards
    compatibility; the UI wants objects.  Unparseable lines degrade to a
    name-only record rather than raising.
    """
    match = _ACTION_LINE.match(str(line).strip())
    if not match:
        name = str(line).split()[0] if str(line).strip() else "?"
        return {"name": name, "risk": "safe", "args": [], "description": str(line).strip()}
    args = [a.strip() for a in match.group("args").split(",") if a.strip() and a.strip() != "-"]
    return {
        "name": match.group("name"),
        "risk": match.group("risk"),
        "args": args,
        "description": match.group("description").strip(),
    }


def safe_workspace_path(work_dir: Path, candidate: str) -> Path:
    """Resolve ``candidate`` inside ``work_dir``; refuse to escape it."""
    if not candidate:
        raise HTTPException(400, "empty path")
    raw = Path(candidate)
    target = raw if raw.is_absolute() else work_dir / raw
    try:
        resolved = target.resolve()
        root = work_dir.resolve()
    except OSError as exc:  # pragma: no cover - depends on filesystem
        raise HTTPException(400, f"bad path: {exc}") from exc
    if resolved != root and root not in resolved.parents:
        raise HTTPException(403, "path is outside the workspace")
    return resolved


def _server_of(client: BridgeClient) -> Any:
    backend = getattr(client, "_backend", None)
    return getattr(backend, "_server", None) if backend else None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(client: BridgeClient, settings: AssistantSettings) -> FastAPI:
    app = FastAPI(title="Local Windows Assistant", version="2.0")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((TEMPLATES / "index.html").read_text(encoding="utf-8"))

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "ts": time.time()}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {
            "bridge": client.info.to_dict() if client.info else None,
            "settings": client.get_status(),
        }

    @app.get("/api/doctor")
    async def doctor(offline: bool = False) -> dict[str, Any]:
        """Run the self-check and return a JSON health report."""
        from ..diagnostics import run_checks

        server = _server_of(client)
        active = server.handlers.settings if server is not None else settings
        report = await asyncio.to_thread(run_checks, active, network=not offline)
        return report.to_dict()

    @app.get("/api/actions")
    async def actions() -> list[str]:
        return client.list_actions()

    @app.get("/api/actions/detail")
    async def actions_detail() -> list[dict[str, Any]]:
        return [parse_action_line(line) for line in client.list_actions()]

    @app.get("/api/models")
    async def models() -> list[str]:
        try:
            return client.list_models()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not list models: {exc}")

    @app.get("/api/history")
    async def history(limit: int = 50) -> list[dict[str, Any]]:
        return client.get_history(limit=limit)

    @app.post("/api/clear")
    async def clear() -> dict[str, bool]:
        client.clear_history()
        return {"cleared": True}

    @app.post("/api/settings")
    async def update_settings(req: SettingsRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if req.provider:
            payload["provider"] = req.provider
        if req.model:
            payload["model"] = req.model
        server = _server_of(client)
        if server is not None and (req.openai_base_url or req.openai_api_key or req.confirm_mode):
            handlers = server.handlers
            llm = dict(handlers.settings.llm.__dict__)
            if req.openai_base_url:
                llm["openai_base_url"] = req.openai_base_url
            if req.openai_api_key:
                llm["openai_api_key"] = req.openai_api_key
            new_llm = type(handlers.settings.llm)(**llm)
            new_settings = handlers.settings.with_overrides(llm=new_llm)
            if req.confirm_mode in {"destructive", "always", "never"}:
                safety = dict(handlers.settings.safety.__dict__)
                safety["confirm_mode"] = req.confirm_mode
                new_settings = new_settings.with_overrides(
                    safety=type(handlers.settings.safety)(**safety)
                )
            handlers.settings = new_settings
            handlers._persist_settings()
        if not payload:
            return {"provider": req.provider or "", "model": req.model or ""}
        try:
            return client.set_model(**payload)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc))

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> dict[str, Any]:
        # The HTTP /api/chat endpoint starts a run and returns its id.
        # Clients use the WebSocket to receive events.
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "chat is only available with an in-process bridge")
        run_id = server.handlers._start_chat_run(req.message)
        return {"run_id": run_id}

    @app.post("/api/invoke")
    async def invoke(req: InvokeRequest) -> dict[str, Any]:
        try:
            result = client.invoke_action(req.name, req.arguments, auto_confirm=req.auto_confirm)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc))
        return result.to_dict()

    @app.post("/api/upload")
    async def upload(req: UploadRequest) -> dict[str, Any]:
        name = Path(req.name).name
        if not name:
            raise HTTPException(400, "missing file name")
        try:
            blob = base64.b64decode(req.content_base64 or "", validate=False)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(400, f"invalid base64 payload: {exc}")
        if len(blob) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file is larger than 25 MB")
        target = safe_workspace_path(settings.work_dir, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        logger.info("uploaded %s (%d bytes)", target, len(blob))
        return {"saved": str(target), "bytes": len(blob)}

    @app.get("/api/file")
    async def get_file(path: str) -> FileResponse:
        target = safe_workspace_path(settings.work_dir, path)
        if not target.is_file():
            raise HTTPException(404, "file not found")
        media_type, _ = mimetypes.guess_type(target.name)
        return FileResponse(str(target), media_type=media_type or "application/octet-stream")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except ValueError:
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": "invalid json"})
                    )
                    continue
                type_ = msg.get("type")
                if type_ == "chat":
                    message = str(msg.get("message", ""))
                    server = _server_of(client)
                    if server is None:
                        await websocket.send_text(
                            json.dumps({"type": "error", "message": "no in-process bridge"})
                        )
                        continue
                    run_id = server.handlers._start_chat_run(message)
                    queue = server.handlers.event_bus.create_run_queue(run_id)
                    try:
                        while True:
                            try:
                                event = await asyncio.to_thread(queue.get, timeout=600)
                            except Exception:
                                # queue.Empty or thread timeout - treat as end of stream
                                break
                            if event is None:
                                break
                            await websocket.send_text(json.dumps({
                                "type": "event",
                                "event_type": event.type,
                                "payload": event.payload,
                                "run_id": event.run_id,
                                "seq": event.seq,
                            }, ensure_ascii=False))
                            if event.type in {"chat_done", "chat_failed"}:
                                break
                    finally:
                        server.handlers.event_bus.destroy_run_queue(run_id)
                elif type_ == "confirm":
                    request_id = str(msg.get("request_id", ""))
                    approved = bool(msg.get("approved", False))
                    server = _server_of(client)
                    if server is None:
                        await websocket.send_text(
                            json.dumps({"type": "error", "message": "no bridge"})
                        )
                        continue
                    ok = server.handlers.resolve_confirmation(request_id, approved)
                    await websocket.send_text(json.dumps({"type": "confirm_result", "ok": ok}))
                elif type_ == "interrupt":
                    server = _server_of(client)
                    if server is not None:
                        server.handlers._interrupt_run(str(msg.get("run_id", "")))
                    await websocket.send_text(json.dumps({"type": "interrupted", "ok": True}))
                elif type_ == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "ts": time.time()}))
        except WebSocketDisconnect:
            return

    # Static assets
    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    return app


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class WebServer:
    """Convenience runner for the Web UI."""

    def __init__(
        self,
        settings: AssistantSettings,
        client: BridgeClient,
        *,
        host: str = "127.0.0.1",
        port: int = 7824,
    ) -> None:
        self.settings = settings
        self.client = client
        self.host = host
        self.port = port
        self._app = create_app(client, settings)
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start_in_thread(self) -> None:
        import uvicorn

        config = uvicorn.Config(self._app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="web-uvicorn")
        self._thread.start()

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        """Block until the socket accepts connections (or the timeout hits)."""
        import socket

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._server is not None and getattr(self._server, "started", False):
                return True
            with socket.socket() as probe:
                probe.settimeout(0.4)
                try:
                    probe.connect((self.host, self.port))
                    return True
                except OSError:
                    time.sleep(0.1)
        return False

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


def run_web(argv: list[str] | None = None) -> int:
    import argparse
    import os

    from ..core.config import load_settings
    from ..utils.platform import log_platform_summary

    parser = argparse.ArgumentParser(
        prog="persian-local-web",
        description="Serve the web UI for the Local Assistant.",
    )
    parser.add_argument("--host", default=os.environ.get("LOCAL_AGENT_WEB_HOST", "127.0.0.1"),
                        help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("LOCAL_AGENT_WEB_PORT", "7824")),
                        help="Port (default: 7824)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    settings = load_settings()
    setup_logging(settings.data_dir)
    log_platform_summary()

    host = args.host
    port = args.port

    # Security: require a token when binding to a non-loopback address
    if host not in {"127.0.0.1", "localhost", "::1"}:
        token_path = settings.data_dir / "bridge.token"
        if not token_path.is_file():
            print(
                "⚠️  هشدار امنیتی: شما در حال اتصال به آدرس غیرمحلی هستید "
                f"({host}). یک توکن احراز هویت لازم است.\n"
                "ابتدا یک بار دستیار را به صورت محلی اجرا کنید تا توکن تولید شود، "
                "یا متغیر LOCAL_AGENT_BRIDGE_TOKEN را تنظیم کنید.\n"
                f"مسیر توکن: {token_path}",
                file=sys.stderr,
            )
            return 1
        print(f"⚠️  حالت سرور: رابط در آدرس {host}:{port} قابل دسترسی خواهد بود.")
        print(f"   توکن احراز هویت: {token_path}")
        print(f"   برای اتصال: http://{host}:{port}/?token=YOUR_TOKEN")

    client = BridgeClient.start_in_process(settings)
    server = WebServer(settings, client, host=host, port=port)
    server.start_in_thread()
    server.wait_until_ready()
    print(f"web UI ready at {server.url}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
    return 0
