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
``GET  /api/artifact``        fetch a tool artifact (workspace or data dir)
``POST /api/provider/detect`` auto-detect provider from base URL + API key
``GET  /api/billing``         live credit / usage for the cloud provider
``POST /api/purge``           full app wipe (confirmed), then shuts down
``WS   /ws``                  chat + confirmation stream
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import os
import re
import sys
import threading
import time
from pathlib import Path
from queue import Empty
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError, StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..bridge import BridgeClient
from ..core.config import AssistantSettings
from ..core.errors import AssistantError
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
    work_dir: str | None = None
    full_system_access: bool | None = None
    # ``telegram`` / ``gmail`` accept a partial dict; only the given keys
    # are applied (blank secret fields keep their stored value).
    telegram: dict[str, Any] | None = None
    gmail: dict[str, Any] | None = None


class UploadRequest(BaseModel):
    name: str
    content_base64: str = ""


class DetectProviderRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""


class PurgeRequest(BaseModel):
    confirm: bool = False
    include_repo_caches: bool = True
    # Exit the process after a successful wipe so the running app cannot
    # recreate the data directory it just deleted.
    shutdown: bool = True


class TelegramVerifyRequest(BaseModel):
    code: str | None = None
    password: str | None = None
    account: str | None = None


class TelegramConnectRequest(BaseModel):
    account: str | None = None


class TelegramSwitchRequest(BaseModel):
    name: str


class ConfirmRequest(BaseModel):
    request_id: str
    approved: bool = False


def _schedule_process_exit(delay: float = 0.8) -> None:
    """Exit this process shortly after the HTTP response has been flushed.

    Purging deletes the very files the running app works with (config,
    history, logs, tokens); exiting straight after the reply guarantees a
    half-alive server never recreates the data directory.  Called in a
    daemon timer so ``POST /api/purge`` can respond first.
    """

    def _exit() -> None:
        os._exit(0)

    threading.Timer(delay, _exit).start()


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


def resolve_artifact_path(work_dir: Path, data_dir: Path, candidate: str) -> Path:
    """Resolve a tool artifact against the workspace *or* the data dir.

    Screenshots are saved under ``data_dir/screenshots`` while other tools
    write into ``work_dir``, so an artifact's ``path`` (as reported by the
    bridge) can live under either root.  Absolute paths inside either root
    are also accepted.  Anything else raises 403/404.

    Two real-world wrinkles are handled here:

    * **Windows-style separators** — artifacts produced on Windows use
      backslashes (``screenshots\\screen.png``); they must resolve on any
      host, so ``\\`` is normalised to ``/`` first (names produced by our
      own tools never contain a real backslash).
    * **cwd-shadowing** — when the process cwd *is* the work dir (the
      normal production layout), a bare relative candidate used to match
      the first (cwd-relative) target which sits inside the work-dir root
      but **does not exist**, hiding the real file in the data dir behind
      a bogus 404.  Candidates are now tried in order and the first one
      that is in scope **and** actually exists wins; when nothing exists
      the first in-scope candidate is returned so the caller can answer
      with an honest 404 instead of a misleading 403.
    """
    if not candidate:
        raise HTTPException(400, "empty path")
    raw = Path(candidate.replace("\\", "/"))
    roots = (work_dir.resolve(), data_dir.resolve())
    targets = [raw] + ([work_dir / raw, data_dir / raw] if not raw.is_absolute() else [])
    first_in_scope: Path | None = None
    for target in targets:
        try:
            resolved = target.resolve()
        except OSError as exc:  # pragma: no cover - depends on filesystem
            raise HTTPException(400, f"bad path: {exc}") from exc
        for root in roots:
            if resolved == root or root in resolved.parents:
                if first_in_scope is None:
                    first_in_scope = resolved
                if resolved.is_file():
                    return resolved
                break
    if first_in_scope is not None:
        return first_in_scope
    raise HTTPException(403, "path is outside the workspace / data directory")


def _server_of(client: BridgeClient) -> Any:
    backend = getattr(client, "_backend", None)
    return getattr(backend, "_server", None) if backend else None


def _coerce_telegram_field(key: str, raw: Any) -> Any:
    if key == "enabled" or key == "confirm_send":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"true", "1", "yes", "on"}
    if key == "api_id":
        try:
            return int(str(raw).strip())
        except ValueError as exc:
            raise HTTPException(400, "api_id باید عدد باشد") from exc
    if key == "session_name":
        value = str(raw).strip()
        return value or "assistant"
    return str(raw).strip()


def _coerce_gmail_field(key: str, raw: Any) -> Any:
    if key == "enabled" or key == "confirm_send":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"true", "1", "yes", "on"}
    return str(raw).strip()


# ---------------------------------------------------------------------------
# Global exception handling
# ---------------------------------------------------------------------------


def register_exception_handlers(app: FastAPI) -> None:
    """Convert every unhandled failure into clean JSON, never HTML.

    Without this, FastAPI answers unexpected exceptions with a bare
    ``500 Internal Server Error`` HTML page — the bug this whole task
    (P0) hunts.  The traceback is logged locally and the client only
    ever sees a short Persian message; no exception text, no paths, no
    secrets.
    """

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        messages: list[str] = []
        for error in exc.errors()[:5]:
            loc = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            message = str(error.get("msg", "مقدار نامعتبر"))
            messages.append(f"{loc}: {message}" if loc else message)
        return JSONResponse(
            status_code=422,
            content={"detail": "ورودی نامعتبر است — " + " | ".join(messages)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc.detail)})

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("endpoint crashed (unhandled exception)")
        return JSONResponse(
            status_code=500,
            content={"detail": "خطای داخلی سرور رخ داد؛ جزئیات در لاگ ثبت شد."},
        )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(client: BridgeClient, settings: AssistantSettings) -> FastAPI:
    app = FastAPI(title="Local Windows Assistant", version="2.0")
    register_exception_handlers(app)

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

    @app.post("/api/provider/detect")
    async def detect_provider_endpoint(req: DetectProviderRequest) -> dict[str, Any]:
        """Identify the gateway for a base URL + API key and validate it.

        Uses the persisted config as fallback when either field is empty.
        Returns the detected provider id/label, whether the key is valid,
        and the real model list from ``/models``.
        """
        from dataclasses import replace

        from ..llm.client import create_client
        from ..llm.providers import detect_provider

        server = _server_of(client)
        current = server.handlers.settings.llm if server is not None else settings.llm
        base_url = req.base_url.strip() or current.openai_base_url
        api_key = req.api_key.strip() or current.openai_api_key
        info = detect_provider(base_url, api_key)
        models: list[str] = []
        valid = False
        error: str | None = None
        if base_url and api_key:
            try:
                probe = replace(
                    current,
                    provider="openai_compatible",
                    openai_base_url=base_url,
                    openai_api_key=api_key,
                )
                models = create_client(probe).list_models()
                valid = True
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
        return {
            "provider": info.id,
            "label": info.label,
            "base_url": base_url or info.default_base_url,
            "valid": valid,
            "models": models,
            "error": error,
        }

    @app.get("/api/billing")
    async def billing() -> dict[str, Any]:
        """Live credit / usage summary for the active cloud provider."""
        from ..llm.providers import fetch_billing

        server = _server_of(client)
        llm = server.handlers.settings.llm if server is not None else settings.llm
        if not llm.openai_base_url or not llm.openai_api_key:
            return {
                "provider": llm.provider,
                "label": "",
                "available": False,
                "error": "درگاه مالی فقط برای ارائه‌دهندگان ابری با کلید API در دسترس است",
            }
        hint = llm.provider if llm.provider == "ollama" else ""
        try:
            return await asyncio.to_thread(
                fetch_billing, llm.openai_base_url, llm.openai_api_key, provider_hint=hint
            )
        except Exception:
            logger.exception("billing fetch failed")
            return {
                "provider": llm.provider,
                "label": "",
                "available": False,
                "error": "دریافت اطلاعات مالی ناموفق بود؛ دوباره تلاش کنید یا لاگ را بررسی کنید.",
            }

    @app.post("/api/purge")
    async def purge(req: PurgeRequest) -> dict[str, Any]:
        """Full wipe of the assistant's on-disk footprint (see core/cleanup).

        Requires explicit ``confirm: true``.  Installed packages, venvs and
        the pip cache are never touched.  On success the process exits a
        moment later (``shutdown``), so the UI shows its "fully wiped" state.
        """
        if not req.confirm:
            raise HTTPException(400, "پاک‌سازی کامل نیازمند تأیید صریح است (confirm)")
        from ..core.cleanup import purge_all

        server = _server_of(client)
        active = server.handlers.settings if server is not None else settings
        result = await asyncio.to_thread(
            purge_all,
            active,
            include_repo_caches=req.include_repo_caches,
            # This very process holds the log files inside data_dir; release
            # them first so Windows can delete the files we still own.
            close_logging=True,
        )
        if req.shutdown and result.get("ok"):
            result["shutdown_scheduled"] = True
            _schedule_process_exit()
        return result

    @app.get("/api/history")
    async def history(limit: int = 50) -> list[dict[str, Any]]:
        return client.get_history(limit=limit)

    @app.post("/api/clear")
    async def clear() -> dict[str, bool]:
        client.clear_history()
        return {"cleared": True}

    @app.post("/api/settings")
    async def update_settings(req: SettingsRequest) -> dict[str, Any]:
        """Apply + persist every setting the UI can edit.

        Everything goes through ``BridgeHandlers._apply_settings`` so the
        runtime, the tool context and ``config.json`` stay in sync, and
        the write is atomic.  Secret values are never echoed back.
        """
        server = _server_of(client)
        if server is None:
            payload: dict[str, Any] = {}
            if req.provider:
                payload["provider"] = req.provider
            if req.model:
                payload["model"] = req.model
            if not payload:
                return {"provider": "", "model": ""}
            try:
                return client.set_model(**payload)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, str(exc))

        handlers = server.handlers
        new_settings = handlers.settings

        # ---- LLM ------------------------------------------------------
        llm_dict = dict(new_settings.llm.__dict__)
        llm_changed = False
        if req.provider:
            llm_dict["provider"] = req.provider
            llm_changed = True
        if req.model:
            llm_dict["ollama_model"] = req.model
            llm_dict["openai_model"] = req.model
            llm_changed = True
        if req.openai_base_url is not None:
            llm_dict["openai_base_url"] = req.openai_base_url.strip()
            llm_changed = True
        if req.openai_api_key is not None and req.openai_api_key.strip():
            llm_dict["openai_api_key"] = req.openai_api_key.strip()
            llm_changed = True
        if llm_changed:
            new_settings = new_settings.with_overrides(
                llm=type(new_settings.llm)(**llm_dict)
            )

        # ---- safety ---------------------------------------------------
        safety_dict = dict(new_settings.safety.__dict__)
        safety_changed = False
        if req.confirm_mode in {"destructive", "always", "never"}:
            safety_dict["confirm_mode"] = req.confirm_mode
            safety_changed = True
        if req.full_system_access is not None:
            safety_dict["full_system_access"] = bool(req.full_system_access)
            safety_changed = True
        if safety_changed:
            new_settings = new_settings.with_overrides(
                safety=type(new_settings.safety)(**safety_dict)
            )

        # ---- work dir (created on demand) ------------------------------
        if req.work_dir is not None and req.work_dir.strip():
            target = Path(req.work_dir).expanduser()
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(400, f"ساخت پوشهٔ کاری ممکن نشد: {exc}")
            new_settings = new_settings.with_overrides(work_dir=target)

        # ---- telegram --------------------------------------------------
        if req.telegram:
            tg_changes: dict[str, Any] = {}
            if "accounts" in req.telegram:
                tg_changes["accounts"] = req.telegram["accounts"]
            for key in ("enabled", "active_account"):
                if key in req.telegram:
                    tg_changes[key] = _coerce_telegram_field(key, req.telegram[key])
            # Legacy single-account fields apply to the active account.
            for key in ("api_id", "api_hash", "phone", "session_name", "confirm_send"):
                if key not in req.telegram:
                    continue
                raw = req.telegram[key]
                # Blank scalar = keep the stored value (the UI never echoes
                # secrets back, so an empty hash is not a change).
                if key != "confirm_send" and (not raw or not str(raw).strip()):
                    continue
                tg_changes[key] = _coerce_telegram_field(key, raw)
            if tg_changes:
                new_settings = new_settings.with_overrides(
                    telegram=new_settings.telegram.updated(tg_changes)
                )

        # ---- gmail -----------------------------------------------------
        if req.gmail:
            gm_dict = dict(new_settings.gmail.__dict__)
            gm_changed = False
            for key in ("enabled", "credentials_file", "token_file", "username", "app_password", "confirm_send"):
                if key in req.gmail:
                    raw = req.gmail[key]
                    if key == "app_password" and (not raw or not str(raw).strip()):
                        continue  # blank = keep the stored password
                    gm_dict[key] = _coerce_gmail_field(key, raw)
                    gm_changed = True
            if gm_changed:
                new_settings = new_settings.with_overrides(
                    gmail=type(new_settings.gmail)(**gm_dict)
                )

        handlers._apply_settings(new_settings)
        llm = new_settings.llm
        model = llm.openai_model if llm.provider != "ollama" else llm.ollama_model
        return {
            "provider": llm.provider,
            "model": model or "",
            "saved": {
                "work_dir": str(new_settings.work_dir),
                "confirm_mode": new_settings.safety.confirm_mode,
                "full_system_access": bool(new_settings.safety.full_system_access),
                "telegram_enabled": bool(new_settings.telegram.enabled),
                "gmail_enabled": bool(new_settings.gmail.enabled),
                # API key is only acknowledged, never returned.
                "openai_api_key_set": bool(llm.openai_api_key),
            },
        }

    @app.post("/api/telegram/connect")
    async def telegram_connect(req: TelegramConnectRequest | None = None) -> dict[str, Any]:
        """Start the personal-Telegram login flow (state: await_code → await_2fa → connected).

        ``account`` selects which account to connect (default: the active one).
        """
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "telegram needs an in-process bridge")
        account = req.account if req is not None else None
        try:
            return server.handlers.start_telegram_login(account)
        except AssistantError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/telegram/verify")
    async def telegram_verify(req: TelegramVerifyRequest) -> dict[str, Any]:
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "telegram needs an in-process bridge")
        state = server.handlers.telegram_status(req.account)["state"]
        try:
            if state == "await_code":
                if not req.code:
                    raise HTTPException(400, "کد تأیید را وارد کنید")
                return server.handlers.submit_telegram_code(req.code, req.account)
            if state == "await_2fa":
                if not req.password:
                    raise HTTPException(400, "رمز دوم‌مرحله‌ای (2FA) را وارد کنید")
                return server.handlers.submit_telegram_password(req.password, req.account)
            raise HTTPException(400, "هیچ فرایند ورودی در جریان نیست؛ دکمهٔ اتصال تلگرام را بزنید")
        except AssistantError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/telegram/disconnect")
    async def telegram_disconnect(req: TelegramConnectRequest | None = None) -> dict[str, Any]:
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "telegram needs an in-process bridge")
        account = req.account if req is not None else None
        return server.handlers.disconnect_telegram(account)

    @app.post("/api/telegram/switch")
    async def telegram_switch(req: TelegramSwitchRequest) -> dict[str, Any]:
        """Set the active Telegram account."""
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "telegram needs an in-process bridge")
        try:
            return server.handlers.switch_telegram_account(str(req.name))
        except AssistantError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/telegram/accounts")
    async def telegram_accounts() -> dict[str, Any]:
        """Status of every Telegram account (no secrets)."""
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "telegram needs an in-process bridge")
        return server.handlers.telegram_accounts_status()

    @app.post("/api/gmail/connect")
    async def gmail_connect() -> dict[str, Any]:
        """Connect Gmail (OAuth browser flow or IMAP/SMTP App Password)."""
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "gmail needs an in-process bridge")
        try:
            return server.handlers.connect_gmail()
        except AssistantError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/gmail/disconnect")
    async def gmail_disconnect() -> dict[str, Any]:
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "gmail needs an in-process bridge")
        return server.handlers.disconnect_gmail()

    @app.post("/api/elevate/restart")
    async def elevate_restart() -> dict[str, Any]:
        """Relaunch the assistant with administrator rights (best-effort).

        Windows: ``ShellExecuteW(..., "runas", ...)`` re-spawns the app
        elevated (UAC prompt).  On POSIX we cannot elevate a running
        process, so we return guidance to restart with sudo.
        """
        from ..utils.platform import Platform, current_platform

        if current_platform() != Platform.WINDOWS:
            return {
                "elevated": False,
                "message": "در لینوکس/مک، برنامه را با sudo دوباره اجرا کنید: "
                "sudo python -m local_agent.web",
            }
        try:
            import ctypes

            argv = list(sys.argv[1:]) if not getattr(sys, "frozen", False) else []
            params = " ".join(argv) if argv else ""
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            if int(result) <= 32:
                return {
                    "elevated": False,
                    "message": "اجرای دوباره به‌عنوان administrator ممکن نشد (شاید تأیید UAC لغو شد).",
                }
            return {
                "elevated": True,
                "message": "برنامه با سطح administrator دوباره اجرا می‌شود؛ این پنجره را ببندید.",
            }
        except Exception as exc:
            logger.exception("elevate/restart failed")
            return {"elevated": False, "message": f"اجرای دوباره ممکن نشد: {exc}"}

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

    @app.get("/api/artifact")
    async def get_artifact(path: str) -> FileResponse:
        """Serve a tool artifact (screenshot, file) from the workspace or data dir."""
        target = resolve_artifact_path(settings.work_dir, settings.data_dir, path)
        if not target.is_file():
            raise HTTPException(404, "file not found")
        media_type, _ = mimetypes.guess_type(target.name)
        return FileResponse(str(target), media_type=media_type or "application/octet-stream")

    @app.post("/api/confirm")
    async def confirm(req: ConfirmRequest) -> dict[str, Any]:
        """Resolve a pending tool-confirmation over plain HTTP.

        This is the fallback path used by the UI when the WebSocket is
        closed or half-alive mid-run (e.g. right after a reconnect): it
        routes to the very same ``resolve_confirmation`` as the ``confirm``
        WebSocket message, so an approval typed in the browser is never
        lost to a socket that stopped draining.
        """
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "confirmation needs an in-process bridge")
        ok = server.handlers.resolve_confirmation(str(req.request_id), bool(req.approved))
        return {"ok": ok}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        """WebSocket chat + confirmation stream.

        The old implementation consumed the chat event queue in a blocking
        inner loop and never called ``receive_text()`` while a run was in
        flight, so ``confirm``/``interrupt``/``ping`` messages from the UI
        piled up in the socket buffer until the run finished — an approval
        clicked mid-run was silently ignored and the action eventually
        timed out as "refused".

        The rewrite runs a dedicated **reader task** that drains
        ``receive_text()`` constantly, plus one per-run **forwarder** task
        that copies bridge events into the same ``asyncio.Queue``, so the
        main loop genuinely multiplexes incoming control messages with
        streamed run events.  Client disconnects exit cleanly (debug-level
        log only) instead of crashing the handler.
        """
        await websocket.accept()
        server = _server_of(client)
        stop = asyncio.Event()
        incoming: asyncio.Queue = asyncio.Queue()
        # run_id -> threading.Queue (the bridge event-bus queue)
        run_queues: dict[str, Any] = {}
        forwarders: dict[str, asyncio.Task] = {}

        async def _send(payload: dict[str, Any]) -> bool:
            """Send one frame; return False when the socket is gone.

            A closed socket surfaces either as a ``WebSocketDisconnect`` or
            as a ``RuntimeError`` ("Unexpected ASGI message ... after sending
            'websocket.close'") depending on timing — both must be treated as
            a clean teardown, never a crash.
            """
            try:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))
                return True
            except Exception:  # noqa: BLE001 - socket gone: clean teardown
                return False

        async def reader() -> None:
            """Continuously drain the socket into the shared asyncio queue."""
            while not stop.is_set():
                try:
                    data = await websocket.receive_text()
                except Exception:  # noqa: BLE001 - disconnect or half-alive socket
                    await incoming.put(None)  # sentinel: this connection is done
                    return
                try:
                    msg = json.loads(data)
                except ValueError:
                    await incoming.put({"type": "__invalid"})
                    continue
                await incoming.put(msg)

        async def forwarder(run_id: str, tq: Any) -> None:
            """Copy bridge events for one run into the shared asyncio queue."""
            while not stop.is_set():
                try:
                    event = await asyncio.to_thread(tq.get, timeout=0.5)
                except Empty:
                    continue
                await incoming.put({"__run": run_id, "event": event})

        reader_task = asyncio.create_task(reader())
        try:
            while True:
                item = await incoming.get()
                if item is None:
                    break  # client disconnected / socket died
                if "__run" in item:
                    run_id, event = item["__run"], item["event"]
                    if event is None:
                        if run_id in forwarders:
                            forwarders.pop(run_id).cancel()
                        if server is not None:
                            server.handlers.event_bus.destroy_run_queue(run_id)
                        continue
                    ok = await _send({
                        "type": "event",
                        "event_type": event.type,
                        "payload": event.payload,
                        "run_id": event.run_id,
                        "seq": event.seq,
                    })
                    if not ok:
                        break
                    if event.type in {"chat_done", "chat_failed"}:
                        if run_id in forwarders:
                            forwarders.pop(run_id).cancel()
                        if server is not None:
                            server.handlers.event_bus.destroy_run_queue(run_id)
                    continue

                type_ = item.get("type")
                if type_ == "__invalid":
                    await _send({"type": "error", "message": "invalid json"})
                    continue
                if type_ == "chat":
                    message = str(item.get("message", ""))
                    if server is None:
                        await _send({"type": "error", "message": "no in-process bridge"})
                        continue
                    run_id = server.handlers._start_chat_run(message)
                    tq = server.handlers.event_bus.create_run_queue(run_id)
                    run_queues[run_id] = tq
                    forwarders[run_id] = asyncio.create_task(forwarder(run_id, tq))
                    continue
                if type_ == "confirm":
                    request_id = str(item.get("request_id", ""))
                    approved = bool(item.get("approved", False))
                    ok = False
                    if server is not None:
                        ok = server.handlers.resolve_confirmation(request_id, approved)
                    await _send({"type": "confirm_result", "ok": ok})
                    continue
                if type_ == "interrupt":
                    if server is not None:
                        server.handlers._interrupt_run(str(item.get("run_id", "")))
                    await _send({"type": "interrupted", "ok": True})
                    continue
                if type_ == "ping":
                    await _send({"type": "pong", "ts": time.time()})
                    continue
        finally:
            stop.set()
            reader_task.cancel()
            for task in forwarders.values():
                task.cancel()
            for run_id in list(run_queues):
                if server is not None:
                    server.handlers.event_bus.destroy_run_queue(run_id)
            logger.debug("websocket handler exited cleanly")

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
    from ..utils.encoding import ensure_utf8_stdio
    from ..utils.platform import log_platform_summary

    ensure_utf8_stdio()

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
