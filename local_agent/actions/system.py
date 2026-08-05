"""System actions: shell command execution and a few OS queries.

We deliberately keep this surface small. Real shell access is the
riskiest capability the assistant has, so it is gated behind
``Risk.DESTRUCTIVE`` and the user can disable it via the policy flag
``safety.restrict_shell_to_workdir``.

High-level hardening added:
- Dangerous pattern blocking (rm -rf /, mkfs, dd, fork-bomb, etc.)
- Working-directory sandbox when restrict_shell_to_workdir=True
- Rich system_info with optional psutil (CPU, RAM, disk)
- Safe open_path with existence and workspace awareness
- Robust shutdown/cancel with proper delay handling on Linux & Windows
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger
from ..utils.platform import is_linux, is_windows
from .registry import ActionContext, ActionRegistry, risk, Risk


logger = get_logger("actions.system")

# Dangerous patterns that should never be executed even with approval
_HARD_BLOCKS = (
    r"\brm\s+.*-rf\s+/\b",
    r"\brm\s+-rf\s+/\*",
    r"\bmkfs\b",
    r"\bdd\s+.*\bof=/dev/",
    r":\(\)\s*\{\s*:\|\:&\s*;\s*\}\s*;",  # fork bomb
    r"\bshutdown\b.*\bnow\b.*\b--no-wall\b",
    r"\bchmod\s+-R\s+777\s+/\b",
    r"\bchown\s+-R\s+.*\s+/\b",
    r"curl.*\|\s*(ba)?sh",
    r"wget.*\|\s*(ba)?sh",
)


def _is_hard_blocked(cmd: str) -> bool:
    low = cmd.lower()
    for pat in _HARD_BLOCKS:
        if re.search(pat, low, re.IGNORECASE):
            return True
    # Block direct disk operations
    if re.search(r"\bformat\s+[a-z]:", low):
        return True
    return False


def _resolve_shell_cwd(
    context: ActionContext, working_dir: str, *, restrict: bool
) -> Path:
    """Pick the working directory for a shell command.

    ``working_dir`` (the ``cd`` equivalent) is validated and then
    *remembered* on the context, so the next command runs in the same
    directory — a stateful session shell.  When no directory is given,
    the last remembered one is reused.  Outside ``full_system_access``
    the workspace sandbox is enforced; a stale out-of-workspace cwd
    (left over from a full-access session) falls back to ``work_dir``.
    """
    if working_dir:
        candidate = Path(working_dir).expanduser()
        if not candidate.is_absolute():
            candidate = (context.work_dir / candidate).resolve()
        else:
            candidate = candidate.resolve()
        if not candidate.is_dir():
            raise AssistantError(f"پوشهٔ کاری وجود ندارد: {candidate}")
        if restrict:
            try:
                candidate.relative_to(context.work_dir.resolve())
            except ValueError:
                raise AssistantError(
                    "در حالت restrict_shell_to_workdir فقط داخل workspace مجاز است؛ "
                    "برای اجرای شل در همهٔ سیستم، «دسترسی کامل سیستم» را فعال کنید."
                )
        context.extra["shell_cwd"] = str(candidate)
        return candidate

    stored = context.extra.get("shell_cwd")
    if stored:
        candidate = Path(stored)
        if candidate.is_dir():
            if restrict:
                try:
                    candidate.relative_to(context.work_dir.resolve())
                except ValueError:
                    return context.work_dir  # stale cwd from a full-access session
            return candidate
    return context.work_dir


def register_system(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="run_shell",
        description=(
            "Run a shell command and return its output. On Windows, commands are "
            "executed via cmd.exe /c; on Linux/macOS via bash -lc. DESTRUCTIVE — "
            "always asks for confirmation unless the policy says otherwise. "
            "Blocked patterns: rm -rf /, mkfs, dd to /dev, fork-bomb, curl|sh. "
            "working_dir changes the session directory (stateful cd); with "
            "«دسترسی کامل سیستم» active the shell may run in any folder, "
            "otherwise only inside the workspace."
        ),
        parameters={
            "command": {"type": "string"},
            "working_dir": {"type": "string"},
            "timeout": {"type": "integer"},
        },
        required=("command",),
    )(run_shell)

    registry.decorator(
        name="system_info",
        description=(
            "Return a detailed summary: OS, hostname, user, Python, CPU, RAM, "
            "disk, working directory. Uses psutil if available. Always safe."
        ),
        parameters={},
    )(system_info)

    registry.decorator(
        name="open_path",
        description=(
            "Open a file or directory with the system's default handler "
            "(Explorer on Windows, xdg-open on Linux, open on macOS). Validates "
            "that the path exists. Safe."
        ),
        parameters={"path": {"type": "string"}},
        required=("path",),
    )(open_path)

    registry.decorator(
        name="shutdown_computer",
        description=(
            "Shut down or restart the local computer. SYSTEM-level — always confirmed. "
            "delay_seconds 0-3600, default 60. On Linux prefers shutdown -h/-r +minutes, "
            "falls back to systemctl. On Windows uses shutdown /s or /r /t."
        ),
        parameters={
            "delay_seconds": {"type": "integer"},
            "restart": {"type": "boolean"},
        },
    )(shutdown_computer)

    registry.decorator(
        name="cancel_shutdown",
        description="Cancel a previously scheduled shutdown (shutdown /a on Windows, shutdown -c on Linux).",
        parameters={},
    )(cancel_shutdown)


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


@risk(Risk.DESTRUCTIVE)
def run_shell(
    *,
    command: str,
    working_dir: str = "",
    timeout: int = 30,
    context: ActionContext,
) -> str:
    if not isinstance(command, str) or not command.strip():
        raise AssistantError("command must be a non-empty string")
    if len(command) > 8000:
        raise AssistantError("دستور بیش از حد طولانی است (max 8000)")
    if _is_hard_blocked(command):
        raise AssistantError("این دستور به‌دلیل خطر بالا مسدود شد (الگوی خطرناک)")

    timeout = max(1, min(int(timeout or 30), context.runtime.settings.safety.shell_timeout_seconds))
    full_access = bool(context.runtime.settings.safety.full_system_access)
    restrict = bool(context.runtime.settings.safety.restrict_shell_to_workdir) and not full_access
    cwd = _resolve_shell_cwd(context, working_dir, restrict=restrict)

    if os.name == "nt":
        argv = ["cmd.exe", "/d", "/s", "/c", command]
    else:
        argv = ["bash", "-lc", command]

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("LANG", "C.UTF-8")

    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"⏰ دستور پس از {timeout} ثانیه متوقف شد (timeout)\n$ {command}"
    except FileNotFoundError as exc:
        raise AssistantError(f"پوستهٔ سیستم پیدا نشد: {exc}") from exc
    except OSError as exc:
        raise AssistantError(f"خطای اجرای پوسته: {exc}") from exc

    output = (completed.stdout or "") + (completed.stderr or "")
    # Truncate for LLM context but keep full length info
    max_chars = context.runtime.settings.llm.max_context_chars
    snippet = output[:max_chars]
    if len(output) > max_chars:
        snippet += f"\n... (خروجی کوتاه شد: {len(output)} -> {max_chars} کاراکتر)"
    return (
        f"$ {command}\n"
        f"پوشه: {cwd or context.work_dir}\n\n"
        f"{snippet}\n\n"
        f"[exit code: {completed.returncode}]"
    )


@risk(Risk.SAFE)
def system_info(*, context: ActionContext) -> str:
    # Base info
    info = {
        "os": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "hostname": platform.node(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "?",
        "cwd": str(context.work_dir),
        "data_dir": str(context.runtime.settings.data_dir),
    }
    lines = [f"  {k}: {v}" for k, v in info.items()]

    # Try psutil enrichment
    try:
        import psutil

        vm = psutil.virtual_memory()
        lines.append(f"  ram: {vm.total // (1024**3)}GB total, {vm.available // (1024**3)}GB avail ({vm.percent}% used)")
        du = psutil.disk_usage(str(context.work_dir))
        lines.append(f"  disk ({context.work_dir}): {du.free // (1024**3)}GB free / {du.total // (1024**3)}GB total ({du.percent}% used)")
        lines.append(f"  cpu: {psutil.cpu_count(logical=True)} logical, {psutil.cpu_count(logical=False)} physical, {psutil.cpu_percent(interval=0.3)}% usage")
        boot = psutil.boot_time()
        import datetime

        lines.append(f"  uptime: boot at {datetime.datetime.fromtimestamp(boot).isoformat()}")
    except ImportError:
        lines.append("  psutil: نصب نیست (برای اطلاعات RAM/CPU: pip install psutil)")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  psutil error: {exc}")

    # Additional diagnostics
    try:
        lines.append(f"  work_dir exists: {context.work_dir.exists()}, free check: {shutil.disk_usage(context.work_dir).free // (1024**2)} MB free")
    except Exception:
        pass

    return "🖥️ اطلاعات سیستم:\n" + "\n".join(lines)


@risk(Risk.SAFE)
def open_path(*, path: str, context: ActionContext) -> str:
    if not isinstance(path, str) or not path.strip():
        raise AssistantError("path must be a non-empty string")
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = (context.work_dir / target).resolve()
    else:
        target = target.resolve()
    if not target.exists():
        raise AssistantError(f"مسیر وجود ندارد: {target}")
    # Optional: warn if outside workspace but allow (SAFE tool should stay inside? we allow but log)
    try:
        work_resolved = context.work_dir.resolve()
        is_inside = target == work_resolved or work_resolved in target.parents
    except Exception:
        is_inside = False
    if not is_inside:
        logger.warning("open_path outside workspace: %s", target)

    try:
        if is_windows():
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(target)], close_fds=True, start_new_session=True)
        else:
            # Linux: try xdg-open, then gio, then sensible-browser
            opener = shutil.which("xdg-open") or shutil.which("gio") or shutil.which("sensible-browser")
            if opener and "gio" in opener:
                subprocess.Popen([opener, "open", str(target)], close_fds=True, start_new_session=True)
            elif opener:
                subprocess.Popen([opener, str(target)], close_fds=True, start_new_session=True)
            else:
                raise AssistantError("هیچ ابزار بازکننده‌ای پیدا نشد (xdg-open نصب کنید)")
    except OSError as exc:
        raise AssistantError(f"باز کردن {target} ممکن نشد: {exc}") from exc
    return f"باز شد: {target} {'(خارج از workspace)' if not is_inside else ''}"


@risk(Risk.SYSTEM)
def shutdown_computer(
    *, delay_seconds: int = 60, restart: bool = False, context: ActionContext
) -> str:
    delay = max(0, min(int(delay_seconds or 60), 3600))

    if is_windows():
        flag = "/r" if restart else "/s"
        try:
            # Windows shutdown /t is seconds
            subprocess.run(
                ["shutdown", flag, "/t", str(delay), "/c", "scheduled by local assistant"],
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError(f"خاموش کردن ناموفق بود: {exc}") from exc
    else:
        # Linux/Unix: prefer shutdown with +minutes for delayed, else systemctl
        minutes = max(1, delay // 60) if delay >= 60 else 0
        cmd = None
        if shutil.which("shutdown"):
            if delay == 0:
                flag = "-r now" if restart else "-h now"
                cmd = ["sh", "-lc", f"shutdown {flag}"]
            else:
                # delay >0
                if minutes > 0:
                    flag = "-r" if restart else "-h"
                    cmd = ["shutdown", flag, f"+{minutes}"]
                else:
                    # Less than a minute: schedule via at or immediate shutdown with wall
                    # Try shutdown directly with seconds via systemd-run if available
                    if shutil.which("systemd-run") and shutil.which("systemctl"):
                        action = "reboot" if restart else "poweroff"
                        # schedule via systemd-run --on-active
                        cmd = [
                            "systemd-run",
                            f"--on-active={delay}s",
                            "--unit=assistant-shutdown",
                            "systemctl",
                            action,
                        ]
                    else:
                        # fallback immediate with wall message
                        flag = "-r" if restart else "-h"
                        cmd = ["shutdown", flag, f"+{minutes if minutes>0 else 1}"]
        elif shutil.which("systemctl"):
            action = "reboot" if restart else "poweroff"
            if delay == 0:
                cmd = ["systemctl", action]
            else:
                # Delayed via systemd-run if possible
                if shutil.which("systemd-run"):
                    cmd = [
                        "systemd-run",
                        f"--on-active={delay}s",
                        f"--unit=assistant-{action}",
                        "systemctl",
                        action,
                    ]
                else:
                    # No delay support, do immediate but warn
                    logger.warning("systemctl has no delay, doing immediate %s despite delay=%s", action, delay)
                    cmd = ["systemctl", action]
        else:
            raise AssistantError(
                "دستور خاموش کردن روی این سیستم در دسترس نیست. "
                "یکی از این‌ها را نصب کنید: systemctl, shutdown"
            )
        try:
            logger.info("shutdown cmd: %s", cmd)
            subprocess.run(cmd, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError(f"خاموش کردن ناموفق بود: {exc}") from exc

    verb = "ری‌استارت" if restart else "خاموش شدن"
    if delay == 0:
        return f"⏰ {verb} فوری برنامه‌ریزی شد"
    return f"⏰ {verb} در {delay} ثانیه ({delay//60} دقیقه) برنامه‌ریزی شد. برای لغو: cancel_shutdown"


@risk(Risk.SYSTEM)
def cancel_shutdown(*, context: ActionContext) -> str:
    if is_windows():
        try:
            subprocess.run(["shutdown", "/a"], check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError(f"لغو خاموش شدن ناموفق بود: {exc}") from exc
    else:
        # Linux: try shutdown -c, then systemctl cancel, then systemd-run cleanup
        errors = []
        if shutil.which("shutdown"):
            try:
                subprocess.run(["shutdown", "-c"], check=False, timeout=10)
                return "لغو خاموش شدن انجام شد (shutdown -c)"
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
        if shutil.which("systemctl"):
            try:
                # Cancel our systemd-run timers
                subprocess.run(
                    ["systemctl", "stop", "assistant-shutdown", "assistant-poweroff", "assistant-reboot"],
                    check=False,
                    timeout=10,
                )
                # Try systemctl cancel (some systems)
                subprocess.run(["systemctl", "cancel"], check=False, timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
        if errors and not shutil.which("shutdown"):
            raise AssistantError(f"لغو ناموفق بود: {'; '.join(errors)}")
    return "✅ درخواست لغو خاموش شدن ارسال شد (اگر زمان‌بندی فعالی بود)"
