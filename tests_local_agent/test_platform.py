"""Tests for the cross-platform detection and capability layer."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from local_agent.utils.platform import (
    Platform,
    capabilities,
    current_platform,
    has_display,
    is_container,
    is_headless_server,
    is_linux,
    is_macos,
    is_windows,
    is_wsl,
    log_platform_summary,
)


class TestPlatformDetection:
    def test_current_platform_returns_enum(self) -> None:
        plat = current_platform()
        assert isinstance(plat, Platform)

    def test_is_windows_returns_bool(self) -> None:
        assert isinstance(is_windows(), bool)

    def test_is_linux_returns_bool(self) -> None:
        assert isinstance(is_linux(), bool)

    def test_is_macos_returns_bool(self) -> None:
        assert isinstance(is_macos(), bool)

    def test_force_platform_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_FORCE_PLATFORM", "windows")
        assert current_platform() == Platform.WINDOWS
        assert is_windows() is True

        monkeypatch.setenv("LOCAL_AGENT_FORCE_PLATFORM", "linux")
        assert current_platform() == Platform.LINUX
        assert is_linux() is True

        monkeypatch.setenv("LOCAL_AGENT_FORCE_PLATFORM", "macos")
        assert current_platform() == Platform.MACOS
        assert is_macos() is True

    def test_exactly_one_platform_is_true(self) -> None:
        count = sum([is_windows(), is_linux(), is_macos()])
        assert count == 1


class TestDisplayDetection:
    def test_has_display_returns_bool(self) -> None:
        assert isinstance(has_display(), bool)

    def test_has_display_true_with_display_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_FORCE_PLATFORM", "linux")
        monkeypatch.setenv("DISPLAY", ":0")
        assert has_display() is True

    def test_has_display_false_without_display_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_FORCE_PLATFORM", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert has_display() is False


class TestHeadlessServer:
    def test_is_headless_server_returns_bool(self) -> None:
        assert isinstance(is_headless_server(), bool)

    def test_headless_when_linux_no_display(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_FORCE_PLATFORM", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert is_headless_server() is True

    def test_not_headless_when_display(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_FORCE_PLATFORM", "linux")
        monkeypatch.setenv("DISPLAY", ":0")
        assert is_headless_server() is False


class TestContainer:
    def test_is_container_returns_bool(self) -> None:
        assert isinstance(is_container(), bool)


class TestWSL:
    def test_is_wsl_returns_bool(self) -> None:
        assert isinstance(is_wsl(), bool)


class TestCapabilities:
    def test_capabilities_returns_dict(self) -> None:
        caps = capabilities()
        assert isinstance(caps, dict)
        for key in ("gui", "tray", "hotkey", "clipboard", "notifications", "shell",
                     "headless", "container", "wsl"):
            assert key in caps, f"missing key: {key}"
            assert isinstance(caps[key], bool), f"key {key} is not bool"

    def test_shell_always_true(self) -> None:
        assert capabilities()["shell"] is True

    def test_gui_false_on_headless_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCAL_AGENT_FORCE_PLATFORM", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert capabilities()["gui"] is False


class TestLogPlatformSummary:
    def test_log_platform_summary_does_not_crash(self) -> None:
        log_platform_summary()  # Should not raise
