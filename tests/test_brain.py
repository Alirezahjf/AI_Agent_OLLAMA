from pathlib import Path
from typing import Any


from agent.brain import OllamaAgent, _command_rules
from agent.config import Settings
from agent.providers import ModelReply, ToolCall
from agent.storage import Storage
from agent.tools import LocalTools, ToolResult


class ScriptedClient:
    model = "test-model"

    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.messages: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> ModelReply:
        self.messages.append(messages)
        return self.replies.pop(0)


class FakeRouter:
    def __init__(self, client: ScriptedClient) -> None:
        self.client = client

    def client_for(self, chat_id: int, provider: str, model: str | None = None) -> ScriptedClient:
        return self.client


def settings(root: Path, auto_approve: bool = False) -> Settings:
    (root / "data").mkdir(exist_ok=True)
    return Settings(
        telegram_token="token",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model="test-model",
        default_provider="ollama",
        default_model="",
        gapgpt_api_key="",
        gapgpt_base_url="https://gap.example/v1",
        avalai_api_key="",
        avalai_base_url="https://aval.example/v1",
        allowed_user_ids=frozenset(),
        workspace_root=root,
        data_dir=root / "data",
        command_timeout=5,
        max_output_chars=2000,
        max_agent_turns=4,
        model_timeout=10,
        auto_approve_mutations=auto_approve,
    )


def test_agent_inspects_then_reports_with_real_tool_result(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    client = ScriptedClient(
        [
            ModelReply("", (ToolCall("list_files", {"path": ".", "depth": 1}),)),
            ModelReply("بررسی انجام شد؛ hello.txt وجود دارد."),
        ]
    )
    storage = Storage(tmp_path / "data" / "agent.sqlite3")
    tools = LocalTools(tmp_path, timeout=5, max_output=2000)
    agent = OllamaAgent(settings(tmp_path), storage, tools, FakeRouter(client))  # type: ignore[arg-type]

    final, pending, result = agent.run(1, "پوشه را بررسی کن", lambda _: None)

    assert pending is None
    assert final == "بررسی انجام شد؛ hello.txt وجود دارد."
    assert result is not None and "hello.txt" in result.text
    # The second model turn has the untrusted-data boundary around the genuine tool result.
    assert any("[TOOL_RESULT" in item["content"] for item in client.messages[1])


def test_agent_returns_mutation_as_pending_for_telegram_consent(tmp_path: Path) -> None:
    client = ScriptedClient(
        [ModelReply("", (ToolCall("write_file", {"path": "app.py", "content": "print(1)"}),))]
    )
    storage = Storage(tmp_path / "data" / "agent.sqlite3")
    agent = OllamaAgent(
        settings(tmp_path), storage, LocalTools(tmp_path, 5, 2000), FakeRouter(client)  # type: ignore[arg-type]
    )

    final, pending, result = agent.run(1, "یک فایل بساز", lambda _: None)

    assert final is None
    assert result is None
    assert pending is not None and pending.tool == "write_file"
    assert not (tmp_path / "app.py").exists()


def test_agent_normalizes_mangled_file_links_in_pending_args(tmp_path: Path) -> None:
    client = ScriptedClient(
        [ModelReply("", (ToolCall("run_command", {"command": "py -3 check_[pw.py](http://pw.py)"}),))]
    )
    storage = Storage(tmp_path / "data" / "agent.sqlite3")
    agent = OllamaAgent(
        settings(tmp_path), storage, LocalTools(tmp_path, 5, 2000), FakeRouter(client)  # type: ignore[arg-type]
    )

    final, pending, _ = agent.run(1, "اسکریپت را اجرا کن", lambda _: None)

    assert final is None
    assert pending is not None
    assert pending.args["command"] == "py -3 check_pw.py"


def test_agent_keeps_artifacts_when_later_tools_are_read_only(tmp_path: Path) -> None:
    """A screenshot taken mid-workflow must survive subsequent read-only steps."""
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    client = ScriptedClient(
        [
            ModelReply(
                "",
                (ToolCall("capture_screenshot", {"url": "https://example.com", "output_path": "shot.png"}),),
            ),
            ModelReply("", (ToolCall("list_files", {"path": ".", "depth": 1}),)),
            ModelReply("اسکرین‌شات ساخته شد و پوشه هم بررسی شد."),
        ]
    )
    storage = Storage(tmp_path / "data" / "agent.sqlite3")
    tools = LocalTools(tmp_path, timeout=5, max_output=2000)
    real_invoke = tools.invoke

    def fake_invoke(name: str, args: dict[str, Any]) -> ToolResult:
        if name == "capture_screenshot":
            return ToolResult("اسکرین‌شات ذخیره شد: shot.png", changed=True, artifacts=(png,))
        return real_invoke(name, args)

    tools.invoke = fake_invoke  # type: ignore[method-assign]
    agent = OllamaAgent(
        settings(tmp_path, auto_approve=True), storage, tools, FakeRouter(client)  # type: ignore[arg-type]
    )

    final, pending, result = agent.run(1, "اسکرین‌շات بگیر و پوشه را هم ببین", lambda _: None)

    assert pending is None
    assert final is not None
    assert result is not None
    assert result.artifacts == (png,)
    # The last tool result text is preserved, so the terminal flow still works.
    assert "shot.png" in result.text


def test_command_rules_cover_windows_and_posix() -> None:
    windows = _command_rules("nt")
    assert "cmd.exe" in windows
    assert "dir" in windows and "py -3" in windows
    assert "diagnose_browser_runtime" in windows
    assert "exit code" in windows
    assert "check_pw.py" in windows

    posix = _command_rules("posix")
    assert "bash" in posix
    assert "diagnose_browser_runtime" in posix


def test_system_prompt_includes_os_environment(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    client = ScriptedClient([ModelReply("انجام شد.")])
    storage = Storage(tmp_path / "data" / "agent.sqlite3")
    tools = LocalTools(tmp_path, timeout=5, max_output=2000)
    agent = OllamaAgent(settings(tmp_path), storage, tools, FakeRouter(client))  # type: ignore[arg-type]

    agent.run(1, "چیزی نکن", lambda _: None)

    system = client.messages[0][0]["content"]
    assert "محیط اجرای command" in system
    assert "Markdown link" in system
