"""Small SQLite persistence layer for chat memory and auditable pending actions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connection() as db:
            db.executescript("""
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
            """)

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
                "INSERT INTO chat_state(chat_id,thread) VALUES (?,1) ON CONFLICT(chat_id) DO UPDATE SET thread=thread+1",
                (chat_id,),
            )
        return self.thread(chat_id)

    def reset(self, chat_id: int) -> None:
        thread = self.thread(chat_id)
        with self._connection() as db:
            db.execute("DELETE FROM messages WHERE chat_id=? AND thread=?", (chat_id, thread))
            db.execute("DELETE FROM pending_actions WHERE chat_id=?", (chat_id,))

    def add(self, chat_id: int, role: str, content: str) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO messages(chat_id,thread,role,content) VALUES (?,?,?,?)",
                (chat_id, self.thread(chat_id), role, content),
            )

    def history(self, chat_id: int, limit: int = 30) -> list[dict[str, str]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT role,content FROM messages WHERE chat_id=? AND thread=? ORDER BY id DESC LIMIT ?",
                (chat_id, self.thread(chat_id), limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def put_pending(self, action_id: str, chat_id: int, payload: dict[str, Any]) -> None:
        with self._connection() as db:
            db.execute("DELETE FROM pending_actions WHERE chat_id=?", (chat_id,))
            db.execute(
                "INSERT INTO pending_actions(id,chat_id,payload) VALUES (?,?,?)",
                (action_id, chat_id, json.dumps(payload, ensure_ascii=False)),
            )

    def pop_pending(self, action_id: str, chat_id: int) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT payload FROM pending_actions WHERE id=? AND chat_id=?", (action_id, chat_id)
            ).fetchone()
            if row:
                db.execute("DELETE FROM pending_actions WHERE id=?", (action_id,))
                return json.loads(row["payload"])
        return None
