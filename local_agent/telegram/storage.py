"""SQLite storage for Telegram mirroring and fuzzy search."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

class TelegramStorage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock, sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            cursor = self.conn_cursor(conn)
            # Table for Chats/Entities
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY,
                    hash INTEGER,
                    username TEXT,
                    phone TEXT,
                    title TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    type TEXT,
                    bio TEXT,
                    about TEXT,
                    participants_count INTEGER,
                    unread_count INTEGER,
                    last_seen_at TEXT,
                    updated_at TEXT
                )
            """)
            # Table for Messages (Selective Mirroring)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER,
                    chat_id INTEGER,
                    sender_id INTEGER,
                    text TEXT,
                    date TEXT,
                    type TEXT,
                    media_path TEXT,
                    PRIMARY KEY (id, chat_id)
                )
            """)
            # Table for Auto-Reply Rules
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS auto_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern TEXT,
                    reply_text TEXT,
                    is_active BOOLEAN
                )
            """)
            conn.commit()

    def conn_cursor(self, conn):
        return conn.cursor()

    def save_entity(self, entity_data: Dict[str, Any]):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute("""
                INSERT OR REPLACE INTO entities (
                    id, username, phone, title, first_name, last_name, type, bio, about, 
                    participants_count, unread_count, updated_at
                ) VALUES (
                    :id, :username, :phone, :title, :first_name, :last_name, :type, :bio, :about,
                    :participants_count, :unread_count, :updated_at
                )
            """, {**entity_data, "updated_at": now})
            conn.commit()

    def search_entities(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            q = f"%{query}%"
            cursor.execute("""
                SELECT * FROM entities 
                WHERE title LIKE ? OR username LIKE ? OR first_name LIKE ? OR last_name LIKE ? OR phone LIKE ? OR bio LIKE ?
                ORDER BY unread_count DESC, updated_at DESC
                LIMIT ?
            """, (q, q, q, q, q, q, limit))
            return [dict(row) for row in cursor.fetchall()]

    def get_entity_by_id(self, entity_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM entities WHERE id = ?", (entity_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
