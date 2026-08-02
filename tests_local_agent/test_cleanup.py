"""Tests for the full app purge («پاک‌سازی کامل»).

Covers the shared core (:func:`purge_all`), the repo-cache rules and the
safety guards — always against ``tmp_path`` fixtures, never the real home
directory or registry.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from local_agent.core.cleanup import (
    PURGE_CONFIRM_WORD,
    clean_repo_caches,
    find_repo_root,
    purge_all,
    purge_with_confirmation,
)
from local_agent.core.config import AssistantSettings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(data_dir: Path) -> AssistantSettings:
    work = data_dir.parent / "workspace"
    work.mkdir(parents=True, exist_ok=True)
    return AssistantSettings(data_dir=data_dir, work_dir=work)


def _populate_data_dir(data_dir: Path) -> list[Path]:
    """Create every on-disk artefact the app is known to leave behind."""
    created: list[Path] = []
    files = [
        "config.json",
        "history.jsonl",
        "memory.json",
        "bridge.token",
        "desktop.pid",
        "update-check.json",
        "window.json",
        "assistant.session",
        "logs/assistant.log",
        "logs/assistant.log.1",
        "screenshots/ui-shot.png",
        "screenshots/nested/deep.png",
    ]
    for relative in files:
        path = data_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dummy", encoding="utf-8")
        created.append(path)
    return created


def _disabler_ok(calls: list[bool]) -> Any:
    def _inner() -> bool:
        calls.append(True)
        return True

    return _inner


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


def test_purge_removes_entire_data_dir_and_reports(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)
    keep = tmp_path / "keep.txt"
    keep.write_text("بیرون از پوشهٔ داده — نباید پاک شود", encoding="utf-8")
    calls: list[bool] = []

    report = purge_all(
        _settings(data_dir),
        autostart_disabler=_disabler_ok(calls),
        include_repo_caches=False,
    )

    assert report["ok"] is True
    assert not data_dir.exists(), "کل پوشهٔ داده باید حذف شود"
    assert keep.read_text(encoding="utf-8"), "مسیر بیرونی نباید دست‌کاری شود"
    assert keep.exists()
    assert calls == [True], "لغو اجرای خودکار باید صدا زده شود"
    assert report["autostart_removed"] is True
    assert report["failed"] == []
    assert len(report["removed"]) >= 11  # همهٔ فایل‌ها + خودِ پوشه
    assert "پاک‌سازی کامل انجام شد" in report["message"]


def test_purge_dry_run_changes_nothing(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)

    report = purge_all(
        _settings(data_dir),
        dry_run=True,
        autostart_disabler=lambda: (_ for _ in ()).throw(AssertionError("نباید صدا زده شود")),
        include_repo_caches=False,
    )

    assert report["dry_run"] is True
    assert report["removed"], "پیش‌نمایش باید فهرستِ موارد را بدهد"
    assert (data_dir / "config.json").exists(), "در حالت پیش‌نمایش چیزی حذف نشود"
    assert data_dir.exists()


def test_purge_refuses_dangerous_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pretend tmp_path *is* the user's home
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    report = purge_all(_settings(tmp_path), autostart_disabler=lambda: True, include_repo_caches=False)
    assert report["ok"] is False
    assert "امن نیست" in report["message"]
    assert tmp_path.exists()


def test_purge_when_nothing_exists_is_fine(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"  # never created
    report = purge_all(_settings(data_dir), autostart_disabler=lambda: True, include_repo_caches=False)
    assert report["ok"] is True
    assert report["removed"] == []
    assert any("وجود نداشت" in s["reason"] for s in report["skipped"])


def test_purge_skips_its_own_process(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)
    (data_dir / "desktop.pid").write_text(str(os.getpid()), encoding="utf-8")

    report = purge_all(_settings(data_dir), autostart_disabler=lambda: True, include_repo_caches=False)

    assert report["ok"] is True
    assert report["stopped_pids"] == []
    assert any("خودِ برنامه" in s["reason"] for s in report["skipped"])
    # و ما هنوز زنده‌ایم :)
    assert os.getpid() > 0


def test_purge_dead_pid_file_is_ignored(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)
    # چنین pid عظیمی قطعاً زنده نیست
    (data_dir / "desktop.pid").write_text("4000000000", encoding="utf-8")
    report = purge_all(_settings(data_dir), autostart_disabler=lambda: True, include_repo_caches=False)
    assert report["ok"] is True
    assert report["stopped_pids"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="fork-based test is POSIX-oriented")
def test_purge_stops_recorded_running_process(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        (data_dir / "desktop.pid").write_text(str(child.pid), encoding="utf-8")
        report = purge_all(
            _settings(data_dir), autostart_disabler=lambda: True, include_repo_caches=False
        )
        assert child.pid in report["stopped_pids"]
        deadline = time.time() + 5
        while child.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert child.poll() is not None, "فرایند ثبت‌شده باید متوقف شود"
        assert report["ok"] is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


# ---------------------------------------------------------------------------
# Autostart integration (real module, isolated HOME)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux autostart path only")
def test_purge_disables_linux_autostart_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_agent.desktop import autostart

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert autostart.enable('"/bin/true" -m local_agent.desktop') is True
    desktop_file = autostart._linux_autostart_path()
    assert desktop_file.is_file()

    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)
    # بدون تزریق disabler — باید مسیر واقعیِ لینوکس استفاده شود
    report = purge_all(_settings(data_dir), include_repo_caches=False)

    assert report["ok"] is True
    assert report["autostart_removed"] is True
    assert not desktop_file.exists()
    assert autostart.is_enabled() is False


def test_purge_autostart_failure_is_a_warning_not_a_crash(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)

    def broken() -> bool:
        raise OSError("registry unavailable")

    report = purge_all(_settings(data_dir), autostart_disabler=broken, include_repo_caches=False)
    assert report["ok"] is True  # شکست اجرای خودکار، پاک‌سازی فایل‌ها را زندانی نمی‌کند
    assert report["autostart_removed"] is False
    assert any("اجرای خودکار" in w for w in report["warnings"])
    assert not data_dir.exists()


# ---------------------------------------------------------------------------
# Repo caches
# ---------------------------------------------------------------------------


def _make_fake_repo(root: Path) -> None:
    (root / "local_agent").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "tests_local_agent").mkdir()
    for folder in (
        "local_agent/__pycache__",
        "tests_local_agent/__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".venv/lib/site-packages/__pycache__",  # هرگز نباید پاک شود
        "node_modules/__pycache__",  # هرگز نباید پاک شود
    ):
        cache = root / folder
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "junk.pyc").write_bytes(b"pyc")


def test_clean_repo_caches_never_touches_dependencies(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_fake_repo(root)

    removed, failed = clean_repo_caches(root)

    assert failed == []
    assert any("__pycache__" in p and ".venv" not in p for p in removed)
    assert (root / ".venv/lib/site-packages/__pycache__/junk.pyc").exists()
    assert (root / "node_modules/__pycache__/junk.pyc").exists()
    assert not (root / "local_agent/__pycache__").exists()
    assert not (root / ".pytest_cache").exists()


def test_purge_includes_repo_caches(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_fake_repo(root)
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)

    report = purge_all(
        _settings(data_dir), autostart_disabler=lambda: True, repo_root=root
    )
    assert report["ok"] is True
    assert report["repo_caches"]
    assert (root / "pyproject.toml").exists(), "کد منبع نباید پاک شود"
    assert (root / "local_agent").is_dir()


def test_find_repo_root_points_at_this_checkout() -> None:
    root = find_repo_root()
    # In the dev sandbox this resolves to the repository; in frozen/installed
    # layouts it is None — both are acceptable, so only sanity-check the type.
    if root is not None:
        assert (root / "pyproject.toml").is_file()
        assert (root / "local_agent").is_dir()


def test_clean_repo_caches_missing_root_is_noop(tmp_path: Path) -> None:
    removed, failed = clean_repo_caches(tmp_path / "does-not-exist")
    assert removed == [] and failed == []


# ---------------------------------------------------------------------------
# Shared interactive flow
# ---------------------------------------------------------------------------


def test_purge_with_confirmation_typed_flow(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)
    echoed: list[str] = []

    code = purge_with_confirmation(
        _settings(data_dir),
        ask=lambda _prompt: PURGE_CONFIRM_WORD,
        echo=echoed.append,
    )
    assert code == 0
    assert not data_dir.exists()
    assert any("کتابخانه" in line for line in echoed)  # وعدهٔ عدم‌حذف بسته‌ها گفته می‌شود


def test_purge_with_confirmation_aborts_on_wrong_answer(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)
    code = purge_with_confirmation(_settings(data_dir), ask=lambda _p: "خیر", echo=lambda _s: None)
    assert code == 1
    assert data_dir.exists()


def test_purge_with_confirmation_assume_yes_skips_prompt(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local_assistant"
    _populate_data_dir(data_dir)

    def ask(_prompt: str) -> str:
        raise AssertionError("با --yes نباید سؤال شود")

    code = purge_with_confirmation(_settings(data_dir), assume_yes=True, ask=ask, echo=lambda _s: None)
    assert code == 0
    assert not data_dir.exists()
