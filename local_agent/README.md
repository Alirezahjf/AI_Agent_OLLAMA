# Local Windows Assistant

یک ایجنت محلی حرفه‌ای برای **دسکتاپ ویندوز شما** — برخلاف ربات تلگرام/بله
که روی سرور اجرا می‌شود، این ایجنت مستقیماً روی لپ‌تاپ/کامپیوتر شما زندگی
می‌کند و به همه چیز دسترسی دارد:

* باز کردن هر برنامهٔ ویندوزی (Chrome، Telegram Desktop، Photoshop، VS Code، Task Manager و...)
* کنترل GUI با ماوس و کیبورد (pyautogui) — drag & drop، کلیک، تایپ
* ارسال پیام از **اکانت شخصی تلگرام شما** (Telethon user client)
* خواندن/نوشتن فایل، اجرای shell command، جستجوی وب
* حافظهٔ بلندمدت بین sessionها
* تأیید هوشمند: کارهای امن (باز کردن برنامه، جستجو) مستقیم انجام می‌شود؛ کارهای
  مخرب (ارسال پیام، پاک کردن فایل، kill کردن پروسس) فقط با تأیید شما

> ⚠️ **تفاوت با ربات تلگرام:** ربات تلگرام روی سرور اجرا می‌شود و به ماشین شما
> دسترسی ندارد. این ایجنت روی **خود ویندوز شما** اجرا می‌شود و واقعاً تلگرام
> دسکتاپ/فتوشاپ/Task Manager شما را کنترل می‌کند.

---

## نصب سریع

### ۱) پیش‌نیازها

* **Windows 10/11** (روی Linux/macOS هم نصب می‌شود ولی فقط بخش‌های read-only کار می‌کنند)
* **Python 3.11+**
* (اختیاری) **Ollama** برای اجرای محلی LLM: [ollama.com](https://ollama.com)
* (اختیاری) **Git Bash** یا **PowerShell** (هر دو کار می‌کنند)

### ۲) نصب

```powershell
# در PowerShell، داخل پوشهٔ پروژه:
git clone https://github.com/Alirezahjf/AI_Agent_OLLAMA.git
cd AI_Agent_OLLAMA
python -m venv .venv
.venv\Scripts\Activate.ps1
python local_agent_setup.py install-all
```

`install-all` همه چیز را نصب می‌کند:
- runtime اصلی (requests, Pillow, dotenv, rich)
- mouse/keyboard automation (`pyautogui`, `mss`)
- Telegram user client (`telethon`)

اگر فقط LLM و کارهای read-only می‌خواهید: `python local_agent_setup.py install`

### ۳) بررسی نصب

```powershell
python local_agent_setup.py doctor
```

اگر همه چیز OK بود:

```powershell
python -m local_agent
```

اولین اجرا یک فایل `config.json` در `%USERPROFILE%\.local_assistant\` می‌سازد.
آن را باز کنید و مدل LLM را تنظیم کنید.

### ۴) تنظیم LLM

فایل `%USERPROFILE%\.local_assistant\config.json` را باز کنید:

```json
{
  "llm": {
    "provider": "ollama",
    "ollama_base_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:7b"
  }
}
```

یا برای GapGPT/AvalAI:

```json
{
  "llm": {
    "provider": "openai_compatible",
    "openai_base_url": "https://api.avalai.ir/v1",
    "openai_api_key": "sk-...",
    "openai_model": "claude-sonnet-5"
  }
}
```

می‌توانید همان مقادیر را با env variable هم override کنید:

```powershell
$env:LOCAL_AGENT_LLM__PROVIDER = "openai_compatible"
$env:LOCAL_AGENT_LLM__OPENAI_API_KEY = "sk-..."
python -m local_agent
```

### ۵) (اختیاری) اتصال تلگرام شخصی

در [my.telegram.org](https://my.telegram.org) یک app بسازید و `api_id` و
`api_hash` بگیرید. سپس در `config.json`:

```json
{
  "telegram": {
    "enabled": true,
    "api_id": 123456,
    "api_hash": "abcdef1234567890abcdef",
    "phone": "+98912..."
  }
}
```

سپس در CLI بنویسید:

```
/telegram connect
```

یک‌بار SMS code از شما می‌پرسد؛ session ذخیره می‌شود و دفعهٔ بعد لازم نیست.

---

## استفاده

### دستورات CLI

| دستور | کار |
|---|---|
| `/help` | راهنما |
| `/status` | مدل، provider، تعداد پیام‌ها |
| `/actions` | لیست همهٔ ابزارهای موجود |
| `/model NAME` | تغییر مدل (مثلاً `/model claude-sonnet-5`) |
| `/provider NAME` | تغییر provider (ollama / openai_compatible / auto) |
| `/approve` | روشن/خاموش‌کردن auto-approve |
| `/confirm MODE` | تنظیم confirm: `destructive` (پیش‌فرض) / `always` / `never` |
| `/reset` | پاک‌کردن حافظهٔ گفتگو |
| `/screenshot` | اسکرین‌شات از صفحه |
| `/telegram connect` | اتصال به تلگرام شخصی |
| `/telegram chats` | لیست چت‌ها |
| `/send NAME TEXT` | ارسال سریع پیام |
| `/quit` | خروج |

### مثال‌ها

```
you > تلگرام رو باز کن
assistant > تلگرام دسکتاپ در حال باز شدن...
  action open_application: started telegram (pid=1234)
assistant > تلگرام باز شد. می‌خواهی به چه کسی پیام بدم؟

you > به علی پیام بده "سلام، ۵ دقیقه دیگه می‌رسم"
assistant > ابتدا لیست چت‌ها رو می‌گیرم...
  action telegram.list_chats: 30 chats loaded
  action telegram.send_message: message sent
assistant > پیام فرستاده شد. ✅

you > مرورگر Chrome رو باز کن و عبارت "آب و هوای تهران" رو سرچ کن
assistant > Chrome رو باز می‌کنم و سرچ می‌کنم.
  action open_application: started chrome
  action type_text: typed 32 characters
  action key_press: pressed enter
assistant > سرچ انجام شد. گوگل نتایج رو نشون داد.

you > فتوشاپ رو باز کن و فایل c:\Users\me\Pictures\photo.jpg رو باز کن
assistant > فتوشاپ رو باز می‌کنم و فایل رو درگ می‌کنم.
  action open_application: started photoshop
  ⚠ approval required: drag_to from=(100,100) to=(500,500)
  approve? [y/N/d(etails)/a(lways-yes)]: y
  action drag_to: dragged (100,100) -> (500,500)
assistant > انجام شد. ✅

you > تسک منیجر رو باز کن و همهٔ Chrome ها رو ببند
assistant > تسک منیجر باز می‌شه و Chrome ها بسته می‌شن.
  action open_application: started taskmgr
  ⚠ approval required: close_application name=chrome
  approve? [y/N]: y
  action close_application: closed chrome (exit 0)
```

---

## لیست کامل ابزارها

### کنترل برنامه‌ها (Safe)
- `open_application(name, arguments, working_dir, wait, timeout)`
- `close_application(name, force)` — Destructive
- `focus_window(title)`
- `list_applications(filter)`
- `locate_application(name)`

### کنترل پنجره‌ها (Safe)
- `list_windows(filter)`
- `move_window(title, x, y, width, height)`
- `minimize_window(title)`
- `maximize_window(title)`

### کنترل پروسس‌ها
- `list_processes(filter, max_results)` — Safe
- `kill_process(pid)` — System
- `open_task_manager()` — Safe

### Clipboard (Safe)
- `clipboard_read()`
- `clipboard_write(text)`

### فایل
- `read_file(path, start_line, max_lines)` — Safe
- `write_file(path, content)` — Destructive
- `list_directory(path)` — Safe
- `make_directory(path)` — Destructive
- `move_path(source, destination)` — Destructive
- `delete_path(path, recursive)` — System
- `search_files(query, path, max_results)` — Safe

### وب (Safe)
- `web_search(query, max_results)`
- `web_fetch(url, max_chars)`

### Shell و سیستم
- `run_shell(command, working_dir, timeout)` — Destructive
- `system_info()` — Safe
- `open_path(path)` — Safe
- `shutdown_computer(delay_seconds, restart)` — System
- `cancel_shutdown()` — System

### GUI Automation (Safe، فقط روی Windows)
- `screen_capture(filename)`
- `mouse_move(x, y, duration)`
- `mouse_click(x, y, button, clicks)`
- `mouse_double_click(x, y)`
- `type_text(text, interval)`
- `key_press(key)`
- `hotkey(keys)`
- `scroll(x, y, amount)`
- `drag_to(from_x, from_y, to_x, to_y, duration)`
- `get_mouse_position()`
- `get_screen_size()`

### تلگرام شخصی (نیاز به `/telegram connect`)
- `telegram.list_chats(limit)`
- `telegram.send_message(chat, text)` — Destructive
- `telegram.send_photo(chat, path, caption)` — Destructive
- `telegram.send_file(chat, path, caption)` — Destructive
- `telegram.search_messages(chat, query, limit)` — Safe
- `telegram.get_me()` — Safe

---

## معماری

```
local_agent/
├── __init__.py
├── __main__.py              ← python -m local_agent
├── core/
│   ├── config.py            ← AssistantSettings (frozen dataclass)
│   ├── context.py           ← RuntimeContext (history + events)
│   ├── errors.py            ← AssistantError, ActionRefused, ...
│   └── logging_setup.py
├── llm/
│   ├── client.py            ← OllamaClient + OpenAICompatibleClient
│   └── errors.py
├── actions/
│   ├── registry.py          ← ActionRegistry, ConfirmationGate, Risk
│   ├── app_control.py       ← open_application, close_application
│   ├── window_control.py    ← list_windows, move_window
│   ├── process_control.py   ← list_processes, kill_process
│   ├── clipboard.py         ← clipboard_read, clipboard_write
│   ├── file_ops.py          ← read_file, write_file, ...
│   ├── web.py               ← web_search, web_fetch
│   ├── system.py            ← run_shell, shutdown_computer
│   └── runner.py
├── automation/
│   ├── gui.py               ← pyautogui wrapper
│   └── screenshot.py        ← mss + PIL fallback
├── telegram/
│   └── client.py            ← Telethon user client wrapper
├── cli/
│   ├── app.py               ← main REPL
│   ├── render.py            ← Rich terminal renderer
│   └── prompts.py           ← dynamic system prompt builder
└── utils/
    └── platform.py          ← Windows helpers (registry, UWP, ctypes)

local_agent_setup.py         ← installer / doctor / config opener
```

---

## تست

```powershell
.venv\Scripts\Activate.ps1
python -m pytest tests_local_agent/ -v
```

۶۲ تست شامل:
- ۱۲ تست برای config و persistence
- ۹ تست برای context و history
- ۹ تست برای action registry
- ۸ تست برای file operations
- ۱۵ تست برای LLM client
- ۵ تست برای CLI smoke
- ۴ تست برای platform helpers

---

## امنیت

* **همیشه تأیید برای کارهای مخرب:** ارسال پیام، پاک کردن فایل، kill کردن پروسس، shutdown.
* **کلید API:** اگر در `config.json` بگذارید، در فایل ذخیره می‌شود. برای امنیت بیشتر از env variable استفاده کنید (`LOCAL_AGENT_LLM__OPENAI_API_KEY`).
* **Shell:** `run_shell` مخرب است — اگر می‌خواهید محدود به work_dir شود، `safety.restrict_shell_to_workdir` را true کنید.
* **Telethon session:** فایل `<session_name>.session` معادل لاگین کامل است. آن را جای امن نگه دارید.

---

## سوالات متداول

**آیا می‌توانم از این ایجنت در کالی لینوکس هم استفاده کنم؟**
CLI اجرا می‌شود ولی فقط ابزارهای read-only (جستجوی وب، file ops، system_info) کار می‌کنند. mouse/keyboard فقط روی Windows.

**آیا می‌توانم همزمان با ربات تلگرام از این ایجنت استفاده کنم؟**
بله، کاملاً مستقل هستند. ربات روی سرور، ایجنت روی دسکتاپ شما.

**چه مدلی پیشنهاد می‌کنید؟**
- برای local: `qwen2.5:7b` (سریع، رایگان، کیفیت خوب)
- برای cloud: `claude-sonnet-5` از AvalAI (قوی‌ترین برای فارسی)
- برای کد: `kimi-k2.7-code` (context بلند)

**آیا می‌توانم یک مدل خاص فقط برای یک task استفاده کنم؟**
بله، با `/model NAME` می‌توانید وسط کار عوض کنید.

**تفاوت با Hermes Agent و Claude Code چیست؟**
- این ایجنت **واقعاً دسکتاپ شما را کنترل می‌کند** (pyautogui + Telethon).
- اکانت شخصی تلگرام شما را دارد (نه فقط bot).
- CLI terminal-based است (نه TUI گرافیکی، برای سرعت).
- متن‌باز، بدون subscription، روی ماشین خودتان.
