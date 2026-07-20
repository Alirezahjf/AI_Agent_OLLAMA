"""SQLite persistence for conversation context, preferences, and auditable actions.

API keys are intentionally *not* represented in this database.  A key entered in
Telegram lives only in the process memory of ``ProviderRouter`` for that session.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, thread INTEGER NOT NULL DEFAULT 1,
                  role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS message_chat_thread ON messages(chat_id, thread, id);
                CREATE TABLE IF NOT EXISTS chat_state (
                  chat_id INTEGER PRIMARY KEY, thread INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS pending_actions (
                  id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, payload TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chat_preferences (
                  chat_id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                  id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, thread INTEGER NOT NULL,
                  event_type TEXT NOT NULL, detail TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS audit_chat_thread ON audit_events(chat_id, thread, id DESC);
                """
            )

    def thread(self, chat_id: int) -> int:
        with self._connection() as db:
            row = db.execute("SELECT thread FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO chat_state(chat_id) VALUES (?)", (chat_id,))
                return 1
            return int(row["thread"])

    def new_chat(self, chat_id: int) -> int:
        with self._connection() as db:
            db.execute(
                "INSERT INTO chat_state(chat_id,thread) VALUES (?,1) "
                "ON CONFLICT(chat_id) DO UPDATE SET thread=thread+1",
                (chat_id,),
            )
        thread = self.thread(chat_id)
        self.audit(chat_id, "new_thread", f"گفتگوی شمارهٔ {thread} ایجاد شد.")
        return thread

    def reset(self, chat_id: int) -> None:
        thread = self.thread(chat_id)
        with self._connection() as db:
            db.execute("DELETE FROM messages WHERE chat_id=? AND thread=?", (chat_id, thread))
            db.execute("DELETE FROM pending_actions WHERE chat_id=?", (chat_id,))
        self.audit(chat_id, "memory_reset", "حافظهٔ گفتگوی فعلی پاک شد.")

    def add(self, chat_id: int, role: str, content: str) -> None:
        if role not in {"system", "user", "assistant"}:
            raise ValueError("invalid conversation role")
        with self._connection() as db:
            db.execute(
                "INSERT INTO messages(chat_id,thread,role,content) VALUES (?,?,?,?)",
                (chat_id, self.thread(chat_id), role, content),
            )

    def history(
        self, chat_id: int, limit: int = 36, max_chars: int = 55_000
    ) -> list[dict[str, str]]:
        """Return recent context within a character budget, preserving whole messages."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT role,content FROM messages WHERE chat_id=? AND thread=? "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, self.thread(chat_id), limit),
            ).fetchall()
        selected: list[dict[str, str]] = []
        used = 0
        for row in rows:  # newest -> oldest
            content = str(row["content"])
            if len(content) > 14_000:
                content = content[:14_000] + "\n… [خروجی برای حافظه کوتاه شد]"
            if selected and used + len(content) > max_chars:
                break
            selected.append({"role": str(row["role"]), "content": content})
            used += len(content)
        return list(reversed(selected))

    def transcript(self, chat_id: int, limit: int = 18) -> list[dict[str, str]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT role,content,created_at FROM messages WHERE chat_id=? AND thread=? "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, self.thread(chat_id), limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def set_preference(self, chat_id: int, provider: str, model: str = "") -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO chat_preferences(chat_id,provider,model) VALUES (?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET provider=excluded.provider, model=excluded.model, "
                "updated_at=CURRENT_TIMESTAMP",
                (chat_id, provider, model),
            )
        self.audit(chat_id, "provider_changed", f"ارائه‌دهنده: {provider} | مدل: {model or 'پیش‌فرض'}")

    def preference(self, chat_id: int, default_provider: str) -> tuple[str, str]:
        with self._connection() as db:
            row = db.execute(
                "SELECT provider,model FROM chat_preferences WHERE chat_id=?", (chat_id,)
            ).fetchone()
        if not row:
            return default_provider, ""
        return str(row["provider"]), str(row["model"])

    def put_pending(self, action_id: str, chat_id: int, payload: dict[str, Any]) -> None:
        with self._connection() as db:
            db.execute("DELETE FROM pending_actions WHERE chat_id=?", (chat_id,))
            db.execute(
                "INSERT INTO pending_actions(id,chat_id,payload) VALUES (?,?,?)",
                (action_id, chat_id, json.dumps(payload, ensure_ascii=False)),
            )
        self.audit(chat_id, "approval_requested", _safe_detail(payload))

    def pop_pending(self, action_id: str, chat_id: int) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT payload FROM pending_actions WHERE id=? AND chat_id=?", (action_id, chat_id)
            ).fetchone()
            if row:
                db.execute("DELETE FROM pending_actions WHERE id=?", (action_id,))
                return json.loads(row["payload"])
        return None

    def audit(self, chat_id: int, event_type: str, detail: str) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO audit_events(chat_id,thread,event_type,detail) VALUES (?,?,?,?)",
                (chat_id, self.thread(chat_id), event_type, detail[:4000]),
            )

    def recent_audit(self, chat_id: int, limit: int = 12) -> list[dict[str, str]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT event_type,detail,created_at FROM audit_events WHERE chat_id=? AND thread=? "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, self.thread(chat_id), limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]


def _safe_detail(payload: dict[str, Any]) -> str:
    """Log only action metadata; content and accidental secrets stay out of the audit log."""
    tool = str(payload.get("tool", "unknown"))
    args = payload.get("args", {})
    if tool in {"write_file", "patch_file"} and isinstance(args, dict):
        return f"{tool}: {args.get('path', '')} (محتوا در audit ذخیره نشد)"
    if tool == "run_command" and isinstance(args, dict):
        return f"run_command: {str(args.get('command', ''))[:1000]}"
    return f"{tool}: درخواست تأیید"
