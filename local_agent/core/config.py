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


def _project_root() -> Path:
    """The project folder the package was launched from (best-effort)."""
    return Path(__file__).resolve().parent.parent.parent


def _try_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _looks_like_assistant_config(payload: dict) -> bool:
    """Heuristic: is this JSON dict one of *our* config files?

    Guards the fallback search / migration against picking up a foreign
    project's ``config.json`` (a repo the user happens to run the app
    from).  A dict carrying any of our known sections is treated as ours.
    """
    return any(
        key in payload
        for key in ("llm", "telegram", "gmail", "github", "safety",
                    "allowed_user_ids", "telegram_token", "bale_token")
    )


def _has_real_settings(payload: dict) -> bool:
    """A config file worth using as the source of truth.

    «تنظیمات واقعی» = at least one non-default value or a meaningful
    ``data_dir`` pointer.  A file that only repeats defaults (the first-run
    template) is not real user data and must not win over a genuine config.
    """
    if not isinstance(payload, dict) or not payload:
        return False
    if _looks_like_assistant_config(payload):
        for key in ("llm", "telegram", "gmail", "github", "safety",
                    "allowed_user_ids", "telegram_token", "bale_token"):
            value = payload.get(key)
            if value not in (None, "", False, 0, [], {}, ()):
                return True
    raw = payload.get("data_dir")
    if raw:
        try:
            if Path(str(raw)).expanduser() != _DEFAULT_DATA_DIR:
                return True
        except OSError:
            pass
    return False


def _default_config_path() -> Path:
    """Resolve the single source of truth for the settings file.

    Priority:

    1. ``LOCAL_AGENT_CONFIG`` — explicit override.
    2. The fixed default ``~/.local_assistant/config.json`` when it exists.
    3. Fallback **search** for a real user config left next to the app:
       ``LOCAL_AGENT_DATA_DIR/config.json``, then ``config.json`` in the
       current folder / project folder (and the ``data_dir`` each one
       points at).  The first file with *real* settings wins — this is what
       keeps a user whose config lives inside the project folder (the old
       write target) from losing every setting after an upgrade.
    4. Last resort: the fixed default (a template is created on first run).

    ``data_dir`` inside the file only says where logs/history/sessions/
    screenshots live; it never redirects the settings file.
    """
    explicit = os.environ.get("LOCAL_AGENT_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    default = _DEFAULT_DATA_DIR / "config.json"
    if default.is_file():
        return default

    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(candidate: Path) -> None:
        candidate = Path(candidate).expanduser()
        resolved = _try_resolve(candidate)
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(candidate)

    env_data = os.environ.get("LOCAL_AGENT_DATA_DIR", "").strip()
    if env_data:
        _add(Path(env_data) / "config.json")
    for base in (Path.cwd(), _project_root()):
        cfg = base / "config.json"
        if cfg.is_file():
            _add(cfg)
            try:
                payload = _read_json(cfg)
            except ConfigError:
                continue
            dd = payload.get("data_dir")
            if dd:
                _add(Path(str(dd)) / "config.json")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            payload = _read_json(candidate)
        except ConfigError:
            continue
        if _has_real_settings(payload):
            return candidate
    return default


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
class GitHubAccount:
    """One GitHub identity (OAuth App credentials OR a Personal Access Token).

    ``auth_mode='oauth'``: the user creates their own OAuth App at
    github.com/settings/developers (Authorization callback URL =
    ``http://localhost:<port>/api/github/callback``) and provides
    ``client_id``/``client_secret``.  The redirect flow exchanges the
    returned code for an ``access_token`` stored in ``token_file``.

    ``auth_mode='pat'``: the user pastes a fine-grained/classic Personal
    Access Token directly; no OAuth App is needed.

    ``client_secret``/``token_file`` are secrets and are auto-masked
    everywhere by the ``config_set`` suffix rules.
    """

    name: str = "اصلی"
    enabled: bool = False
    auth_mode: str = "oauth"  # oauth | pat
    client_id: str = ""
    client_secret: str = ""  # OAuth only (masked)
    callback_url: str = ""
    token_file: str = ""  # default: <data_dir>/github_<name>.json (masked suffix)
    api_base: str = "https://api.github.com"
    confirm_push: bool = True  # ask before every push/merge/force


@dataclass(frozen=True)
class GitHubSettings:
    """Multi-account GitHub integration (OAuth redirect flow + PAT).

    ``enabled`` toggles the feature; ``accounts`` holds every identity and
    ``active_account`` names the default one.  ``default_scope`` is the
    space-separated OAuth scope requested during the redirect flow.
    """

    enabled: bool = False
    active_account: str = "اصلی"
    accounts: tuple[GitHubAccount, ...] = field(default_factory=tuple)
    default_scope: str = "repo workflow read:user"

    def account(self, name: str | None = None) -> GitHubAccount:
        name = (name or self.active_account) or "اصلی"
        for acc in self.accounts:
            if acc.name == name:
                return acc
        return GitHubAccount(name=name, enabled=False)

    def active(self) -> GitHubAccount:
        return self.account(self.active_account)

    def updated(self, changes: dict[str, Any]) -> GitHubSettings:
        enabled = changes.get("enabled", self.enabled)
        active = changes.get("active_account", self.active_account)
        if "accounts" in changes:
            accounts = tuple(
                _github_account_from_dict(a) for a in (changes["accounts"] or [])
            )
        else:
            fields = {
                k: v for k, v in changes.items()
                if k in ("auth_mode", "client_id", "client_secret", "callback_url", "token_file",
                         "api_base", "confirm_push", "enabled")
            }
            current = list(self.accounts)
            if not current:
                current = [GitHubAccount(name=active, enabled=enabled)]
            accounts = tuple(
                replace(acc, **fields) if acc.name == active else acc
                for acc in current
            )
        return GitHubSettings(
            enabled=enabled, active_account=active, accounts=accounts,
            default_scope=changes.get("default_scope", self.default_scope),
        )

    @property
    def auth_mode(self) -> str:
        return self.active().auth_mode

    @property
    def client_id(self) -> str:
        return self.active().client_id

    @property
    def client_secret(self) -> str:
        return self.active().client_secret

    @property
    def api_base(self) -> str:
        return self.active().api_base or "https://api.github.com"

    @property
    def confirm_push(self) -> bool:
        return self.active().confirm_push


@dataclass(frozen=True)
class AssistantSettings:
    """Top-level, immutable configuration."""

    data_dir: Path = field(default_factory=_default_data_dir)
    work_dir: Path = field(default_factory=Path.cwd)
    llm: LLMSettings = field(default_factory=LLMSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    gmail: GmailSettings = field(default_factory=GmailSettings)
    github: GitHubSettings = field(default_factory=GitHubSettings)
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

    def github_token_path_for(self, account: str | None = None) -> Path:
        """Token file for a GitHub account (default: active)."""
        acc = self.github.account(account)
        raw = (acc.token_file or "").strip()
        if raw:
            return Path(raw).expanduser()
        safe = "".join(c if c.isalnum() else "_" for c in acc.name) or "account"
        return self.data_dir / "github" / f"github_{safe}.json"

    @property
    def github_token_path(self) -> Path:
        return self.github_token_path_for()

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
            github = _github_from_payload(payload.get("github") or {})
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
                github=github,
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


def _github_from_payload(gh_payload: dict) -> GitHubSettings:
    """Build GitHubSettings, migrating legacy single-account fields."""
    enabled = bool(gh_payload.get("enabled", False))
    active = str(gh_payload.get("active_account", "اصلی") or "اصلی")
    default_scope = str(gh_payload.get("default_scope", "repo workflow read:user"))
    raw_accounts = gh_payload.get("accounts") or []
    if not raw_accounts:
        raw_accounts = [{
            "name": "اصلی",
            "enabled": enabled,
            "auth_mode": str(gh_payload.get("auth_mode", "oauth") or "oauth"),
            "client_id": gh_payload.get("client_id", ""),
            "client_secret": gh_payload.get("client_secret", ""),
            "callback_url": gh_payload.get("callback_url", ""),
            "token_file": gh_payload.get("token_file", ""),
            "api_base": gh_payload.get("api_base", "https://api.github.com"),
            "confirm_push": gh_payload.get("confirm_push", True),
        }]
    accounts = [_github_account_from_dict(a) for a in raw_accounts if isinstance(a, dict)]
    if not accounts:
        accounts = [GitHubAccount(name="اصلی", enabled=enabled)]
    names = {a.name for a in accounts}
    if active not in names:
        active = accounts[0].name
    return GitHubSettings(
        enabled=enabled, active_account=active,
        accounts=tuple(accounts), default_scope=default_scope,
    )


def _github_account_from_dict(raw: dict) -> GitHubAccount:
    return GitHubAccount(
        name=str(raw.get("name", "اصلی") or "اصلی"),
        enabled=bool(raw.get("enabled", True)),
        auth_mode=str(raw.get("auth_mode", "oauth") or "oauth"),
        client_id=str(raw.get("client_id", "")),
        client_secret=str(raw.get("client_secret", "")),
        callback_url=str(raw.get("callback_url", "")),
        token_file=str(raw.get("token_file", "")),
        api_base=str(raw.get("api_base", "https://api.github.com") or "https://api.github.com"),
        confirm_push=bool(raw.get("confirm_push", True)),
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

    # Migrate settings that a previous version wrote elsewhere (the old
    # write target ``<data_dir>/config.json``, the project folder, ...) so
    # existing users never lose their data after an upgrade.
    _migrate_old_config(target_path, _legacy_config_paths(target_path))
    # Migration may have folded values into the file — re-read it so THIS
    # process sees them immediately (not only after the next restart).
    payload = _read_json(target_path) or payload

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


def _legacy_config_paths(target_path: Path) -> list[Path]:
    """Every place a previous version may have written a real config.

    The old (buggy) write target was ``<data_dir>/config.json`` where
    ``data_dir`` came from the file itself; versions before that simply
    wrote into the project folder next to the app.  All candidates are
    checked so a user whose config lives in any of those places keeps it
    after an upgrade.  Foreign ``config.json`` files (from another repo the
    user happens to run the app from) are filtered out.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    target_resolved = _try_resolve(target_path)

    def _add(candidate: Path) -> None:
        candidate = Path(candidate).expanduser()
        resolved = _try_resolve(candidate)
        if resolved == target_resolved or resolved in seen:
            return
        seen.add(resolved)
        out.append(candidate)

    try:
        payload = _read_json(target_path)
    except ConfigError:
        payload = {}
    data_dir = payload.get("data_dir")
    if data_dir:
        _add(Path(str(data_dir)) / "config.json")
    env_data = os.environ.get("LOCAL_AGENT_DATA_DIR", "").strip()
    if env_data:
        _add(Path(env_data) / "config.json")
    for base in (Path.cwd(), _project_root()):
        cfg = base / "config.json"
        if not cfg.is_file():
            continue
        try:
            candidate_payload = _read_json(cfg)
        except ConfigError:
            continue
        if not _looks_like_assistant_config(candidate_payload):
            continue
        _add(cfg)
        candidate_data_dir = candidate_payload.get("data_dir")
        if candidate_data_dir:
            _add(Path(str(candidate_data_dir)) / "config.json")
    return out


def _migrate_old_config(target_path: Path, legacy_paths: list[Path]) -> None:
    """Fold non-default settings from older config locations into the primary
    settings file (once, idempotently, with a clear Persian log)."""
    for legacy_path in legacy_paths:
        _migrate_one_config(target_path, legacy_path)


def _migrate_one_config(target_path: Path, legacy_path: Path) -> None:
    if not legacy_path.is_file():
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
