"""FastAPI app that exposes the Bridge over HTTP and a tiny WebSocket."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..bridge import BridgeClient
from ..core.config import AssistantSettings
from ..core.logging_setup import get_logger, setup_logging


logger = get_logger("web")


HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"


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


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(client: BridgeClient, settings: AssistantSettings) -> FastAPI:
    app = FastAPI(title="Local Windows Assistant", version="1.0")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((TEMPLATES / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {"bridge": client.info.to_dict() if client.info else None, "settings": client.get_status()}

    @app.get("/api/actions")
    async def actions() -> list[str]:
        return client.list_actions()

    @app.get("/api/history")
    async def history(limit: int = 50) -> list[dict[str, Any]]:
        return client.get_history(limit=limit)

    @app.post("/api/clear")
    async def clear() -> dict[str, bool]:
        client.clear_history()
        return {"cleared": True}

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> dict[str, Any]:
        # The HTTP /api/chat endpoint starts a run and returns its id.
        # Clients use the WebSocket to receive events.  We post a chat
        # request through the in-process backend by calling start_chat
        # through the BridgeClient's exposed handle.
        backend = getattr(client, "_backend", None)
        server = getattr(backend, "_server", None) if backend else None
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

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except ValueError:
                    await websocket.send_text(json.dumps({"type": "error", "message": "invalid json"}))
                    continue
                type_ = msg.get("type")
                if type_ == "chat":
                    message = str(msg.get("message", ""))
                    backend = getattr(client, "_backend", None)
                    server = getattr(backend, "_server", None) if backend else None
                    if server is None:
                        await websocket.send_text(json.dumps({"type": "error", "message": "no in-process bridge"}))
                        continue
                    run_id = server.handlers._start_chat_run(message)
                    queue = server.handlers.event_bus.create_run_queue(run_id)
                    try:
                        while True:
                            event = await asyncio.to_thread(queue.get, timeout=600)
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
                    backend = getattr(client, "_backend", None)
                    server = getattr(backend, "_server", None) if backend else None
                    if server is None:
                        await websocket.send_text(json.dumps({"type": "error", "message": "no bridge"}))
                        continue
                    ok = server.handlers.resolve_confirmation(request_id, approved)
                    await websocket.send_text(json.dumps({"type": "confirm_result", "ok": ok}))
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

    def __init__(self, settings: AssistantSettings, client: BridgeClient, *, host: str = "127.0.0.1", port: int = 7824) -> None:
        self.settings = settings
        self.client = client
        self.host = host
        self.port = port
        self._app = create_app(client, settings)
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def start_in_thread(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            self._app, host=self.host, port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="web-uvicorn")
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


def run_web(argv: list[str] | None = None) -> int:
    from ..core.config import load_settings

    settings = load_settings()
    setup_logging(settings.data_dir)
    client = BridgeClient.start_in_process(settings)
    port = int(__import__("os").environ.get("LOCAL_AGENT_WEB_PORT", "7824"))
    host = __import__("os").environ.get("LOCAL_AGENT_WEB_HOST", "127.0.0.1")
    server = WebServer(settings, client, host=host, port=port)
    server.start_in_thread()
    print(f"web UI ready at http://{host}:{port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
    return 0
