"""Tests for the self-check ("doctor") report."""

from __future__ import annotations

import json
from pathlib import Path

from local_agent import diagnostics as dx
from local_agent.core.config import AssistantSettings, LLMSettings


def _settings(tmp_path: Path, **llm: object) -> AssistantSettings:
    base = dict(provider="openai_compatible", openai_base_url="http://127.0.0.1:1/v1",
                openai_api_key="sk-test", openai_model="m")
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
