"""Tests for unique screenshot naming (P4).

Two back-to-back captures must produce two different files, a custom
name that already exists must get a numeric suffix (never overwrite),
and each artifact URL must keep serving *its own* file so old chat
messages never show a newer image.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from local_agent.actions import run_action
from local_agent.bridge.api.handlers import BridgeHandlers, _collect_artifacts
from local_agent.core.config import AssistantSettings
from local_agent.web.app import resolve_artifact_path


@pytest.fixture
def ctx(tmp_path: Path) -> BridgeHandlers:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path)
    handlers = BridgeHandlers.build(settings)
    handlers.gate.auto_approve()
    return handlers


@pytest.fixture
def fake_camera(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Replace the real screen grabber with a deterministic PNG factory."""
    import local_agent.automation.screenshot as screenshot_mod
    from local_agent.automation.screenshot import Screenshot

    produced: list[bytes] = []

    def fake_take(monitor: int = 0) -> Screenshot:
        color = (20 + len(produced) * 40) % 255
        image = Image.new("RGB", (8, 8), (color, color, color))
        shot = Screenshot(image=image, taken_at=0.0, backend="fake")
        produced.append(shot.to_bytes())
        return shot

    monkeypatch.setattr(screenshot_mod, "take_screenshot", fake_take)
    return produced


def test_two_captures_produce_two_different_files(
    tmp_path: Path, ctx, fake_camera
) -> None:
    registry = ctx.registry
    first = run_action(registry, "screen_capture", {}, ctx)
    second = run_action(registry, "screen_capture", {}, ctx)
    assert first != second
    shots = list((tmp_path / "screenshots").glob("screen-*.png"))
    assert len(shots) == 2
    assert shots[0].read_bytes() != shots[1].read_bytes()
    # Both names must appear in the tool outputs so the LLM keeps stable
    # refs; each capture names its own file (no overwriting).
    name1 = first.rsplit("/", 1)[-1].split(")", 1)[0]
    name2 = second.rsplit("/", 1)[-1].split(")", 1)[0]
    assert name1 in first and name1 != name2
    assert name2 in second


def test_custom_name_gets_numeric_suffix_when_exists(
    tmp_path: Path, ctx, fake_camera
) -> None:
    registry = ctx.registry
    first = run_action(registry, "screen_capture", {"filename": "my_shot.png"}, ctx)
    second = run_action(registry, "screen_capture", {"filename": "my_shot.png"}, ctx)
    assert "my_shot.png" in first
    assert "my_shot-1.png" in second
    names = sorted(p.name for p in (tmp_path / "screenshots").iterdir())
    assert names == ["my_shot-1.png", "my_shot.png"]


def test_artifact_paths_resolve_to_their_own_file(
    tmp_path: Path, ctx, fake_camera
) -> None:
    settings = ctx.settings
    registry = ctx.registry
    first = run_action(registry, "screen_capture", {}, ctx)
    second = run_action(registry, "screen_capture", {}, ctx)

    artifacts1 = _collect_artifacts(first, settings)
    artifacts2 = _collect_artifacts(second, settings)
    assert len(artifacts1) == 1 and len(artifacts2) == 1
    assert artifacts1[0]["path"] != artifacts2[0]["path"]

    # Each artifact path resolves to a *different* existing file, and the
    # old path still points at the old file (messages never change image).
    resolved1 = resolve_artifact_path(settings.work_dir, settings.data_dir, artifacts1[0]["path"])
    resolved2 = resolve_artifact_path(settings.work_dir, settings.data_dir, artifacts2[0]["path"])
    assert resolved1.is_file() and resolved2.is_file()
    assert resolved1 != resolved2
    assert resolved1.read_bytes() == fake_camera[0]
    assert resolved2.read_bytes() == fake_camera[1]


def test_old_artifact_url_still_serves_old_image(web_server, ctx, fake_camera) -> None:
    """End-to-end: the /api/artifact URL of the first shot keeps its own bytes."""
    import requests

    settings = ctx.settings
    registry = ctx.registry
    first = run_action(registry, "screen_capture", {}, ctx)
    second = run_action(registry, "screen_capture", {}, ctx)
    path1 = _collect_artifacts(first, settings)[0]["path"]
    _ = _collect_artifacts(second, settings)

    base = f"http://127.0.0.1:{web_server.port}"
    # The web_server fixture shares tmp_path, so the file is already
    # visible to the artifact endpoint through data_dir.
    r = requests.get(base + "/api/artifact", params={"path": path1}, timeout=3)
    assert r.status_code == 200
    assert r.content == fake_camera[0]
    assert r.content != fake_camera[1]
