"""Configuration loader for the local Windows assistant.

Reads ``<DATA_DIR>/config.json`` (or the path in ``LOCAL_AGENT_CONFIG``)
with a layered fallback:

  1. file
  2. environment variables (``LOCAL_AGENT_*``)
  3. built-in defaults

The Settings dataclass is frozen so accidentally mutating it in a tool
will be caught early.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .errors import ConfigError

_DEFAULT_DATA_DIR = Path.home() / ".local_assistant"


def _default_data_dir() -> Path:
    """Resolve the data directory from env or fall back to ``~/.local_assistant``."""
    raw = os.environ.get("LOCAL_AGENT_DATA_DIR", "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_DATA_DIR


def _default_config_path() -> Path:
    """Resolve the single source of truth for the settings file.

    This is **fixed** and deliberately independent of the ``data_dir``
    field read from the file: either ``LOCAL_AGENT_CONFIG`` when set, or
    ``~/.local_assistant/config.json``.  A user whose config contains a
    ``data_dir`` pointing at an old project folder must still have their
    settings read *and* written here — ``data_dir`` only says where logs,
    history, sessions and screenshots live.  (Setting ``LOCAL_AGENT_DATA_DIR``
    moves the *data* directory, never this settings file.)
    """
    explicit = os.environ.get("LOCAL_AGENT_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return _DEFAULT_DATA_DIR / "config.json"


@dataclass(frozen=True)
class LLMSettings:
    """Provider-agnostic LLM configuration."""

    provider: str = "ollama"  # ollama | openai_compatible | auto
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    openai_base_url: str = ""  # e.g. https://api.avalai.ir/v1
    openai_api_key: str = ""
    openai_model: str = "claude-sonnet-5"
    timeout_seconds: int = 180
    max_retries: int = 4
    temperature: float = 0.1
    max_context_messages: int = 40
    max_context_chars: int = 80_000


@dataclass(frozen=True)
class TelegramAccount:
    """One personal Telegram account (user credentials, NOT a bot token).

    Each account owns a separate Telethon session persisted at
    ``<data_dir>/sessions/<session_name>.session`` so every account logs in
    independently.  The same ``api_id``/``api_hash`` (from my.telegram.org)
    can be shared across accounts; only ``phone`` and the session differ.
    """

    name: str = "اصلی"
    enabled: bool = False
    api_id: int = 0
    api_hash: str = ""
    phone: str = ""  # E.164, e.g. +98912...
    session_name: str = "assistant"  # file is <session_name>.session
    confirm_send: bool = True  # ask before every outgoing message


@dataclass(frozen=True)
class TelegramSettings:
    """Personal-account Telegram via Telethon (multi-account).

    The global ``enabled`` toggles the feature; ``accounts`` holds every
    account and ``active_account`` names the one the agent acts as by
    default.  ``confirm_send`` is honoured per account, so an account can
    skip the outgoing-message confirmation even when ``confirm_mode`` is
    ``destructive``.
    """

    enabled: bool = False
    active_account: str = "اصلی"
    accounts: tuple[TelegramAccount, ...] = field(default_factory=tuple)

    # ---- account lookup ---------------------------------------------

    def account(self, name: str | None = None) -> TelegramAccount:
        """Return the named account (default: the active one).

        Unknown names yield a disabled placeholder (never raise) so callers
        can show a Persian "unknown account" message.
        """
        name = (name or self.active_account) or "اصلی"
        for acc in self.accounts:
            if acc.name == name:
                return acc
        return TelegramAccount(name=name, enabled=False)

    def active(self) -> TelegramAccount:
        return self.account(self.active_account)

    def updated(self, changes: dict[str, Any]) -> TelegramSettings:
        """Return a new settings object after a partial update.

        Accepts the structural keys (``enabled``, ``active_account``,
        ``accounts``) or the legacy per-field keys (``api_id``, ``api_hash``,
        ``phone``, ``session_name``, ``confirm_send``) which are applied to
        the *active* account only.
        """
        enabled = changes.get("enabled", self.enabled)
        active = changes.get("active_account", self.active_account)
        if "accounts" in changes:
            accounts = tuple(
                _telegram_account_from_dict(a) for a in (changes["accounts"] or [])
            )
        else:
            fields = {
                k: v for k, v in changes.items()
                if k in ("api_id", "api_hash", "phone", "session_name", "confirm_send")
            }
            current = list(self.accounts)
            if not current:
                # No accounts materialised yet (direct construction) — create
                # the active one so legacy fields have somewhere to land.
                current = [TelegramAccount(name=active, enabled=enabled)]
            accounts = tuple(
                replace(acc, **fields) if acc.name == active else acc
                for acc in current
            )
        return TelegramSettings(enabled=enabled, active_account=active, accounts=accounts)

    # ---- backward-compatible accessors (delegate to the active account)
    # These keep ``settings.telegram.api_id`` & co working for the existing
    # handlers/actions until they are migrated to ``.accounts``.

    @property
    def api_id(self) -> int:
        return self.active().api_id

    @property
    def api_hash(self) -> str:
        return self.active().api_hash

    @property
    def phone(self) -> str:
        return self.active().phone

    @property
    def session_name(self) -> str:
        return self.active().session_name

    @property
    def confirm_send(self) -> bool:
        return self.active().confirm_send


@dataclass(frozen=True)
class SafetySettings:
    """Policy knobs for the agent's autonomy."""

    # Operations tagged 'destructive' require explicit approval. 'safe' never do.
    require_confirm_for_destructive: bool = True
    # Maximum agent loop turns before we stop and ask the human.
    max_agent_turns: int = 12
    # Per-command shell timeout.
    shell_timeout_seconds: int = 60
    # When True, the assistant will not run shell commands that touch
    # anything outside the assistant's working directory.
    restrict_shell_to_workdir: bool = False
    # Confirm policy: 'always' (every ask), 'destructive' (default),
    # 'never' (auto-execute; only for disposable VMs).
    confirm_mode: str = "destructive"
    # When True, file tools drop the workspace sandbox (whole filesystem
    # becomes reachable) and the shell runs without a workdir limit.
    # Sensitive files (.ssh, .env, credentials, ...) stay blocked and
    # destructive actions still ask for confirmation. Default OFF.
    full_system_access: bool = False


@dataclass(frozen=True)
class GmailSettings:
    """Gmail integration (OAuth2 installed-app, IMAP/SMTP fallback).

    ``credentials_file`` is the OAuth client JSON downloaded from Google
    Cloud Console (Desktop app); ``token_file`` stores the user's token
    after the first approval.  When no OAuth files are present but an
    ``app_password`` is set, the client falls back to IMAP/SMTP.
    """

    enabled: bool = False
    credentials_file: str = ""  # default: <data_dir>/credentials.json
    token_file: str = ""  # default: <data_dir>/gmail_token.json
    username: str = ""  # Gmail address, needed for the IMAP/SMTP fallback
    app_password: str = ""  # IMAP/SMTP fallback (16-char App Password)
    confirm_send: bool = True  # ask before every outgoing email


@dataclass(frozen=True)
class AssistantSettings:
    """Top-level, immutable configuration."""

    data_dir: Path = field(default_factory=_default_data_dir)
    work_dir: Path = field(default_factory=Path.cwd)
    llm: LLMSettings = field(default_factory=LLMSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    gmail: GmailSettings = field(default_factory=GmailSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    # The exact file :func:`load_settings` read from.  This is the *single
    # source of truth* for persistence: every write must target the same
    # path so settings survive a restart even when ``data_dir`` points
    # elsewhere.  ``None`` (direct construction) falls back to
    # ``data_dir/config.json``.
    config_path: Path | None = None
    # Bot tokens (used by the Telegram/Bale bot)
    telegram_token: str = ""
    bale_token: str = ""
    bale_base_url: str = "https://tapi.bale.ai"
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    extra: dict[str, str] = field(default_factory=dict)

    # ---- Convenience accessors -------------------------------------------

    def effective_config_path(self) -> Path:
        """The settings file that should be read/written.

        Prefers the path :func:`load_settings` set (the real file that was
        read); falls back to ``data_dir/config.json`` only when the object
        was constructed directly (tests, transient configs).
        """
        return self.config_path or (self.data_dir / "config.json")

    @property
    def history_path(self) -> Path:
        return self.data_dir / "history.jsonl"

    @property
    def memory_path(self) -> Path:
        return self.data_dir / "memory.json"

    @property
    def telegram_session_path(self) -> Path:
        return self.telegram_session_path_for()

    def telegram_session_path_for(self, account: str | None = None) -> Path:
        """Session file for a given account (default: active).

        Multi-account sessions live under ``data_dir/sessions/``; pre-existing
        single-account session files are left in place untouched.
        """
        acc = self.telegram.account(account)
        return self.data_dir / "sessions" / f"{acc.session_name}.session"

    @property
    def gmail_credentials_path(self) -> Path:
        raw = self.gmail.credentials_file.strip()
        return Path(raw).expanduser() if raw else self.data_dir / "credentials.json"

    @property
    def gmail_token_path(self) -> Path:
        raw = self.gmail.token_file.strip()
        return Path(raw).expanduser() if raw else self.data_dir / "gmail_token.json"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    # ---- Serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["data_dir"] = str(self.data_dir)
        payload["work_dir"] = str(self.work_dir)
        # ``config_path`` is a runtime pointer to the file we read/write; it
        # must never be persisted (it would leak an absolute path and freeze
        # the location across restarts).
        payload.pop("config_path", None)
        # Convert frozenset to list for JSON serialization
        if "allowed_user_ids" in payload and isinstance(payload["allowed_user_ids"], frozenset):
            payload["allowed_user_ids"] = list(payload["allowed_user_ids"])
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> AssistantSettings:
        try:
            llm_payload = dict(payload.get("llm") or {})
            provider = str(llm_payload.get("provider", "ollama")).lower()
            if provider not in {"ollama", "openai_compatible", "auto"}:
                raise ConfigError(
                    f"unknown LLM provider {provider!r}; expected ollama | openai_compatible | auto"
                )
            llm = LLMSettings(**llm_payload)

            tg_payload = dict(payload.get("telegram") or {})
            confirm_mode = str((payload.get("safety") or {}).get("confirm_mode", "destructive"))
            if confirm_mode not in {"destructive", "always", "never"}:
                raise ConfigError(
                    f"invalid confirm_mode {confirm_mode!r}; expected destructive | always | never"
                )
            safety = SafetySettings(**(payload.get("safety") or {}))
            tg = _telegram_from_payload(tg_payload)
            gmail = GmailSettings(**(payload.get("gmail") or {}))
            data_dir = Path(payload.get("data_dir", _default_data_dir())).expanduser()
            work_dir = Path(payload.get("work_dir", str(Path.cwd()))).expanduser()
            extra = payload.get("extra") or {}
            # Bot tokens
            telegram_token = str(payload.get("telegram_token", ""))
            bale_token = str(payload.get("bale_token", ""))
            bale_base_url = str(payload.get("bale_base_url", "https://tapi.bale.ai"))
            raw_ids = payload.get("allowed_user_ids") or []
            allowed_ids = frozenset(int(i) for i in raw_ids if isinstance(i, (int, float)))
            return cls(
                data_dir=data_dir,
                work_dir=work_dir,
                llm=llm,
                telegram=tg,
                gmail=gmail,
                safety=safety,
                telegram_token=telegram_token,
                bale_token=bale_token,
                bale_base_url=bale_base_url,
                allowed_user_ids=allowed_ids,
                extra={str(k): str(v) for k, v in extra.items()},
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid config payload: {exc}") from exc

    def with_overrides(self, **changes) -> AssistantSettings:
        """Return a new settings object with the given fields replaced."""
        return replace(self, **changes)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _telegram_from_payload(tg_payload: dict) -> TelegramSettings:
    """Build TelegramSettings, migrating the old single-account fields.

    If ``accounts`` is empty, one account named «اصلی» is reconstructed from
    the legacy ``enabled/api_id/api_hash/phone/session_name/confirm_send``
    fields so existing configs keep working unchanged.
    """
    enabled = bool(tg_payload.get("enabled", False))
    active = str(tg_payload.get("active_account", "اصلی") or "اصلی")
    raw_accounts = tg_payload.get("accounts") or []
    if not raw_accounts:
        raw_accounts = [{
            "name": "اصلی",
            "enabled": enabled,
            "api_id": tg_payload.get("api_id", 0),
            "api_hash": tg_payload.get("api_hash", ""),
            "phone": tg_payload.get("phone", ""),
            "session_name": tg_payload.get("session_name", "assistant"),
            "confirm_send": tg_payload.get("confirm_send", True),
        }]
    accounts: list[TelegramAccount] = []
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            continue
        accounts.append(TelegramAccount(
            name=str(raw.get("name", "اصلی") or "اصلی"),
            enabled=bool(raw.get("enabled", True)),
            api_id=int(raw.get("api_id", 0) or 0),
            api_hash=str(raw.get("api_hash", "")),
            phone=str(raw.get("phone", "")),
            session_name=str(raw.get("session_name", "assistant") or "assistant"),
            confirm_send=bool(raw.get("confirm_send", True)),
        ))
    if not accounts:
        accounts = [TelegramAccount(name="اصلی", enabled=enabled)]
    names = {a.name for a in accounts}
    if active not in names:
        active = accounts[0].name
    return TelegramSettings(enabled=enabled, active_account=active, accounts=tuple(accounts))


def _telegram_account_from_dict(raw: dict) -> TelegramAccount:
    return TelegramAccount(
        name=str(raw.get("name", "اصلی") or "اصلی"),
        enabled=bool(raw.get("enabled", True)),
        api_id=int(raw.get("api_id", 0) or 0),
        api_hash=str(raw.get("api_hash", "")),
        phone=str(raw.get("phone", "")),
        session_name=str(raw.get("session_name", "assistant") or "assistant"),
        confirm_send=bool(raw.get("confirm_send", True)),
    )


def _read_json(path: Path) -> dict:
    """Read a config file, tolerating the ``#`` comments in our template.

    :func:`_build_template` writes a header of ``#`` lines above the JSON
    body so a first-time user can read what the file is for.  Plain
    ``json.loads`` chokes on those, which used to make every run after
    the very first one fail with "config file is not valid JSON".  We
    strip comment lines before parsing, and only report a real syntax
    error if the stripped text still does not parse.
    """
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        stripped = _strip_template_comments(raw).strip()
        if not stripped:
            # Comment-only or empty file: fall back to the defaults rather
            # than refusing to start.
            return {}
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config file is not valid JSON: {path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"config file must contain a JSON object: {path}")
    return payload


def _apply_env_overrides(payload: dict) -> dict:
    """Layer environment variables on top of a config dict.

    Environment variables use the prefix ``LOCAL_AGENT_`` and ``__`` as
    a path separator, e.g. ``LOCAL_AGENT_LLM__PROVIDER=avalai``.
    """
    prefix = "LOCAL_AGENT_"
    out = json.loads(json.dumps(payload))  # deep copy
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        path = env_key[len(prefix):].lower().split("__")
        cursor = out
        for step in path[:-1]:
            cursor = cursor.setdefault(step, {})
            if not isinstance(cursor, dict):
                cursor = {}
        cursor[path[-1]] = _coerce_env_value(env_value)
    return out


def _coerce_env_value(raw: str) -> object:
    """Best-effort coercion of string env values into JSON-ish types."""
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def load_settings(
    config_path: Path | None = None,
    *,
    data_dir: Path | None = None,
) -> AssistantSettings:
    """Load settings from disk + env, creating defaults if nothing exists.

    The directory and the config file are created on first run so a fresh
    user can edit them in place.
    """
    load_dotenv(override=False)

    target_path = Path(config_path).expanduser() if config_path else _default_config_path()
    target_dir = data_dir.expanduser() if data_dir else target_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    payload = _read_json(target_path)
    if not payload:
        # First run: persist a commented template so the user can edit.
        template = _build_template(target_dir)
        target_path.write_text(template, encoding="utf-8")
        payload = json.loads(_strip_template_comments(template))

    # Migrate settings that a previous version wrote into
    # ``<data_dir>/config.json`` (the old write target) so existing users
    # whose ``data_dir`` points at an old project folder don't lose data.
    _migrate_old_config(target_path, _legacy_config_path(target_path))

    payload = _apply_env_overrides(payload)
    payload.setdefault("data_dir", str(target_dir))
    settings = AssistantSettings.from_dict(payload)
    # Remember exactly which file we read so every later write goes to the
    # same place (the B2 bug: reads came from one path, writes from another).
    settings = replace(settings, config_path=target_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    if not settings.work_dir.exists():
        settings.work_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _legacy_config_path(target_path: Path) -> Path:
    """The old (buggy) write location: ``<data_dir>/config.json``.

    When ``target_path`` is the fixed settings file and ``data_dir`` in the
    file differs from its parent, previous versions wrote settings to
    ``data_dir/config.json`` — a file ``load_settings`` never read.  This is
    the file we look for when migrating.
    """
    # A previous version wrote settings to ``<data_dir>/config.json`` (the
    # ``config_path`` property), so compute that legacy location from the
    # ``data_dir`` recorded in the file we actually read.
    payload = _read_json(target_path)
    data_dir = payload.get("data_dir")
    if data_dir:
        legacy = Path(str(data_dir)).expanduser() / "config.json"
        if legacy != target_path:
            return legacy
    # No data_dir override → the legacy path equals the fixed one; nothing to
    # migrate.
    return target_path


def _migrate_old_config(target_path: Path, legacy_path: Path) -> None:
    """Fold non-default settings from the old ``<data_dir>/config.json`` into
    the fixed settings file (once, idempotently, with a clear Persian log)."""
    if legacy_path == target_path or not legacy_path.is_file():
        return
    try:
        legacy = _read_json(legacy_path)
        current = _read_json(target_path)
    except ConfigError:
        return
    if not legacy:
        return
    from ..core.logging_setup import get_logger

    logger = get_logger("config")
    defaults = AssistantSettings().to_dict()
    changed = False
    for key, value in legacy.items():
        if key == "data_dir":
            # The old file's own data_dir pointer must never redirect the
            # fixed settings file.
            continue
        default_value = defaults.get(key)
        present = current.get(key) is not None
        is_default = value == default_value or value in (None, "", False, 0, {})
        if not present and not is_default:
            current[key] = value
            changed = True
    if changed:
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = target_path.with_suffix(target_path.suffix + ".tmp")
            tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, target_path)
        except OSError as exc:
            logger.warning("migrating settings failed: %s", exc)
            return
        logger.info(
            "تنظیمات قدیمی از %s به %s منتقل شد تا پس از ری‌استارت از بین نروند.",
            legacy_path, target_path,
        )


def _build_template(data_dir: Path) -> str:
    body = json.dumps(
        AssistantSettings(data_dir=data_dir).to_dict(),
        indent=2,
        ensure_ascii=False,
    )
    return (
        "# Local Windows Assistant configuration\n"
        "# Edit values below; environment variables (LOCAL_AGENT_*) override these.\n"
        "# After editing, restart the assistant for changes to take effect.\n"
        f"{body}\n"
    )


def _strip_template_comments(text: str) -> str:
    """Drop leading ``#`` lines so the template can be parsed as JSON."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines)
