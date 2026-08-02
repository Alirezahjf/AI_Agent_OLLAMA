"""Full application purge («پاک‌سازی کامل»).

Erases every trace the assistant leaves on the machine — config, history,
memory, logs, screenshots, tokens, pid/lock files, Telegram session files
and the start-with-system registration — **without** touching anything
that costs internet traffic to reinstall:

  * installed packages (``site-packages``) are NEVER removed,
  * virtual environments are NEVER removed,
  * ``pip cache`` is NEVER purged,
  * the repository checkout itself is never deleted (only its
    ``__pycache__`` / ``.pytest_cache`` style caches, which are not data).

One shared core (:func:`purge_all`) backs three entry points:

  * ``POST /api/purge``  (web UI & desktop, with a two-step confirm button)
  * ``/purge``           (CLI REPL command, typed confirmation)
  * ``--purge``          (CLI & desktop process flag, ``--yes`` to skip)

Every step is best-effort: failures are collected into ``failed`` instead
of raising, and destructive-guard refuses obviously-dangerous ``data_dir``
values (filesystem root or the user's home) before deleting anything.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .config import AssistantSettings
from .logging_setup import get_logger, shutdown_logging


logger = get_logger("core.cleanup")

#: Phrase the CLI asks the user to type before wiping everything.
PURGE_CONFIRM_WORD = "پاک کن"

#: Cache directories we may delete inside the dev repository — never data.
_REPO_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})

#: Directories that must never be traversed while cleaning repo caches:
#: dependencies live here and re-downloading them costs the user's traffic.
_REPO_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "env", ".env", "node_modules", "site-packages", "dist", "build"}
)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class PurgeRefused(Exception):
    """The requested purge target failed a safety check."""


def _resolve_data_dir(data_dir: Path) -> Path:
    """Resolve ``data_dir`` and refuse to wipe a filesystem root / home."""
    resolved = Path(data_dir).expanduser().resolve()
    anchor = Path(resolved.anchor).resolve()
    if resolved == anchor:
        raise PurgeRefused(f"آدرس پوشهٔ داده امن نیست (ریشهٔ درایو): {resolved}")
    try:
        home = Path.home().resolve()
    except OSError:
        home = None
    if home is not None and (resolved == home or resolved == home.parent):
        raise PurgeRefused(f"آدرس پوشهٔ داده امن نیست (پوشهٔ خانگی): {resolved}")
    return resolved


def find_repo_root(start: Path | None = None) -> Path | None:
    """Locate the dev repository root (``pyproject.toml`` next to ``local_agent/``).

    Returns ``None`` for frozen (PyInstaller) or installed (site-packages)
    layouts — cleaning source caches only makes sense in a real checkout.
    """
    here = (start or Path(__file__)).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "local_agent").is_dir():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for ``pid`` (False for any error)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - windows only
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            synchronize = 0x00100000
            handle = kernel32.OpenProcess(synchronize, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        # OverflowError: a stale pid file can hold a value above pid_max.
        return False
    return True


def _reap_if_child(pid: int) -> None:
    """Reap ``pid`` if it is a dead child of *this* process.

    A SIGTERMed child of ours stays visible to ``kill(pid, 0)`` as a zombie
    until it is reaped, which would make the liveness check lie.  For
    unrelated processes (the desktop app the pid file points at) this is a
    no-op — their own parent reaps them.
    """
    if sys.platform == "win32":  # pragma: no cover - windows only
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _terminate_pid(pid: int, *, timeout: float = 3.0) -> bool:
    """Stop ``pid`` gracefully, escalating if needed. True when it is gone."""
    if not _pid_alive(pid):
        return True
    try:
        if sys.platform == "win32":  # pragma: no cover - windows only
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return not _pid_alive(pid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        _reap_if_child(pid)
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    if sys.platform != "win32":
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        _reap_if_child(pid)
        time.sleep(0.3)
    return not _pid_alive(pid)


# ---------------------------------------------------------------------------
# Autostart
# ---------------------------------------------------------------------------


def _default_autostart_disabler() -> bool:
    """Remove the start-with-system registration for this app (Win/Linux)."""
    from ..desktop import autostart

    return autostart.disable()


# ---------------------------------------------------------------------------
# Filesystem deletion helpers
# ---------------------------------------------------------------------------


def _remove_tree(tree_root: Path, *, dry_run: bool = False) -> tuple[list[str], list[dict[str, str]]]:
    """Delete every entry under ``tree_root`` and then the directory itself.

    Returns (removed_paths, failures).  Symlinked directories are unlinked,
    never followed.  When any item fails, the (non-empty) root directory is
    left in place.
    """
    removed: list[str] = []
    failed: list[dict[str, str]] = []
    if not tree_root.is_dir():
        return removed, failed
    try:
        entries = sorted(tree_root.iterdir())
    except OSError as exc:
        return removed, [{"path": str(tree_root), "error": str(exc)}]
    for entry in entries:
        if dry_run:
            removed.append(str(entry))
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed.append(str(entry))
        except OSError as exc:
            failed.append({"path": str(entry), "error": str(exc)})
    if not failed:
        if dry_run:
            removed.append(str(tree_root))
        else:
            try:
                tree_root.rmdir()
                removed.append(str(tree_root))
            except OSError as exc:
                failed.append({"path": str(tree_root), "error": str(exc)})
    return removed, failed


def clean_repo_caches(
    repo_root: Path, *, dry_run: bool = False
) -> tuple[list[str], list[dict[str, str]]]:
    """Remove ``__pycache__`` / ``.pytest_cache`` … inside a *dev* repo.

    Dependency directories (``.venv``/``node_modules``/… ) are never
    traversed, so nothing that needs re-downloading is touched.
    """
    removed: list[str] = []
    failed: list[dict[str, str]] = []
    repo_root = Path(repo_root)
    if not repo_root.is_dir():
        return removed, failed
    for dirpath, dirnames, _filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _REPO_SKIP_DIRS]
        for name in list(dirnames):
            if name not in _REPO_CACHE_DIRS:
                continue
            dirnames.remove(name)
            target = Path(dirpath) / name
            if dry_run:
                removed.append(str(target))
                continue
            try:
                shutil.rmtree(target)
                removed.append(str(target))
            except OSError as exc:
                failed.append({"path": str(target), "error": str(exc)})
    return removed, failed


# ---------------------------------------------------------------------------
# The shared purge core
# ---------------------------------------------------------------------------


def purge_all(
    settings: AssistantSettings,
    *,
    dry_run: bool = False,
    kill_processes: bool = True,
    include_autostart: bool = True,
    include_repo_caches: bool = True,
    close_logging: bool = False,
    autostart_disabler: Callable[[], bool] | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Erase every trace of the assistant except installed packages.

    Parameters
    ----------
    settings:
        The active :class:`AssistantSettings`; only ``data_dir`` matters.
    dry_run:
        Report what *would* be removed without changing anything.
    kill_processes:
        Read ``data_dir/desktop.pid`` and stop the recorded process when it
        is alive and is not *this* process (we never kill ourselves — the
        web/desktop caller exits on its own right after).
    include_autostart:
        Remove the start-with-system registration (Windows Run key or the
        Linux ``~/.config/autostart/*.desktop`` file).
    include_repo_caches:
        Also delete ``__pycache__`` / ``.pytest_cache`` … in the dev repo
        checkout (no-op in installed/frozen layouts).
    close_logging:
        Shut down the assistant's log handlers first, so the running
        process releases ``data_dir/logs/*`` (required on Windows when the
        *current* process is the one being wiped).
    autostart_disabler / repo_root:
        Test hooks — override the autostart removal call / the repo path.

    Returns
    -------
    dict with ``ok``, ``dry_run``, ``data_dir``, ``removed``, ``failed``,
    ``skipped``, ``stopped_pids``, ``autostart_removed``, ``repo_caches``,
    ``warnings`` and a Persian ``message`` summary.  Never raises.
    """
    report: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "data_dir": str(settings.data_dir),
        "removed": [],
        "failed": [],
        "skipped": [],
        "stopped_pids": [],
        "autostart_removed": None,
        "repo_caches": [],
        "warnings": [],
        "message": "",
    }
    try:
        data_dir = _resolve_data_dir(settings.data_dir)
    except PurgeRefused as exc:
        report.update(ok=False, failed=[{"path": str(settings.data_dir), "error": str(exc)}])
        report["message"] = f"❌ پاک‌سازی انجام نشد: {exc}"
        return report
    report["data_dir"] = str(data_dir)

    # 1) Stop the recorded desktop process (never ourselves). -------------
    own_pid = os.getpid()
    if kill_processes:
        pid_file = data_dir / "desktop.pid"
        recorded: int | None = None
        try:
            recorded = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            recorded = None
        if recorded and recorded > 0 and recorded != own_pid and _pid_alive(recorded):
            if dry_run:
                report["stopped_pids"].append(recorded)
            elif _terminate_pid(recorded):
                report["stopped_pids"].append(recorded)
            else:
                report["failed"].append(
                    {"path": str(pid_file), "error": f"متوقف‌کردن فرایند {recorded} ناموفق بود"}
                )
        elif recorded == own_pid:
            report["skipped"].append(
                {"path": str(pid_file), "reason": "فرایند جاری خودِ برنامه است و متوقف نمی‌شود"}
            )

    # 2) Unregister start-with-system. ------------------------------------
    if include_autostart:
        disabler = autostart_disabler or _default_autostart_disabler
        if dry_run:
            report["autostart_removed"] = True
        else:
            try:
                report["autostart_removed"] = bool(disabler())
            except Exception as exc:  # noqa: BLE001
                report["autostart_removed"] = False
                report["warnings"].append(f"لغو اجرای خودکار ناموفق بود: {exc}")

    # 3) Release our own log file handles before deleting (Windows). ------
    if close_logging and not dry_run:
        try:
            shutdown_logging()
        except Exception as exc:  # noqa: BLE001
            report["warnings"].append(f"آزادسازی فایل‌های لاگ ناموفق بود: {exc}")

    # 4) Delete the whole data directory. ---------------------------------
    removed, failed = _remove_tree(data_dir, dry_run=dry_run)
    report["removed"].extend(removed)
    report["failed"].extend(failed)
    if not data_dir.exists():
        report["skipped"].append({"path": str(data_dir), "reason": "پوشهٔ داده از قبل وجود نداشت"})

    # 5) Dev-repo caches (never dependencies). ----------------------------
    if include_repo_caches:
        root = repo_root if repo_root is not None else find_repo_root()
        if root is not None:
            cache_removed, cache_failed = clean_repo_caches(root, dry_run=dry_run)
            report["repo_caches"] = cache_removed
            report["removed"].extend(cache_removed)
            report["failed"].extend(cache_failed)

    report["ok"] = not report["failed"]
    report["message"] = _build_summary(report)
    try:
        logging.getLogger("local_assistant.core.cleanup").debug(
            "purge finished: ok=%s removed=%d failed=%d",
            report["ok"], len(report["removed"]), len(report["failed"]),
        )
    except Exception:  # noqa: BLE001
        pass
    return report


def _build_summary(report: dict[str, Any]) -> str:
    removed = len(report["removed"])
    verb = "می‌شدند (پیش‌نمایش)" if report["dry_run"] else "شدند"
    if not report["ok"]:
        lines = [f"⚠️ پاک‌سازی ناقص انجام شد: {removed} مورد حذف {verb} اما برخی موارد نشدند:"]
        for item in report["failed"][:8]:
            lines.append(f"   • {item['path']}: {item['error']}")
        return "\n".join(lines)
    if not removed and report["autostart_removed"] in (None, True):
        return "هیچ داده‌ای از دستیار روی سیستم نبود؛ همه‌چیز از قبل پاک بود. ✅"
    return (
        f"🗑 پاک‌سازی کامل انجام شد: {removed} مورد حذف {verb}"
        + ("، ثبت اجرای خودکار لغو شد" if report["autostart_removed"] else "")
        + ". کتابخانه‌های نصب‌شده و محیط مجازی دست‌نخورده باقی ماندند."
        + ("\nبرای شروع دوباره، برنامه را دوباره اجرا کنید." if not report["dry_run"] else "")
    )


# ---------------------------------------------------------------------------
# Shared CLI flow (used by both ``python -m local_agent`` and the desktop app)
# ---------------------------------------------------------------------------


def purge_with_confirmation(
    settings: AssistantSettings,
    *,
    assume_yes: bool = False,
    ask: Callable[[str], str] | None = None,
    echo: Callable[[str], None] = print,
    extra_kwargs: dict[str, Any] | None = None,
) -> int:
    """Interactive ``--purge`` flow: warn, confirm, wipe, report.

    Returns a process exit code (``0`` wiped, ``1`` aborted, ``2`` partial
    failure).  ``assume_yes`` is the explicit safety switch required by
    automation — it bypasses the typed confirmation.
    """
    echo("⚠️  پاک‌سازی کامل دستیار")
    echo("   همهٔ داده‌ها و تنظیمات حذف می‌شوند: تاریخچه، حافظه، لاگ‌ها، اسکرین‌شات‌ها،")
    echo("   توکن‌ها، فایل‌های قفل و ثبت «اجرای خودکار». این عمل قابل بازگشت نیست.")
    echo("   ✅ کتابخانه‌های نصب‌شده (site-packages)، محیط مجازی و کش pip حذف نمی‌شوند.")
    echo(f"   پوشهٔ داده: {settings.data_dir}")
    if not assume_yes:
        ask = ask or (lambda prompt: input(prompt))
        try:
            answer = ask(f"برای تأیید، عبارت «{PURGE_CONFIRM_WORD}» را بنویسید: ")
        except (EOFError, KeyboardInterrupt):
            echo("\nلغو شد — چیزی پاک نشد.")
            return 1
        if answer.strip() not in {PURGE_CONFIRM_WORD, "بله", "yes", "y"}:
            echo("تأیید نشد — چیزی پاک نشد.")
            return 1
    report = purge_all(settings, **(extra_kwargs or {}))
    echo(report["message"])
    for warning in report["warnings"]:
        echo(f"   ⚠️ {warning}")
    return 0 if report["ok"] else 2


__all__ = [
    "PURGE_CONFIRM_WORD",
    "PurgeRefused",
    "clean_repo_caches",
    "find_repo_root",
    "purge_all",
    "purge_with_confirmation",
]
