"""Self-check ("doctor") for the local assistant.

Answers the only question that matters before you start debugging:
*does this installation actually work on this machine?*

Every check is small, isolated and never raises: it returns a
:class:`CheckResult` with a Persian message and a Persian hint on how to
fix the problem.  The same results power three surfaces:

* ``python -m local_agent.diagnostics``  — a coloured terminal report
* ``GET /api/doctor``                    — the web/desktop "سلامت سیستم" panel
* ``/doctor`` in the Telegram/Bale bot   — a remote health report

The checks are ordered from "cheap and local" to "talks to the network".
"""

from __future__ import annotations

import json
import platform
import shutil
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core.config import AssistantSettings, load_settings

OK = "ok"
WARN = "warn"
FAIL = "fail"

_ICONS = {OK: "✅", WARN: "⚠️", FAIL: "❌"}


@dataclass
class CheckResult:
    """The outcome of a single diagnostic check."""

    name: str            # machine-readable id, e.g. "llm.reachable"
    title: str           # Persian, human-readable
    status: str          # ok | warn | fail
    detail: str = ""     # Persian, one line
    hint: str = ""       # Persian, how to fix it
    data: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def icon(self) -> str:
        return _ICONS.get(self.status, "•")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "data": self.data,
            "duration_ms": self.duration_ms,
        }

    def line(self) -> str:
        text = f"{self.icon} {self.title} — {self.detail}" if self.detail else f"{self.icon} {self.title}"
        if self.hint and self.status != OK:
            text += f"\n     ↳ {self.hint}"
        return text


@dataclass
class DoctorReport:
    """The full set of checks plus a roll-up verdict."""

    results: list[CheckResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    @property
    def status(self) -> str:
        if any(r.status == FAIL for r in self.results):
            return FAIL
        if any(r.status == WARN for r in self.results):
            return WARN
        return OK

    @property
    def summary(self) -> str:
        counts = {s: sum(1 for r in self.results if r.status == s) for s in (OK, WARN, FAIL)}
        return (
            f"{counts[OK]} سالم · {counts[WARN]} هشدار · {counts[FAIL]} خطا"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "results": [r.to_dict() for r in self.results],
            "generated_at": self.started_at,
        }

    def render(self) -> str:
        head = "🩺 بررسی سلامت دستیار محلی"
        body = "\n".join(r.line() for r in self.results)
        verdict = {
            OK: "همه‌چیز آمادهٔ کار است.",
            WARN: "کار می‌کند، ولی چند نکته را بهتر است درست کنید.",
            FAIL: "دستیار در وضعیت فعلی درست کار نمی‌کند؛ موارد ❌ را برطرف کنید.",
        }[self.status]
        return f"{head}\n{'─' * 46}\n{body}\n{'─' * 46}\n{self.summary}\n{verdict}"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _timed(fn: Callable[[], CheckResult]) -> CheckResult:
    start = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - a check must never crash the report
        result = CheckResult(
            name=getattr(fn, "__name__", "unknown"),
            title="بررسی ناموفق",
            status=FAIL,
            detail=str(exc)[:200],
            hint="این یک خطای غیرمنتظره است؛ لطفاً لاگ را بررسی کنید.",
        )
    result.duration_ms = int((time.perf_counter() - start) * 1000)
    return result


def check_python() -> CheckResult:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 11):
        return CheckResult(
            "python.version", "نسخهٔ پایتون", FAIL,
            f"پایتون {version} پیدا شد",
            "این پروژه به پایتون ۳٫۱۱ یا بالاتر نیاز دارد.",
            {"version": version},
        )
    return CheckResult(
        "python.version", "نسخهٔ پایتون", OK, f"پایتون {version}", "", {"version": version}
    )


def check_platform() -> CheckResult:
    from .utils.platform import capabilities, current_platform

    caps = capabilities()
    name = platform.platform()
    plat = current_platform().value
    if plat == "windows":
        return CheckResult("platform", "سیستم‌عامل", OK, f"ویندوز ({name})", "", caps)
    return CheckResult(
        "platform", "سیستم‌عامل", WARN,
        f"{plat} — این دستیار برای ویندوز ساخته شده",
        "ابزارهای مخصوص ویندوز (پنجره‌ها، تسک‌منیجر، تلگرام دسکتاپ) در دسترس نخواهند بود.",
        caps,
    )


def check_dependencies() -> CheckResult:
    """Report missing packages grouped by the feature they unlock.

    Only the packages the assistant genuinely cannot start without are
    fatal.  Everything else is reported as a warning naming the *extra*
    that installs it, so the hint is a command the user can paste.
    """
    import importlib.util

    required = {"requests": "requests", "PIL": "Pillow", "dotenv": "python-dotenv"}
    # module -> (pip name, extra that provides it)
    optional = {
        "fastapi": ("fastapi", "web"),
        "uvicorn": ("uvicorn", "web"),
        "pydantic": ("pydantic", "web"),
        "pyautogui": ("pyautogui", "desktop"),
        "mss": ("mss", "desktop"),
        "telethon": ("telethon", "desktop"),
        "pyperclip": ("pyperclip", "desktop"),
        "rich": ("rich", "desktop"),
        "telegram": ("python-telegram-bot", "desktop"),
        "webview": ("pywebview", "app"),
        "pystray": ("pystray", "app"),
    }

    missing = [pkg for mod, pkg in required.items() if importlib.util.find_spec(mod) is None]
    absent: dict[str, list[str]] = {}
    for module, (pkg, extra) in optional.items():
        if importlib.util.find_spec(module) is None:
            absent.setdefault(extra, []).append(pkg)

    data = {"missing_required": missing, "missing_optional": absent}
    if missing:
        return CheckResult(
            "deps", "وابستگی‌ها", FAIL,
            "بستهٔ ضروری غایب: " + "، ".join(missing),
            f"نصب کنید: {sys.executable} -m pip install " + " ".join(missing),
            data,
        )
    if absent:
        flat = [pkg for pkgs in absent.values() for pkg in pkgs]
        # The web UI cannot start at all without these three.
        blocking = absent.get("web", [])
        detail = "بسته‌های غایب: " + "، ".join(flat)
        if blocking:
            return CheckResult(
                "deps", "وابستگی‌ها", FAIL,
                detail + f" — بدون {'، '.join(blocking)} رابط وب و اپ دسکتاپ اجرا نمی‌شوند",
                'نصب کنید: pip install -e ".[all]"  (یا: python local_agent_setup.py install-all)',
                data,
            )
        extras = ",".join(sorted(absent))
        return CheckResult(
            "deps", "وابستگی‌ها", WARN, detail,
            f'نصب کنید: pip install -e ".[{extras}]"',
            data,
        )
    return CheckResult("deps", "وابستگی‌ها", OK, "همهٔ بسته‌ها نصب‌اند", "", data)


def check_packaging() -> CheckResult:
    """Verify ``pip install -e .`` can actually resolve this project.

    A flat layout with several top-level directories (``agent``,
    ``local_agent``, ``tests_local_agent``) makes setuptools bail out with
    "Multiple top-level packages discovered in a flat-layout" unless the
    packages are listed explicitly.  We check the declaration rather than
    shelling out to pip so the check stays instant and offline.
    """
    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return CheckResult(
            "packaging", "بسته‌بندی پروژه", WARN, "pyproject.toml پیدا نشد",
            "احتمالاً پروژه به‌صورت نصب‌شده اجرا می‌شود؛ معمولاً مشکلی نیست.",
        )
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult("packaging", "بسته‌بندی پروژه", WARN, str(exc)[:120], "")

    problems: list[str] = []
    if "[build-system]" not in text:
        problems.append("بخش [build-system] تعریف نشده")
    explicit = (
        "[tool.setuptools.packages.find]" in text
        or "[tool.setuptools]" in text
        or "packages =" in text
    )
    top_level = sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and (entry / "__init__.py").is_file() and not entry.name.startswith(".")
    )
    if len(top_level) > 1 and not explicit:
        problems.append(
            "چند پکیج سطح‌بالا (" + "، ".join(top_level) + ") بدون تعیین صریح packages"
        )
    data = {"top_level": top_level, "explicit_packages": explicit}
    if problems:
        return CheckResult(
            "packaging", "بسته‌بندی پروژه", FAIL, "؛ ".join(problems),
            "در pyproject.toml بخش [tool.setuptools.packages.find] را با "
            'include = ["agent*", "local_agent*"] اضافه کنید، وگرنه '
            "pip install -e . با خطای flat-layout شکست می‌خورد.",
            data,
        )
    return CheckResult(
        "packaging", "بسته‌بندی پروژه", OK, "pip install -e . قابل اجراست", "", data
    )


def check_paths(settings: AssistantSettings) -> CheckResult:
    problems: list[str] = []
    for label, path in (("پوشهٔ داده", settings.data_dir), ("پوشهٔ کاری", settings.work_dir)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".doctor_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(f"{label} ({path}): {exc}")
    data = {"data_dir": str(settings.data_dir), "work_dir": str(settings.work_dir)}
    if problems:
        return CheckResult(
            "paths", "مسیرها", FAIL, "؛ ".join(problems),
            "دسترسی نوشتن به این پوشه‌ها را بررسی کنید یا مسیر دیگری در config.json بگذارید.",
            data,
        )
    return CheckResult(
        "paths", "مسیرها", OK,
        f"داده: {settings.data_dir} · کاری: {settings.work_dir}", "", data,
    )


def check_config(settings: AssistantSettings) -> CheckResult:
    path = settings.config_path
    data = {"config_path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return CheckResult(
            "config", "فایل تنظیمات", WARN, f"{path} هنوز ساخته نشده",
            "با اولین اجرا ساخته می‌شود؛ یا از تنظیمات رابط وب ذخیره بزنید.", data,
        )
    from .core.config import _read_json

    try:
        payload = _read_json(path)
        if not payload:
            raise ValueError("فایل خالی یا بدون محتوای JSON است")
    except (OSError, ValueError) as exc:
        return CheckResult(
            "config", "فایل تنظیمات", FAIL, f"خواندن {path} ممکن نشد: {exc}",
            "فایل را اصلاح کنید یا حذفش کنید تا از نو ساخته شود.", data,
        )
    data["keys"] = sorted(payload)
    return CheckResult("config", "فایل تنظیمات", OK, str(path), "", data)


def check_llm_config(settings: AssistantSettings) -> CheckResult:
    llm = settings.llm
    data = {"provider": llm.provider, "model": llm.openai_model or llm.ollama_model}
    if llm.provider == "openai_compatible":
        if not llm.openai_api_key:
            return CheckResult(
                "llm.config", "تنظیمات مدل", FAIL, "کلید API خالی است",
                "در تنظیمات، کلید AvalAI خود را وارد کنید.", data,
            )
        if not llm.openai_base_url:
            return CheckResult(
                "llm.config", "تنظیمات مدل", FAIL, "آدرس پایهٔ API خالی است",
                "مثلاً https://api.avalai.ir/v1 را وارد کنید.", data,
            )
        return CheckResult(
            "llm.config", "تنظیمات مدل", OK,
            f"{llm.openai_base_url} · مدل {llm.openai_model}", "", data,
        )
    return CheckResult(
        "llm.config", "تنظیمات مدل", OK,
        f"Ollama · مدل {llm.ollama_model}", "", data,
    )


def _probe_tcp(url: str, default_port: int, timeout: float = 2.0) -> bool:
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else default_port)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_llm_reachable(settings: AssistantSettings, *, network: bool = True) -> CheckResult:
    llm = settings.llm
    if not network:
        return CheckResult("llm.reachable", "اتصال به مدل", WARN, "بررسی شبکه رد شد", "", {})
    if llm.provider == "ollama":
        if _probe_tcp(llm.ollama_base_url, 11434):
            return CheckResult("llm.reachable", "اتصال به مدل", OK, f"Ollama روی {llm.ollama_base_url}")
        hint = (
            "Ollama را اجرا کنید (ollama serve) یا در تنظیمات به «سازگار با OpenAI» "
            "سوئیچ کنید و کلید AvalAI را وارد نمایید."
        )
        return CheckResult(
            "llm.reachable", "اتصال به مدل", FAIL,
            f"Ollama در {llm.ollama_base_url} پاسخ نمی‌دهد", hint,
        )

    from .llm import create_client

    try:
        client = create_client(llm)
        models = client.list_models()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "llm.reachable", "اتصال به مدل", FAIL,
            f"اتصال ناموفق: {str(exc)[:160]}",
            "آدرس پایه، کلید API و دسترسی اینترنت را بررسی کنید.",
        )
    if not models:
        return CheckResult(
            "llm.reachable", "اتصال به مدل", WARN,
            "سرویس پاسخ داد ولی فهرست مدل‌ها خالی بود",
            "ممکن است ارائه‌دهنده endpoint مدل‌ها را پشتیبانی نکند؛ معمولاً مشکلی نیست.",
            {"models": []},
        )
    active = llm.openai_model
    detail = f"{len(models)} مدل در دسترس"
    if active and active not in models:
        return CheckResult(
            "llm.reachable", "اتصال به مدل", WARN,
            f"{detail}، ولی «{active}» بینشان نیست",
            "نام مدل را از فهرست تنظیمات انتخاب کنید.",
            {"models": models[:50], "active": active},
        )
    return CheckResult(
        "llm.reachable", "اتصال به مدل", OK, detail, "", {"models": models[:50], "active": active}
    )


def check_actions(settings: AssistantSettings) -> CheckResult:
    from .actions import build_default_registry
    from .actions.registry import ActionContext, ConfirmationGate
    from .automation import register_gui
    from .core.context import RuntimeContext

    runtime = RuntimeContext(settings)
    context = ActionContext(
        runtime=runtime,
        confirmation_gate=ConfirmationGate(settings.safety),
        work_dir=settings.work_dir,
    )
    registry = build_default_registry(context)
    register_gui(registry, context)
    actions = registry.all()
    unavailable = [a.name for a in actions if getattr(a, "unavailable", False)]
    data = {"total": len(actions), "unavailable": unavailable}
    if not actions:
        return CheckResult("actions", "ابزارها", FAIL, "هیچ ابزاری ثبت نشد",
                           "نصب پروژه ناقص است؛ دوباره pip install -e . بزنید.", data)
    if unavailable:
        return CheckResult(
            "actions", "ابزارها", WARN,
            f"{len(actions)} ابزار ثبت شد، {len(unavailable)} تای آن‌ها در این محیط کار نمی‌کنند",
            "این ابزارها به محیط گرافیکی ویندوز نیاز دارند: " + "، ".join(unavailable[:8]),
            data,
        )
    return CheckResult("actions", "ابزارها", OK, f"{len(actions)} ابزار آماده است", "", data)


def check_screenshot(settings: AssistantSettings) -> CheckResult:
    try:
        from .automation.screenshot import take_screenshot

        image = take_screenshot()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "screenshot", "اسکرین‌شات", FAIL, str(exc)[:160],
            "نصب کنید: pip install mss Pillow",
        )
    data = {"width": getattr(image, "width", 0), "height": getattr(image, "height", 0),
            "backend": getattr(image, "backend", "unknown")}
    if data["backend"] == "placeholder":
        return CheckResult(
            "screenshot", "اسکرین‌شات", WARN, "تصویر واقعی گرفته نشد (تصویر جایگزین)",
            "روی سرور بدون نمایشگر طبیعی است؛ روی ویندوز mss را نصب کنید: pip install mss",
            data,
        )
    if data["width"] <= 1 or data["height"] <= 1:
        return CheckResult("screenshot", "اسکرین‌شات", WARN, "تصویر خالی برگشت",
                           "روی سرور بدون نمایشگر طبیعی است.", data)
    return CheckResult(
        "screenshot", "اسکرین‌شات", OK,
        f"{data['width']}×{data['height']} از طریق {data['backend']}", "", data,
    )


def _is_our_web_server(port: int, timeout: float = 1.5) -> bool:
    """Best-effort: is the process listening on ``127.0.0.1:port`` ours?

    Probes our own ``/healthz`` endpoint (``{"ok": true, ...}``) and the
    page title.  Anything else — another program, a proxy, a firewall —
    answers False so the user gets an honest warning instead of a wrong
    diagnosis.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(
                b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            data = sock.recv(512).decode("utf-8", "replace")
        return '"ok"' in data and "true" in data.lower()
    except OSError:
        return False


def check_port(settings: AssistantSettings, port: int = 7824) -> CheckResult:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            if _is_our_web_server(port):
                return CheckResult(
                    "port", "پورت رابط وب", OK,
                    f"پورت {port} توسط همین دستیار در حال استفاده است",
                    "", {"port": port, "ours": True},
                )
            return CheckResult(
                "port", "پورت رابط وب", WARN, f"پورت {port} مشغول است",
                "یا دستیار از قبل باز است، یا با --port پورت دیگری بدهید.",
                {"port": port, "ours": False},
            )
    return CheckResult(
        "port", "پورت رابط وب", OK, f"پورت {port} آزاد است", "", {"port": port, "ours": False}
    )


def check_interpreter() -> CheckResult:
    """Is the assistant running inside a virtual environment?

    A venv that exists next to the project but is *not* activated is a
    classic Windows trap (double-clicking the wrong python), so that
    case is reported as FAIL with a fix hint.
    """
    prefix = getattr(sys, "prefix", "")
    base_prefix = getattr(sys, "base_prefix", "")
    data = {"venv": prefix != base_prefix, "prefix": prefix, "base_prefix": base_prefix}
    if prefix != base_prefix:
        return CheckResult(
            "interpreter", "محیط پایتون", OK,
            f"محیط مجازی فعال است ({prefix})", "", data,
        )
    root = Path(__file__).resolve().parent.parent
    for candidate in (root, root.parent):
        for name in (".venv", "venv"):
            if (candidate / name).is_dir():
                return CheckResult(
                    "interpreter", "محیط پایتون", FAIL,
                    "محیط مجازی (venv) ساخته شده ولی فعال نیست",
                    "قبل از اجرای دستیار، محیط مجازی را فعال کنید "
                    "(مثلاً .venv\\Scripts\\activate روی ویندوز).",
                    data,
                )
    return CheckResult(
        "interpreter", "محیط پایتون", WARN,
        "محیط مجازی فعال نیست",
        "توصیه می‌شود دستیار داخل یک venv اجرا شود تا وابستگی‌ها ایزوله بمانند.",
        data,
    )


def check_encoding() -> CheckResult:
    """Is stdout UTF-8?  Persian text garbles (mojibake) on cp720/cp1256."""
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    data = {"encoding": encoding}
    if encoding in {"utf8", "utf8sig"}:
        return CheckResult("encoding", "رمزگذاری خروجی", OK, "UTF-8 فعال است", "", data)
    return CheckResult(
        "encoding", "رمزگذاری خروجی", WARN,
        f"رمزگذاری فعلی: {encoding or 'نامشخص'}",
        "در PowerShell: [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 "
        "سپس chcp 65001 (OutputEncoding را UTF-8 کنید).",
        data,
    )


def check_desktop() -> CheckResult:
    import importlib.util

    from .utils.platform import capabilities

    caps = capabilities()
    has_webview = importlib.util.find_spec("webview") is not None
    has_tray = importlib.util.find_spec("pystray") is not None
    data = {"pywebview": has_webview, "pystray": has_tray, **caps}
    if not caps.get("gui"):
        return CheckResult(
            "desktop", "اپ دسکتاپ", WARN, "نمایشگری پیدا نشد؛ فقط حالت مرورگر/سرور کار می‌کند",
            "روی ویندوز این هشدار نباید ظاهر شود.", data,
        )
    if not has_webview:
        return CheckResult(
            "desktop", "اپ دسکتاپ", WARN, "pywebview نصب نیست؛ رابط در مرورگر باز می‌شود",
            "برای پنجرهٔ بومی: pip install pywebview pystray", data,
        )
    if not has_tray:
        return CheckResult(
            "desktop", "اپ دسکتاپ", WARN, "pystray نصب نیست؛ آیکون نوار وظیفه غیرفعال است",
            "نصب کنید: pip install pystray", data,
        )
    return CheckResult("desktop", "اپ دسکتاپ", OK, "پنجرهٔ بومی و نوار وظیفه آماده‌اند", "", data)


def check_bots(settings: AssistantSettings) -> CheckResult:
    data = {
        "telegram_bot": bool(settings.telegram_token),
        "bale_bot": bool(settings.bale_token),
        "telegram_userbot": settings.telegram.enabled,
        "allowlist": len(settings.allowed_user_ids),
    }
    if not (settings.telegram_token or settings.bale_token):
        return CheckResult("bots", "ربات‌ها", WARN, "هیچ توکن رباتی تنظیم نشده",
                           "اختیاری است؛ برای کنترل از راه دور توکن را در config.json بگذارید.", data)
    if not settings.allowed_user_ids:
        return CheckResult(
            "bots", "ربات‌ها", FAIL, "توکن ربات هست ولی فهرست کاربران مجاز خالی است",
            "خطر امنیتی: allowed_user_ids را با شناسهٔ عددی خودتان پر کنید.", data,
        )
    return CheckResult("bots", "ربات‌ها", OK,
                       f"{len(settings.allowed_user_ids)} کاربر مجاز", "", data)


def check_disk(settings: AssistantSettings) -> CheckResult:
    try:
        usage = shutil.disk_usage(settings.data_dir)
    except OSError as exc:
        return CheckResult("disk", "فضای دیسک", WARN, str(exc)[:120], "", {})
    free_gb = usage.free / (1024 ** 3)
    data = {"free_gb": round(free_gb, 1)}
    if free_gb < 1:
        return CheckResult("disk", "فضای دیسک", FAIL, f"تنها {free_gb:.1f} گیگابایت آزاد است",
                           "فضای دیسک را آزاد کنید.", data)
    if free_gb < 5:
        return CheckResult("disk", "فضای دیسک", WARN, f"{free_gb:.1f} گیگابایت آزاد", "", data)
    return CheckResult("disk", "فضای دیسک", OK, f"{free_gb:.1f} گیگابایت آزاد", "", data)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_checks(
    settings: AssistantSettings | None = None,
    *,
    network: bool = True,
    port: int = 7824,
) -> DoctorReport:
    """Run every check and return the aggregated report."""
    settings = settings or load_settings()
    report = DoctorReport()
    checks: list[Callable[[], CheckResult]] = [
        check_python,
        check_platform,
        check_interpreter,
        check_encoding,
        check_dependencies,
        check_packaging,
        lambda: check_paths(settings),
        lambda: check_config(settings),
        lambda: check_llm_config(settings),
        lambda: check_llm_reachable(settings, network=network),
        lambda: check_actions(settings),
        lambda: check_screenshot(settings),
        lambda: check_port(settings, port),
        check_desktop,
        lambda: check_bots(settings),
        lambda: check_disk(settings),
    ]
    for check in checks:
        report.results.append(_timed(check))
    return report


def main(argv: list[str] | None = None) -> int:
    """``python -m local_agent.diagnostics`` entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="local-assistant-doctor",
        description="بررسی سلامت نصب دستیار محلی",
    )
    parser.add_argument("--json", action="store_true", help="خروجی JSON بده")
    parser.add_argument("--offline", action="store_true", help="بررسی‌های شبکه‌ای را رد کن")
    parser.add_argument("--port", type=int, default=7824, help="پورتی که باید آزاد باشد")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    report = run_checks(network=not args.offline, port=args.port)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render())
    return 0 if report.status != FAIL else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
