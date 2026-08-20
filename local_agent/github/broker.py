"""Deployable secret-holding OAuth broker for the desktop/web GitHub App.

Run behind HTTPS with ``GITHUB_CLIENT_ID``, ``GITHUB_CLIENT_SECRET`` and an
exact comma-separated ``GITHUB_CALLBACK_URLS`` allow-list.  The native app uses
PKCE; this broker never persists tokens or codes and returns ``Cache-Control:
no-store``.  It is intentionally a tiny code-exchange service, not a general
GitHub proxy.
"""

import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import urlparse

import requests

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")


def create_broker_app():
    try:
        from fastapi import FastAPI, HTTPException, Request, Response
        from pydantic import BaseModel, ConfigDict, Field
    except ImportError as exc:  # pragma: no cover - dependency message
        raise RuntimeError("Install the web extra to run the GitHub OAuth broker") from exc

    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "")
    github_web_url = os.environ.get("GITHUB_WEB_URL", "https://github.com").strip().rstrip("/")
    github_api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").strip().rstrip("/")
    callback_items = [
        item.strip()
        for item in os.environ.get("GITHUB_CALLBACK_URLS", "").split(",")
        if item.strip()
    ]
    callbacks = set(callback_items)
    if not client_id or not client_secret or not callbacks:
        raise RuntimeError(
            "GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET and GITHUB_CALLBACK_URLS are required"
        )
    if (
        not _CLIENT_ID_RE.fullmatch(client_id)
        or len(client_secret) > 8192
        or client_secret != client_secret.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in client_secret)
        or len(callback_items) > 100
        or any(len(callback) > 2048 for callback in callback_items)
        or len(github_web_url) > 2048
        or len(github_api_url) > 2048
    ):
        raise RuntimeError("GitHub broker configuration contains an invalid value")
    for field_name, base_url in (
        ("GITHUB_WEB_URL", github_web_url),
        ("GITHUB_API_URL", github_api_url),
    ):
        try:
            parsed = urlparse(base_url)
            hostname = parsed.hostname
            _port = parsed.port
        except ValueError as exc:
            raise RuntimeError(f"{field_name} contains an invalid URL") from exc
        loopback = hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.params
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not loopback)
        ):
            raise RuntimeError(f"{field_name} must be a secure HTTP(S) base URL")
    for callback in callbacks:
        try:
            parsed = urlparse(callback)
            hostname = parsed.hostname
            _port = parsed.port
        except ValueError as exc:
            raise RuntimeError("GITHUB_CALLBACK_URLS contains an invalid URL") from exc
        loopback = hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.params
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not loopback)
        ):
            raise RuntimeError("GITHUB_CALLBACK_URLS must contain secure HTTP(S) callback URLs")

    class BrokerRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    class ExchangeRequest(BrokerRequest):
        client_id: str = Field(min_length=1, max_length=255)
        code: str = Field(min_length=1, max_length=2048)
        # The endpoint performs the RFC 7636 minimum-length check below so
        # clients retain the broker's established 400 response contract.
        code_verifier: str = Field(min_length=1, max_length=128)
        redirect_uri: str = Field(min_length=1, max_length=2048)

    class RefreshRequest(BrokerRequest):
        client_id: str = Field(min_length=1, max_length=255)
        refresh_token: str = Field(min_length=1, max_length=8192)

    class RevokeRequest(BrokerRequest):
        client_id: str = Field(min_length=1, max_length=255)
        access_token: str = Field(min_length=1, max_length=8192)

    app = FastAPI(
        title="Persian Local Assistant GitHub OAuth Broker", docs_url=None, redoc_url=None
    )
    limiter = _RateLimiter()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = None
        if not limiter.allow(_peer(request)):
            response = Response("rate limit exceeded", status_code=429)
        try:
            declared_size = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            declared_size = -1
        body_limit = 32 * 1024
        if response is None and (declared_size < 0 or declared_size > body_limit):
            response = Response("request body is too large", status_code=413)
        if response is None and request.method == "POST":
            # Content-Length can be absent or dishonest (for example with
            # chunked transfer encoding), so enforce the limit while streaming
            # rather than buffering an unbounded body first.
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > body_limit:
                    response = Response("request body is too large", status_code=413)
                    break
                body.extend(chunk)
            if response is None:
                request._body = bytes(body)  # Starlette request replay contract
        if response is None:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/exchange")
    def exchange(body: ExchangeRequest) -> dict[str, Any]:
        _client(body.client_id, client_id)
        if body.redirect_uri not in callbacks:
            raise HTTPException(400, "redirect_uri is not allow-listed")
        if len(body.code_verifier) < 43 or len(body.code_verifier) > 128:
            raise HTTPException(400, "invalid PKCE verifier")
        return _token_request(
            client_id,
            client_secret,
            github_web_url,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": body.code,
                "redirect_uri": body.redirect_uri,
                "code_verifier": body.code_verifier,
            },
        )

    @app.post("/refresh")
    def refresh(body: RefreshRequest) -> dict[str, Any]:
        _client(body.client_id, client_id)
        return _token_request(
            client_id,
            client_secret,
            github_web_url,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": body.refresh_token,
            },
        )

    @app.post("/revoke")
    def revoke(body: RevokeRequest) -> dict[str, bool]:
        _client(body.client_id, client_id)
        try:
            response = requests.delete(
                f"{github_api_url}/applications/{client_id}/token",
                auth=(client_id, client_secret),
                json={"access_token": body.access_token},
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=(5, 30),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise HTTPException(502, "GitHub token revocation endpoint is unavailable") from exc
        if response.status_code not in {204, 404, 422}:
            raise HTTPException(502, "GitHub rejected token revocation")
        return {"ok": True}

    return app


class _RateLimiter:
    def __init__(self, limit: int = 30, window: int = 60, max_peers: int = 10_000) -> None:
        self.limit = limit
        self.window = window
        self.max_peers = max_peers
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            if key not in self._hits and len(self._hits) >= self.max_peers:
                expired = [peer for peer, hits in self._hits.items() if not hits or hits[-1] < cutoff]
                for peer in expired:
                    self._hits.pop(peer, None)
                if len(self._hits) >= self.max_peers:
                    return False
            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


def _peer(request: Any) -> str:
    return str(request.client.host if request.client else "unknown")


def _client(got: str, expected: str) -> None:
    from fastapi import HTTPException

    if not secrets.compare_digest(got, expected):
        raise HTTPException(400, "unknown client_id")


def _token_request(
    client_id: str, client_secret: str, github_web_url: str, data: dict[str, str]
) -> dict[str, Any]:
    from fastapi import HTTPException

    try:
        response = requests.post(
            f"{github_web_url}/login/oauth/access_token",
            data=data,
            headers={"Accept": "application/json", "User-Agent": f"GitHub-App/{client_id}"},
            timeout=(5, 30),
            allow_redirects=False,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(502, "GitHub token endpoint is unavailable") from exc
    error = payload.get("error") if isinstance(payload, dict) else None
    if not 200 <= response.status_code < 300 or not isinstance(payload, dict) or error:
        # Do not relay arbitrary GitHub fields that might contain request data.
        raise HTTPException(400, "GitHub rejected the token request")
    allowed = {
        "access_token",
        "token_type",
        "scope",
        "expires_in",
        "refresh_token",
        "refresh_token_expires_in",
    }
    return {key: value for key, value in payload.items() if key in allowed}


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Install the web extra first") from exc
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(create_broker_app(), host=host, port=port, proxy_headers=True)


if __name__ == "__main__":  # pragma: no cover
    main()
