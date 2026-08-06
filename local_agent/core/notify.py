"""اعلان دسکتاپ (best-effort و cross-platform).

* ویندوز: plyer (اگر نصب باشد) و بعد win10toast؛ در غیر این صورت فقط لاگ.
* لینوکس: ``notify-send`` اگر موجود باشد.
* هیچ‌وقت raise نمی‌کند — اعلان شکست‌خورده نباید کاری را خراب کند.

روی ویندوز ۱۱ واقعی باید تأیید شود (toast واقعی ممکن است به پکیج بومی
یا نسخهٔ ویندوز وابسته باشد)؛ تست‌ها آن را mock می‌کنند.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from ..core.logging_setup import get_logger

logger = get_logger("notify")


def notify_desktop(title: str, message: str) -> None:
    """یک اعلان دسکتاپ نشان می‌دهد؛ هر خطایی را فقط لاگ می‌کند."""
    try:
        if os.name == "nt":
            _notify_windows(title, message)
        else:
            _notify_posix(title, message)
    except Exception as exc:  # noqa: BLE001 - best-effort notification
        logger.debug("desktop notification failed: %s", exc)


def _notify_windows(title: str, message: str) -> None:
    try:
        from plyer import notification  # type: ignore

        notification.notify(title=title, message=message, timeout=10)
        return
    except ImportError:
        pass
    try:
        from win10toast import ToastNotifier  # type: ignore

        ToastNotifier().show_toast(title, message, duration=10)
        return
    except ImportError:
        pass
    logger.info("desktop toast: %s — %s", title, message)


def _notify_posix(title: str, message: str) -> None:
    binary = shutil.which("notify-send")
    if binary is None:
        logger.info("desktop notification (fallback): %s — %s", title, message)
        return
    result = subprocess.run(
        [binary, "--app-name=LocalAssistant", title, message],
        capture_output=True, timeout=10, check=False,
    )
    if result.returncode != 0:
        logger.debug("notify-send failed: %s", result.stderr.decode("utf-8", "replace")[:200])


def _win32_toast_available() -> bool:
    """برای تست: آیا یک toast واقعی ویندوز ممکن است؟ (نیازمند تأیید ویندوز ۱۱)"""
    if os.name != "nt":
        return False
    try:
        import plyer  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import win10toast  # noqa: F401
        return True
    except ImportError:
        pass
    return False


def notify_platform_hint() -> str:
    """توضیح فارسی وضعیت اعلان دسکتاپ برای doctor/گزارش."""
    if os.name == "nt":
        if _win32_toast_available():
            return "اعلان دسکتاپ ویندوز: plyer/win10toast آماده است"
        return "اعلان دسکتاپ ویندوز: plyer یا win10toast نصب نیست؛ فقط لاگ می‌شود"
    if shutil.which("notify-send"):
        return "اعلان دسکتاپ: notify-send در دسترس است"
    return "اعلان دسکتاپ: notify-send نیست؛ فقط لاگ می‌شود"


__all__ = ["notify_desktop", "notify_platform_hint"]
