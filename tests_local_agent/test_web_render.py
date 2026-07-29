"""Browser-level tests for the redesigned web UI.

These drive a real Chromium through Playwright, so they verify what the
static-markup tests in ``test_web.py`` cannot: that Alpine actually
mounts, that markdown and syntax highlighting render, and that the
responsive breakpoints behave.

The whole module skips when Playwright or a browser binary is missing,
so a plain ``pip install -r requirements.txt`` checkout still gets a
green suite.  To run them:

    pip install playwright && playwright install chromium
    pytest tests_local_agent/test_web_render.py

Set ``PLAYWRIGHT_CHROMIUM_PATH`` to point at an existing Chromium build
instead of the Playwright-managed one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest


playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

INDEX = (
    Path(__file__).resolve().parents[1] / "local_agent" / "web" / "templates" / "index.html"
)
INDEX_URL = INDEX.as_uri()

LAUNCH_ARGS = ["--no-sandbox", "--disable-gpu"]


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with playwright_api.sync_playwright() as pw:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None
        try:
            instance = pw.chromium.launch(executable_path=executable, args=LAUNCH_ARGS)
        except Exception as exc:  # noqa: BLE001 - no browser binary available
            pytest.skip(f"chromium is not available: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def page(browser):  # noqa: ANN001 - Playwright types are dynamic
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on(
        "console",
        lambda msg: errors.append(msg.text) if msg.type == "error" else None,
    )
    page.goto(INDEX_URL)
    page.wait_for_selector("#app", state="attached")
    page.wait_for_timeout(1200)  # let Alpine mount and render
    page.errors = errors  # type: ignore[attr-defined]
    yield page
    page.close()


def state(page, expression: str):  # noqa: ANN001
    """Read a value off the Alpine component."""
    return page.evaluate(f"Alpine.$data(document.getElementById('app')).{expression}")


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------


def test_page_renders_without_javascript_errors(page) -> None:  # noqa: ANN001
    assert page.errors == [], f"console/page errors: {page.errors}"
    assert page.title()


def test_alpine_component_mounts(page) -> None:  # noqa: ANN001
    assert page.evaluate("typeof window.Alpine") == "object"
    assert page.evaluate("typeof window.assistantApp") == "function"
    assert state(page, "theme") == "dark"


def test_offline_showcase_activates_from_file_url(page) -> None:  # noqa: ANN001
    # Opened over file://, the UI must still be reviewable.
    assert state(page, "connection") == "offline"
    assert state(page, "messages.length") > 0
    assert state(page, "actions.length") > 0


def test_core_regions_are_visible(page) -> None:  # noqa: ANN001
    for selector in (".topbar", ".chat", ".composer", ".panel", ".pill--status"):
        assert page.is_visible(selector), f"not visible: {selector}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_markdown_and_syntax_highlighting_render(page) -> None:  # noqa: ANN001
    assert page.locator(".markdown").count() > 0
    assert page.locator(".code-block").count() > 0
    # highlight.js emits span.hljs-* tokens inside the code block
    assert page.locator(".code-block code span").count() > 0


def test_tool_cards_and_approval_dialog_render(page) -> None:  # noqa: ANN001
    assert page.locator(".tool").count() >= 1
    assert page.locator(".tool--done").count() >= 1
    approval = page.locator(".approval").first
    assert approval.is_visible()
    assert approval.locator(".btn--danger").is_visible()   # yes
    assert approval.locator(".btn--ghost").is_visible()    # no


def test_actions_are_grouped_by_category(page) -> None:  # noqa: ANN001
    groups = state(page, "filteredActionGroups.map(g => g.name)")
    assert len(groups) >= 3
    assert page.locator(".group").count() >= 3


def test_empty_state_shows_suggestions(page) -> None:  # noqa: ANN001
    page.evaluate("Alpine.$data(document.getElementById('app')).messages = []")
    page.wait_for_timeout(400)
    assert page.is_visible(".empty")
    assert page.locator(".suggestion").count() >= 3


# ---------------------------------------------------------------------------
# Interaction
# ---------------------------------------------------------------------------


def test_theme_toggle_switches_to_light_and_back(page) -> None:  # noqa: ANN001
    page.evaluate("Alpine.$data(document.getElementById('app')).toggleTheme()")
    page.wait_for_timeout(300)
    assert page.get_attribute("html", "data-theme") == "light"
    page.evaluate("Alpine.$data(document.getElementById('app')).toggleTheme()")
    page.wait_for_timeout(300)
    assert page.get_attribute("html", "data-theme") == "dark"


def test_settings_modal_opens_and_closes(page) -> None:  # noqa: ANN001
    page.evaluate("Alpine.$data(document.getElementById('app')).openSettings()")
    page.wait_for_timeout(400)
    assert page.is_visible(".modal__card")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    assert state(page, "settingsOpen") is False


def test_history_sidebar_toggles(page) -> None:  # noqa: ANN001
    page.evaluate("Alpine.$data(document.getElementById('app')).historyOpen = true")
    page.wait_for_timeout(400)
    assert page.is_visible(".sidebar")
    assert page.locator(".conv").count() >= 1


def test_approval_buttons_resolve_the_request(page) -> None:  # noqa: ANN001
    page.locator(".approval .btn--danger").first.click()
    page.wait_for_timeout(300)
    resolved = state(
        page, "messages.filter(m => m.role === 'approval' && m.resolved).length"
    )
    assert resolved >= 1


def test_composer_accepts_typing(page) -> None:  # noqa: ANN001
    page.fill(".composer__box textarea", "سلام")
    assert state(page, "draft") == "سلام"


def test_export_produces_markdown(page) -> None:  # noqa: ANN001
    markdown = page.evaluate(
        "Alpine.$data(document.getElementById('app')).toMarkdown()"
    )
    assert "# " in markdown
    assert "🤖" in markdown or "👤" in markdown


# ---------------------------------------------------------------------------
# Responsive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "width,height,label",
    [(390, 844, "phone"), (834, 1112, "tablet"), (1440, 900, "desktop")],
)
def test_layout_has_no_horizontal_overflow(browser, width, height, label) -> None:  # noqa: ANN001
    page = browser.new_page(viewport={"width": width, "height": height})
    try:
        page.goto(INDEX_URL)
        page.wait_for_timeout(1200)
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 2, f"{label} viewport overflows by {overflow}px"
        assert page.is_visible(".composer__box")
    finally:
        page.close()


def test_side_panel_is_hidden_on_phones(browser) -> None:  # noqa: ANN001
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(INDEX_URL)
        page.wait_for_timeout(1200)
        # off-canvas until the user opens it
        assert page.evaluate(
            "getComputedStyle(document.querySelector('.panel')).transform !== 'none'"
        )
        page.evaluate("Alpine.$data(document.getElementById('app')).panelOpen = true")
        page.wait_for_timeout(500)
        assert page.is_visible(".panel")
    finally:
        page.close()
