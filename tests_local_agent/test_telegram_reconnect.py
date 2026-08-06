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


def test_switch_unknown_account_still_raises(tmp_path: Path) -> None:
    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    with pytest.raises(Exception) as exc:
        handlers.switch_telegram_account("وجودندارد")
    assert "وجود ندارد" in str(exc.value)


def test_accounts_status_never_leaks_secrets(tmp_path: Path) -> None:
    handlers = BridgeHandlers.build(_settings_with_disabled_account(tmp_path))
    blob = json.dumps(handlers.telegram_accounts_status())
    assert "a" * 32 not in blob
    assert "+989120000000" in blob  # phone is fine (it is not a secret)
