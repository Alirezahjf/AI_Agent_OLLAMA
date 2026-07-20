from pathlib import Path
from typing import Any

from agent.brain import OllamaAgent
from agent.config import Settings
from agent.providers import ModelReply, ToolCall
from agent.storage import Storage
from agent.tools import LocalTools


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


def settings(root: Path) -> Settings:
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
        auto_approve_mutations=False,
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
