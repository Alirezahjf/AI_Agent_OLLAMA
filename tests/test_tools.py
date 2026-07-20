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


def test_sensitive_files_are_never_exposed_or_written(tools: LocalTools) -> None:
    with pytest.raises(ToolError):
        tools.write_file(".env", "TOKEN=not-for-the-agent")
    with pytest.raises(ToolError):
        tools.run_command("cat .env")


def test_patch_requires_one_exact_match(tools: LocalTools) -> None:
    tools.write_file("src/app.py", "answer = 1\n")
    result = tools.patch_file("src/app.py", "answer = 1", "answer = 42")
    assert result.changed
    assert "42" in tools.read_file("src/app.py").text
    with pytest.raises(ToolError, match="دقیقاً یک"):
        tools.patch_file("src/app.py", "missing", "anything")


def test_command_output_and_exit_code(tools: LocalTools) -> None:
    result = tools.run_command("printf hello")
    assert "hello" in result.text
    assert "exit code: 0" in result.text


def test_obviously_destructive_command_is_blocked(tools: LocalTools) -> None:
    with pytest.raises(ToolError):
        tools.run_command("rm -rf /")


def test_mutations_need_approval_and_chaining_is_not_read_only(tools: LocalTools) -> None:
    assert not tools.requires_approval("run_command", {"command": "git status"})
    assert tools.requires_approval("run_command", {"command": "pytest -q"})
    assert tools.requires_approval("run_command", {"command": "git status; touch pwned"})
    assert tools.requires_approval("write_file", {"path": "x", "content": "x"})


def test_directory_organisation_previews_then_moves_without_overwriting(tools: LocalTools) -> None:
    tools.create_directory("downloads")
    tools.write_file("downloads/photo.png", "not a real png")
    tools.write_file("downloads/notes.txt", "hello")
    tools.create_directory("downloads/Documents")
    tools.write_file("downloads/Documents/notes.txt", "keep this")

    preview = tools.organize_files("downloads", apply=False)
    assert "حالت پیش‌نمایش" in preview.text
    assert "photo.png" in preview.text
    assert tools._path("downloads/photo.png").exists()

    applied = tools.organize_files("downloads", apply=True)
    assert applied.changed
    assert tools._path("downloads/Images/photo.png").is_file()
    # Existing destination is protected; the source remains untouched.
    assert tools._path("downloads/notes.txt").is_file()
    assert tools._path("downloads/Documents/notes.txt").read_text() == "keep this"
