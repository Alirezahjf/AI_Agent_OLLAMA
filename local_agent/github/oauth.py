"""GitHub App authorization-code + PKCE client flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

from ..core.config import GitHubSettings
from ..core.errors import AssistantError
from .credentials import CredentialVault, TokenBundle, credential_binding

_STATE_TTL_SECONDS = 10 * 60
_MAX_PENDING_STATES = 1_000


@dataclass(frozen=True)
class PendingOAuth:
    state: str
    verifier: str
    redirect_uri: str
    browser_session: str
    origin: str
    created_at: float


class GitHubOAuth:
    """Owns short-lived OAuth state and talks to the secret-holding broker."""

    def __init__(self, settings: GitHubSettings, vault: CredentialVault) -> None:
        self.settings = settings
        self.vault = vault
        self._pending: dict[str, PendingOAuth] = {}
        self._lock = threading.Lock()

    def update_settings(self, settings: GitHubSettings) -> None:
        with self._lock:
            auth_identity = ("enabled", "client_id", "broker_url", "callback_url", "web_url")
            if any(
                getattr(settings, field) != getattr(self.settings, field) for field in auth_identity
            ):
                self._pending.clear()
            self.settings = settings

    def start(self, *, redirect_uri: str, browser_session: str, origin: str) -> str:
        settings = self.settings
        if not settings.enabled:
            raise AssistantError("اتصال GitHub در تنظیمات فعال نشده است")
        if not settings.client_id or not settings.broker_url:
            raise AssistantError("Client ID و نشانی کارگزار OAuth GitHub باید تنظیم شوند")
        self.vault.require()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        session_digest = _session_digest(browser_session)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        pending = PendingOAuth(
            state=state,
            verifier=verifier,
            redirect_uri=redirect_uri,
            browser_session=session_digest,
            origin=origin,
            created_at=time.monotonic(),
        )
        with self._lock:
            self._prune_locked()
            # Only the newest authorization attempt for one browser session is
            # useful; replacing older attempts also bounds popup retry abuse.
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if value.browser_session != session_digest
            }
            if len(self._pending) >= _MAX_PENDING_STATES:
                oldest = min(self._pending, key=lambda key: self._pending[key].created_at)
                self._pending.pop(oldest, None)
            self._pending[state] = pending
        query = urlencode(
            {
                "client_id": settings.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{settings.web_url.rstrip('/')}/login/oauth/authorize?{query}"

    def complete(
        self,
        *,
        state: str,
        code: str,
        browser_session: str,
    ) -> tuple[TokenBundle, str]:
        if not self.settings.enabled:
            raise AssistantError("اتصال GitHub در تنظیمات فعال نشده است")
        if (
            not state
            or len(state) > 256
            or not code
            or len(code) > 2048
            or code != code.strip()
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in code)
        ):
            raise AssistantError("پارامترهای بازگشت GitHub نامعتبرند")
        with self._lock:
            self._prune_locked()
            pending = self._pending.pop(state, None)
        if pending is None or not state:
            raise AssistantError("درخواست ورود GitHub منقضی یا قبلاً استفاده شده است")
        if not hmac.compare_digest(pending.browser_session, _session_digest(browser_session)):
            raise AssistantError("نشست مرورگر با درخواست ورود GitHub مطابقت ندارد")
        payload = self._broker(
            "exchange",
            {
                "code": code,
                "code_verifier": pending.verifier,
                "redirect_uri": pending.redirect_uri,
            },
        )
        token = TokenBundle.from_oauth(
            payload,
            client_id=self.settings.client_id,
            binding=self._credential_binding(),
        )
        self.vault.save(token)
        return token, pending.origin

    def refresh(self, token: TokenBundle) -> TokenBundle:
        self._require_bound_token(token)
        if not token.refresh_token or token.refresh_expires_within(0):
            # An expired, non-refreshable credential has no future use and
            # must not linger in the OS vault looking connected.
            self.vault.delete()
            raise AssistantError(
                "توکن GitHub منقضی شده و refresh token معتبر موجود نیست؛ دوباره متصل شوید"
            )
        payload = self._broker("refresh", {"refresh_token": token.refresh_token})
        refreshed = TokenBundle.from_oauth(
            payload,
            client_id=self.settings.client_id,
            binding=self._credential_binding(),
        )
        # GitHub can omit a replacement refresh token. Preserve the current one.
        if not refreshed.refresh_token:
            refreshed = TokenBundle(
                access_token=refreshed.access_token,
                token_type=refreshed.token_type,
                scope=refreshed.scope,
                expires_at=refreshed.expires_at,
                refresh_token=token.refresh_token,
                refresh_expires_at=token.refresh_expires_at,
                client_id=refreshed.client_id,
                binding=refreshed.binding,
            )
        self.vault.save(refreshed)
        return refreshed

    def revoke(self, token: TokenBundle) -> None:
        try:
            self._require_bound_token(token)
            self._broker("revoke", {"access_token": token.access_token})
        finally:
            # Local disconnect is deterministic even if remote revocation is
            # unavailable. The caller still receives the broker error.
            self.vault.delete()

    def _require_bound_token(self, token: TokenBundle) -> None:
        if (
            token.client_id != self.settings.client_id
            or not token.binding
            or token.binding != self._credential_binding()
        ):
            self.vault.delete()
            raise AssistantError(
                "اعتبار ذخیره‌شده GitHub به این پیکربندی تعلق ندارد؛ دوباره متصل شوید"
            )

    def _credential_binding(self) -> str:
        return credential_binding(
            client_id=self.settings.client_id,
            broker_url=self.settings.broker_url,
            api_url=self.settings.api_url,
            web_url=self.settings.web_url,
            graphql_url=self.settings.graphql_url,
        )

    def _broker(self, operation: str, body: dict[str, str]) -> dict[str, Any]:
        url = f"{self.settings.broker_url.rstrip('/')}/{operation}"
        try:
            response = requests.post(
                url,
                json={"client_id": self.settings.client_id, **body},
                headers={"Accept": "application/json", "User-Agent": "Persian-Local-Assistant/2"},
                timeout=(5, 30),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise AssistantError("ارتباط امن با کارگزار OAuth GitHub برقرار نشد") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        detail = None
        if isinstance(payload, dict):
            detail = (
                payload.get("error_description") or payload.get("detail") or payload.get("error")
            )
        if not 200 <= response.status_code < 300 or not isinstance(payload, dict) or detail:
            # A secret-bearing broker response is untrusted diagnostic input;
            # do not reflect it into UI/logging where it could contain a token.
            raise AssistantError(
                f"کارگزار OAuth GitHub درخواست را رد کرد (HTTP {response.status_code})"
            )
        return payload

    def _prune_locked(self) -> None:
        cutoff = time.monotonic() - _STATE_TTL_SECONDS
        self._pending = {
            key: value for key, value in self._pending.items() if value.created_at >= cutoff
        }


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _session_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
