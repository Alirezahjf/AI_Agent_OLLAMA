"""Configuration is deliberately explicit: this agent controls a real machine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    ollama_base_url: str
    ollama_model: str
    allowed_user_ids: frozenset[int]
    workspace_root: Path
    data_dir: Path
    command_timeout: int
    max_output_chars: int
    auto_approve_mutations: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required; copy .env.example to .env.")
        raw_ids = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")
        ids = frozenset(int(part.strip()) for part in raw_ids.split(",") if part.strip())
        root = Path(os.getenv("WORKSPACE_ROOT", str(Path.cwd()))).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"WORKSPACE_ROOT does not exist or is not a directory: {root}")
        data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            telegram_token=token,
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            allowed_user_ids=ids,
            workspace_root=root,
            data_dir=data_dir,
            command_timeout=max(1, int(os.getenv("COMMAND_TIMEOUT_SECONDS", "120"))),
            max_output_chars=max(1000, int(os.getenv("MAX_OUTPUT_CHARS", "12000"))),
            auto_approve_mutations=os.getenv("AUTO_APPROVE_MUTATIONS", "false").lower() == "true",
        )
