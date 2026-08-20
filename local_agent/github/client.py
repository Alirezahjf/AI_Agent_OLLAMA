"""Typed GitHub REST/GraphQL transport with pagination and rate limits."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from ..core.config import GitHubSettings
from ..core.errors import AssistantError

_MAX_RAW_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_RELEASE_UPLOAD_BYTES = 256 * 1024 * 1024
_MAX_JSON_REQUEST_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class RateLimit:
    limit: int = 0
    remaining: int = 0
    reset: int = 0
    resource: str = ""

    def to_dict(self) -> dict[str, int | str]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset": self.reset,
            "resource": self.resource,
        }


class GitHubAPIError(AssistantError):
    def __init__(
        self,
        status: int,
        message: str,
        *,
        documentation_url: str = "",
        required_permission: str = "",
        request_id: str = "",
    ) -> None:
        self.status = status
        self.documentation_url = documentation_url
        self.required_permission = required_permission
        self.request_id = request_id
        permission = f"؛ مجوز لازم: {required_permission}" if required_permission else ""
        suffix = f" (شناسهٔ درخواست: {request_id})" if request_id else ""
        super().__init__(f"GitHub API ({status}): {message}{permission}{suffix}")


class GitHubClient:
    def __init__(self, settings: GitHubSettings, token_provider: Callable[[], str]) -> None:
        self.settings = settings
        self._token_provider = token_provider
        self._session = requests.Session()
        self.rate_limit = RateLimit()

    def update_settings(self, settings: GitHubSettings) -> None:
        self.settings = settings

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        raw: bool = False,
        required_permission: str = "",
    ) -> Any:
        if not path.startswith("/"):
            raise ValueError("GitHub REST path must begin with /")
        method = method.upper()
        if raw and method != "GET":
            raise ValueError("Raw GitHub downloads must use GET")
        supplied_headers = headers or {}
        protected_headers = {
            "authorization",
            "host",
            "content-length",
            "transfer-encoding",
            "x-github-api-version",
            "user-agent",
        }
        if json_body is not None:
            protected_headers.add("content-type")
        if any(key.casefold() in protected_headers for key in supplied_headers):
            raise ValueError("Protected GitHub transport headers cannot be overridden")
        encoded_body = _serialize_json_request(json_body) if json_body is not None else None
        url = urljoin(self.settings.api_url.rstrip("/") + "/", path.lstrip("/"))
        access_token = self._token_provider()
        request_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Persian-Local-Assistant/2",
        }
        if encoded_body is not None:
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        request_headers.update(supplied_headers)
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                data=encoded_body,
                headers=request_headers,
                timeout=(5, 45),
                # Raw downloads are GET-only and GitHub commonly redirects
                # them to short-lived object storage; requests strips auth on
                # cross-host redirects. API/GraphQL requests never redirect,
                # preventing mutation bodies from being replayed elsewhere.
                allow_redirects=raw,
                stream=raw,
            )
        except requests.RequestException as exc:
            raise AssistantError("ارتباط با GitHub API برقرار نشد") from exc
        self._capture_rate(response)
        if response.status_code >= 300:
            try:
                self._raise_api_error(response, required_permission, secret=access_token)
            finally:
                if raw:
                    response.close()
        if raw:
            try:
                declared = int(response.headers.get("Content-Length", "0") or 0)
            except ValueError:
                declared = 0
            if declared > _MAX_RAW_DOWNLOAD_BYTES:
                response.close()
                raise AssistantError("دانلود GitHub از سقف امن ۲۵۶ مگابایت بزرگ‌تر است")
            data = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    data.extend(chunk)
                    if len(data) > _MAX_RAW_DOWNLOAD_BYTES:
                        raise AssistantError("دانلود GitHub از سقف امن ۲۵۶ مگابایت بزرگ‌تر است")
                return bytes(data)
            except requests.RequestException as exc:
                raise AssistantError("دانلود GitHub در میانهٔ انتقال قطع شد") from exc
            finally:
                response.close()
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubAPIError(response.status_code, "پاسخ JSON معتبر نبود") from exc

    def upload_release_asset(
        self,
        path: str,
        *,
        name: str,
        data: bytes,
        content_type: str,
        label: str = "",
    ) -> dict[str, Any]:
        """Upload raw bytes to GitHub's dedicated, non-redirecting upload origin."""
        if not path.startswith("/repos/") or not path.endswith("/assets"):
            raise ValueError("GitHub release upload path is invalid")
        if not isinstance(data, bytes) or not data or len(data) > _MAX_RELEASE_UPLOAD_BYTES:
            raise AssistantError("فایل Release باید بین ۱ بایت و ۲۵۶ مگابایت باشد")
        api = urlparse(self.settings.api_url)
        if api.hostname == "api.github.com":
            url = f"https://uploads.github.com{path}"
        else:
            # GHES serves uploads from its configured API origin. Keeping the
            # URL derived from trusted settings prevents token exfiltration via
            # caller-controlled upload_url values.
            url = urljoin(self.settings.api_url.rstrip("/") + "/", path.lstrip("/"))
        access_token = self._token_provider()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Persian-Local-Assistant/2",
            "Content-Type": content_type,
        }
        query = {"name": name}
        if label:
            query["label"] = label
        try:
            response = self._session.post(
                url,
                params=query,
                data=data,
                headers=headers,
                timeout=(5, 300),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise AssistantError("آپلود فایل Release به GitHub انجام نشد") from exc
        self._capture_rate(response)
        if response.status_code >= 300:
            self._raise_api_error(response, "Contents: write", secret=access_token)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubAPIError(response.status_code, "پاسخ آپلود Release معتبر نبود") from exc
        if not isinstance(payload, dict) or not payload.get("id"):
            raise GitHubAPIError(502, "قالب پاسخ آپلود Release ناشناخته است")
        return payload

    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        item_key: str | None = None,
        max_items: int = 500,
        required_permission: str = "",
    ) -> dict[str, Any]:
        if max_items < 1 or max_items > 2000:
            raise AssistantError("max_items باید بین ۱ و ۲۰۰۰ باشد")
        query = dict(params or {})
        query["per_page"] = min(100, max_items)
        items: list[Any] = []
        page = 1
        while len(items) < max_items:
            query["page"] = page
            payload = self.request(
                "GET",
                path,
                params=query,
                required_permission=required_permission,
            )
            if item_key:
                if not isinstance(payload, dict) or item_key not in payload:
                    raise GitHubAPIError(502, "قالب پاسخ صفحه‌بندی‌شده ناشناخته است")
                batch = payload[item_key]
            else:
                batch = payload
            if not isinstance(batch, list):
                raise GitHubAPIError(502, "قالب پاسخ صفحه‌بندی‌شده ناشناخته است")
            items.extend(batch[: max_items - len(items)])
            if len(batch) < query["per_page"]:
                break
            page += 1
        return {
            "items": items,
            "pagination": {
                "count": len(items),
                "pages_fetched": page,
                "truncated": len(items) >= max_items,
            },
            "rate_limit": self.rate_limit.to_dict(),
        }

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {"query": query, "variables": variables or {}}
        encoded_body = _serialize_json_request(body)
        access_token = self._token_provider()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Persian-Local-Assistant/2",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            response = self._session.post(
                self.settings.graphql_url,
                data=encoded_body,
                headers=headers,
                timeout=(5, 45),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise AssistantError("ارتباط با GitHub GraphQL برقرار نشد") from exc
        self._capture_rate(response)
        if response.status_code >= 300:
            self._raise_api_error(
                response, "بسته به فیلدهای GraphQL", secret=access_token
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubAPIError(response.status_code, "پاسخ GraphQL معتبر نبود") from exc
        if not isinstance(payload, dict):
            raise GitHubAPIError(response.status_code, "قالب پاسخ GraphQL ناشناخته است")
        errors = payload.get("errors")
        if errors:
            if not isinstance(errors, list):
                raise GitHubAPIError(422, "قالب خطای GraphQL ناشناخته است")
            messages = "; ".join(
                str(error.get("message", "خطای GraphQL"))[:500]
                if isinstance(error, dict)
                else "خطای GraphQL"
                for error in errors[:3]
            )
            messages = _safe_diagnostic(messages, maximum=1_500).replace(
                access_token, "[REDACTED]"
            )
            raise GitHubAPIError(422, messages)
        if "data" not in payload or not isinstance(payload["data"], dict):
            raise GitHubAPIError(502, "قالب دادهٔ GraphQL ناشناخته است")
        return {"data": payload["data"], "rate_limit": self.rate_limit.to_dict()}

    def _capture_rate(self, response: requests.Response) -> None:
        def number(name: str) -> int:
            try:
                return int(response.headers.get(name, "0"))
            except ValueError:
                return 0

        self.rate_limit = RateLimit(
            limit=number("X-RateLimit-Limit"),
            remaining=number("X-RateLimit-Remaining"),
            reset=number("X-RateLimit-Reset"),
            resource=response.headers.get("X-RateLimit-Resource", ""),
        )

    def _raise_api_error(
        self,
        response: requests.Response,
        required_permission: str,
        *,
        secret: str = "",
    ) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        message = _safe_diagnostic(
            str(payload.get("message") or response.reason or "درخواست رد شد"),
            maximum=1_000,
        )
        if secret:
            message = message.replace(secret, "[REDACTED]")
        if (
            response.status_code in {403, 429}
            and response.headers.get("X-RateLimit-Remaining") == "0"
        ):
            message = "سهمیهٔ API تمام شده است؛ تا زمان reset صبر کنید"
        # GitHub's endpoint response is authoritative and can account for
        # alternate accepted permission sets. Fall back to our declaration
        # only when the header is absent.
        accepted = _safe_diagnostic(
            response.headers.get("X-Accepted-GitHub-Permissions", ""), maximum=1_000
        )
        permission = accepted or required_permission
        raise GitHubAPIError(
            response.status_code,
            message,
            documentation_url=_safe_diagnostic(
                str(payload.get("documentation_url") or ""), maximum=2_048
            ),
            required_permission=permission,
            request_id=_safe_diagnostic(
                response.headers.get("X-GitHub-Request-Id", ""), maximum=256
            ),
        )


def _serialize_json_request(value: Any) -> bytes:
    """Return the exact bounded UTF-8 body that will be sent on the wire."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AssistantError("بدنهٔ درخواست GitHub باید JSON معتبر باشد") from exc
    if len(encoded) > _MAX_JSON_REQUEST_BYTES:
        raise AssistantError("بدنهٔ درخواست GitHub از سقف ۲ مگابایت بزرگ‌تر است")
    return encoded


def _safe_diagnostic(value: str, *, maximum: int) -> str:
    if not value:
        return ""
    cleaned = "".join(
        character if ord(character) >= 0x20 and ord(character) != 0x7F else " "
        for character in value[:maximum]
    )
    return cleaned.strip()


def compact_json(value: Any) -> str:
    """Stable agent-facing JSON without leaking transport objects."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
