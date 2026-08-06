"""Tests for the self-check ("doctor") report."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from local_agent import diagnostics as dx
from local_agent.core.config import AssistantSettings, LLMSettings


def _settings(tmp_path: Path, **llm: object) -> AssistantSettings:
    base = {
        "provider": "openai_compatible",
        "openai_base_url": "http://127.0.0.1:1/v1",
        "openai_api_key": "sk-test",
        "openai_model": "m",
    }
    base.update(llm)
    return AssistantSettings(
        data_dir=tmp_path, work_dir=tmp_path / "ws", llm=LLMSettings(**base)  # type: ignore[arg-type]
    )


def test_report_status_rolls_up_to_the_worst_result() -> None:
    report = dx.DoctorReport(results=[
        dx.CheckResult("a", "A", dx.OK),
        dx.CheckResult("b", "B", dx.WARN),
    ])
    assert report.status == dx.WARN
    report.results.append(dx.CheckResult("c", "C", dx.FAIL))
    assert report.status == dx.FAIL


def test_report_renders_persian_text_and_icons() -> None:
    report = dx.DoctorReport(results=[
        dx.CheckResult("a", "بررسی", dx.FAIL, "خراب است", "درستش کن"),
    ])
    text = report.render()
    assert "❌" in text and "بررسی" in text and "درستش کن" in text
    assert "0 سالم" in text


def test_python_check_passes_on_supported_interpreter() -> None:
    assert dx.check_python().status == dx.OK


def test_paths_check_creates_missing_directories(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert not settings.work_dir.exists()
    result = dx.check_paths(settings)
    assert result.status == dx.OK
    assert settings.work_dir.is_dir()


def test_paths_check_fails_on_unwritable_directory(tmp_path: Path) -> None:
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    settings = AssistantSettings(data_dir=blocked / "sub", work_dir=tmp_path)
    assert dx.check_paths(settings).status == dx.FAIL


def test_llm_config_check_flags_missing_api_key(tmp_path: Path) -> None:
    result = dx.check_llm_config(_settings(tmp_path, openai_api_key=""))
    assert result.status == dx.FAIL
    assert "کلید" in result.detail


def test_llm_reachable_check_fails_when_ollama_is_down(tmp_path: Path) -> None:
    settings = _settings(tmp_path, provider="ollama", ollama_base_url="http://127.0.0.1:1")
    result = dx.check_llm_reachable(settings)
    assert result.status == dx.FAIL
    assert "AvalAI" in result.hint


def test_llm_reachable_check_can_skip_the_network(tmp_path: Path) -> None:
    result = dx.check_llm_reachable(_settings(tmp_path), network=False)
    assert result.status == dx.WARN


def test_actions_check_reports_the_registry_size(tmp_path: Path) -> None:
    result = dx.check_actions(_settings(tmp_path))
    assert result.status in {dx.OK, dx.WARN}
    assert result.data["total"] > 10


def test_bots_check_fails_when_a_token_has_no_allowlist(tmp_path: Path) -> None:
    settings = AssistantSettings(data_dir=tmp_path, work_dir=tmp_path, telegram_token="123:abc")
    result = dx.check_bots(settings)
    assert result.status == dx.FAIL
    assert "امنیت" in result.hint


def test_run_checks_produces_a_serialisable_report(tmp_path: Path) -> None:
    report = dx.run_checks(_settings(tmp_path), network=False)
    payload = report.to_dict()
    json.dumps(payload)  # must not raise
    assert payload["results"] and payload["status"] in {dx.OK, dx.WARN, dx.FAIL}
    assert all(r["title"] for r in payload["results"])


def test_a_crashing_check_is_reported_not_raised() -> None:
    def boom() -> dx.CheckResult:
        raise RuntimeError("kaboom")

    result = dx._timed(boom)
    assert result.status == dx.FAIL
    assert "kaboom" in result.detail


def test_main_prints_json(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_CONFIG", str(tmp_path / "config.json"))
    code = dx.main(["--json", "--offline"])
    payload = json.loads(capsys.readouterr().out)
    assert code in {0, 1}
    assert "results" in payload


# ---------------------------------------------------------------------------
# Packaging / installability
# ---------------------------------------------------------------------------


def _write_project(root: Path, pyproject: str, packages: list[str]) -> Path:
    """Build a fake repo tree and return a diagnostics-like __file__ path."""
    for name in packages:
        pkg = root / name
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return root / "local_agent" / "diagnostics.py"


_GOOD = """
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "x"
version = "1.0"

[tool.setuptools.packages.find]
include = ["agent*", "local_agent*"]
"""

_FLAT_LAYOUT_TRAP = """
[project]
name = "x"
version = "1.0"
"""


def test_packaging_check_passes_when_packages_are_explicit(tmp_path, monkeypatch) -> None:
    probe = _write_project(tmp_path, _GOOD, ["agent", "local_agent", "tests_local_agent"])
    monkeypatch.setattr(dx, "__file__", str(probe))
    result = dx.check_packaging()
    assert result.status == dx.OK
    assert result.data["explicit_packages"] is True


def test_packaging_check_catches_the_flat_layout_error(tmp_path, monkeypatch) -> None:
    """This is the exact failure users hit: 'Multiple top-level packages'."""
    probe = _write_project(
        tmp_path, _FLAT_LAYOUT_TRAP, ["agent", "local_agent", "tests_local_agent"]
    )
    monkeypatch.setattr(dx, "__file__", str(probe))
    result = dx.check_packaging()
    assert result.status == dx.FAIL
    assert "packages" in result.hint
    assert set(result.data["top_level"]) == {"agent", "local_agent", "tests_local_agent"}


def test_packaging_check_flags_a_missing_build_system(tmp_path, monkeypatch) -> None:
    probe = _write_project(
        tmp_path,
        '[project]\nname = "x"\nversion = "1.0"\n[tool.setuptools.packages.find]\ninclude = ["a*"]\n',
        ["local_agent"],
    )
    monkeypatch.setattr(dx, "__file__", str(probe))
    result = dx.check_packaging()
    assert result.status == dx.FAIL
    assert "build-system" in result.detail


def test_packaging_check_allows_a_single_top_level_package(tmp_path, monkeypatch) -> None:
    probe = _write_project(
        tmp_path, "[build-system]\nrequires = []\n" + _FLAT_LAYOUT_TRAP, ["local_agent"]
    )
    monkeypatch.setattr(dx, "__file__", str(probe))
    assert dx.check_packaging().status == dx.OK


def test_dependency_check_marks_missing_web_deps_as_fatal(monkeypatch) -> None:
    import importlib.util

    real = importlib.util.find_spec

    def fake(name: str, *a, **k):
        if name in {"uvicorn", "fastapi"}:
            return None
        return real(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    result = dx.check_dependencies()
    assert result.status == dx.FAIL
    assert "uvicorn" in result.detail
    assert "[all]" in result.hint


def test_dependency_check_groups_optional_extras(monkeypatch) -> None:
    import importlib.util

    real = importlib.util.find_spec

    def fake(name: str, *a, **k):
        if name in {"webview", "pystray"}:
            return None
        return real(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    result = dx.check_dependencies()
    # Web deps are present here, so a missing native shell is only a warning.
    assert result.status == dx.WARN
    assert result.data["missing_optional"]["app"] == ["pywebview", "pystray"]
    # The hint must be a runnable command naming every affected extra.
    assert result.hint.startswith("نصب کنید: pip install -e")
    assert "app" in result.hint


# ---------------------------------------------------------------------------


def test_check_interpreter_inside_venv(monkeypatch):
    monkeypatch.setattr(dx.sys, "prefix", "/some/venv")
    monkeypatch.setattr(dx.sys, "base_prefix", "/usr")
    result = dx.check_interpreter()
    assert result.status == dx.OK
    assert result.data.get("venv") is True


def test_check_interpreter_outside_venv_with_dotvenv(tmp_path, monkeypatch):
    (tmp_path / ".venv").mkdir()
    # make __file__ point inside the project so Path calculation works
    monkeypatch.setattr(dx, "__file__", str(tmp_path / "local_agent" / "diagnostics.py"))
    monkeypatch.setattr(dx.sys, "prefix", "/usr")
    monkeypatch.setattr(dx.sys, "base_prefix", "/usr")
    result = dx.check_interpreter()
    assert result.status == dx.FAIL
    assert "فعال نیست" in result.detail


def test_check_interpreter_no_venv(monkeypatch):
    monkeypatch.setattr(dx.sys, "prefix", "/usr")
    monkeypatch.setattr(dx.sys, "base_prefix", "/usr")
    result = dx.check_interpreter()
    assert result.status == dx.WARN


def test_check_dependencies_uses_sys_executable(monkeypatch):
    import importlib.util
    real = importlib.util.find_spec

    def fake(name, *a, **k):
        if name in {"requests"}:
            return None
        return real(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    result = dx.check_dependencies()
    assert result.status == dx.FAIL
    assert sys.executable in result.hint or "python" in result.hint.lower()


def test_check_port_our_server(monkeypatch):
    def fake_is_our(port, timeout=1.5):
        return True

    monkeypatch.setattr(dx, "_is_our_web_server", fake_is_our)
    # simulate bind failure
    import socket as real_socket
    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def bind(self, addr):
            raise OSError("address in use")

    monkeypatch.setattr(real_socket, "socket", lambda *a, **k: FakeSocket())
    result = dx.check_port(_settings(tmp_path=Path("/tmp")), 7824)
    assert result.status == dx.OK
    assert result.data.get("ours") is True


def test_check_port_other_program(monkeypatch):
    def fake_is_our(port, timeout=1.5):
        return False

    monkeypatch.setattr(dx, "_is_our_web_server", fake_is_our)
    import socket as real_socket
    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def bind(self, addr):
            raise OSError("address in use")

    monkeypatch.setattr(real_socket, "socket", lambda *a, **k: FakeSocket())
    result = dx.check_port(_settings(tmp_path=Path("/tmp")), 7824)
    assert result.status == dx.WARN
    assert result.data.get("ours") is False


def test_check_encoding_utf8(monkeypatch):
    # sys.stdout.encoding is read-only; mock the whole stdout object instead.
    monkeypatch.setattr(dx.sys, "stdout", types.SimpleNamespace(encoding="utf-8"))
    result = dx.check_encoding()
    assert result.status == dx.OK
    assert result.data.get("encoding") == "utf8"


def test_check_encoding_cp720_warns(monkeypatch):
    monkeypatch.setattr(dx.sys, "stdout", types.SimpleNamespace(encoding="cp720"))
    result = dx.check_encoding()
    assert result.status == dx.WARN
    assert "OutputEncoding" in result.hint or "PowerShell" in result.hint


# ---------------------------------------------------------------------------
# B2: config consistency (single source of truth + legacy migration check)
# ---------------------------------------------------------------------------


def test_config_consistency_ok_when_config_path_writable(tmp_path: Path) -> None:
    import json

    from local_agent.core.config import load_settings
    from local_agent.diagnostics import check_config_consistency

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path)}), encoding="utf-8")
    settings = load_settings(config)
    result = check_config_consistency(settings)
    assert result.status == "ok"
    assert result.data["writable"] is True


def test_config_consistency_warns_on_stray_legacy_config(tmp_path: Path) -> None:
    import json

    from local_agent.core.config import load_settings
    from local_agent.diagnostics import check_config_consistency

    data_dir = tmp_path / "olddata"
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"llm": {"provider": "ollama"}}), encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(data_dir)}), encoding="utf-8")

    settings = load_settings(config)
    result = check_config_consistency(settings)
    assert result.status == "warn"
    assert "سرگردان" in result.detail


def test_config_consistency_fails_on_unwritable_config_path(tmp_path: Path, monkeypatch) -> None:
    import json

    from local_agent.core.config import load_settings
    from local_agent.diagnostics import check_config_consistency

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path)}), encoding="utf-8")
    settings = load_settings(config)
    # Make the write probe fail.
    def _boom(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = check_config_consistency(settings)
    assert result.status == "fail"
    assert result.data["writable"] is False
