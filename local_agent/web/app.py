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
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any, NoReturn
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError, StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..bridge import BridgeClient
from ..core.config import AssistantSettings
from ..core.errors import AssistantError
from ..core.logging_setup import get_logger, setup_logging
from ..telegram.client import TelegramError
from ..utils.paths import web_static_dir, web_templates_dir

logger = get_logger("web")


TEMPLATES = web_templates_dir()
STATIC = web_static_dir()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_GITHUB_RELEASE_ASSET_BYTES = 256 * 1024 * 1024
MAX_GITHUB_JSON_BODY_BYTES = 2 * 1024 * 1024
MAX_GITHUB_SENSITIVE_BODY_BYTES = 96 * 1024
_GITHUB_SENSITIVE_OPERATIONS = frozenset(
    {
        "actions_secret_set",
        "organization_actions_secret_set",
        "environment_actions_secret_set",
        "codespace_secret_set",
        "webhook_create",
        "webhook_update",
    }
)


class _GitHubJSONBodyLimitMiddleware:
    """Bound every ordinary GitHub POST body before FastAPI buffers JSON.

    ``Content-Length`` is only an early-rejection hint: a client can omit it,
    use chunked transfer encoding, or send more bytes than declared. Wrapping
    ASGI ``receive`` keeps the limit effective in all of those cases.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        protected = (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and path.startswith("/api/github/")
            and path not in {"/api/github/release-asset", "/api/github/sensitive"}
        )
        if not protected:
            await self.app(scope, receive, send)
            return

        length_values = [
            value
            for name, value in scope.get("headers", ())
            if name.lower() == b"content-length"
        ]
        if len(length_values) > 1:
            await self._reject(
                scope,
                receive,
                send,
                "Content-Length تکراری یا مبهم است",
                status_code=400,
            )
            return
        declared: int | None = None
        if length_values:
            raw_length = length_values[0]
            if not isinstance(raw_length, bytes) or not re.fullmatch(rb"[0-9]+", raw_length):
                await self._reject(
                    scope,
                    receive,
                    send,
                    "Content-Length نامعتبر است",
                    status_code=400,
                )
                return
            try:
                declared = int(raw_length)
            except (TypeError, ValueError):
                await self._reject(
                    scope,
                    receive,
                    send,
                    "Content-Length نامعتبر است",
                    status_code=400,
                )
                return
            if declared > self.max_bytes:
                await self._reject(scope, receive, send, "بدنهٔ درخواست GitHub بیش از ۲ مگابایت است")
                return

        consumed = 0
        exceeded = False
        invalid_length = False

        async def bounded_receive() -> dict[str, Any]:
            nonlocal consumed, exceeded, invalid_length
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    exceeded = True
                    raise _GitHubBodyTooLarge
                if (
                    not message.get("more_body", False)
                    and declared is not None
                    and consumed != declared
                ):
                    invalid_length = True
                    raise _GitHubBodyLengthMismatch
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except _GitHubBodyTooLarge:
            if not exceeded:  # pragma: no cover - defensive invariant
                raise
            await self._reject(
                scope,
                receive,
                send,
                "بدنهٔ درخواست GitHub بیش از ۲ مگابایت است",
            )
        except _GitHubBodyLengthMismatch:
            if not invalid_length:  # pragma: no cover - defensive invariant
                raise
            await self._reject(
                scope,
                receive,
                send,
                "Content-Length با اندازهٔ واقعی بدنه یکسان نیست",
                status_code=400,
            )

    @staticmethod
    async def _reject(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        detail: str,
        *,
        status_code: int = 413,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail},
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)


class _GitHubBodyTooLarge(Exception):
    """Internal control flow used by the bounded ASGI receive wrapper."""


class _GitHubBodyLengthMismatch(Exception):
    """Declared body length did not match the complete ASGI request stream."""


def _github_declared_content_length(request: Request, *, subject: str, maximum: int) -> int | None:
    """Parse one canonical Content-Length value from the raw ASGI headers."""
    values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if len(values) > 1:
        raise HTTPException(400, f"Content-Length {subject} تکراری یا مبهم است")
    if not values:
        return None
    raw = values[0]
    if not isinstance(raw, bytes) or not re.fullmatch(rb"[0-9]+", raw):
        raise HTTPException(400, f"Content-Length {subject} نامعتبر است")
    try:
        declared = int(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Content-Length {subject} نامعتبر است") from exc
    if declared > maximum:
        raise HTTPException(413, f"اندازهٔ اعلام‌شدهٔ {subject} بیش از سقف امن است")
    return declared


def _clear_bytearray(value: bytearray, *, overwrite: bool = False) -> None:
    """Release a temporary buffer, optionally overwriting small sensitive data first."""
    if overwrite:
        value[:] = b"\x00" * len(value)
    value.clear()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    auto_confirm: bool = False
    session_id: str | None = None


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
    github: dict[str, Any] | None = None


class GitHubOperationRequest(BaseModel):
    operation: str
    params: dict[str, Any] = {}
    confirm: bool = False


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


class TelegramAccountToggleRequest(BaseModel):
    name: str
    enabled: bool = False


class TelegramResolveRequest(BaseModel):
    target: str
    account: str | None = None


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


def _connected_telegram_client(client: BridgeClient, account: str | None = None) -> tuple[str, Any]:
    server = _server_of(client)
    if server is None:
        raise HTTPException(503, "تلگرام به Bridge درون‌پردازه نیاز دارد")
    try:
        name, telegram = server.handlers._account_client(account)
    except AssistantError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not telegram.is_connected:
        raise HTTPException(409, f"اکانت تلگرام «{name}» متصل نیست")
    return name, telegram


def _raise_telegram_http_error(
    exc: AssistantError, *, legacy_bad_request: bool = False
) -> NoReturn:
    if isinstance(exc, TelegramError):
        statuses = {
            "not_connected": 409,
            "flood_wait": 429,
            "timeout": 504,
            "network": 503,
            "session_revoked": 401,
            "authorization_required": 403,
            "account_restricted": 403,
            "privacy_restricted": 403,
            "admin_required": 403,
            "write_forbidden": 403,
            "peer_invalid": 404,
            "message_invalid": 404,
            "target_ambiguous": 409,
            "media_invalid": 409,
            "local_file_missing": 404,
        }
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        status = 400 if legacy_bad_request else statuses.get(exc.code, 400)
        raise HTTPException(status, detail=exc.to_dict(), headers=headers) from exc
    raise HTTPException(400, str(exc)) from exc


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


def _github_service(client: BridgeClient) -> Any:
    server = _server_of(client)
    service = server.handlers.context.extra.get("github") if server is not None else None
    if service is None:
        raise HTTPException(503, "GitHub به Bridge درون‌پردازه نیاز دارد")
    return service


def _canonical_origin(value: str) -> str:
    """Return a comparable HTTP(S) origin, or an empty string if malformed."""
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return ""
        scheme = parsed.scheme.lower()
        port = parsed.port
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if port and port != (443 if scheme == "https" else 80):
            host = f"{host}:{port}"
        return f"{scheme}://{host}"
    except (TypeError, ValueError):
        return ""


def _external_origin(request: Request) -> str:
    # Trust only the ASGI scope. Uvicorn applies Forwarded/X-Forwarded-* only
    # when the direct peer is in its trusted-proxy allow-list. Reading those
    # headers here would let an arbitrary client spoof HTTPS and weaken Secure
    # cookie and OAuth callback decisions.
    host = request.headers.get("host", request.url.netloc).split(",")[0].strip()
    return _canonical_origin(f"{request.url.scheme}://{host}") or _canonical_origin(
        str(request.base_url)
    )


def _websocket_external_origin(websocket: WebSocket) -> str:
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    host = websocket.headers.get("host", websocket.url.netloc).split(",")[0].strip()
    return _canonical_origin(f"{scheme}://{host}")


def _is_loopback_bind(host: str) -> bool:
    value = host.strip().removeprefix("[").removesuffix("]")
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _valid_web_access_token(token: Any) -> bool:
    return (
        isinstance(token, str)
        and 32 <= len(token) <= 512
        and token == token.strip()
        and not any(ord(character) < 0x21 or ord(character) == 0x7F for character in token)
    )


def _signed_session(raw: str, secret: bytes) -> str:
    signature = hmac.new(secret, raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{signature}"


def _verified_session(value: str, secret: bytes) -> str:
    try:
        raw, signature = value.rsplit(".", 1)
    except ValueError:
        return ""
    expected = hmac.new(secret, raw.encode("ascii"), hashlib.sha256).hexdigest()
    return raw if hmac.compare_digest(signature, expected) else ""


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
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
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
    async def _http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = (
            exc.detail
            if isinstance(exc.detail, (dict, list, str, int, float, bool))
            else str(exc.detail)
        )
        if isinstance(detail, dict) and "message" in detail:
            content = {"detail": str(detail["message"]), "error": detail}
        else:
            content = {"detail": detail}
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

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


def create_app(
    client: BridgeClient,
    settings: AssistantSettings,
    *,
    remote_access_token: str = "",
) -> FastAPI:
    if remote_access_token and not _valid_web_access_token(remote_access_token):
        raise AssistantError("توکن دسترسی Web باید ۳۲ تا ۵۱۲ نویسهٔ غیرکنترلی داشته باشد")
    app = FastAPI(title="Local Windows Assistant", version="2.0")
    register_exception_handlers(app)
    app.add_middleware(
        _GitHubJSONBodyLimitMiddleware,
        max_bytes=MAX_GITHUB_JSON_BODY_BYTES,
    )
    github_web_secret = secrets.token_bytes(32)
    remote_auth_enabled = bool(remote_access_token)
    remote_cookie_value = secrets.token_urlsafe(32)
    failed_auth: dict[str, tuple[float, int]] = {}
    failed_auth_lock = threading.Lock()

    def remote_auth_rate_limited(request: Request) -> bool:
        peer = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with failed_auth_lock:
            stale = [key for key, (started, _) in failed_auth.items() if now - started >= 60]
            for key in stale:
                failed_auth.pop(key, None)
            started, count = failed_auth.get(peer, (now, 0))
            if len(failed_auth) >= 10_000 and peer not in failed_auth:
                return True
            count += 1
            failed_auth[peer] = (started, count)
            return count > 10

    @app.middleware("http")
    async def browser_security(request: Request, call_next):
        if remote_auth_enabled and request.url.path != "/healthz":
            # The bearer credential must never cross a plaintext network.
            # Uvicorn changes the ASGI scheme for explicitly trusted reverse
            # proxies, so a properly terminated HTTPS deployment still works.
            if request.url.scheme != "https":
                return JSONResponse(
                    status_code=426,
                    content={"detail": "دسترسی راه‌دور Web فقط پشت HTTPS مجاز است"},
                    headers={"Upgrade": "TLS/1.2", "Cache-Control": "no-store"},
                )
            cookie = request.cookies.get("pla_remote_auth", "")
            cookie_ok = bool(cookie) and hmac.compare_digest(cookie, remote_cookie_value)
            authorization = request.headers.get("authorization", "")
            prefix = "Bearer "
            supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
            bearer_ok = (
                _valid_web_access_token(supplied)
                and len(supplied) == len(remote_access_token)
                and hmac.compare_digest(supplied, remote_access_token)
            )
            request.state.remote_authenticated = cookie_ok or bearer_ok
            login_page = request.method == "GET" and request.url.path == "/"
            if not request.state.remote_authenticated and not login_page:
                limited = bool(supplied) and remote_auth_rate_limited(request)
                return JSONResponse(
                    status_code=429 if limited else 401,
                    content={"detail": "احراز هویت Web نامعتبر است"},
                    headers={
                        "Cache-Control": "no-store",
                        **({"Retry-After": "60"} if limited else {}),
                    },
                )
        else:
            request.state.remote_authenticated = not remote_auth_enabled

        signed = request.cookies.get("pla_browser_session", "")
        browser_session = _verified_session(signed, github_web_secret)
        fresh = not browser_session
        if fresh:
            browser_session = secrets.token_urlsafe(32)
        request.state.github_browser_session = browser_session
        response = await call_next(request)
        if fresh:
            response.set_cookie(
                "pla_browser_session",
                _signed_session(browser_session, github_web_secret),
                httponly=True,
                secure=_external_origin(request).startswith("https://"),
                samesite="lax",
                path="/",
                max_age=12 * 60 * 60,
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def github_guard(request: Request, *, csrf: bool = True) -> str:
        origin = _canonical_origin(request.headers.get("origin", ""))
        active = _server_of(client)
        github_settings = active.handlers.settings.github if active is not None else settings.github
        allowed = {
            value
            for value in (
                _external_origin(request),
                *(_canonical_origin(item) for item in github_settings.allowed_origins),
            )
            if value
        }
        if not origin or origin not in allowed:
            raise HTTPException(403, "Origin درخواست GitHub معتبر نیست")
        session = str(getattr(request.state, "github_browser_session", ""))
        if not session:
            raise HTTPException(403, "نشست مرورگر معتبر نیست")
        if csrf:
            expected = hmac.new(
                github_web_secret, session.encode("ascii"), hashlib.sha256
            ).hexdigest()
            supplied = request.headers.get("x-csrf-token", "")
            if not supplied or not hmac.compare_digest(supplied, expected):
                raise HTTPException(403, "توکن CSRF معتبر نیست")
        return session

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        if remote_auth_enabled and not request.state.remote_authenticated:
            nonce = secrets.token_urlsafe(18)
            page = f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ورود امن دستیار محلی</title><style>body{{font-family:Tahoma,sans-serif;background:#0d1117;color:#e6edf3;display:grid;place-items:center;min-height:100vh;margin:0}}main{{width:min(28rem,calc(100% - 3rem));background:#161b22;border:1px solid #30363d;border-radius:14px;padding:2rem}}input,button{{box-sizing:border-box;width:100%;padding:.8rem;margin-top:.8rem;border-radius:8px}}input{{background:#0d1117;color:#fff;border:1px solid #484f58;direction:ltr}}button{{background:#238636;color:#fff;border:0;cursor:pointer}}p{{line-height:1.8}}#error{{color:#ff7b72}}</style></head>
<body><main><h1>ورود امن</h1><p>توکن Bridge را وارد کنید. توکن در نشانی یا حافظهٔ مرورگر ذخیره نمی‌شود.</p><form id="login"><input id="token" type="password" autocomplete="off" minlength="32" maxlength="512" required aria-label="توکن Bridge"><button type="submit">ورود</button></form><p id="error" role="alert"></p></main>
<script nonce="{nonce}">document.getElementById('login').addEventListener('submit',async(e)=>{{e.preventDefault();const t=document.getElementById('token');const m=document.getElementById('error');m.textContent='';try{{const r=await fetch('/api/auth/bootstrap',{{method:'POST',headers:{{Authorization:'Bearer '+t.value}}}});t.value='';if(!r.ok)throw new Error(r.status===429?'تلاش‌های ناموفق بیش از حد است؛ کمی صبر کنید.':'توکن معتبر نیست.');location.replace('/');}}catch(x){{m.textContent=x.message||'ورود انجام نشد.';}}}});</script></body></html>"""
            return HTMLResponse(
                page,
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": (
                        "default-src 'none'; "
                        f"script-src 'nonce-{nonce}'; "
                        "style-src 'unsafe-inline'; connect-src 'self'; "
                        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                    ),
                },
            )
        return HTMLResponse((TEMPLATES / "index.html").read_text(encoding="utf-8"))

    @app.post("/api/auth/bootstrap")
    async def remote_auth_bootstrap(request: Request) -> Response:
        if not remote_auth_enabled:
            raise HTTPException(404, "احراز هویت راه‌دور فعال نیست")
        # Middleware permits this route only after exact bearer validation.
        if not request.state.remote_authenticated:
            raise HTTPException(401, "احراز هویت Web نامعتبر است")
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            "pla_remote_auth",
            remote_cookie_value,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
            max_age=12 * 60 * 60,
        )
        return response

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
    async def purge(req: PurgeRequest, request: Request) -> dict[str, Any]:
        """Full wipe of the assistant's on-disk footprint (see core/cleanup).

        Requires the signed browser session, an exact allowed Origin, CSRF,
        and explicit ``confirm: true``. Installed packages, venvs and the pip
        cache are never touched. On success the process exits a moment later.
        """
        github_guard(request, csrf=True)
        if not req.confirm:
            raise HTTPException(400, "پاک‌سازی کامل نیازمند تأیید صریح است (confirm)")
        from ..core.cleanup import purge_all

        server = _server_of(client)
        active = server.handlers.settings if server is not None else settings
        revocation_warning: str | None = None
        if server is not None:
            github = server.handlers.context.extra.get("github")
            if github is not None and github.vault.available:
                try:
                    # Service.disconnect deletes the local vault item in a
                    # finally block even if remote revocation fails.
                    await asyncio.to_thread(github.disconnect)
                except AssistantError:
                    revocation_warning = (
                        "لغو دسترسی GitHub از راه دور ناموفق بود؛ اعتبار محلی حذف شد. "
                        "در صورت نیاز دسترسی برنامه را در تنظیمات GitHub لغو کنید."
                    )
                    logger.warning("could not revoke GitHub credential during purge")
                    try:
                        github.vault.delete()
                    except AssistantError:
                        logger.warning("could not remove GitHub credential during purge")
        result = await asyncio.to_thread(
            purge_all,
            active,
            include_repo_caches=req.include_repo_caches,
            # This very process holds the log files inside data_dir; release
            # them first so Windows can delete the files we still own.
            close_logging=True,
        )
        if revocation_warning:
            result["revocation_warning"] = revocation_warning
        if req.shutdown and result.get("ok"):
            result["shutdown_scheduled"] = True
            _schedule_process_exit()
        return result

    @app.get("/api/history")
    async def history(limit: int = 50, session_id: str | None = None) -> list[dict[str, Any]]:
        return client.get_history(limit=limit, session_id=session_id)

    @app.post("/api/clear")
    async def clear(session_id: str | None = None) -> dict[str, bool]:
        client.clear_history(session_id=session_id)
        return {"cleared": True}

    @app.post("/api/settings")
    async def update_settings(req: SettingsRequest, request: Request) -> dict[str, Any]:
        """Apply + persist every setting the UI can edit.

        Everything goes through ``BridgeHandlers._apply_settings`` so the
        runtime, the tool context and ``config.json`` stay in sync, and
        the write is atomic.  Secret values are never echoed back.
        """
        if req.github is not None:
            github_guard(request)
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
            new_settings = new_settings.with_overrides(llm=type(new_settings.llm)(**llm_dict))

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

        # ---- github ----------------------------------------------------
        if req.github is not None:
            gh_dict = dict(new_settings.github.__dict__)
            allowed_keys = {
                "enabled",
                "client_id",
                "broker_url",
                "callback_url",
                "api_url",
                "web_url",
                "graphql_url",
                "selected_repositories",
                "local_clone_root",
                "allowed_origins",
            }
            unknown = set(req.github) - allowed_keys
            if unknown:
                raise HTTPException(400, f"تنظیم GitHub ناشناخته است: {', '.join(sorted(unknown))}")
            for key, raw in req.github.items():
                if key == "enabled":
                    if not isinstance(raw, bool):
                        raise HTTPException(400, "github.enabled باید boolean باشد")
                    gh_dict[key] = raw
                elif key in {"selected_repositories", "allowed_origins"}:
                    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                        raise HTTPException(400, f"{key} باید فهرست رشته‌ها باشد")
                    gh_dict[key] = tuple(item.strip() for item in raw if item.strip())
                else:
                    if not isinstance(raw, str):
                        raise HTTPException(400, f"github.{key} باید رشته باشد")
                    gh_dict[key] = raw.strip()
            try:
                github_settings = type(new_settings.github)(**gh_dict)
                github_settings.validate()
            except AssistantError as exc:
                raise HTTPException(400, str(exc)) from exc
            new_settings = new_settings.with_overrides(github=github_settings)

        # ---- gmail -----------------------------------------------------
        if req.gmail:
            gm_dict = dict(new_settings.gmail.__dict__)
            gm_changed = False
            for key in (
                "enabled",
                "credentials_file",
                "token_file",
                "username",
                "app_password",
                "confirm_send",
            ):
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

        try:
            handlers._apply_settings(new_settings)
        except AssistantError as exc:
            raise HTTPException(400, str(exc)) from exc
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
                "github_enabled": bool(new_settings.github.enabled),
                # API key is only acknowledged, never returned.
                "openai_api_key_set": bool(llm.openai_api_key),
            },
        }

    # GitHub routes are intentionally scoped behind signed browser sessions,
    # strict Origin checks and CSRF. No OAuth token is ever returned here.
    @app.post("/api/github/security")
    async def github_security(request: Request) -> dict[str, Any]:
        session = github_guard(request, csrf=False)
        csrf_token = hmac.new(
            github_web_secret, session.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return {"csrf_token": csrf_token, "expires_in": 12 * 60 * 60}

    @app.post("/api/github/status")
    async def github_status(request: Request, verify: bool = True) -> dict[str, Any]:
        github_guard(request, csrf=False)
        service = _github_service(client)
        result = await asyncio.to_thread(service.status, verify=verify)
        active = _server_of(client)
        gh = active.handlers.settings.github if active is not None else settings.github
        result["configuration"] = {
            "enabled": gh.enabled,
            "client_id": gh.client_id,
            "broker_url": gh.broker_url,
            "callback_url": gh.callback_url,
            "api_url": gh.api_url,
            "web_url": gh.web_url,
            "graphql_url": gh.graphql_url,
            "selected_repositories": list(gh.selected_repositories),
            "local_clone_root": gh.local_clone_root,
            "allowed_origins": list(gh.allowed_origins),
        }
        return result

    @app.post("/api/github/oauth/start")
    async def github_oauth_start(request: Request) -> dict[str, str]:
        browser_session = github_guard(request)
        service = _github_service(client)
        active = _server_of(client)
        gh = active.handlers.settings.github if active is not None else settings.github
        redirect_uri = gh.callback_url or f"{_external_origin(request)}/api/github/oauth/callback"
        try:
            authorization_url = service.oauth.start(
                redirect_uri=redirect_uri,
                browser_session=browser_session,
                origin=_canonical_origin(request.headers["origin"]),
            )
        except AssistantError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"authorization_url": authorization_url}

    @app.get("/api/github/oauth/callback", response_class=HTMLResponse)
    async def github_oauth_callback(
        request: Request,
        state: str = "",
        code: str = "",
        error: str = "",
        error_description: str = "",
    ) -> HTMLResponse:
        nonce = secrets.token_urlsafe(18)
        headers = {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Security-Policy": f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
        }
        if error or not state or not code:
            message = "مجوز GitHub صادر نشد." if error else "پارامترهای بازگشت GitHub ناقص‌اند."
            if error_description:
                message += " پنجره را ببندید و دوباره تلاش کنید."
            return HTMLResponse(
                f"<!doctype html><meta charset='utf-8'><style nonce='{nonce}'>body{{font-family:sans-serif;direction:rtl;padding:3rem;background:#0d1117;color:#f0f6fc}}</style><p>{message}</p>",
                status_code=400,
                headers=headers,
            )
        service = _github_service(client)
        try:
            origin = await asyncio.to_thread(
                service.complete_oauth,
                state=state,
                code=code,
                browser_session=str(getattr(request.state, "github_browser_session", "")),
            )
        except AssistantError as exc:
            logger.warning("GitHub OAuth callback rejected: %s", type(exc).__name__)
            return HTMLResponse(
                f"<!doctype html><meta charset='utf-8'><style nonce='{nonce}'>body{{font-family:sans-serif;direction:rtl;padding:3rem;background:#0d1117;color:#f0f6fc}}</style><p>اتصال GitHub کامل نشد. پنجره را ببندید و دوباره تلاش کنید.</p>",
                status_code=400,
                headers=headers,
            )
        safe_origin = json.dumps(origin, ensure_ascii=True).replace("</", "<\\/")
        html = f"""<!doctype html><meta charset='utf-8'>
<style nonce='{nonce}'>body{{font-family:sans-serif;direction:rtl;padding:3rem;background:#0d1117;color:#f0f6fc}}</style>
<p>حساب GitHub با موفقیت متصل شد. این پنجره بسته می‌شود.</p>
<script nonce='{nonce}'>if(window.opener){{window.opener.postMessage({{source:'pla-github-oauth',ok:true}},{safe_origin});window.close();}}else{{location.replace({safe_origin}+'/?github=connected');}}</script>"""
        return HTMLResponse(html, headers=headers)

    @app.post("/api/github/disconnect")
    async def github_disconnect(request: Request) -> dict[str, bool]:
        github_guard(request)
        try:
            await asyncio.to_thread(_github_service(client).disconnect)
        except AssistantError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"disconnected": True}

    @app.post("/api/github/read")
    async def github_read(request: Request, req: GitHubOperationRequest) -> Any:
        github_guard(request)
        try:
            if req.operation.startswith("local_"):
                return await asyncio.to_thread(
                    _github_service(client).local_read, req.operation, req.params
                )
            return await asyncio.to_thread(_github_service(client).read, req.operation, req.params)
        except AssistantError as exc:
            raise HTTPException(getattr(exc, "status", 400), str(exc)) from exc

    @app.post("/api/github/write")
    async def github_write(request: Request, req: GitHubOperationRequest) -> Any:
        github_guard(request)
        if req.operation in _GITHUB_SENSITIVE_OPERATIONS:
            raise HTTPException(400, "این عملیات فقط از مسیر مستقیم و محافظت‌شدهٔ UI مجاز است")
        if not req.confirm:
            raise HTTPException(409, "عملیات تغییردهندهٔ GitHub به تأیید صریح نیاز دارد")
        try:
            return await asyncio.to_thread(_github_service(client).write, req.operation, req.params)
        except AssistantError as exc:
            raise HTTPException(getattr(exc, "status", 400), str(exc)) from exc

    @app.post("/api/github/sensitive")
    async def github_sensitive_write(request: Request) -> Response:
        """Run UI-only writes whose plaintext must never enter agent schemas."""
        github_guard(request)
        if request.headers.get("x-github-confirm", "").casefold() != "true":
            raise HTTPException(409, "عملیات حساس GitHub به تأیید صریح نیاز دارد")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().casefold() != "application/json":
            raise HTTPException(415, "بدنهٔ عملیات حساس باید JSON باشد")
        declared = _github_declared_content_length(
            request,
            subject="عملیات حساس",
            maximum=MAX_GITHUB_SENSITIVE_BODY_BYTES,
        )
        raw = bytearray()
        try:
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_GITHUB_SENSITIVE_BODY_BYTES:
                    raise HTTPException(413, "بدنهٔ عملیات حساس بیش از سقف امن است")
            if not raw or declared is not None and declared != len(raw):
                raise HTTPException(400, "بدنهٔ عملیات حساس ناقص یا نامعتبر است")
            try:
                payload = json.loads(bytes(raw).decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise HTTPException(400, "بدنهٔ عملیات حساس JSON معتبر نیست") from exc
        finally:
            _clear_bytearray(raw, overwrite=True)
        if not isinstance(payload, dict):
            raise HTTPException(400, "ساختار عملیات حساس نامعتبر است")
        operation = payload.get("operation")
        params = payload.get("params")
        if operation not in _GITHUB_SENSITIVE_OPERATIONS or not isinstance(params, dict):
            raise HTTPException(400, "عملیات حساس GitHub مجاز نیست")
        try:
            result = await asyncio.to_thread(
                _github_service(client).write, operation, params
            )
        except AssistantError as exc:
            raise HTTPException(getattr(exc, "status", 400), str(exc)) from exc
        finally:
            params.pop("value", None)
            webhook_config = params.get("config")
            if isinstance(webhook_config, dict):
                webhook_config.pop("secret", None)
            params.pop("secret", None)
            payload.clear()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/api/github/release-asset")
    async def github_release_asset_upload(request: Request) -> Any:
        github_guard(request)
        if request.headers.get("x-github-confirm", "").casefold() != "true":
            raise HTTPException(409, "آپلود فایل Release به تأیید صریح نیاز دارد")
        declared = _github_declared_content_length(
            request,
            subject="آپلود Release",
            maximum=MAX_GITHUB_RELEASE_ASSET_BYTES,
        )
        data = bytearray()
        try:
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > MAX_GITHUB_RELEASE_ASSET_BYTES:
                    raise HTTPException(413, "فایل Release از سقف ۲۵۶ مگابایت بزرگ‌تر است")
            if not data or declared is not None and declared != len(data):
                raise HTTPException(400, "بدنهٔ فایل Release ناقص یا نامعتبر است")
            params = {
                "owner": request.query_params.get("owner", ""),
                "repo": request.query_params.get("repo", ""),
                "release_id": request.query_params.get("release_id", ""),
                "name": request.query_params.get("name", ""),
                "label": request.query_params.get("label", ""),
            }
            try:
                return await asyncio.to_thread(
                    _github_service(client).upload_release_asset,
                    params,
                    data=bytes(data),
                    content_type=request.headers.get("content-type", "application/octet-stream"),
                )
            except AssistantError as exc:
                raise HTTPException(getattr(exc, "status", 400), str(exc)) from exc
        finally:
            _clear_bytearray(data)

    @app.post("/api/github/download")
    async def github_download(request: Request, req: GitHubOperationRequest) -> Response:
        github_guard(request)
        try:
            data, filename, media_type = await asyncio.to_thread(
                _github_service(client).download,
                req.operation,
                req.params,
            )
        except AssistantError as exc:
            raise HTTPException(getattr(exc, "status", 400), str(exc)) from exc
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename).lstrip(".")
        safe_name = safe_name or "github-download"
        return Response(
            data,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "Cache-Control": "no-store",
            },
        )

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
            _raise_telegram_http_error(exc, legacy_bad_request=True)

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
            _raise_telegram_http_error(exc, legacy_bad_request=True)

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
            _raise_telegram_http_error(exc)

    @app.get("/api/telegram/accounts")
    async def telegram_accounts() -> dict[str, Any]:
        """Status of every Telegram account (no secrets)."""
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "telegram needs an in-process bridge")
        return server.handlers.telegram_accounts_status()

    @app.post("/api/telegram/account")
    async def telegram_account_toggle(req: TelegramAccountToggleRequest) -> dict[str, Any]:
        """Toggle one account's ``enabled`` flag (name + bool only, no secrets)."""
        server = _server_of(client)
        if server is None:
            raise HTTPException(503, "telegram needs an in-process bridge")
        try:
            return server.handlers.set_telegram_account_enabled(str(req.name), bool(req.enabled))
        except AssistantError as exc:
            _raise_telegram_http_error(exc)

    @app.get("/api/telegram/chats")
    async def telegram_chats(
        account: str | None = None,
        kind: str = "all",
        query: str = "",
        sort: str = "recent",
        limit: int = 50,
        offset: int = 0,
        archived: bool | None = None,
        unread_only: bool = False,
    ) -> dict[str, Any]:
        name, telegram = _connected_telegram_client(client, account)
        page_size = max(1, min(limit, 200))
        page_offset = max(0, offset)
        try:
            items = await asyncio.to_thread(
                telegram.list_chats,
                page_size + 1,
                kind,
                query,
                sort,
                offset=page_offset,
                archived=archived,
                unread_only=unread_only,
            )
        except AssistantError as exc:
            _raise_telegram_http_error(exc)
        has_more = len(items) > page_size
        items = items[:page_size]
        return {
            "account": name,
            "source": "live",
            "offset": page_offset,
            "next_offset": page_offset + len(items),
            "has_more": has_more,
            "items": [item.to_dict() for item in items],
        }

    @app.get("/api/telegram/contacts")
    async def telegram_contacts(
        account: str | None = None,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        name, telegram = _connected_telegram_client(client, account)
        page_size = max(1, min(limit, 500))
        page_offset = max(0, offset)
        fetch_limit = page_offset + page_size + 1
        try:
            if query.strip():
                all_items = await asyncio.to_thread(telegram.search_contacts, query, fetch_limit)
            else:
                all_items = await asyncio.to_thread(telegram.list_contacts, fetch_limit)
        except AssistantError as exc:
            _raise_telegram_http_error(exc)
        items = all_items[page_offset : page_offset + page_size]
        return {
            "account": name,
            "source": "live",
            "offset": page_offset,
            "next_offset": page_offset + len(items),
            "has_more": len(all_items) > page_offset + page_size,
            "items": items,
        }

    @app.get("/api/telegram/stats")
    async def telegram_stats(account: str | None = None) -> dict[str, Any]:
        name, telegram = _connected_telegram_client(client, account)
        try:
            data = await asyncio.to_thread(telegram.refresh_summary)
        except AssistantError as exc:
            _raise_telegram_http_error(exc)
        return {"account": name, **data}

    @app.get("/api/telegram/history")
    async def telegram_history(
        target: str,
        account: str | None = None,
        limit: int = 50,
        offset_id: int = 0,
    ) -> dict[str, Any]:
        name, telegram = _connected_telegram_client(client, account)
        try:
            resolved = await asyncio.to_thread(telegram.resolve_target, target)
            items = await asyncio.to_thread(
                telegram.get_chat_history,
                str(resolved["id"]),
                max(1, min(limit, 200)),
                max(0, offset_id),
            )
        except AssistantError as exc:
            _raise_telegram_http_error(exc)
        return {
            "account": name,
            "source": "live",
            "chat": resolved,
            "items": [item.to_dict() for item in items],
        }

    @app.post("/api/telegram/resolve")
    async def telegram_resolve(req: TelegramResolveRequest) -> dict[str, Any]:
        name, telegram = _connected_telegram_client(client, req.account)
        try:
            resolved = await asyncio.to_thread(telegram.resolve_target, req.target)
        except AssistantError as exc:
            _raise_telegram_http_error(exc)
        return {"account": name, "source": "live", **resolved}

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
        run_id = server.handlers._start_chat_run(req.message, session_id=req.session_id)
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
        external_origin = _websocket_external_origin(websocket)
        supplied_origin = _canonical_origin(websocket.headers.get("origin", ""))
        active = _server_of(client)
        github_settings = active.handlers.settings.github if active is not None else settings.github
        allowed_origins = {
            value
            for value in (
                external_origin,
                *(_canonical_origin(item) for item in github_settings.allowed_origins),
            )
            if value
        }
        # Browsers always send Origin on WebSocket handshakes. Non-browser
        # local clients may omit it; any supplied origin must be exact.
        if supplied_origin and supplied_origin not in allowed_origins:
            await websocket.close(code=1008, reason="origin_not_allowed")
            return
        if remote_auth_enabled:
            cookie = websocket.cookies.get("pla_remote_auth", "")
            authorization = websocket.headers.get("authorization", "")
            supplied = (
                authorization[len("Bearer ") :] if authorization.startswith("Bearer ") else ""
            )
            authenticated = (bool(cookie) and hmac.compare_digest(cookie, remote_cookie_value)) or (
                _valid_web_access_token(supplied)
                and len(supplied) == len(remote_access_token)
                and hmac.compare_digest(supplied, remote_access_token)
            )
            if websocket.url.scheme != "wss" or not authenticated:
                await websocket.close(code=1008, reason="authentication_required")
                return
        await websocket.accept()
        server = _server_of(client)
        stop = asyncio.Event()
        incoming: asyncio.Queue = asyncio.Queue()
        # run_id -> threading.Queue (the bridge event-bus queue)
        run_queues: dict[str, Any] = {}
        forwarders: dict[str, asyncio.Task] = {}
        # Global (run_id="") events — telegram_state, scheduled_fired — are
        # broadcast to every connected frontend.  The event-bus listener runs
        # on a bridge thread, so a threading.Queue + to_thread is used (the
        # same pattern as the per-run queues).
        global_queue: Any = Queue()

        def _push_global(event: Any) -> None:
            # Only truly global events (run_id="") belong to every frontend;
            # run-scoped events flow through their own per-run forwarder.
            if event.run_id:
                return
            try:
                global_queue.put_nowait(event)
            except Exception:  # noqa: BLE001 - socket half-dead: drop the event
                logger.debug("global event dropped for a closed websocket")

        async def global_forwarder() -> None:
            while not stop.is_set():
                try:
                    event = await asyncio.to_thread(global_queue.get, timeout=0.5)
                except Empty:
                    continue
                await incoming.put({"__global": True, "event": event})

        if server is not None:
            server.handlers.event_bus.subscribe(_push_global)
        global_task = asyncio.create_task(global_forwarder())

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
                if "__global" in item:
                    event = item["event"]
                    ok = await _send(
                        {
                            "type": "event",
                            "event_type": event.type,
                            "payload": event.payload,
                            "run_id": event.run_id,
                            "seq": event.seq,
                        }
                    )
                    if not ok:
                        break
                    continue
                if "__run" in item:
                    run_id, event = item["__run"], item["event"]
                    if event is None:
                        if run_id in forwarders:
                            forwarders.pop(run_id).cancel()
                        if server is not None:
                            server.handlers.event_bus.destroy_run_queue(run_id)
                        continue
                    ok = await _send(
                        {
                            "type": "event",
                            "event_type": event.type,
                            "payload": event.payload,
                            "run_id": event.run_id,
                            "seq": event.seq,
                        }
                    )
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
                    run_id = server.handlers._start_chat_run(
                        message, session_id=item.get("session_id")
                    )
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
            global_task.cancel()
            for task in forwarders.values():
                task.cancel()
            for run_id in list(run_queues):
                if server is not None:
                    server.handlers.event_bus.destroy_run_queue(run_id)
            if server is not None:
                server.handlers.event_bus.unsubscribe(_push_global)
            logger.debug("websocket handler exited cleanly")

    # Static assets
    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    return app


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _bridge_access_token(client: BridgeClient) -> str:
    backend = getattr(client, "_backend", None)
    server = getattr(backend, "_server", None) if backend is not None else None
    token = getattr(getattr(server, "config", None), "token", "")
    if not token and backend is not None:
        token = getattr(backend, "_token", "")
    if not _valid_web_access_token(token):
        raise AssistantError("برای Web راه‌دور، Bridge باید توکن امن ۳۲ تا ۵۱۲ نویسه‌ای داشته باشد")
    return token


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
        remote_access_token = "" if _is_loopback_bind(host) else _bridge_access_token(client)
        self._app = create_app(client, settings, remote_access_token=remote_access_token)
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
    parser.add_argument(
        "--host",
        default=os.environ.get("LOCAL_AGENT_WEB_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LOCAL_AGENT_WEB_PORT", "7824")),
        help="Port (default: 7824)",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    settings = load_settings()
    setup_logging(settings.data_dir)
    log_platform_summary()

    host = args.host
    port = args.port

    remote_access = not _is_loopback_bind(host)
    client = BridgeClient.start_in_process(settings)
    try:
        server = WebServer(settings, client, host=host, port=port)
    except AssistantError as exc:
        print(f"خطای امنیتی Web: {exc}", file=sys.stderr)
        return 1
    if remote_access:
        token_path = settings.data_dir / "bridge.token"
        print(f"⚠️  حالت سرور: رابط روی {host}:{port} گوش می‌دهد.")
        print("   دسترسی راه‌دور فقط از طریق reverse proxy امن HTTPS پذیرفته می‌شود.")
        if os.environ.get("LOCAL_AGENT_BRIDGE_TOKEN", "").strip():
            print("   توکن ورود از متغیر LOCAL_AGENT_BRIDGE_TOKEN خوانده می‌شود.")
        else:
            print(f"   توکن ورود: {token_path}")
        print("   نشانی HTTPS عمومی را باز کنید و توکن را در فرم ورود وارد کنید.")
    server.start_in_thread()
    server.wait_until_ready()
    print(f"web UI ready at {server.url}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
    return 0
