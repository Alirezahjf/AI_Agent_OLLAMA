"""Offline tests — گ ۶: اکانت تلگرام بعد از اتصال باید enabled بماند و بعد از
ری‌استارت دوباره ساخته شود (fake telethon، بدون شبکه)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from local_agent.bridge.api.handlers import BridgeHandlers
from local_agent.core.config import (
    AssistantSettings,
    TelegramAccount,
    TelegramSettings,
)


class _FakeClient:
    """جایگزین PersonalTelegram: فلوی ورود سادهٔ حالت‌دار."""

    def __init__(self, connected: bool = False, state: str = "disconnected") -> None:
        self._connected = connected
        self.login_state = state
        self.calls: list[str] = []
        self.last_error = ""
        self.last_error_code = ""

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start_login(self) -> dict[str, Any]:
        self.calls.append("start_login")
        if self._connected:
            return {"state": "connected", "message": "already connected"}
        self.login_state = "await_code"
        return {"state": "await_code", "message": "کد ارسال شد"}

    def submit_code(self, code: str) -> dict[str, Any]:
        self.calls.append(f"code:{code}")
        self.login_state = "await_2fa"
        return {"state": "await_2fa", "message": "2FA"}

    def submit_password(self, password: str) -> dict[str, Any]:
        self.calls.append(f"password:{password}")
        self._connected = True
        self.login_state = "connected"
        return {"state": "connected", "message": "ok"}

    def disconnect(self) -> None:
        self._connected = False


def _settings_with_disabled_account(data_dir: Path) -> AssistantSettings:
    return AssistantSettings(
        data_dir=data_dir,
        work_dir=data_dir,
        telegram=TelegramSettings(
            enabled=True,
            active_account="اصلی",
            accounts=(
                TelegramAccount(
                    name="اصلی", enabled=False,
                    api_id=111, api_hash="a" * 32, phone="+989120000000",
                    session_name="main",
                ),
            ),
        ),
    )


def test_connected_login_persists_enabled_and_survives_restart(tmp_path: Path) -> None:
    """اتصال موفق → enabled=True در config؛ بعد از «ری‌استارت» کلاینت ساخته می‌شود."""
    settings = _settings_with_disabled_account(tmp_path)
    handlers = BridgeHandlers.build(settings)
    assert handlers._telegram_accounts == {}  # disabled → no client yet

    fake = _FakeClient()
    handlers._telegram_accounts["اصلی"] = fake
    handlers.telegram = fake
    handlers.context.extra["telegram"] = fake

    result = handlers.start_telegram_login("اصلی")
    assert result["state"] == "await_code"
    result = handlers.submit_telegram_code("12345", "اصلی")
    assert result["state"] == "await_2fa"
    result = handlers.submit_telegram_password("p4ss", "اصلی")
    assert result["state"] == "connected"

    # enabled باید در settings و در فایل config ثبت شده باشد.
    assert handlers.settings.telegram.account("اصلی").enabled is True
    persisted = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    accounts = persisted["telegram"]["accounts"]
    assert accounts[0]["enabled"] is True

    # شبیه‌سازی ری‌استارت: ساخت دوباره از همان config → کلاینت همان اکانت ساخته می‌شود.
    from local_agent.core.config import load_settings

    reloaded = load_settings(tmp_path / "config.json")
    handlers2 = BridgeHandlers.build(reloaded)
    assert "اصلی" in handlers2._telegram_accounts
    assert handlers2._telegram_accounts["اصلی"].account_name == "اصلی"


def test_session_valid_start_login_marks_enabled(tmp_path: Path) -> None:
    """start_login که مستقیم connected برگرداند (سشن معتبر) هم enabled را ثبت می‌کند."""
    settings = _settings_with_disabled_account(tmp_path)
    handlers = BridgeHandlers.build(settings)

    fake = _FakeClient(connected=True, state="connected")
    handlers._telegram_accounts["اصلی"] = fake
    handlers.telegram = fake
    handlers.context.extra["telegram"] = fake

    result = handlers.start_telegram_login("اصلی")
    assert result["state"] == "connected"
    assert handlers.settings.telegram.account("اصلی").enabled is True


def test_switch_account_enables_target_and_persists(tmp_path: Path) -> None:
    settings = AssistantSettings(
        data_dir=tmp_path,
        work_dir=tmp_path,
        telegram=TelegramSettings(
            enabled=True,
            active_account="اصلی",
            accounts=(
                TelegramAccount(name="اصلی", enabled=False, api_id=111,
                                api_hash="a" * 32, phone="+1"),
                TelegramAccount(name="کار", enabled=False, api_id=222,
                                api_hash="b" * 32, phone="+2"),
            ),
        ),
    )
    handlers = BridgeHandlers.build(settings)
    handlers.switch_telegram_account("کار")
    assert handlers.settings.telegram.active_account == "کار"
    assert handlers.settings.telegram.account("کار").enabled is True
    persisted = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    by_name = {a["name"]: a for a in persisted["telegram"]["accounts"]}
    assert by_name["کار"]["enabled"] is True


def test_telegram_status_shows_per_account_enabled(tmp_path: Path) -> None:
    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    status = handlers.telegram_status("اصلی")
    assert status["enabled"] is False  # per-account
    assert status["feature_enabled"] is True  # global toggle
    assert status["state"] == "disabled"


def test_telegram_status_exposes_safe_error_code(tmp_path: Path) -> None:
    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    fake = _FakeClient()
    fake.last_error = "ارتباط برقرار نشد"
    fake.last_error_code = "network"
    handlers._telegram_accounts["اصلی"] = fake
    status = handlers.telegram_status("اصلی")
    assert status["last_error"] == "ارتباط برقرار نشد"
    assert status["last_error_code"] == "network"


def test_switch_unknown_account_still_raises(tmp_path: Path) -> None:
    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    with pytest.raises(Exception) as exc:
        handlers.switch_telegram_account("وجودندارد")
    assert "وجود ندارد" in str(exc.value)


def test_remove_unknown_account_is_rejected_before_touching_session(tmp_path: Path) -> None:
    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    with pytest.raises(Exception, match="وجود ندارد"):
        handlers.remove_telegram_account("ghost", confirmed=True)


def test_accounts_status_never_leaks_secrets(tmp_path: Path) -> None:
    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    blob = json.dumps(handlers.telegram_accounts_status())
    assert "a" * 32 not in blob
    assert "+989120000000" in blob  # phone is fine (it is not a secret)


# ===========================================================================
# گ ۶-ب) خطای شبکهٔ تلگرام باید پیام فارسی جدا و روشن بدهد
# ===========================================================================


def test_start_login_network_error_gives_persian_hint(tmp_path: Path) -> None:
    from local_agent.core.errors import AssistantError

    class _NetworkFailingClient:
        is_connected = False
        login_state = "disconnected"

        def start_login(self) -> dict[str, Any]:
            raise ConnectionError("Connection to Telegram failed 5 time(s)")

    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    handlers._telegram_accounts["اصلی"] = _NetworkFailingClient()

    with pytest.raises(AssistantError) as exc:
        handlers.start_telegram_login("اصلی")
    assert "فیلترشکن" in str(exc.value) or "اینترنت" in str(exc.value)


def test_submit_code_network_error_gives_persian_hint(tmp_path: Path) -> None:
    from local_agent.core.errors import AssistantError

    class _NetworkFailingClient:
        is_connected = False
        login_state = "await_code"

        def submit_code(self, code: str) -> dict[str, Any]:
            raise TimeoutError("timed out")

    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    handlers._telegram_accounts["اصلی"] = _NetworkFailingClient()

    with pytest.raises(AssistantError) as exc:
        handlers.submit_telegram_code("12345", "اصلی")
    assert "فیلترشکن" in str(exc.value) or "اینترنت" in str(exc.value)


def test_non_network_failure_keeps_generic_message(tmp_path: Path) -> None:
    from local_agent.core.errors import AssistantError

    class _WeirdFailingClient:
        is_connected = False
        login_state = "disconnected"

        def start_login(self) -> dict[str, Any]:
            raise RuntimeError("weird local error")

    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    handlers._telegram_accounts["اصلی"] = _WeirdFailingClient()

    with pytest.raises(AssistantError) as exc:
        handlers.start_telegram_login("اصلی")
    assert "VPN" not in str(exc.value)  # پیام شبکهٔ اختصاصی نیست


# ===========================================================================
# گ ۷) منوی اکانت‌های تلگرام در تنظیمات وب
# ===========================================================================


def test_accounts_status_synthesizes_active_account_when_list_empty(tmp_path: Path) -> None:
    """config مستقیم (accounts خالی) → حداقل ردیف «اکانت فعال» ساخته شود."""
    settings = AssistantSettings(
        data_dir=tmp_path, work_dir=tmp_path,
        telegram=TelegramSettings(enabled=True, active_account="اصلی", accounts=()),
    )
    handlers = BridgeHandlers.build(settings)
    status = handlers.telegram_accounts_status()
    assert status["active_account"] == "اصلی"
    assert len(status["accounts"]) == 1
    assert status["accounts"][0]["account"] == "اصلی"


def test_web_status_includes_telegram_accounts(web_server) -> None:
    import requests

    body = requests.get(f"http://127.0.0.1:{web_server.port}/api/status", timeout=5).json()
    settings_block = body["settings"]["settings"]
    assert "telegram_accounts" in settings_block
    assert isinstance(settings_block["telegram_accounts"]["accounts"], list)
    assert settings_block["telegram_active_account"]


def test_web_toggle_account_enabled_keeps_secrets(web_server) -> None:
    import requests

    backend = web_server.client._backend
    handlers = backend._server.handlers

    # یک اکانت واقعی با اعتبارنامه ثبت کن (فعالیت پیش‌فرض خاموش است)
    settings = handlers.settings.with_overrides(
        telegram=handlers.settings.telegram.updated({
            "api_id": 123, "api_hash": "s" * 32, "phone": "+989120000000",
        })
    )
    handlers._apply_settings(settings)
    assert handlers.settings.telegram.account("اصلی").enabled is False

    # روشن کن از طریق endpoint (فقط name + enabled)
    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/telegram/account",
        json={"name": "اصلی", "enabled": True}, timeout=5,
    )
    assert r.status_code == 200, r.text
    assert handlers.settings.telegram.account("اصلی").enabled is True
    # اعتبارنامه‌ها دست‌نخورده‌اند
    assert handlers.settings.telegram.account("اصلی").api_hash == "s" * 32
    assert handlers.settings.telegram.account("اصلی").phone == "+989120000000"

    # خاموشش کن
    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/telegram/account",
        json={"name": "اصلی", "enabled": False}, timeout=5,
    )
    assert r.status_code == 200
    assert handlers.settings.telegram.account("اصلی").enabled is False
    assert handlers.settings.telegram.account("اصلی").api_hash == "s" * 32


def test_web_toggle_unknown_account_returns_400(web_server) -> None:
    import requests

    r = requests.post(
        f"http://127.0.0.1:{web_server.port}/api/telegram/account",
        json={"name": "ghost", "enabled": True}, timeout=5,
    )
    assert r.status_code == 400
    assert "وجود ندارد" in r.text


def test_index_html_renders_account_row_controls() -> None:
    """مودال تنظیمات باید نام/شماره/وضعیت/اتصال/فعال/فعال‌کن را داشته باشد."""
    from local_agent.utils.paths import web_templates_dir

    html = (web_templates_dir() / "index.html").read_text(encoding="utf-8")
    assert "telegramAccounts" in html
    assert "toggleAccountEnabled" in html
    assert "connectTelegram(acc.account)" in html
    assert "switchAccount(acc.account)" in html
    assert "accountStateLabel(acc.state)" in html
    assert "اکانت فعال: " in html
