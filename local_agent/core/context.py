"""Runtime context shared by every component.

Holds the live settings, the current conversation, and a tiny pub/sub
for events (e.g. "user approved action X"). Tools and the CLI both read
from this object; nothing else is global.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import AssistantSettings


@dataclass
class ConversationMessage:
    """A single turn in the assistant's conversation."""

    role: str  # 'system' | 'user' | 'assistant' | 'tool'
    content: str
    name: str | None = None  # tool name for tool messages
    tool_call_id: str | None = None  # for OpenAI-compatible providers
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "name": self.name,
            "tool_call_id": self.tool_call_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ConversationMessage":
        return cls(
            role=str(payload.get("role", "user")),
            content=str(payload.get("content", "")),
            name=payload.get("name"),
            tool_call_id=payload.get("tool_call_id"),
            timestamp=float(payload.get("timestamp", time.time())),
        )

    def to_openai(self) -> dict[str, Any]:
        """Render to the OpenAI chat-completions shape."""
        if self.role == "tool":
            return {
                "role": "tool",
                "content": self.content,
                "tool_call_id": self.tool_call_id or "",
            }
        if self.role == "assistant" and self.name:
            return {"role": "assistant", "content": self.content, "name": self.name}
        return {"role": self.role, "content": self.content}


class RuntimeContext:
    """Shared, thread-safe runtime state for the local assistant.

    The CLI thread writes user messages; tool threads append tool results;
    the LLM thread reads a snapshot. A re-entrant lock keeps the snapshot
    consistent.
    """

    def __init__(self, settings: AssistantSettings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._messages: deque[ConversationMessage] = deque()
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._system_prompt: str | None = None
        self._load_history()

    # ------------------------------------------------------------------ I/O

    @property
    def system_prompt(self) -> str:
        with self._lock:
            return self._system_prompt or ""

    def set_system_prompt(self, prompt: str) -> None:
        with self._lock:
            self._system_prompt = prompt

    def append(self, message: ConversationMessage) -> None:
        with self._lock:
            self._messages.append(message)
            self._persist_history_locked()
            self._emit("message", message.to_dict())

    def replace_last_assistant(self, content: str) -> None:
        """Replace the last assistant message in place (for streamed edits)."""
        with self._lock:
            for index in range(len(self._messages) - 1, -1, -1):
                if self._messages[index].role == "assistant":
                    self._messages[index] = ConversationMessage(
                        role="assistant", content=content, timestamp=time.time()
                    )
                    self._persist_history_locked()
                    return

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
            self._persist_history_locked()
            self._emit("cleared", {})

    def snapshot(self) -> list[ConversationMessage]:
        """Return a deep copy of the messages for safe use by another thread."""
        with self._lock:
            return list(self._messages)

    def compact(self, max_messages: int, max_chars: int) -> None:
        """Drop oldest messages until the snapshot fits the budget."""
        with self._lock:
            while len(self._messages) > max_messages:
                self._messages.popleft()
            total = sum(len(m.content) for m in self._messages)
            while total > max_chars and len(self._messages) > 1:
                removed = self._messages.popleft()
                total -= len(removed.content)
            self._persist_history_locked()

    # -------------------------------------------------------- Event system

    def on(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event, payload)
            except Exception:  # noqa: BLE001 - listeners must never break the loop
                pass

    # -------------------------------------------------------- Persistence

    def _load_history(self) -> None:
        path = self.settings.history_path
        if not path.is_file():
            return
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._messages.append(ConversationMessage.from_dict(payload))
        except OSError:
            return

    def _persist_history_locked(self) -> None:
        path = self.settings.history_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for message in self._messages:
                    handle.write(json.dumps(message.to_dict(), ensure_ascii=False))
                    handle.write("\n")
            tmp.replace(path)
        except OSError:
            pass

    # ----------------------------------------------------------- Debug

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "messages": len(self._messages),
                "total_chars": sum(len(m.content) for m in self._messages),
                "history_path": str(self.settings.history_path),
            }
