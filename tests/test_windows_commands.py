"""Windows shell selection and shell-failure resilience for run_command.

None of these tests require Windows; the platform layer is indirection-based so
the Windows code paths are exercised on any CI host.
"""

from pathlib import Path
import subprocess

import pytest

import agent.tools as tools_module
from agent.tools import LocalTools


@pytest.fixture
def tools(tmp_path: Path) -> LocalTools:
    return LocalTools(tmp_path, timeout=5, max_output=2000)


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "dir listing") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_run_command_uses_cmd_exe_on_windows(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> _Completed:
        captured["argv"] = args[0]
        captured["env"] = kwargs.get("env", {})
        return _Completed(returncode=0, stdout="file.txt")

    monkeypatch.setattr(tools_module, "_platform_name", lambda: "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setattr(tools_module.subprocess, "run", fake_run)

    result = tools.run_command("dir")

    assert captured["argv"] == [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", "dir"]
    assert "bash" not in captured["argv"][0]
    assert "exit code: 0" in result.text
    env = captured["env"]
    assert env["COMSPEC"] == r"C:\Windows\System32\cmd.exe"
    assert "SystemRoot" in env


def test_comspec_falls_back_to_systemroot(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    monkeypatch.delenv("COMSPEC", raising=False)
    monkeypatch.setenv("SystemRoot", r"D:\WinNT")
    assert tools_module._windows_comspec() == r"D:\WinNT\System32\cmd.exe"

    monkeypatch.delenv("SystemRoot", raising=False)
    assert tools_module._windows_comspec() == r"C:\Windows\System32\cmd.exe"


def test_run_command_uses_bash_on_posix(monkeypatch: pytest.MonkeyPatch, tools: LocalTools) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> _Completed:
        captured["argv"] = args[0]
        return _Completed()

    monkeypatch.setattr(tools_module, "_platform_name", lambda: "posix")
    monkeypatch.setattr(tools_module.subprocess, "run", fake_run)

    tools.run_command("ls")
    assert captured["argv"] == ["bash", "-lc", "ls"]


def test_missing_shell_returns_result_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    def fake_run(*args: object, **kwargs: object) -> _Completed:
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(tools_module, "_platform_name", lambda: "nt")
    monkeypatch.setenv("COMSPEC", r"C:\missing\cmd.exe")
    monkeypatch.setattr(tools_module.subprocess, "run", fake_run)

    result = tools.run_command("dir")

    assert "exit code: unavailable" in result.text
    assert r"C:\missing\cmd.exe" in result.text
    assert not result.changed


def test_shell_oserror_also_degrades_cleanly(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    def fake_run(*args: object, **kwargs: object) -> _Completed:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tools_module.subprocess, "run", fake_run)

    result = tools.run_command("printf hi")
    assert "exit code: unavailable" in result.text
    assert not result.changed


def test_timeout_result_is_still_reported(
    monkeypatch: pytest.MonkeyPatch, tools: LocalTools
) -> None:
    def fake_run(*args: object, **kwargs: object) -> _Completed:
        raise subprocess.TimeoutExpired(cmd=["bash"], timeout=5, output="partial")

    monkeypatch.setattr(tools_module.subprocess, "run", fake_run)

    result = tools.run_command("sleep 99")
    assert "زمان دستور" in result.text
    assert "partial" in result.text
