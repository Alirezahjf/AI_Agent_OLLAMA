from pathlib import Path
import pytest

from agent.tools import LocalTools, ToolError


@pytest.fixture
def tools(tmp_path: Path) -> LocalTools:
    return LocalTools(tmp_path, timeout=5, max_output=2000)


def test_read_and_write_stay_in_workspace(tools: LocalTools) -> None:
    tools.write_file("src/a.py", "print('ok')\n")
    result = tools.read_file("src/a.py")
    assert "print('ok')" in result.text
    with pytest.raises(ToolError):
        tools.read_file("../secret.txt")


def test_command_output_and_exit_code(tools: LocalTools) -> None:
    result = tools.run_command("printf hello")
    assert "hello" in result.text
    assert "exit code: 0" in result.text


def test_obviously_destructive_command_is_blocked(tools: LocalTools) -> None:
    with pytest.raises(ToolError):
        tools.run_command("rm -rf /")


def test_mutations_need_approval(tools: LocalTools) -> None:
    assert not tools.requires_approval("run_command", {"command": "git status"})
    assert tools.requires_approval("run_command", {"command": "pytest -q"})
    assert tools.requires_approval("write_file", {"path": "x", "content": "x"})
