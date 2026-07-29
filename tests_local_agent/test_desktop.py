"""Tests for the native desktop app.

These run on any platform: everything Windows-specific is exercised
through its graceful-degradation path so the suite stays green on the
Linux CI box while still covering the real logic (version comparison,
hotkey parsing, the single-instance lock, spec generation, ...).
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from local_agent.desktop import (
    APP_NAME,
    APP_VERSION,
    DesktopApi,
    DesktopApp,
    DesktopConfig,
    is_pywebview_available,
)
from local_agent.desktop import autostart, build, hotkey, single_instance, tray, updater
from local_agent.core.config import AssistantSettings


# ---------------------------------------------------------------------------
# Module structure
# ---------------------------------------------------------------------------


def test_desktop_package_imports() -> None:
    import local_agent.desktop as pkg

    for name in ("run", "DesktopApp", "DesktopConfig", "DesktopApi", "APP_VERSION"):
        assert hasattr(pkg, name), f"missing export: {name}"


def test_expected_modules_exist() -> None:
    root = Path(__file__).resolve().parents[1] / "local_agent" / "desktop"
    for filename in (
        "__init__.py",
        "__main__.py",
        "app.py",
        "tray.py",
        "hotkey.py",
        "single_instance.py",
        "updater.py",
        "build.py",
        "autostart.py",
        "installer.iss",
    ):
        assert (root / filename).is_file(), f"missing {filename}"


def test_pywebview_probe_returns_bool() -> None:
    assert isinstance(is_pywebview_available(), bool)


def test_app_metadata() -> None:
    assert APP_VERSION.count(".") == 2
    assert APP_NAME  # Persian display name is set


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_default_window_geometry() -> None:
    config = DesktopConfig()
    assert (config.width, config.height) == (1200, 800)
    assert (config.min_width, config.min_height) == (800, 600)
    assert config.hotkey == "ctrl+alt+a"
    assert config.minimize_to_tray is True


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_WEB_PORT", "7999")
    monkeypatch.setenv("LOCAL_AGENT_HOTKEY", "ctrl+shift+space")
    monkeypatch.setenv("LOCAL_AGENT_MINIMIZE_TO_TRAY", "false")
    monkeypatch.setenv("LOCAL_AGENT_START_HIDDEN", "yes")
    config = DesktopConfig.from_env()
    assert config.port == 7999
    assert config.hotkey == "ctrl+shift+space"
    assert config.minimize_to_tray is False
    assert config.start_hidden is True


def test_find_free_port_falls_back_when_busy() -> None:
    from local_agent.desktop.app import find_free_port

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        chosen = find_free_port("127.0.0.1", taken)
        assert chosen != taken
        assert 1024 < chosen < 65536


# ---------------------------------------------------------------------------
# Version comparison / updater
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.2.3", (1, 2, 3, 4, "")),
        ("v1.2.3", (1, 2, 3, 4, "")),
        ("2.0", (2, 0, 0, 4, "")),
        ("3", (3, 0, 0, 4, "")),
        ("  v0.9.1  ", (0, 9, 1, 4, "")),
    ],
)
def test_parse_version(value: str, expected: tuple) -> None:
    assert updater.parse_version(value) == expected


@pytest.mark.parametrize("value", ["", "not-a-version", "latest", "vX.Y.Z"])
def test_parse_version_rejects_garbage(value: str) -> None:
    assert updater.parse_version(value) is None


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("1.0.0", "1.0.0", 0),
        ("1.0.1", "1.0.0", 1),
        ("1.0.0", "1.0.1", -1),
        ("2.0.0", "1.9.9", 1),
        ("1.10.0", "1.9.0", 1),  # numeric, not lexicographic
        ("v2.1.0", "2.1.0", 0),
        ("1.0.0", "1.0.0-rc1", 1),  # final beats pre-release
        ("1.0.0-beta1", "1.0.0-alpha9", 1),
        ("garbage", "1.0.0", -1),
    ],
)
def test_compare_versions(left: str, right: str, expected: int) -> None:
    assert updater.compare_versions(left, right) == expected


def test_is_newer() -> None:
    assert updater.is_newer("2.1.0", "2.0.0") is True
    assert updater.is_newer("2.0.0", "2.0.0") is False
    assert updater.is_newer("1.9.0", "2.0.0") is False
    assert updater.is_newer("", "2.0.0") is False


def test_release_from_api_and_installer_asset() -> None:
    release = updater.Release.from_api({
        "tag_name": "v2.1.0",
        "name": "Release 2.1.0",
        "html_url": "https://example.invalid/releases/2.1.0",
        "body": "notes",
        "assets": [
            {"browser_download_url": "https://example.invalid/notes.txt"},
            {"browser_download_url": "https://example.invalid/Setup.exe"},
        ],
    })
    assert release.version == "v2.1.0"
    assert release.installer_url.endswith("Setup.exe")


def test_updater_reports_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    up = updater.Updater("2.0.0", data_dir=tmp_path)
    monkeypatch.setattr(
        up, "fetch_latest", lambda: updater.Release(version="v2.5.0", name="n", url="u")
    )
    result = up.check(force=True)
    assert result.available is True
    assert result.release.version == "v2.5.0"
    assert result.to_dict()["release"]["version"] == "v2.5.0"


def test_updater_no_update_when_current_is_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    up = updater.Updater("9.9.9", data_dir=tmp_path)
    monkeypatch.setattr(
        up, "fetch_latest", lambda: updater.Release(version="v2.5.0", name="n", url="u")
    )
    result = up.check(force=True)
    assert result.available is False
    assert result.release is None


def test_updater_survives_network_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    up = updater.Updater("2.0.0", data_dir=tmp_path)
    monkeypatch.setattr(up, "fetch_latest", lambda: None)
    result = up.check(force=True)
    assert result.available is False
    assert result.error


def test_updater_cooldown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    up = updater.Updater("2.0.0", data_dir=tmp_path)
    assert up.should_check() is True
    calls: list[int] = []

    def fake_fetch():
        calls.append(1)
        return updater.Release(version="v2.0.0", name="n", url="u")

    monkeypatch.setattr(up, "fetch_latest", fake_fetch)
    up.check(force=True)
    assert up.should_check() is False
    up.check()  # inside the cooldown: must not hit the network again
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Hotkey parsing
# ---------------------------------------------------------------------------


def test_parse_default_hotkey() -> None:
    parsed = hotkey.parse_hotkey("ctrl+alt+a")
    assert parsed.modifiers & hotkey.MOD_CONTROL
    assert parsed.modifiers & hotkey.MOD_ALT
    assert parsed.vk == ord("A")
    assert parsed.has_modifier


@pytest.mark.parametrize(
    "spec,vk",
    [
        ("ctrl+shift+space", 0x20),
        ("ctrl+alt+f5", 0x74),
        ("win+a", ord("A")),
        ("CTRL+ALT+Z", ord("Z")),
        ("ctrl-alt-1", ord("1")),
    ],
)
def test_parse_various_hotkeys(spec: str, vk: int) -> None:
    assert hotkey.parse_hotkey(spec).vk == vk


@pytest.mark.parametrize("spec", ["", "   ", "ctrl", "ctrl+alt", "ctrl+alt+notakey", "a+b"])
def test_parse_hotkey_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(hotkey.HotkeyError):
        hotkey.parse_hotkey(spec)


def test_hotkey_manager_degrades_off_windows() -> None:
    manager = hotkey.HotkeyManager("ctrl+alt+a", lambda: None)
    started = manager.start()
    if sys.platform == "win32":  # pragma: no cover - depends on the host
        manager.stop()
    else:
        assert started is False
        assert manager.supported is False
        assert manager.active is False
        assert "not supported" in (manager.error or "")
    manager.stop()  # must be safe either way


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_single_instance_second_launch_raises(tmp_path: Path) -> None:
    port = _free_port()
    first = single_instance.SingleInstance(tmp_path, port=port)
    first.acquire()
    try:
        assert first.held is True
        assert first.pid_path.is_file()
        second = single_instance.SingleInstance(tmp_path, port=port)
        with pytest.raises(single_instance.AlreadyRunning):
            second.acquire()
    finally:
        first.release()
    assert first.held is False
    assert not first.pid_path.exists()


def test_single_instance_relock_after_release(tmp_path: Path) -> None:
    port = _free_port()
    first = single_instance.SingleInstance(tmp_path, port=port)
    first.acquire()
    first.release()
    second = single_instance.SingleInstance(tmp_path, port=port)
    second.acquire()  # must succeed now
    second.release()


def test_single_instance_activation_signal(tmp_path: Path) -> None:
    port = _free_port()
    woken = threading.Event()
    holder = single_instance.SingleInstance(tmp_path, port=port)
    holder.acquire(on_activate=woken.set)
    try:
        other = single_instance.SingleInstance(tmp_path, port=port)
        assert other.signal_existing() is True
        assert woken.wait(timeout=3.0) is True
    finally:
        holder.release()


def test_single_instance_records_pid(tmp_path: Path) -> None:
    import os

    lock = single_instance.SingleInstance(tmp_path, port=_free_port())
    lock.acquire()
    try:
        assert lock.read_pid() == os.getpid()
    finally:
        lock.release()


def test_single_instance_context_manager(tmp_path: Path) -> None:
    port = _free_port()
    with single_instance.SingleInstance(tmp_path, port=port) as lock:
        assert lock.held
    assert single_instance.SingleInstance(tmp_path, port=port).read_pid() is None


# ---------------------------------------------------------------------------
# Tray
# ---------------------------------------------------------------------------


def test_tray_availability_is_boolean() -> None:
    assert isinstance(tray.is_available(), bool)


def test_tray_icon_is_drawable(tmp_path: Path) -> None:
    image = tray.build_icon_image(64)
    assert image.size == (64, 64)
    assert image.mode == "RGBA"
    target = tray.save_icon(tmp_path / "icon.ico")
    assert target.is_file() and target.stat().st_size > 0


def test_tray_manager_without_pystray_is_harmless() -> None:
    manager = tray.TrayManager()
    started = manager.start()
    assert isinstance(started, bool)
    if not manager.available:
        assert started is False
        assert manager.running is False
    manager.stop()  # safe regardless


def test_tray_callbacks_are_all_optional() -> None:
    callbacks = tray.TrayCallbacks()
    for name in (
        "on_show", "on_hide", "on_toggle", "on_open_workspace",
        "on_settings", "on_check_updates", "on_about", "on_quit",
    ):
        assert getattr(callbacks, name) is None


# ---------------------------------------------------------------------------
# Auto-start
# ---------------------------------------------------------------------------


def test_autostart_reports_platform_support() -> None:
    assert autostart.supported() is (sys.platform == "win32")


def test_autostart_is_noop_off_windows() -> None:
    if sys.platform == "win32":  # pragma: no cover - never on CI
        pytest.skip("would modify the real registry")
    assert autostart.is_enabled() is False
    assert autostart.enable() is False
    assert autostart.disable() is False
    assert autostart.set_enabled(True) is False


def test_launch_command_quotes_the_interpreter() -> None:
    command = autostart.launch_command()
    assert command.startswith('"')
    assert "local_agent.desktop" in command or command.endswith('"')


# ---------------------------------------------------------------------------
# DesktopApp wiring (no GUI)
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path) -> DesktopApp:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    return DesktopApp(settings=settings, config=DesktopConfig(port=_free_port()))


def test_window_helpers_are_safe_without_a_window(app: DesktopApp) -> None:
    assert app.show_window() is False
    assert app.hide_window() is False
    assert app.minimize_window() is False
    assert app.pick_file() == []
    assert app.pick_folder() == ""
    app.update_title("busy")  # must not raise


def test_info_snapshot(app: DesktopApp) -> None:
    info = app.info()
    assert info["version"] == APP_VERSION
    assert info["work_dir"] == str(app.settings.work_dir)
    assert info["hotkey"] == "ctrl+alt+a"
    assert info["tray_active"] is False
    assert info["hotkey_active"] is False


def test_notify_never_raises(app: DesktopApp) -> None:
    assert isinstance(app.notify("عنوان", "متن"), bool)


def test_taskbar_progress_off_windows(app: DesktopApp) -> None:
    if sys.platform != "win32":
        assert app.set_taskbar_progress(0.5) is False


def test_quit_is_idempotent(app: DesktopApp) -> None:
    app.quit()
    app.quit()  # second call must be a no-op, not a crash


def test_js_api_surface(app: DesktopApp) -> None:
    api = DesktopApi(app)
    for name in (
        "show", "hide", "minimize", "quit", "notify", "set_progress",
        "open_workspace", "pick_file", "pick_folder",
        "get_autostart", "set_autostart", "get_info", "check_updates",
    ):
        assert callable(getattr(api, name)), f"DesktopApi.{name} is missing"
    assert api.get_info()["version"] == APP_VERSION
    assert isinstance(api.get_autostart(), bool)


def test_backend_boots_and_serves_the_ui(app: DesktopApp) -> None:
    import requests

    url = app.start_backend()
    try:
        assert app.server.wait_until_ready(timeout=15) is True
        response = requests.get(url, timeout=5)
        assert response.status_code == 200
        assert "<html" in response.text.lower()
        health = requests.get(url + "/healthz", timeout=5)
        assert health.json()["ok"] is True
    finally:
        app.quit()


# ---------------------------------------------------------------------------
# Build tooling
# ---------------------------------------------------------------------------


def test_spec_contains_entry_point_and_assets() -> None:
    spec = build.build_spec(icon=None, onefile=True)
    assert "Analysis(" in spec and "EXE(" in spec
    assert "__main__.py" in spec
    assert "local_agent/web/templates" in spec
    assert "local_agent/web/static" in spec
    assert "webview" in spec and "pystray" in spec
    assert build.EXE_NAME in spec


def test_spec_onedir_variant_collects() -> None:
    assert "COLLECT(" in build.build_spec(icon=None, onefile=False)
    assert "COLLECT(" not in build.build_spec(icon=None, onefile=True)


def test_spec_can_be_written(tmp_path: Path) -> None:
    path = build.write_spec(tmp_path / "app.spec", icon=None)
    assert path.is_file()
    assert "Analysis(" in path.read_text(encoding="utf-8")


def test_web_assets_are_discovered() -> None:
    pairs = build.web_datas()
    destinations = {dst for _, dst in pairs}
    assert "local_agent/web/templates" in destinations
    assert "local_agent/web/static" in destinations


def test_installer_script_is_well_formed() -> None:
    text = (Path(build.__file__).resolve().parent / "installer.iss").read_text(encoding="utf-8")
    for section in ("[Setup]", "[Files]", "[Icons]", "[Run]", "[Tasks]", "[Code]"):
        assert section in text
    assert "PersianLocalAssistant.exe" in text
    assert "WebView2" in text  # runtime check present
