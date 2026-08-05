"""Gmail integration (OAuth2 installed-app + IMAP/SMTP fallback)."""

from .client import GmailClient, GmailError, GmailMessage

__all__ = ["GmailClient", "GmailError", "GmailMessage"]
