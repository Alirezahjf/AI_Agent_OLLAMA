# Bridge: Hermes-style architecture

این پروژه حالا یه **Bridge** (مثل Hermes) داره که **مرکز فرماندهی** تمام رابط‌ها و سرویس‌ها است.

## معماری

```
┌─────────────────────────────────────────────────────────────┐
│  Frontends (thin clients)                                   │
│                                                              │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐   │
│  │ Desktop   │ │ Web UI    │ │ CLI       │ │ Telegram  │   │
│  │ app       │ │ (browser) │ │ (terminal)│ │ / Bale bot│   │
│  │ pywebview │ │ Alpine.js │ │ Rich      │ │ PTB       │   │
│  └─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └─────┬─────┘   │
│        │             │             │             │          │
└────────┼─────────────┼─────────────┼─────────────┼──────────┘
         │             │             │             │
         └─────────────┴─────────────┴─────────────┘
                            │
                  ┌─────────▼──────────┐
                  │   Bridge          │
                  │  (localhost:7823) │
                  │  - state          │
                  │  - tools          │
                  │  - agent loop     │
                  │  - LLM client     │
                  └─────────┬─────────┘
                            │
        ┌───────────────────┴───────────────────┐
        │                                       │
  ┌─────▼──────┐                         ┌─────▼──────┐
  │ Windows    │                         │ Personal   │
  │ Desktop    │                         │ Telegram   │
  │ (pyautogui │                         │ (Telethon) │
  │  UIA)      │                         │            │
  └────────────┘                         └────────────┘
```

## ویژگی‌های کلیدی

### ۱. State مشترک
- همهٔ frontendها **یک history** رو می‌بینن
- وقتی توی CLI چت می‌کنید، Web UI و ربات تلگرام همون history رو می‌بینن
- وقتی از ربات تلگرام پیام می‌فرستید، CLI همون پیام رو توی history داره

### ۲. یک پروسه = یک دسکتاپ
- فقط **یک پروسه** با GUI و Telethon session کار می‌کنه
- هیچ وقت دو پروسه همزمان ماوس رو کنترل نمی‌کنن
- هیچ deadlock یا race condition

### ۳. تأیید هوشمند
- Bridge تصمیم می‌گیره کدوم عملیات نیاز به تأیید داره
- تأیید از هر frontend (CLI، Web، Telegram) ممکنه
- مثلاً اگه ربات تلگرام درخواست delete می‌فرسته، Web UI یه دکمهٔ تأیید نشون می‌ده

### ۴. ابزارهای جدید
علاوه بر ۲۸ ابزار قبلی، الان اینا هم هستن:

- **Telegram Desktop GUI** (با UIA + verification):
  - `send_telegram_desktop(chat_name, message, verify=True)`
  - `send_telegram_desktop_batch(chat_names, message, verify=True)`
  - اینها **واقعاً** پیام رو از تلگرام دسکتاپ می‌فرستن (مثل نمونهٔ شما)
  - تأیید واقعی: بعد از ارسال، چت رو می‌خونه و verify می‌کنه

- **UI Automation** (مثل نمونهٔ شما):
  - `list_windows_advanced(filter)`
  - `focus_window_advanced(title)`
  - `find_controls(name, class_name, automation_id, control_type)`
  - با virtual key codes (Ctrl+F حتی روی کیبورد فارسی کار می‌کنه)

### ۵. رابط‌های گرافیکی

- **اپ دسکتاپ** (`local_agent/desktop/`) — پنجرهٔ بومی ویندوز با
  pywebview روی Edge WebView2، به‌علاوهٔ آیکون tray، کلید میان‌بر
  سراسری، نوتیفیکیشن ویندوز، قفل تک‌نمونه و اجرای خودکار.
  Bridge و سرور وب را **در همان پروسه** بالا می‌آورد.
  → [DESKTOP.md](DESKTOP.md)

- **رابط وب** (`local_agent/web/`) — single-page app فارسی/RTL با
  حالت تاریک، کارت اجرای ابزار، دیالوگ تأیید و رندر Markdown.
  → [WEB_UI.md](WEB_UI.md)

> اپ دسکتاپ **دقیقاً همان** front-end وب را لود می‌کند؛ هیچ رابط دومی
> برای نگهداری وجود ندارد.

### ۶. Protocol
- JSON-RPC style با type-safety
- WebSocket/SSE برای streaming events
- Bearer token auth (auto-generated)

## استفاده

### حالت ۱: CLI تنها (ساده)
```powershell
python -m local_agent
```
همه چیز in-process. CLI خودش Bridge رو می‌سازه.

### حالت ۲: اپ دسکتاپ (پیشنهادی)
```powershell
python local_agent_setup.py desktop
```
پنجرهٔ بومی ویندوز + آیکون tray + کلید میان‌بر `Ctrl+Alt+A`.
Bridge و سرور وب داخل همون پروسه اجرا می‌شن.

### حالت ۳: Web UI
```powershell
python local_agent_setup.py web
```
Web UI روی `http://127.0.0.1:7824` باز می‌شه. از گوشی هم (روی همون
شبکه، با `LOCAL_AGENT_WEB_HOST=0.0.0.0`) قابل دسترسه.

### حالت ۴: Telegram Bot + Bridge
ابتدا Bridge رو در یک ترمینال روی ماشین خودتون start کنید:
```powershell
set BRIDGE_URL=http://127.0.0.1:7823
set TELEGRAM_BOT_TOKEN=...
python local_agent_setup.py bot-telegram
```

ربات تلگرام به Bridge محلی وصل می‌شه. حالا از هر جا (حتی خارج از خونه) می‌تونید به Bridge دستور بدید.

### حالت ۵: همه با هم
```powershell
# Terminal 1: اپ دسکتاپ (Bridge اینجا بالا میاد)
python local_agent_setup.py desktop

# Terminal 2: CLI روی همون Bridge
python -m local_agent

# Terminal 3: ربات تلگرام
python local_agent_setup.py bot-telegram
```

## نمونه: ارسال پیام از ربات تلگرام

شما توی تلگرام (از هر جا) می‌فرستید:
```
به علی پیام بده "سلام، فردا ساعت ۵"
```

داخل Bridge:
1. پیام شما → Bridge (از طریق ربات)
2. LLM می‌فهمه: send_telegram_desktop
3. تأیید می‌خواد (اگه فعال باشه) → شما توی تلگرام دکمه می‌زنید
4. Bridge تلگرام دسکتاپ رو باز می‌کنه
5. Ctrl+F، paste، Enter، paste message، Enter
6. **verify**: چت رو می‌خونه و چک می‌کنه پیام واقعاً ارسال شده
7. نتیجه رو برمی‌گردونه به ربات تلگرام

## امنیت

- **Token**: Bridge یه token تصادفی 32 کاراکتری می‌سازه و توی `<DATA_DIR>/bridge.token` ذخیره می‌کنه. هر frontend این token رو می‌خونه.
- **localhost only**: به طور پیش‌فرض فقط `127.0.0.1`. اگه می‌خواهید از شبکه قابل دسترس باشه، `LOCAL_AGENT_BRIDGE_HOST=0.0.0.0` تنظیم کنید (با احتیاط!).
- **Confirmation gate**: در سطح Bridge، نه frontend. وقتی CLI تأیید می‌کنه، Web UI هم می‌فهمه.
- **File permissions**: token file روی POSIX با 0o600 تنظیم می‌شه.

## تست

```powershell
.venv\Scripts\Activate.ps1
python -m pytest tests_local_agent/ -v
```

تست‌ها:
- `test_bridge.py` — پروتکل، handlerها، استریم چت
- `test_bridge_http.py` — سرور HTTP و SSE
- `test_web.py` — endpointهای FastAPI، markup و assetهای رابط
- `test_web_render.py` — تست واقعی مرورگر با Playwright (اختیاری)
- `test_desktop.py` — tray، هات‌کی، قفل تک‌نمونه، آپدیتر، بیلد
- `test_bridge_bot.py` — کیبوردهای inline ربات
- `test_gui_advanced.py` — UIA wrapper و درایور تلگرام دسکتاپ
- `test_integration.py` — سناریوهای end-to-end
- و بقیه (config، actions، llm، telegram، file ops، ...)

مجموعاً **۲۸۶ تست** (+۱۸ تست مرورگری با نصب Playwright).
