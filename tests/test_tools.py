import os
from pathlib import Path

import pytest

from agent.tools import LocalTools, ToolError, normalize_tool_args, normalize_tool_text


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


def test_read_many_files_and_search_files(tools: LocalTools) -> None:
    tools.write_file("src/app.py", "def add(a, b):\n    return a + b\n")
    tools.write_file("tests/test_app.py", "from src.app import add\nassert add(1, 2) == 3\n")

    bundle = tools.read_many_files(["src/app.py", "tests/test_app.py"])
    assert "===== src/app.py =====" in bundle.text
    assert "return a + b" in bundle.text
    assert "===== tests/test_app.py =====" in bundle.text

    found = tools.search_files("add", file_glob="*.py")
    assert "src/app.py:1" in found.text
    assert "tests/test_app.py:1" in found.text


def test_search_files_respects_sensitive_and_binary_boundaries(tools: LocalTools) -> None:
    tools.write_file("visible.txt", "needle here\n")
    (tools.root / ".env").write_text("needle secret", encoding="utf-8")
    (tools.root / "binary.bin").write_bytes(b"\xff\xfe needle")

    result = tools.search_files("needle")

    assert "visible.txt:1" in result.text
    assert ".env" not in result.text
    assert "binary.bin" not in result.text


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


# --------------------------------------------------------------------------
# Markdown-link / HTML-entity normalisation of model arguments
# --------------------------------------------------------------------------


def test_normalizer_strips_simple_markdown_links() -> None:
    assert normalize_tool_text("py -3 check_[pw.py](http://pw.py)") == "py -3 check_pw.py"
    assert normalize_tool_text("python [calculator.py](http://calculator.py)") == "python calculator.py"


def test_normalizer_unwraps_nested_chat_mangled_links() -> None:
    mangled = "check_[[pw.py](http://pw.py)]([http://pw.py](http://pw.py))"
    assert normalize_tool_text(mangled) == "check_pw.py"


def test_normalizer_decodes_html_entities_repeatedly() -> None:
    assert normalize_tool_text("dir &amp;&amp; tree") == "dir && tree"
    assert normalize_tool_text("&amp;amp;&amp;amp;") == "&&"


def test_normalizer_never_keeps_hidden_link_urls() -> None:
    # Only the visible text survives; the URL inside the link must not execute.
    cleaned = normalize_tool_text("python [app.py](https://evil.example/app.py)")
    assert cleaned == "python app.py"
    assert "evil.example" not in cleaned


def test_normalizer_rejects_truncated_markdown_link() -> None:
    with pytest.raises(ToolError, match="Markdown link"):
        normalize_tool_text("py -3 check_[pw.py](http://pw.py")


def test_normalizer_leaves_plain_parentheses_and_brackets_alone() -> None:
    assert normalize_tool_text("pytest tests/test_x.py::test_a[param]") == "pytest tests/test_x.py::test_a[param]"
    assert normalize_tool_text("C:\\Users\\me\\Desktop project (1)\\file.py") == "C:\\Users\\me\\Desktop project (1)\\file.py"


def test_normalize_tool_args_only_touches_path_like_keys() -> None:
    args = {
        "command": "py -3 [x.py](http://x.py)",
        "content": "[keep this link](https://example.com) exactly",
    }
    cleaned = normalize_tool_args("write_file", args)
    assert cleaned["command"] == "py -3 x.py"
    assert cleaned["content"] == "[keep this link](https://example.com) exactly"


def test_invoke_normalizes_command_before_execution(tools: LocalTools) -> None:
    result = tools.invoke("run_command", {"command": "printf hello-[ok](http://gone.example)"})
    assert "exit code: 0" in result.text
    assert "hello-ok" in result.text
    assert "gone.example" not in result.text


# --------------------------------------------------------------------------
# Destructive Windows commands and outside-workspace paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "del /f /q secret.txt",
        "erase output.log",
        "rmdir /s /q build",
        "rd junk",
        "format c:",
        "diskpart",
        "Remove-Item -Recurse folder",
        "cmd /c del important.txt",
        'powershell -Command "Remove-Item x"',
    ],
)
def test_windows_destructive_commands_are_hard_blocked(tools: LocalTools, command: str) -> None:
    assert tools._command_is_hard_blocked(command)
    with pytest.raises(ToolError):
        tools.run_command(command)


def test_format_as_argument_is_not_blocked(tools: LocalTools) -> None:
    assert not tools._command_is_hard_blocked("npm run format")
    assert not tools._command_is_hard_blocked("git log --format=%H")
    assert not tools._command_is_hard_blocked("python -m ruff format agent")


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/passwd",
        "type C:\\Users\\victim\\AppData\\Local\\ms-playwright\\chrome.exe",
        "dir C:\\Users\\victim\\Documents",
        "ls \\\\server\\share",
        "cat ../../../etc/shadow",
        "cd .. && dir",
        'py -3 "C:\\Users\\victim\\Desktop\\other_folder\\check_pw.py"',
    ],
)
def test_commands_referencing_outside_paths_are_rejected(tools: LocalTools, command: str) -> None:
    with pytest.raises(ToolError, match="WORKSPACE_ROOT"):
        tools.run_command(command)


@pytest.mark.skipif(os.name == "nt", reason="POSIX absolute-path scenario")
def test_absolute_path_inside_workspace_is_allowed(tools: LocalTools) -> None:
    tools.write_file("note.txt", "inside-note")
    result = tools.run_command(f"cat {tools.root}/note.txt")
    assert "inside-note" in result.text
    assert "exit code: 0" in result.text


def test_urls_in_commands_are_not_treated_as_paths(tools: LocalTools) -> None:
    assert not tools._command_mentions_outside_paths("git clone https://github.com/user/repo.git")
    assert not tools._command_mentions_outside_paths("pip install https://example.com/pkg.whl")
    assert not tools._command_mentions_outside_paths("git log main..HEAD~1")


# --------------------------------------------------------------------------
# Read-only classification (POSIX + Windows)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["dir", "dir /s /b", "tree", "type report.txt", "where python", 'findstr /s "TODO" *.py'],
)
def test_windows_read_only_commands_are_recognised(command: str) -> None:
    assert LocalTools.is_read_only(command)


def test_windows_command_chain_is_not_read_only() -> None:
    assert not LocalTools.is_read_only("cd /d folder && dir")
    assert not LocalTools.is_read_only("dir & tree")


def test_windows_read_only_commands_need_no_approval(tools: LocalTools) -> None:
    assert not tools.requires_approval("run_command", {"command": "dir"})
    assert not tools.requires_approval("run_command", {"command": "where python"})
    # Chains still require approval even if every segment is read-only.
    assert tools.requires_approval("run_command", {"command": "cd /d folder && dir"})


def test_blocked_or_sensitive_commands_still_require_the_approval_gate(tools: LocalTools) -> None:
    assert tools.requires_approval("run_command", {"command": "cat .env"})
    assert tools.requires_approval("run_command", {"command": "del file.txt"})
    assert tools.requires_approval("run_command", {"command": "cat /etc/passwd"})


def test_diagnose_browser_runtime_needs_no_approval(tools: LocalTools) -> None:
    assert not tools.requires_approval("diagnose_browser_runtime", {})


def test_volatile_cache_directories_are_hidden_from_listing(tools: LocalTools) -> None:
    tools.write_file("app.py", "print('ok')\n")
    tools.write_file(".gradle/caches/module.txt", "ignore volatile cache")
    result = tools.list_files(".")
    assert "app.py" in result.text
    assert ".gradle" not in result.text
    assert "module.txt" not in result.text


def test_invoke_wraps_os_errors_as_tool_errors(tools: LocalTools) -> None:
    def boom(**kwargs):
        raise FileNotFoundError("volatile cache disappeared")

    tools.read_file = boom  # type: ignore[method-assign]
    with pytest.raises(ToolError, match="خطای فایل/سیستم"):
        tools.invoke("read_file", {"path": "anything.txt"})
