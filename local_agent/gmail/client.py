"""Gmail integration for the local assistant.

Two transport backends are supported:

* **OAuth2 installed-app** (preferred) — official
  ``google-api-python-client`` + ``google-auth-oauthlib`` libraries.  The
  user downloads a Desktop-app client JSON from Google Cloud Console and
  approves once in the browser; the token is stored in ``token_file``
  and refreshed automatically afterwards.
* **IMAP/SMTP with an App Password** — fallback for users who cannot or
  will not create an OAuth client.  Configure ``gmail.app_password``.

The backend object is injected for tests (fake service, fully offline);
production builds it from ``GmailSettings``.
"""

from __future__ import annotations

import base64
import email
import email.message
import imaplib
import smtplib
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger

logger = get_logger("gmail")


class GmailError(AssistantError):
    """A user-facing failure from the Gmail integration."""


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"


@dataclass
class GmailMessage:
    """A single email returned by the Gmail actions."""

    id: str
    subject: str
    sender: str
    snippet: str
    date: str
    is_unread: bool = False

    def to_text(self) -> str:
        flags = "📩" if not self.is_unread else "📬"
        return f"{flags} [{self.id}] {self.subject} — {self.sender} ({self.date})"


class GmailBackend:
    """Interface implemented by the OAuth and IMAP backends."""

    def connect(self) -> str:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        raise NotImplementedError

    def list_unread(self, limit: int) -> list[GmailMessage]:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> list[GmailMessage]:
        raise NotImplementedError

    def read(self, msg_id: str) -> GmailMessage:
        raise NotImplementedError

    def send(self, to: str, subject: str, body: str) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OAuth2 backend (official Google libraries, optional dependency)
# ---------------------------------------------------------------------------


class _OAuthGmailBackend(GmailBackend):
    def __init__(self, *, credentials_file: Path, token_file: Path) -> None:
        self._credentials_file = Path(credentials_file)
        self._token_file = Path(token_file)
        self._creds: Any | None = None
        self._service: Any | None = None

    # ------------------------------------------------------------- connect

    def connect(self) -> str:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise GmailError(
                "کتابخانه‌های جیمیل نصب نیستند. نصب کنید: pip install -e \".[gmail]\""
            ) from exc

        if self._creds is None and self._token_file.is_file():
            try:
                self._creds = Credentials.from_authorized_user_file(str(self._token_file), SCOPES)
            except Exception as exc:  # noqa: BLE001
                logger.warning("gmail token file unreadable: %s", exc)
                self._creds = None

        if self._creds is None or not self._creds.valid:
            if self._creds and self._creds.expired and self._creds.refresh_token:
                self._creds.refresh(Request())
            else:
                self._creds = self._run_installed_flow()
                self._token_file.parent.mkdir(parents=True, exist_ok=True)
                self._token_file.write_text(self._creds.to_json(), encoding="utf-8")

        from googleapiclient.discovery import build

        self._service = build("gmail", "v1", credentials=self._creds)
        profile = self._service.users().getProfile(userId="me").execute()
        return f"connected as {profile.get('emailAddress', '?')}"

    def _run_installed_flow(self) -> Any:
        if not self._credentials_file.is_file():
            raise GmailError(
                "فایل credentials.json پیدا نشد. از Google Cloud Console یک "
                "OAuth Client (Desktop app) بسازید و آن را در "
                f"{self._credentials_file} قرار دهید."
            )
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(self._credentials_file), SCOPES)
        return flow.run_local_server(port=0)

    def disconnect(self) -> None:
        self._service = None
        self._creds = None

    @property
    def is_connected(self) -> bool:
        return self._service is not None

    # ------------------------------------------------------------- actions

    def _users(self) -> Any:
        if self._service is None:
            raise GmailError("جیمیل وصل نیست؛ ابتدا اتصال را برقرار کنید")
        return self._service.users()

    def list_unread(self, limit: int) -> list[GmailMessage]:
        results = (
            self._users()
            .messages()
            .list(userId="me", q="is:unread", maxResults=max(1, limit))
            .execute()
        )
        return [self._summary(msg) for msg in results.get("messages", [])]

    def search(self, query: str, limit: int) -> list[GmailMessage]:
        results = (
            self._users()
            .messages()
            .list(userId="me", q=query, maxResults=max(1, limit))
            .execute()
        )
        return [self._summary(msg) for msg in results.get("messages", [])]

    def read(self, msg_id: str) -> GmailMessage:
        payload = self._users().messages().get(userId="me", id=msg_id).execute()
        headers = {h["name"].lower(): h["value"] for h in payload.get("payload", {}).get("headers", [])}
        body = _extract_body(payload.get("payload", {}))
        return GmailMessage(
            id=msg_id,
            subject=headers.get("subject", ""),
            sender=headers.get("from", ""),
            snippet=f"{body[:300]}" if body else payload.get("snippet", ""),
            date=headers.get("date", ""),
            is_unread=False,
        )

    def send(self, to: str, subject: str, body: str) -> str:
        message = _build_mime(to, subject, body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        sent = self._users().messages().send(userId="me", body={"raw": raw}).execute()
        return str(sent.get("id", "?"))

    def _summary(self, msg: dict[str, Any]) -> GmailMessage:
        payload = self._users().messages().get(userId="me", id=msg["id"]).execute()
        headers = {h["name"].lower(): h["value"] for h in payload.get("payload", {}).get("headers", [])}
        return GmailMessage(
            id=msg["id"],
            subject=headers.get("subject", "(بدون موضوع)"),
            sender=headers.get("from", "?"),
            snippet=payload.get("snippet", ""),
            date=headers.get("date", ""),
            is_unread=True,
        )


# ---------------------------------------------------------------------------
# IMAP/SMTP backend (App Password fallback)
# ---------------------------------------------------------------------------


class _ImapGmailBackend(GmailBackend):
    def __init__(self, *, username: str, app_password: str) -> None:
        self._username = username
        self._app_password = app_password
        self._imap: Any | None = None
        self._lock = threading.RLock()

    def connect(self) -> str:
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, 993, ssl_context=ssl.create_default_context())
            imap.login(self._username, self._app_password)
        except imaplib.IMAP4.error as exc:
            raise GmailError(
                "ورود به جیمیل ناموفق بود. «App Password» دو مرحله‌ای (بدون فاصله) را "
                "در تنظیمات گوگل بسازید و در gmail.app_password بگذارید."
            ) from exc
        except OSError as exc:
            raise GmailError(f"اتصال به سرور جیمیل ممکن نشد: {exc}") from exc
        self._imap = imap
        return f"connected as {self._username} (IMAP)"

    def disconnect(self) -> None:
        with self._lock:
            if self._imap is not None:
                try:
                    self._imap.logout()
                except Exception as exc:  # noqa: BLE001 - best-effort teardown
                    logger.debug("imap logout failed: %s", exc)
                self._imap = None

    @property
    def is_connected(self) -> bool:
        return self._imap is not None

    # ------------------------------------------------------------- helpers

    def _select(self) -> Any:
        if self._imap is None:
            raise GmailError("جیمیل وصل نیست؛ ابتدا اتصال را برقرار کنید")
        self._imap.select("INBOX")
        return self._imap

    def _fetch_messages(self, criteria: str, limit: int) -> list[GmailMessage]:
        with self._lock:
            imap = self._select()
            status, data = imap.search(None, criteria)
            if status != "OK":
                return []
            ids = data[0].split()
            out: list[GmailMessage] = []
            for msg_id in ids[-max(1, limit):]:
                status, msg_data = imap.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue
                parsed = email.message_from_bytes(msg_data[0][1])
                out.append(_message_from_rfc822(str(int(msg_id)), parsed))
            return out

    def list_unread(self, limit: int) -> list[GmailMessage]:
        return self._fetch_messages("UNSEEN", limit)

    def search(self, query: str, limit: int) -> list[GmailMessage]:
        # IMAP search is crude; a plain text query on subject+body.
        criteria = f'TEXT "{query}"'
        return self._fetch_messages(criteria, limit)

    def read(self, msg_id: str) -> GmailMessage:
        with self._lock:
            imap = self._select()
            status, msg_data = imap.fetch(str(msg_id), "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                raise GmailError(f"ایمیلی با شناسهٔ {msg_id} پیدا نشد")
            parsed = email.message_from_bytes(msg_data[0][1])
            body = _rfc822_body(parsed)
            return GmailMessage(
                id=str(msg_id),
                subject=str(parsed.get("Subject", "")),
                sender=str(parsed.get("From", "?")),
                snippet=body[:300],
                date=str(parsed.get("Date", "")),
                is_unread=False,
            )

    def send(self, to: str, subject: str, body: str) -> str:
        message = _build_mime(to, subject, body)
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, 465, context=ssl.create_default_context()) as smtp:
                smtp.login(self._username, self._app_password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise GmailError(f"ارسال ایمیل ناموفق بود: {exc}") from exc
        return "sent"


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------


class GmailClient:
    """High-level Gmail facade used by the action layer and the web UI."""

    def __init__(self, *, backend: GmailBackend | None = None, **settings: Any) -> None:
        self._backend = backend or _build_backend(**settings)

    @classmethod
    def from_settings(cls, gmail_settings: Any, data_dir: Path) -> GmailClient:
        credentials_file = (
            Path(gmail_settings.credentials_file).expanduser()
            if gmail_settings.credentials_file
            else data_dir / "credentials.json"
        )
        token_file = (
            Path(gmail_settings.token_file).expanduser()
            if gmail_settings.token_file
            else data_dir / "gmail_token.json"
        )
        return cls(
            credentials_file=credentials_file,
            token_file=token_file,
            username=gmail_settings.username,
            app_password=gmail_settings.app_password,
        )

    @property
    def backend(self) -> GmailBackend:
        return self._backend

    @property
    def is_connected(self) -> bool:
        return self._backend.is_connected

    def connect(self) -> str:
        return self._backend.connect()

    def disconnect(self) -> None:
        self._backend.disconnect()

    def list_unread(self, limit: int = 20) -> list[GmailMessage]:
        return self._backend.list_unread(max(1, int(limit or 20)))

    def search(self, query: str, limit: int = 20) -> list[GmailMessage]:
        return self._backend.search(query, max(1, int(limit or 20)))

    def read(self, msg_id: str) -> GmailMessage:
        return self._backend.read(msg_id)

    def send(self, to: str, subject: str, body: str) -> str:
        return self._backend.send(to, subject, body)


def _build_backend(
    *,
    credentials_file: Path | None = None,
    token_file: Path | None = None,
    username: str = "",
    app_password: str = "",
) -> GmailBackend:
    """Pick OAuth when client files exist, IMAP/SMTP otherwise."""
    if credentials_file and Path(credentials_file).is_file():
        return _OAuthGmailBackend(
            credentials_file=Path(credentials_file),
            token_file=Path(token_file or credentials_file.parent / "gmail_token.json"),
        )
    if app_password:
        if not username:
            raise GmailError("برای حالت App Password، آدرس ایمیل (username) لازم است")
        return _ImapGmailBackend(username=username, app_password=app_password)
    raise GmailError(
        "هیچ روش اتصال جیمیل پیکربندی نشده است. یا credentials.json (OAuth) را از "
        "Google Cloud Console بگذارید، یا gmail.app_password را تنظیم کنید."
    )


# ---------------------------------------------------------------------------
# MIME / parsing helpers
# ---------------------------------------------------------------------------


def _build_mime(to: str, subject: str, body: str) -> email.message.EmailMessage:
    message = email.message.EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message["From"] = "me"
    message.set_content(body)
    return message


def _extract_body(payload: dict[str, Any]) -> str:
    if payload.get("body", {}).get("data"):
        try:
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    for part in payload.get("parts", []):
        body = _extract_body(part)
        if body:
            return body
    return ""


def _rfc822_body(parsed: email.message.Message) -> str:
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception as exc:  # noqa: BLE001 - try the next part
                    logger.debug("could not decode part: %s", exc)
                    continue
        return ""
    try:
        return parsed.get_payload(decode=True).decode(parsed.get_content_charset() or "utf-8", "replace")
    except Exception:  # noqa: BLE001
        return str(parsed.get_payload() or "")


def _message_from_rfc822(msg_id: str, parsed: email.message.Message) -> GmailMessage:
    body = _rfc822_body(parsed)
    return GmailMessage(
        id=msg_id,
        subject=str(parsed.get("Subject", "(بدون موضوع)")),
        sender=str(parsed.get("From", "?")),
        snippet=body[:200],
        date=str(parsed.get("Date", "")),
        is_unread=True,
    )
