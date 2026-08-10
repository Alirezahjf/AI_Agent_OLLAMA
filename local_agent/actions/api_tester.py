"""API Tester — send HTTP requests and inspect responses (like Postman).

All actions are SAFE (read-only network calls from the user's perspective,
though the requests themselves may be write operations to external APIs).
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from ..core.errors import AssistantError
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_api_tester(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="api_request",
        description=(
            "ارسال HTTP request به یک URL و نمایش response (مثل Postman). "
            "متدها: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS. SAFE."
        ),
        parameters={
            "url": {"type": "string", "description": "آدرس کامل URL"},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
            "headers": {"type": "object", "description": "HTTP headers (JSON object)"},
            "body": {"type": "string", "description": "request body (JSON string یا متن خام)"},
            "auth_type": {"type": "string", "enum": ["none", "bearer", "basic", "api_key"]},
            "auth_value": {"type": "string", "description": "توکن/کلید (bearer token, basic base64, api key)"},
            "auth_header": {"type": "string", "description": "نام header برای api_key (پیش‌فرض: X-API-Key)"},
            "timeout": {"type": "integer", "description": "حداکثر ثانیه (پیش‌فرض 30)"},
            "follow_redirects": {"type": "boolean"},
            "parse_json": {"type": "boolean", "description": "تلاش برای parse کردن JSON response"},
        },
        required=("url",),
    )(api_request)

    registry.decorator(
        name="api_test_endpoint",
        description=(
            "تست کامل یک API endpoint: ارسال request، بررسی status code، "
            "اندازه‌گیری زمان پاسخ، اعتبارسنجی JSON response. SAFE."
        ),
        parameters={
            "url": {"type": "string"},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
            "headers": {"type": "object"},
            "body": {"type": "string"},
            "expected_status": {"type": "integer", "description": "status code مورد انتظار"},
            "expected_fields": {"type": "array", "items": {"type": "string"},
                                "description": "فیلدهایی که باید در JSON response باشند"},
            "auth_type": {"type": "string", "enum": ["none", "bearer", "basic", "api_key"]},
            "auth_value": {"type": "string"},
        },
        required=("url",),
    )(api_test_endpoint)

    registry.decorator(
        name="api_benchmark",
        description=(
            "بنچمارک یک API endpoint: چندبار درخواست می‌فرستد و "
            "min/max/avg/p95 زمان پاسخ را گزارش می‌کند. SAFE."
        ),
        parameters={
            "url": {"type": "string"},
            "method": {"type": "string", "enum": ["GET", "POST"]},
            "headers": {"type": "object"},
            "body": {"type": "string"},
            "iterations": {"type": "integer", "description": "تعداد تکرار (پیش‌فرض 5، حداکثر 20)"},
            "auth_type": {"type": "string", "enum": ["none", "bearer", "api_key"]},
            "auth_value": {"type": "string"},
        },
        required=("url",),
    )(api_benchmark)


# ===========================================================================
# Helpers
# ===========================================================================


def _build_headers(
    headers: dict | None,
    auth_type: str = "none",
    auth_value: str = "",
    auth_header: str = "",
) -> dict[str, str]:
    """Merge user headers with auth."""
    h = dict(headers or {})
    at = str(auth_type or "none").lower()
    av = str(auth_value or "").strip()

    if at == "bearer" and av:
        h["Authorization"] = f"Bearer {av}"
    elif at == "basic" and av:
        h["Authorization"] = f"Basic {av}"
    elif at == "api_key" and av:
        key_name = str(auth_header or "X-API-Key").strip()
        h[key_name] = av

    return h


def _parse_body(body: str | None) -> tuple[Any, str | None]:
    """Parse body string → (parsed_data, content_type_hint)."""
    if not body:
        return None, None
    text = str(body).strip()
    # Try JSON
    try:
        return json.loads(text), "application/json"
    except (json.JSONDecodeError, ValueError):
        pass
    return text, "text/plain"


def _format_response(resp: requests.Response, parse_json: bool = True,
                     max_body: int = 5000) -> str:
    """Format an HTTP response into readable text."""
    elapsed_ms = resp.elapsed.total_seconds() * 1000
    lines = [
        f"📡 {resp.request.method} {resp.url}",
        f"  Status: {resp.status_code} {resp.reason}",
        f"  Time: {elapsed_ms:.0f}ms",
        f"  Size: {len(resp.content):,} bytes",
    ]

    # Response headers (important ones)
    important_headers = [
        "content-type", "content-length", "x-ratelimit-remaining",
        "x-ratelimit-limit", "x-request-id", "server", "date",
        "cache-control", "etag", "location",
    ]
    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    header_lines = []
    for h in important_headers:
        if h in resp_headers:
            header_lines.append(f"    {h}: {resp_headers[h]}")
    if header_lines:
        lines.append("  Headers:")
        lines.extend(header_lines)

    # Body
    content_type = resp.headers.get("content-type", "")
    body_text = ""

    if parse_json and "json" in content_type:
        try:
            data = resp.json()
            body_text = json.dumps(data, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            body_text = resp.text
    elif "text" in content_type or "html" in content_type or "xml" in content_type:
        body_text = resp.text
    else:
        body_text = f"(binary: {content_type}, {len(resp.content)} bytes)"

    if body_text:
        if len(body_text) > max_body:
            body_text = body_text[:max_body] + f"\n… ({len(body_text) - max_body} chars truncated)"
        lines.append(f"\n  Body:\n{body_text}")

    return "\n".join(lines)


# ===========================================================================
# Implementations
# ===========================================================================


@risk(Risk.SAFE)
def api_request(*, url: str, method: str = "GET",
                headers: dict | None = None, body: str = "",
                auth_type: str = "none", auth_value: str = "",
                auth_header: str = "", timeout: int = 30,
                follow_redirects: bool = True,
                parse_json: bool = True,
                context: ActionContext) -> str:
    """Send an HTTP request and return the formatted response."""
    target_url = str(url).strip()
    if not target_url:
        raise AssistantError("URL خالی است")
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    m = str(method or "GET").upper()
    t = max(5, min(int(timeout or 30), 120))

    h = _build_headers(headers, auth_type, auth_value, auth_header)
    parsed_body, content_hint = _parse_body(body)
    if content_hint and "Content-Type" not in h and "content-type" not in h:
        h["Content-Type"] = content_hint

    try:
        resp = requests.request(
            method=m,
            url=target_url,
            headers=h,
            json=parsed_body if content_hint == "application/json" else None,
            data=parsed_body if content_hint != "application/json" and parsed_body else None,
            timeout=t,
            allow_redirects=follow_redirects,
        )
    except requests.Timeout:
        return f"⏱️ Timeout: درخواست بعد از {t} ثانیه پاسخ نداد."
    except requests.ConnectionError as exc:
        return f"❌ Connection Error: {exc}"
    except requests.RequestException as exc:
        return f"❌ Request Error: {exc}"

    return _format_response(resp, parse_json=parse_json)


@risk(Risk.SAFE)
def api_test_endpoint(*, url: str, method: str = "GET",
                      headers: dict | None = None, body: str = "",
                      expected_status: int = 0,
                      expected_fields: list[str] | None = None,
                      auth_type: str = "none", auth_value: str = "",
                      context: ActionContext) -> str:
    """Test an API endpoint with validation."""
    target_url = str(url).strip()
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    m = str(method or "GET").upper()
    h = _build_headers(headers, auth_type, auth_value)
    parsed_body, content_hint = _parse_body(body)
    if content_hint and "Content-Type" not in h:
        h["Content-Type"] = content_hint

    start = time.time()
    try:
        resp = requests.request(
            method=m, url=target_url, headers=h,
            json=parsed_body if content_hint == "application/json" else None,
            data=parsed_body if content_hint != "application/json" and parsed_body else None,
            timeout=30,
        )
    except requests.RequestException as exc:
        return f"❌ FAIL: Connection error — {exc}"
    elapsed_ms = (time.time() - start) * 1000

    lines = [f"🧪 API Test: {m} {target_url}"]
    passed = True

    # Status check
    exp_status = int(expected_status or 0)
    if exp_status:
        if resp.status_code == exp_status:
            lines.append(f"  ✅ Status: {resp.status_code} (expected {exp_status})")
        else:
            lines.append(f"  ❌ Status: {resp.status_code} (expected {exp_status})")
            passed = False
    else:
        ok = 200 <= resp.status_code < 400
        icon = "✅" if ok else "❌"
        lines.append(f"  {icon} Status: {resp.status_code} {resp.reason}")
        if not ok:
            passed = False

    lines.append(f"  ⏱️ Time: {elapsed_ms:.0f}ms")
    lines.append(f"  📦 Size: {len(resp.content):,} bytes")

    # JSON field validation
    fields = expected_fields or []
    if fields:
        try:
            data = resp.json()
            lines.append("  📋 Field validation:")
            for field in fields:
                # Support nested fields with dot notation
                parts = field.split(".")
                current = data
                found = True
                for part in parts:
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    else:
                        found = False
                        break
                if found:
                    val_preview = str(current)[:80]
                    lines.append(f"    ✅ {field} = {val_preview}")
                else:
                    lines.append(f"    ❌ {field} — not found")
                    passed = False
        except (json.JSONDecodeError, ValueError):
            lines.append("  ❌ Response is not valid JSON (cannot validate fields)")
            passed = False

    # Summary
    result = "✅ PASS" if passed else "❌ FAIL"
    lines.insert(1, f"  Result: {result}")

    # Include body on failure
    if not passed:
        body_text = resp.text[:2000] if resp.text else "(empty)"
        lines.append(f"\n  Response body:\n{body_text}")

    return "\n".join(lines)


@risk(Risk.SAFE)
def api_benchmark(*, url: str, method: str = "GET",
                  headers: dict | None = None, body: str = "",
                  iterations: int = 5,
                  auth_type: str = "none", auth_value: str = "",
                  context: ActionContext) -> str:
    """Benchmark an API endpoint with multiple requests."""
    target_url = str(url).strip()
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    m = str(method or "GET").upper()
    n = max(2, min(int(iterations or 5), 20))
    h = _build_headers(headers, auth_type, auth_value)
    parsed_body, content_hint = _parse_body(body)
    if content_hint and "Content-Type" not in h:
        h["Content-Type"] = content_hint

    times: list[float] = []
    statuses: list[int] = []
    errors = 0

    for i in range(n):
        start = time.time()
        try:
            resp = requests.request(
                method=m, url=target_url, headers=h,
                json=parsed_body if content_hint == "application/json" else None,
                data=parsed_body if content_hint != "application/json" and parsed_body else None,
                timeout=30,
            )
            elapsed = (time.time() - start) * 1000
            times.append(elapsed)
            statuses.append(resp.status_code)
        except requests.RequestException:
            errors += 1
            times.append(-1)

    valid_times = [t for t in times if t >= 0]
    if not valid_times:
        return f"❌ همهٔ {n} درخواست با خطا مواجه شدند."

    valid_times.sort()
    avg = sum(valid_times) / len(valid_times)
    p95_idx = int(len(valid_times) * 0.95)
    p95 = valid_times[min(p95_idx, len(valid_times) - 1)]

    # Status distribution
    from collections import Counter
    status_counts = Counter(statuses)

    lines = [
        f"📊 Benchmark: {m} {target_url}",
        f"  Requests: {n} | Success: {len(valid_times)} | Errors: {errors}",
        f"",
        f"  ⏱️ Response Times:",
        f"    Min: {min(valid_times):.0f}ms",
        f"    Max: {max(valid_times):.0f}ms",
        f"    Avg: {avg:.0f}ms",
        f"    P95: {p95:.0f}ms",
        f"",
        f"  📋 Status Codes:",
    ]
    for code, count in sorted(status_counts.items()):
        lines.append(f"    {code}: {count}x")

    # Individual results
    lines.append(f"\n  📝 Individual:")
    for i, (t, s) in enumerate(zip(times, statuses), 1):
        if t >= 0:
            lines.append(f"    #{i}: {t:.0f}ms ({s})")
        else:
            lines.append(f"    #{i}: ❌ error")

    return "\n".join(lines)
