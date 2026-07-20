"""capture_screenshot and diagnose_browser_runtime with a fake Playwright runtime."""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest

from agent.tools import LocalTools, ToolError, tool_definitions


class FakePlaywrightError(Exception):
    pass


class Recorder:
    def __init__(self) -> None:
        self.launch_kwargs: dict[str, Any] | None = None
        self.new_page_kwargs: dict[str, Any] | None = None
        self.goto: tuple[str, str, int] | None = None
        self.screenshot: tuple[str, bool] | None = None
        self.browser_closed = False
        self.goto_error: Exception | None = None
        self.launch_error: Exception | None = None
        self.executable_path = r"C:\fake-cache\chromium\chrome.exe"


class _FakePage:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    def goto(self, url: str, wait_until: str, timeout: int) -> None:
        if self.recorder.goto_error:
            raise self.recorder.goto_error
        self.recorder.goto = (url, wait_until, timeout)

    def screenshot(self, path: str, full_page: bool) -> None:
        self.recorder.screenshot = (path, full_page)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake-image")


class _FakeBrowser:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    def new_page(self, **kwargs: Any) -> _FakePage:
        self.recorder.new_page_kwargs = kwargs
        return _FakePage(self.recorder)

    def close(self) -> None:
        self.recorder.browser_closed = True


class _FakeChromium:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.executable_path = recorder.executable_path

    def launch(self, **kwargs: Any) -> _FakeBrowser:
        if self.recorder.launch_error:
            raise self.recorder.launch_error
        self.recorder.launch_kwargs = kwargs
        return _FakeBrowser(self.recorder)


class _FakePlaywrightManager:
    def __init__(self, recorder: Recorder) -> None:
        self.chromium = _FakeChromium(recorder)


class _FakeSyncPlaywright:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    def __call__(self) -> "_FakeSyncPlaywright":
        return self

    def __enter__(self) -> _FakePlaywrightManager:
        return _FakePlaywrightManager(self.recorder)

    def __exit__(self, *exc: Any) -> None:
        return None


def install_fake_playwright(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    package = ModuleType("playwright")
    sync_api = ModuleType("playwright.sync_api")
    sync_api.Error = FakePlaywrightError  # type: ignore[attr-defined]
    sync_api.sync_playwright = _FakeSyncPlaywright(recorder)  # type: ignore[attr-defined]
    package.sync_api = sync_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def block_playwright_import(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry in sys.modules makes `import playwright` raise ImportError.
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


@pytest.fixture
def tools(tmp_path: Path) -> LocalTools:
    return LocalTools(tmp_path, timeout=5, max_output=2000)


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "javascript:alert(1)", "example.com"])
def test_capture_rejects_non_http_urls(tools: LocalTools, url: str) -> None:
    with pytest.raises(ToolError, match="http"):
        tools.capture_screenshot(url, "shot.png")


def test_capture_rejects_non_png_output(tools: LocalTools) -> None:
    with pytest.raises(ToolError, match="PNG"):
        tools.capture_screenshot("https://example.com", "shot.jpg")


def test_capture_rejects_output_outside_workspace(tools: LocalTools) -> None:
    with pytest.raises(ToolError):
        tools.capture_screenshot("https://example.com", "../shot.png")


def test_capture_success_returns_png_artifact(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    recorder = Recorder()
    install_fake_playwright(monkeypatch, recorder)

    result = tools.capture_screenshot("https://example.com", "shots/example.png", full_page=True)

    target = tools.root / "shots" / "example.png"
    assert result.changed
    assert result.artifacts == (target,)
    assert target.is_file() and target.read_bytes().startswith(b"\x89PNG")
    assert recorder.launch_kwargs == {"headless": True}
    assert recorder.new_page_kwargs is not None
    assert recorder.new_page_kwargs["viewport"] == {"width": 1440, "height": 1000}
    assert recorder.goto is not None and recorder.goto[0] == "https://example.com"
    assert recorder.goto[1] == "networkidle"
    assert recorder.screenshot == (str(target), True)
    assert recorder.browser_closed  # browser is closed even on the happy path


def test_capture_closes_browser_when_navigation_fails(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    recorder = Recorder()
    recorder.goto_error = FakePlaywrightError("net::ERR_CONNECTION_REFUSED at https://dead.example")
    install_fake_playwright(monkeypatch, recorder)

    with pytest.raises(ToolError, match="ERR_CONNECTION_REFUSED"):
        tools.capture_screenshot("https://dead.example", "dead.png")
    # Failure must not leak a Python stack trace into the user-facing message.
    try:
        tools.capture_screenshot("https://dead.example", "dead.png")
    except ToolError as exc:
        assert "Traceback" not in str(exc)
    assert recorder.browser_closed


def test_capture_without_playwright_shows_interpreter_install_command(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    block_playwright_import(monkeypatch)
    with pytest.raises(ToolError) as excinfo:
        tools.capture_screenshot("https://example.com", "shot.png")
    message = str(excinfo.value)
    assert sys.executable in message
    assert "-m pip install playwright" in message
    assert "-m playwright install chromium" in message
    assert "[browser]" not in message  # no vague pip install -e advice


def test_capture_without_chromium_shows_actionable_install_command(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    recorder = Recorder()
    recorder.launch_error = FakePlaywrightError(
        "Executable doesn't exist at C:\\cache\\ms-playwright\\chromium\\chrome.exe\n"
        "Looks like Playwright was just installed or updated."
    )
    install_fake_playwright(monkeypatch, recorder)

    with pytest.raises(ToolError) as excinfo:
        tools.capture_screenshot("https://example.com", "shot.png")
    message = str(excinfo.value)
    assert sys.executable in message
    assert "-m playwright install chromium" in message
    assert "Chromium" in message


def test_diagnose_reports_install_command_when_playwright_missing(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    block_playwright_import(monkeypatch)
    result = tools.invoke("diagnose_browser_runtime", {})
    assert sys.executable in result.text
    assert sys.version.split()[0] in result.text
    assert "playwright.sync_api: ❌" in result.text
    assert "-m pip install playwright" in result.text


def test_diagnose_reports_missing_browser_binary(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    recorder = Recorder()
    install_fake_playwright(monkeypatch, recorder)
    result = tools.diagnose_browser_runtime()
    assert "playwright.sync_api: ✅" in result.text
    assert recorder.executable_path in result.text
    assert "❌ خیر" in result.text  # fake chrome.exe does not exist on disk
    assert "-m playwright install chromium" in result.text


def test_diagnose_confirms_existing_browser_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: LocalTools
) -> None:
    recorder = Recorder()
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"MZ")
    recorder.executable_path = str(fake_chrome)
    install_fake_playwright(monkeypatch, recorder)

    result = tools.diagnose_browser_runtime()
    assert "✅ بله" in result.text
    assert "-m playwright install chromium" not in result.text


def test_diagnose_tool_is_registered_and_needs_no_approval() -> None:
    names = {definition["function"]["name"] for definition in tool_definitions()}
    assert "diagnose_browser_runtime" in names
    assert not LocalTools(Path("."), 5, 1000).requires_approval("diagnose_browser_runtime", {})
