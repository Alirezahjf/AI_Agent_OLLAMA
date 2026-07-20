"""Explicit, security-conscious configuration for the local Telegram agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


PROVIDERS = frozenset({"ollama", "gapgpt", "avalai"})


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    ollama_base_url: str
    ollama_model: str
    default_provider: str
    default_model: str
    gapgpt_api_key: str
    gapgpt_base_url: str
    avalai_api_key: str
    avalai_base_url: str
    allowed_user_ids: frozenset[int]
    workspace_root: Path
    data_dir: Path
    command_timeout: int
    max_output_chars: int
    max_agent_turns: int
    model_timeout: int
    auto_approve_mutations: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required; copy .env.example to .env.")

        raw_ids = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")
        try:
            ids = frozenset(int(part.strip()) for part in raw_ids.split(",") if part.strip())
        except ValueError as exc:
            raise RuntimeError("ALLOWED_TELEGRAM_USER_IDS must contain comma-separated numeric IDs.") from exc

        root = Path(os.getenv("WORKSPACE_ROOT", str(Path.cwd()))).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"WORKSPACE_ROOT does not exist or is not a directory: {root}")
        data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        provider = os.getenv("DEFAULT_PROVIDER", "ollama").strip().lower()
        if provider == "auto":
            # Auto means: cloud credentials in this order, otherwise local Ollama.
            provider = "avalai" if os.getenv("AVALAI_API_KEY") else (
                "gapgpt" if os.getenv("GAPGPT_API_KEY") else "ollama"
            )
        if provider not in PROVIDERS:
            choices = ", ".join(sorted(PROVIDERS | {"auto"}))
            raise RuntimeError(f"DEFAULT_PROVIDER must be one of: {choices}")

        return cls(
            telegram_token=token,
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            default_provider=provider,
            default_model=os.getenv("DEFAULT_MODEL", "").strip(),
            gapgpt_api_key=os.getenv("GAPGPT_API_KEY", "").strip(),
            gapgpt_base_url=os.getenv("GAPGPT_BASE_URL", "https://api.gapgpt.app/v1").rstrip("/"),
            avalai_api_key=os.getenv("AVALAI_API_KEY", "").strip(),
            avalai_base_url=os.getenv("AVALAI_BASE_URL", "https://api.avalai.ir/v1").rstrip("/"),
            allowed_user_ids=ids,
            workspace_root=root,
            data_dir=data_dir,
            command_timeout=max(1, int(os.getenv("COMMAND_TIMEOUT_SECONDS", "120"))),
            max_output_chars=max(1000, int(os.getenv("MAX_OUTPUT_CHARS", "12000"))),
            max_agent_turns=max(1, min(32, int(os.getenv("MAX_AGENT_TURNS", "16")))),
            model_timeout=max(10, int(os.getenv("MODEL_TIMEOUT_SECONDS", "180"))),
            auto_approve_mutations=_bool("AUTO_APPROVE_MUTATIONS"),
        )
