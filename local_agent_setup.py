#!/usr/bin/env python3
"""One-shot installer for the Local Windows Assistant.

Run with:

    python local_agent_setup.py install      # install runtime deps
    python local_agent_setup.py install-all  # install everything (browser etc.)
    python local_agent_setup.py doctor       # verify the install
    python local_agent_setup.py config       # open the config in the default editor
    python local_agent_setup.py uninstall    # remove the package (editable only)
    python local_agent_setup.py start        # launch the Bridge daemon (in-process)
    python local_agent_setup.py web          # launch the local web UI
    python local_agent_setup.py desktop      # launch the native desktop app
    python local_agent_setup.py build-desktop # build the single-file .exe
    python local_agent_setup.py bot-telegram # launch the Telegram bot
    python local_agent_setup.py bot-bale     # launch the Bale bot

The script is intentionally zero-dependency so it works on a fresh
Windows box where only the standard Python interpreter is present.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "local_agent" / "requirements.txt"
REQUIREMENTS_FULL = ROOT / "local_agent" / "requirements-full.txt"


def _python() -> str:
    return sys.executable or "python"


def _pip(*args: str) -> int:
    return subprocess.call([_python(), "-m", "pip", *args])


def _print_banner(message: str) -> None:
    bar = "=" * min(70, max(20, len(message) + 4))
    print(bar)
    print(f"  {message}")
    print(bar)


def _confirm(message: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{message} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    _print_banner("installing Local Windows Assistant (minimal)")
    if not REQUIREMENTS.is_file():
        print(f"!! requirements.txt not found at {REQUIREMENTS}")
        return 1
    rc = _pip("install", "-r", str(REQUIREMENTS))
    if rc != 0:
        return rc
    print("installing this project (editable)...")
    return _pip("install", "-e", str(ROOT))


def cmd_install_all(args: argparse.Namespace) -> int:
    rc = cmd_install(args)
    if rc != 0:
        return rc
    if not REQUIREMENTS_FULL.is_file():
        print(f"!! requirements-full.txt not found at {REQUIREMENTS_FULL}")
        return 1
    print("installing full feature set (browser automation, telegram, web UI, ...)...")
    return _pip("install", "-r", str(REQUIREMENTS_FULL))


def cmd_doctor(args: argparse.Namespace) -> int:
    _print_banner("checking the local assistant install")
    print(f"python:   {_python()}  ({platform.python_version()})")
    print(f"platform: {platform.platform()}")
    print(f"cwd:      {os.getcwd()}")

    sys.path.insert(0, str(ROOT))
    try:
        import local_agent  # noqa: F401

        print("local_agent: imported OK")
    except Exception as exc:  # noqa: BLE001
        print(f"local_agent: import FAILED ({exc})")
        return 1

    deps = [
        ("requests", "requests"),
        ("Pillow", "PIL"),
        ("python-dotenv", "dotenv"),
        ("rich (optional)", "rich"),
        ("pyautogui (optional)", "pyautogui"),
        ("mss (optional)", "mss"),
        ("telethon (optional)", "telethon"),
        ("fastapi (web UI)", "fastapi"),
        ("uvicorn (web UI)", "uvicorn"),
        ("pywebview (desktop app)", "webview"),
        ("pystray (tray icon)", "pystray"),
    ]
    for label, mod in deps:
        try:
            __import__(mod)
            print(f"{label}: OK")
        except ImportError:
            print(f"{label}: MISSING")

    if platform.system() == "Windows":
        try:
            import uiautomation  # type: ignore  # noqa: F401

            print("uiautomation: OK")
        except ImportError:
            print("uiautomation: MISSING (run: pip install uiautomation)")

    # Full runtime report (config, model connectivity, tools, ports, ...)
    try:
        from local_agent.diagnostics import run_checks

        print()
        report = run_checks(network=not getattr(args, "offline", False))
        print(report.render())
        failed = report.status == "fail"
    except Exception as exc:  # noqa: BLE001
        print(f"\nfull self-check unavailable: {exc}")
        failed = False

    print()
    print("After installing everything, you can:")
    print("    python -m local_agent            # start the local CLI (in-process bridge)")
    print("    python local_agent_setup.py web  # start the web UI on http://127.0.0.1:7824")
    print("    python local_agent_setup.py desktop  # start the native desktop app")
    print("    python local_agent_setup.py bot-telegram  # start the Telegram bot (needs TELEGRAM_BOT_TOKEN)")
    return 1 if failed else 0


def cmd_config(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT))
    try:
        from local_agent.core.config import load_settings
    except Exception as exc:  # noqa: BLE001
        print(f"could not import config: {exc}")
        return 1
    settings = load_settings()
    target = settings.config_path
    print(f"config path: {target}")
    if not _confirm("open in the default editor?", assume_yes=args.yes):
        return 0
    if platform.system() == "Windows":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not _confirm("uninstall the local assistant package?", assume_yes=args.yes):
        return 0
    return _pip("uninstall", "-y", "local-agent")


def cmd_start(args: argparse.Namespace) -> int:
    """Launch the local CLI in interactive mode."""
    sys.path.insert(0, str(ROOT))
    from local_agent.cli import run_cli
    return run_cli([])


def cmd_web(args: argparse.Namespace) -> int:
    """Launch the web UI."""
    sys.path.insert(0, str(ROOT))
    from local_agent.web import run_web
    return run_web([])


def cmd_desktop(args: argparse.Namespace) -> int:
    """Launch the native desktop app (pywebview window + tray icon)."""
    sys.path.insert(0, str(ROOT))
    from local_agent.desktop import run as run_desktop

    argv: list[str] = []
    if getattr(args, "hidden", False):
        argv.append("--hidden")
    if getattr(args, "browser", False):
        argv.append("--browser")
    if getattr(args, "debug", False):
        argv.append("--debug")
    return run_desktop(argv)


def cmd_build_desktop(args: argparse.Namespace) -> int:
    """Build the single-file Windows executable with PyInstaller."""
    sys.path.insert(0, str(ROOT))
    from local_agent.desktop.build import main as build_main

    argv: list[str] = []
    if getattr(args, "installer", False):
        argv.append("--installer")
    if getattr(args, "onedir", False):
        argv.append("--onedir")
    if getattr(args, "spec_only", False):
        argv.append("--spec-only")
    return build_main(argv)


def cmd_bot_telegram(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT))
    from local_agent.bridge.telegram_bot import run_telegram
    return run_telegram([])


def cmd_bot_bale(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT))
    from local_agent.bridge.telegram_bot import run_bale
    return run_bale([])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local Windows Assistant installer / runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            commands:
              install        install the minimum runtime requirements
              install-all    install minimum + browser / telegram / web extras
              doctor         verify the install
              config         open the user config file
              uninstall      remove the package
              start          start the local CLI (in-process bridge)
              web            start the local web UI on http://127.0.0.1:7824
              desktop        start the native desktop app (window + tray icon)
              build-desktop  build the single-file .exe with PyInstaller
              bot-telegram   start the Telegram bot (needs TELEGRAM_BOT_TOKEN)
              bot-bale       start the Bale bot (needs BALE_BOT_TOKEN)
            """
        ),
    )
    parser.add_argument("-y", "--yes", action="store_true", help="assume yes for prompts")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install").set_defaults(func=cmd_install)
    sub.add_parser("install-all").set_defaults(func=cmd_install_all)
    doctor = sub.add_parser("doctor", help="verify the install and the live config")
    doctor.add_argument("--offline", action="store_true", help="skip network checks")
    doctor.set_defaults(func=cmd_doctor)
    sub.add_parser("config").set_defaults(func=cmd_config)
    sub.add_parser("uninstall").set_defaults(func=cmd_uninstall)
    sub.add_parser("start").set_defaults(func=cmd_start)
    sub.add_parser("web").set_defaults(func=cmd_web)

    desktop = sub.add_parser("desktop", help="start the native desktop app")
    desktop.add_argument("--hidden", action="store_true", help="start minimised to the tray")
    desktop.add_argument("--browser", action="store_true", help="use the system browser instead")
    desktop.add_argument("--debug", action="store_true", help="open the webview devtools")
    desktop.set_defaults(func=cmd_desktop)

    build = sub.add_parser("build-desktop", help="build the Windows .exe")
    build.add_argument("--installer", action="store_true", help="also run Inno Setup")
    build.add_argument("--onedir", action="store_true", help="build a folder instead of one file")
    build.add_argument("--spec-only", dest="spec_only", action="store_true", help="write the spec only")
    build.set_defaults(func=cmd_build_desktop)
    sub.add_parser("bot-telegram").set_defaults(func=cmd_bot_telegram)
    sub.add_parser("bot-bale").set_defaults(func=cmd_bot_bale)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
