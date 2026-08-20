"""Operating-system credential-vault storage for GitHub OAuth tokens."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from ..core.errors import AssistantError, DependencyMissing

_SERVICE = "persian-local-assistant.github"
_ACCOUNT = "github-app-user-token"


def _valid_credential(value: str) -> bool:
    """OAuth credentials are opaque, bounded, single-line values."""
    return bool(
        isinstance(value, str)
        and value
        and len(value) <= 16_384
        and value == value.strip()
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _valid_metadata(value: str, maximum: int) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) <= maximum
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def credential_binding(
    *, client_id: str, broker_url: str, api_url: str, web_url: str, graphql_url: str
) -> str:
    """Bind a vault credential to every endpoint that can receive it."""
    identity = f"{client_id}\x00{broker_url}\x00{api_url}\x00{web_url}\x00{graphql_url}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False)
class TokenBundle:
    access_token: str
    token_type: str = "bearer"
    scope: str = ""
    expires_at: str = ""
    refresh_token: str = ""
    refresh_expires_at: str = ""
    client_id: str = ""
    binding: str = ""

    @classmethod
    def from_oauth(
        cls, payload: dict[str, Any], *, client_id: str, binding: str = ""
    ) -> TokenBundle:
        now = datetime.now(UTC).timestamp()

        def expiry(seconds_key: str, explicit_key: str) -> str:
            explicit = str(payload.get(explicit_key) or "")
            if explicit:
                try:
                    parsed = datetime.fromisoformat(explicit)
                    if len(explicit) > 128 or parsed.tzinfo is None:
                        raise ValueError("expiry must be bounded and timezone-aware")
                except (TypeError, ValueError, OverflowError) as exc:
                    raise AssistantError("کارگزار OAuth زمان انقضای معتبری برنگرداند") from exc
                return explicit
            try:
                seconds = int(payload.get(seconds_key) or 0)
                if seconds < 0 or seconds > 10 * 365 * 24 * 60 * 60:
                    raise ValueError("expiry is out of range")
                return datetime.fromtimestamp(now + seconds, UTC).isoformat() if seconds else ""
            except (TypeError, ValueError, OverflowError, OSError) as exc:
                raise AssistantError("کارگزار OAuth زمان انقضای معتبری برنگرداند") from exc

        access_token = str(payload.get("access_token") or "")
        refresh_token = str(payload.get("refresh_token") or "")
        token_type = str(payload.get("token_type") or "bearer")
        scope = str(payload.get("scope") or "")
        if (
            not _valid_credential(access_token)
            or (refresh_token and not _valid_credential(refresh_token))
            or token_type.casefold() != "bearer"
            or not _valid_metadata(scope, 4_096)
            or not _valid_metadata(client_id, 255)
            or (binding and not re.fullmatch(r"[0-9a-f]{64}", binding))
        ):
            raise AssistantError("کارگزار OAuth توکن دسترسی معتبر برنگرداند")
        return cls(
            access_token=access_token,
            token_type="bearer",
            scope=scope,
            expires_at=expiry("expires_in", "expires_at"),
            refresh_token=refresh_token,
            refresh_expires_at=expiry("refresh_token_expires_in", "refresh_expires_at"),
            client_id=client_id,
            binding=binding,
        )

    @classmethod
    def from_json(cls, raw: str) -> TokenBundle:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65_536:
            raise ValueError("credential is not a bounded string")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError("credential is not an object")
        allowed = set(cls.__dataclass_fields__)
        if set(payload) - allowed:
            raise ValueError("credential contains unknown fields")
        token = cls(**{key: str(value or "") for key, value in payload.items()})

        def valid_expiry(value: str) -> bool:
            if not value:
                return True
            try:
                parsed = datetime.fromisoformat(value)
                return len(value) <= 128 and parsed.tzinfo is not None
            except (TypeError, ValueError, OverflowError):
                return False

        if (
            not _valid_credential(token.access_token)
            or (token.refresh_token and not _valid_credential(token.refresh_token))
            or token.token_type.casefold() != "bearer"
            or not _valid_metadata(token.scope, 4_096)
            or not _valid_metadata(token.client_id, 255)
            or (token.binding and not re.fullmatch(r"[0-9a-f]{64}", token.binding))
            or not valid_expiry(token.expires_at)
            or not valid_expiry(token.refresh_expires_at)
        ):
            raise ValueError("credential contains invalid metadata")
        return token

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, separators=(",", ":"))

    def __repr__(self) -> str:
        """Never put bearer or refresh credentials in diagnostics/logs."""
        return (
            "TokenBundle(token_type="
            f"{self.token_type!r}, scope={self.scope!r}, expires_at={self.expires_at!r}, "
            f"refresh_expires_at={self.refresh_expires_at!r}, client_id={self.client_id!r}, "
            f"has_refresh_token={bool(self.refresh_token)!r})"
        )

    def expires_within(self, seconds: int) -> bool:
        return self._expiry_within(self.expires_at, seconds)

    def refresh_expires_within(self, seconds: int) -> bool:
        return self._expiry_within(self.refresh_expires_at, seconds)

    @staticmethod
    def _expiry_within(value: str, seconds: int) -> bool:
        if not value:
            return False
        try:
            expiry = datetime.fromisoformat(value)
            return (expiry - datetime.now(UTC)).total_seconds() <= seconds
        except (AttributeError, TypeError, ValueError, OverflowError):
            # Malformed expiry metadata must fail closed.
            return True


class CredentialVault:
    """Small keyring wrapper with no plaintext fallback."""

    def __init__(self) -> None:
        self._keyring: Any | None = None
        self._password_delete_error: type[Exception] | None = None
        self._error = ""
        try:
            import keyring
            from keyring.errors import KeyringError, PasswordDeleteError

            backend = keyring.get_keyring()
            if float(getattr(backend, "priority", 0) or 0) <= 0:
                raise KeyringError("no secure keyring backend is available")
            self._keyring = keyring
            self._password_delete_error = PasswordDeleteError
        except ImportError:
            self._error = "بستهٔ keyring نصب نیست؛ افزونهٔ github را نصب کنید"
        except Exception as exc:  # noqa: BLE001  # keyring backends expose OS-specific errors
            self._error = f"صندوق امن سیستم‌عامل در دسترس نیست ({type(exc).__name__})"

    @property
    def available(self) -> bool:
        return self._keyring is not None

    @property
    def error(self) -> str:
        return self._error

    def require(self) -> Any:
        if self._keyring is None:
            if "نصب نیست" in self._error:
                raise DependencyMissing(
                    self._error, install_hint="pip install 'persian-ollama-coding-agent[github]'"
                )
            raise AssistantError(self._error or "صندوق امن سیستم‌عامل در دسترس نیست")
        return self._keyring

    def load(self) -> TokenBundle | None:
        keyring = self.require()
        try:
            raw = keyring.get_password(_SERVICE, _ACCOUNT)
            return TokenBundle.from_json(raw) if raw else None
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            self.delete()
            return None
        except Exception as exc:
            raise AssistantError(
                f"خواندن توکن GitHub از صندوق امن ناموفق بود ({type(exc).__name__})"
            ) from exc

    def save(self, token: TokenBundle) -> None:
        keyring = self.require()
        if not _valid_credential(token.access_token) or (
            token.refresh_token and not _valid_credential(token.refresh_token)
        ):
            raise AssistantError("توکن GitHub برای ذخیره‌سازی معتبر نیست")
        try:
            keyring.set_password(_SERVICE, _ACCOUNT, token.to_json())
        except Exception as exc:
            raise AssistantError(
                f"ذخیرهٔ امن توکن GitHub ناموفق بود ({type(exc).__name__})"
            ) from exc

    def delete(self) -> None:
        keyring = self.require()
        try:
            keyring.delete_password(_SERVICE, _ACCOUNT)
        except Exception as exc:
            # A missing credential is already the desired state. Generic
            # backend failures must not be reported as a successful deletion.
            if self._password_delete_error is None or not isinstance(
                exc, self._password_delete_error
            ):
                raise AssistantError(f"حذف توکن GitHub ناموفق بود ({type(exc).__name__})") from exc
