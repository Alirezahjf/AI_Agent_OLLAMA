# 🤖 دستیار هوشمند فارسی — AI Agent Platform

یک پلتفرم **ایجنت هوشمند فارسی** با بیش از **۱۰۰ ابزار**، **۱۳ Skill**، **Skill System** با Model Routing و Action Filtering، **Analytics Engine** برای تحلیل داده و ارتباطات، و یکپارچگی با **GitHub, Discord, Slack, Notion, Google Calendar, Home Assistant** و بسیاری دیگر. همه ابزارهای AI از **AvalAI API** (همان کلید و endpoint پروژه) استفاده می‌کنند.

## ⭐ دو ایجنت، یک اکوسیستم

| ایجنت | مسیر | چه کار می‌کند |
|---|---|---|
| **ربات تلگرام/بله** | `agent/` | عامل کدنویسی و مدیریت workspace از راه دور |
| **دستیار محلی** | `local_agent/` | کنترل دسکتاپ + **۱۰۰+ ابزار** + Skill System + Analytics |

### رابط‌های دستیار محلی

| رابط | دستور | توضیح |
|---|---|---|
| 🖥️ اپ دسکتاپ | `python local_agent_setup.py desktop` | پنجره بومی + tray + hotkey |
| 🌐 رابط وب | `python local_agent_setup.py web` | SPA فارسی RTL + Dark Mode |
| ⌨️ CLI | `python -m local_agent` | ترمینال با Rich |
| ✈️ ربات تلگرام/بله | `python local_agent_setup.py bot-telegram` | کنترل از راه دور |

---

## 🧰 ۱۰۰+ ابزار در ۱۳ دسته

### 🐙 GitHub (۲۷ ابزار)
مدیریت کامل repos، issues، PRs، branches، releases، files، search و notifications.
اتصال با **Device Flow** (OAuth 2.0) یا **Personal Access Token**.
Web API endpoints در رابط وب.

### ✈️ تلگرام شخصی (۴۰+ ابزار)
کنترل اکانت شخصی با **Telethon**: لیست چت‌ها (خصوصی/گروه/کانال)، جست‌وجوی مخاطبین،
ارسال پیام/فایل/عکس/ویدیو/ویس/استیکر/لوکیشن، فوروارد، ریپلای، حذف، ویرایش،
مدیریت مخاطبین، بلاک/آنبلاک، عضویت/خروج کانال، پروفایل.
**Cache درون‌session** و **Fuzzy Entity Resolution** (شناسه عددی، @username، +phone، نام).

### 📧 Gmail (۶ ابزار)
خواندن، جست‌وجو، ارسال، پاسخ، دانلود پیوست. OAuth2 یا IMAP/SMTP.

### 📅 Google Calendar (۸ ابزار)
لیست/ساخت/حذف رویداد، OAuth flow، timezone Tehran.

### 📈 Analytics Engine (۸ ابزار)
تحلیل عمیق چت‌های تلگرام، گروه‌ها، Gmail و فایل‌های داده:
- `analytics.analyze_chat` — ساعات اوج، فعال‌ترین اعضا، موضوعات، توزیع هفتگی
- `analytics.analyze_person` — پروفایل شخص: کلمات، سبک، ایموجی، نسبت پیام
- `analytics.analyze_group_members` — رتبه‌بندی اعضا با درصد فعالیت
- `analytics.analyze_gmail` — فرستنده‌های برتر، موضوعات، توزیع ساعتی
- `analytics.compare_chats` — مقایسه side-by-side چند چت
- `analytics.data_analyze` — CSV/Excel با pandas (describe, groupby, query, plot)
- `analytics.schedule_report` — زمان‌بندی گزارش تحلیل
- `analytics.detect_language` — تشخیص زبان متن (فارسی/انگلیسی/عربی/ترکی/...)

### 🤖 AI Content — AvalAI API (۱۴ ابزار)
همه از **همان AVALAI_API_KEY** پروژه:
- `generate_image` — DALL-E 3, GPT-Image, FLUX, Stability AI, Qwen-Image
- `edit_image` — ویرایش تصویر با ماسک
- `generate_video` — Sora, Veo
- `ocr` — Mistral OCR (خروجی Markdown) + Tesseract fallback
- `text_to_speech` — OpenAI TTS (tts-1, tts-1-hd)
- `speech_to_text` — Whisper (whisper-1, whisper-large-v3)
- `translate` — ترجمه با LLM
- `analyze_image` — Vision API (GPT-4o, Claude, Gemini)
- `list_ai_models` — لیست مدل‌ها به‌صورت داینامیک
- `run_code` — Python/JavaScript sandbox
- `pdf_read` — PyPDF2
- `generate_password` — Cryptographically secure
- `db_query` / `db_tables` — SQLite read-only

### 🎮 Discord (۸ ابزار)
لیست سرورها/کانال‌ها، خواندن/ارسال/حذف پیام، ساخت webhook، ارسال از webhook.

### 💬 Slack (۴ ابزار)
لیست کانال‌ها، خواندن/ارسال پیام.

### 📝 Notion (۵ ابزار)
جست‌وجو، خواندن/ساخت صفحه، لیست دیتابیس‌ها.

### 🏠 Home Assistant (۴ ابزار)
لیست entities، گرفتن/تغییر وضعیت، call service.

### 🔔 Notifications (۲ ابزار)
ntfy.sh (رایگان) و Pushbullet.

### 🌐 اطلاعات و اخبار (۵ ابزار)
آب‌وهوا (Open-Meteo)، ارز (150+)، رمزارز (CoinGecko)، YouTube (Invidious)، RSS.

### 📊 System Monitor (۴ ابزار)
CPU/RAM/Disk/Network/Processes.

### 🔗 API Tester (۳ ابزار)
HTTP request (مثل Postman)، تست endpoint، بنچمارک.

### 🔧 سایر
فایل، شل، کلیپ‌بورد، پنجره‌ها، پروسس‌ها، GUI automation، screenshot، scheduler.

---

## 🧠 Skill System

یک سیستم **مدیریت قابلیت‌ها** که هر ابزار را در یک Skill دسته‌بندی می‌کند:

### ۱۳ Skill پیش‌فرض
github, telegram, email, calendar, system, web_info, ai_content, database, discord, slack, notion, smart_home, analytics

### قابلیت‌ها
- **Activate/Deactivate** — ابزارهای skill غیرفعال از context LLM **حذف** می‌شوند
- **Per-skill Model Override** — مثلاً skill GitHub از `claude-sonnet-5` و skill ترجمه از `gpt-4o-mini` استفاده کند
- **Per-skill System Prompt** — دستورالعمل اختصاصی هر skill
- **Model Routing** — تشخیص خودکار skill از پیام کاربر و route به model مناسب
- **Action Filtering** — ابزارهای skill غیرفعال واقعاً از LLM مخفی می‌شوند
- **Persistent** — وضعیت در `data_dir/skills.json` ذخیره می‌شود
- **Web UI** — مدیریت skills از مودال تنظیمات رابط وب
- **Real-time Sync** — تغییرات skill فوری به همه frontends ارسال می‌شود

---

## 🏗️ معماری

```
local_agent/
├── core/
│   ├── config.py          — AssistantSettings (frozen dataclass)
│   ├── context.py         — RuntimeContext
│   ├── skills.py          — SkillManager (13 skills, routing, filtering)
│   ├── analytics.py       — Analytics Engine (people, chats, emails)
│   ├── scheduler.py       — Scheduled reminders/tasks
│   └── errors.py, logging_setup.py, notify.py, cleanup.py
├── llm/
│   └── client.py          — OllamaClient + OpenAICompatibleClient (streaming)
├── telegram/
│   └── client.py          — PersonalTelegram (cache, fuzzy resolve, rich data)
├── github/
│   └── client.py          — GitHubClient (Device Flow + PAT)
├── gmail/
│   └── client.py          — GmailClient (OAuth2 + IMAP/SMTP)
├── actions/               — 100+ tools registered as actions
│   ├── telegram_actions.py, github_actions.py, gmail_actions.py
│   ├── google_calendar.py, integrations.py (Discord/Slack/Notion)
│   ├── ai_content.py (AvalAI: image/ocr/tts/stt/translate/vision)
│   ├── analytics_actions.py, api_tester.py, system_monitor.py
│   ├── info_services.py (weather/currency/crypto/youtube/rss)
│   ├── notifications.py (ntfy/pushbullet/home assistant)
│   ├── skill_actions.py, config_actions.py, scheduler_actions.py
│   ├── file_ops.py, web.py, system.py, app_control.py, ...
│   └── registry.py, runner.py
├── bridge/
│   ├── api/handlers.py    — BridgeHandlers (chat loop, model routing, filtering)
│   ├── server/server.py   — HTTP + SSE server
│   ├── protocol.py        — Event types (SKILLS_CHANGED, ...)
│   └── telegram_bot/      — Bot bridge
├── web/
│   ├── app.py             — FastAPI (50+ endpoints)
│   ├── templates/index.html — SPA with Skills modal
│   └── static/app.js, style.css
├── desktop/               — pywebview + tray + hotkey + installer
└── cli/                   — Rich terminal REPL
```

---

## 📦 نصب

```bash
git clone https://github.com/Alirezahjf/AI_Agent_OLLAMA.git
cd AI_Agent_OLLAMA
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[all]"                # همه‌چیز
# یا ساده‌تر:
python local_agent_setup.py install-all
```

### پیش‌نیازها
- Python **3.11+**
- **AvalAI API Key** (یا GapGPT/Ollama) — برای AI tools و LLM
- **Telegram Bot Token** (اختیاری) — برای ربات
- **Telethon credentials** (اختیاری) — برای تلگرام شخصی

### پیکربندی

```dotenv
# .env — حداقل تنظیمات
DEFAULT_PROVIDER=avalai
AVALAI_BASE_URL=https://api.avalai.ir/v1
AVALAI_API_KEY=sk-...
DEFAULT_MODEL=claude-sonnet-5

# تلگرام شخصی (اختیاری)
# در config.json → telegram section

# GitHub (اختیاری)
# GITHUB_TOKEN یا از Device Flow در رابط وب

# Discord / Slack / Notion (اختیاری)
# DISCORD_BOT_TOKEN, SLACK_BOT_TOKEN, NOTION_API_KEY
```

### اجرا

```bash
python local_agent_setup.py web        # رابط وب → http://127.0.0.1:7824
python local_agent_setup.py desktop    # اپ دسکتاپ
python -m local_agent                  # CLI
python -m agent.bot                    # ربات تلگرام
python -m agent.bale_bot               # ربات بله
```

---

## 🔒 امنیت

1. **تأیید برای کارهای مخرب** — ابزارهای DESTRUCTIVE همیشه تأیید می‌خواهند
2. **Sandbox مسیر** — فایل‌ها فقط داخل workspace
3. **Hard-block** — `rm`, `mkfs`, `shutdown`, `del`, `format` و...
4. **API Key** — هرگز در SQLite/audit ذخیره نمی‌شود
5. **Allow-list** — فقط user IDهای مجاز
6. **Skill Filtering** — ابزارهای skill غیرفعال از LLM مخفی‌اند

---

## 🧪 تست

```bash
pytest tests_local_agent/ -v
```

---

## 📋 ابزارهای AvalAI API

| Endpoint | مدل‌ها | ابزار |
|---|---|---|
| `/v1/chat/completions` | claude-sonnet-5, gpt-5.6-sol, kimi-k2.7-code, deepseek-v4-pro | چت + translate + vision |
| `/v1/images/generations` | dall-e-3, gpt-image-1, flux-pro, qwen-image | generate_image |
| `/v1/images/edits` | gpt-image-1, dall-e-2 | edit_image |
| `/v1/ocr` | mistral-ocr-latest | ocr |
| `/v1/audio/speech` | tts-1, tts-1-hd | text_to_speech |
| `/v1/audio/transcriptions` | whisper-1, whisper-large-v3 | speech_to_text |
| `/v1/videos` | sora, veo | generate_video |
| `GET /v1/models` | — | list_ai_models |

همه از **همان کلید** (`AVALAI_API_KEY`) و **همان base URL** استفاده می‌کنند.
