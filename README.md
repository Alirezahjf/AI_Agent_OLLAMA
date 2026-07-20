# عامل حرفه‌ای فارسی برای Telegram — Ollama / GapGPT / AvalAI

یک **عامل واقعی برای workflow توسعه و مدیریت workspace** است، نه یک chatbot که فقط جواب یا یک JSON نمایشی می‌دهد. کاربر در تلگرام هدف را با زبان طبیعی می‌گوید؛ عامل وضعیت واقعی پوشه را بررسی می‌کند، برنامهٔ اجرایی می‌چیند، فایل‌ها و ساختار پروژه را مرحله‌ای می‌سازد، برای هر تغییر تأیید می‌گیرد، تست را اجرا می‌کند، خطا را تحلیل می‌کند و تا سقف مشخص اصلاح/اعتبارسنجی را ادامه می‌دهد. خروجی آخرین دستور نیز به‌شکل تصویر PNG ترمینال در تلگرام فرستاده می‌شود.

> **امنیت قبل از اتوماسیون:** این برنامه روی فایل‌ها و فرمان‌های واقعی کار می‌کند. هیچ LLM—even بهترین مدل ابری—جایگزین تأیید انسان و محیط ایزوله نیست. برای کار جدی آن را در یک VM یا container بدون secret و با یک workspace mount‌شده اجرا کنید.

---

## چه چیزهایی نسبت به نسخهٔ ساده بهتر شده‌اند؟

### Agent workflow، نه پاسخ نمایشی

عامل دستورالعمل عملیاتی مشخصی دارد:

1. **فهم هدف و بررسی واقعی:** ابتدا workspace و فایل‌های مرتبط را با ابزار می‌خواند؛ ساختار یا نام فایل را حدس نمی‌زند.
2. **برنامه و ساختار:** برای پروژهٔ جدید، پوشه‌ها، manifest، ماژول‌ها، تست‌ها و فایل‌های لازم را مرحله‌ای می‌سازد. برای پروژهٔ موجود، ابتدا فایل مرتبط را می‌خواند و با patch کوچک تغییر می‌دهد.
3. **کنترل تغییر:** نوشتن/patch فایل، ساخت پوشه، اجرای فرمان تغییردهنده، جابه‌جایی فایل و ساخت screenshot همگی در تلگرام دکمهٔ **تأیید و اجرا / لغو** دارند.
4. **تست و حلقهٔ اصلاح:** بعد از کد، عامل lint/test مناسب را اجرا یا پیشنهاد می‌کند. exit code و خروجی را تحلیل می‌کند؛ اگر خطا ببیند علت و اصلاح حداقلی را گزارش می‌کند و پس از تأیید دوباره اعتبارسنجی می‌کند.
5. **گزارش قابل پیگیری:** در پایان فایل‌های مهم، تست‌های واقعاً اجراشده، خطاهای باقی‌مانده و گام بعدی را می‌گوید. رخدادها و history هر گفتگوی تلگرام در SQLite ثبت می‌شوند.

حلقهٔ ابزار با `MAX_AGENT_TURNS` محدود شده تا مدل در loop بی‌پایان نماند. اگر به سقف برسد، صادقانه توقف را گزارش می‌کند.

### ابزارهای واقعی و محافظت‌شده

| قابلیت | رفتار |
|---|---|
| بررسی پروژه | `inspect_project`، درخت فایل، manifestها و تست‌ها را نشان می‌دهد |
| فایل | list، read با شمارهٔ خط، write اتمیک، و `patch_file` با تطابق دقیق یک‌باره |
| ساخت پروژه | `create_directory` و `write_file`، با تأیید کاربر |
| اجرا و تست | `run_command` با timeout، خروجی محدود، exit code و تصویر ترمینال |
| دسته‌بندی فایل | ابتدا `analyze_directory` (پسوندها و duplicateهای تا 5MB)، سپس preview؛ جابه‌جایی فقط با `apply=true` و تأیید، بدون overwrite |
| وب | `search_web` متادیتای نتایج عمومی را می‌گیرد و آن را صریحاً **دادهٔ غیرقابل‌اعتماد** می‌داند |
| تشخیص مرورگر | `diagnose_browser_runtime` وضع Python/Playwright/Chromiumِ **همان interpreter ربات** را فقط‌خواندنی گزارش می‌کند و در صورت نقص، دستور نصب دقیق با همان interpreter را می‌دهد |
| screenshot وب | `capture_screenshot` برای URLهای HTTP(S)، با Playwright اختیاری و خروجی PNG در workspace؛ فایل واقعی PNG بلافاصله با `reply_photo` به تلگرام ارسال می‌شود |
| screenshot خروجی command | خود ربات، تصویر آخرین خروجی ترمینال را بدون نیاز به desktop session می‌فرستد |

تمام pathها با `resolve()` زیر `WORKSPACE_ROOT` کنترل می‌شوند. `run_command` نیز دستورهایی را که به مسیر مطلق خارج از workspace (مثل `AppData` کاربر دیگر) یا به `..` خارج‌شونده اشاره دارند مسدود می‌کند. خواندن/نوشتن `.env`، credentialها، کلید SSH و مسیرهای حساس مسدود است. دستورهای واضحاً مخرب (`rm`، `mkfs`، shutdown، `git clean`، `git reset --hard` و …) و دستورهای مخرب Windows (`del`، `erase`، `rmdir`، `rd`، `format`، `diskpart`، `Remove-Item`) hard-block هستند. تشخیص «read-only» عمداً کوچک و بدون shell chaining است (`dir`، `tree`، `type`، `where`، `findstr` و همتاهای لینوکسی)؛ هر فرمان دیگر — از جمله chain مثل `cd /d folder && dir` — برای تأیید می‌آید.

### اجرای command در Windows

اگر ربات روی Windows اجرا شود، `run_command` دستورها را با `cmd.exe /d /s /c` (از `COMSPEC` با fallback به `SystemRoot\System32\cmd.exe`) اجرا می‌کند و به Git Bash یا WSL وابستگی ندارد. در Linux/macOS همان `bash -lc` استفاده می‌شود. اگر پوستهٔ سیستم پیدا نشود، workflow با پیام فارسی خوانا و `exit code: unavailable` تمام می‌شود، نه crash.

مدل گاهی در چت نام فایل را به Markdown link تبدیل می‌کند (`check_[pw.py](http://pw.py)`) یا `&&` را به‌صورت `&amp;&amp;` می‌فرستد؛ یک normalizer داخل ابزارها متن visible را نگه می‌دارد، URL مخفی داخل link هرگز اجرا نمی‌شود و ورودی نامعتبر با ToolError رد می‌شود.

### اسکرین‌شات وب و Playwright

نکتهٔ مهم: Playwright باید در **همان Pythonی** نصب شود که ربات با آن اجرا می‌شود، نه لزوماً `py -3`. اگر `capture_screenshot` گفت Playwright/Chromium نصب نیست، خود پیام خطا دستور دقیق را با همان interpreter می‌دهد؛ مثلاً:

```bat
"C:\path\to\project\.venv\Scripts\python.exe" -m pip install playwright
"C:\path\to\project\.venv\Scripts\python.exe" -m playwright install chromium
```

این دستورها را می‌توانید مستقیم با run_command اجرا و تأیید کنید (داخل workspace اجرا می‌شوند و نیازی به cd خارج از workspace نیست). ابزار `diagnose_browser_runtime` نیز هر زمان این وضعیت را گزارش می‌دهد.

---

## ارائه‌دهندگان و مدل‌ها

عامل یک لایهٔ provider مستقل دارد؛ ابزارها و workflow برای همه یکسان‌اند:

| Provider | اتصال | مدل پیشنهادی برای agent/coding |
|---|---|---|
| **Ollama** | `http://127.0.0.1:11434/api/chat` | مدل محلی `OLLAMA_MODEL` (مثلاً `qwen2.5:7b`) |
| **GapGPT** | OpenAI-compatible: `https://api.gapgpt.app/v1/chat/completions` | `claude-sonnet-5`؛ کیفیت بیشتر: `gpt-5.6-sol`؛ متعادل: `gpt-5.6-terra` |
| **AvalAI** | OpenAI-compatible: `https://api.avalai.ir/v1/chat/completions` | `claude-sonnet-5`؛ استدلال بیشتر: `gpt-5.6-sol`؛ کدنویسی long-context: `kimi-k2.7-code` |

مدل‌های OpenAI-compatible با native **function calling** صدا زده می‌شوند. اگر مدل یا Ollama نصب‌شده function call را برنگرداند، عامل fallback محدود JSON تک‌ابزاری دارد؛ هیچ متن تولیدشده توسط مدل مستقیماً به shell یا Python داده نمی‌شود.

### کلید API: امن و انعطاف‌پذیر

دو روش وجود دارد:

1. **پیشنهادی (پایدار):** کلید را فقط در محیط یا secret manager نگه دارید:

```dotenv
DEFAULT_PROVIDER=avalai
AVALAI_API_KEY=sk-...
# یا
DEFAULT_PROVIDER=gapgpt
GAPGPT_API_KEY=...
```

2. **موقت از تلگرام:** منوی `⚙️ مدل و API` → GapGPT/AvalAI یا `🔎 تشخیص API`. کلید برای تشخیص با `GET /models` روی endpoint مستند هر سرویس بررسی می‌شود. در صورت تشخیص، دکمهٔ **درست است / اشتباه است** می‌آید و کاربر می‌تواند provider را دستی تعیین کند.

کلید واردشده در Telegram **هرگز در SQLite، audit یا تاریخچهٔ agent ذخیره نمی‌شود** و ربات تلاش می‌کند پیام کلید را حذف کند؛ با این حال Telegram محیط secret manager نیست. برای کلید دائمی/حساس، `.env` یا secret manager روش درست است. کلید session با restart برنامه از RAM پاک می‌شود.

از `/models` یا دکمهٔ `🔄 دریافت مدل‌های قابل‌دسترسی` برای فهرست واقعی مدل‌های account فعلی استفاده کنید. `/model MODEL_ID` نیز نام مدل دلخواه را تنظیم می‌کند.

---

## نصب

### 1) پیش‌نیازها

- Python **3.11+**
- یک bot token از [@BotFather](https://t.me/BotFather)
- یک `WORKSPACE_ROOT` اختصاصی و موجود
- برای حالت محلی: [Ollama](https://ollama.com) و یک مدل

```bash
ollama pull qwen2.5:7b
ollama serve
```

### 2) نصب پکیج

```bash
git clone https://github.com/Alirezahjf/AI_Agent_OLLAMA.git
cd AI_Agent_OLLAMA
python3 -m venv .venv
source .venv/bin/activate              # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e '.[dev]'
cp .env.example .env
```

برای screenshot واقعی صفحهٔ وب، browser اختیاری را هم نصب کنید — حتماً با **همان interpreterی** که ربات را اجرا می‌کنید (اگر venv فعال است، همان `python`):

```bash
pip install -e '.[browser,dev]'
python -m playwright install chromium
```

در Windows اگر ربات را از `.venv` اجرا می‌کنید، `py -3 -m playwright install chromium` کافی نیست چون ممکن است به interpreter دیگری نصب کند؛ از ابزار `diagnose_browser_runtime` دستور دقیق را بگیرید.

### 3) پیکربندی `.env`

حداقل نمونه برای Ollama:

```dotenv
TELEGRAM_BOT_TOKEN=123456:real-bot-token
ALLOWED_TELEGRAM_USER_IDS=123456789
WORKSPACE_ROOT=/home/me/projects
DATA_DIR=/home/me/.local/share/persian-agent
DEFAULT_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:7b
```

نمونه GapGPT:

```dotenv
DEFAULT_PROVIDER=gapgpt
GAPGPT_BASE_URL=https://api.gapgpt.app/v1
GAPGPT_API_KEY=YOUR_GAPGPT_API_KEY
DEFAULT_MODEL=claude-sonnet-5
```

نمونه AvalAI:

```dotenv
DEFAULT_PROVIDER=avalai
AVALAI_BASE_URL=https://api.avalai.ir/v1
AVALAI_API_KEY=sk-...
DEFAULT_MODEL=claude-sonnet-5
```

`DEFAULT_PROVIDER=auto` در صورت وجود `AVALAI_API_KEY` ابتدا AvalAI، سپس GapGPT و در نبود هر دو Ollama را انتخاب می‌کند. تمام تنظیمات و حدهای زمان در [`.env.example`](.env.example) توضیح داده شده‌اند.

> ابتدا `/start` را بفرستید تا ID عددی خود را ببینید؛ سپس `ALLOWED_TELEGRAM_USER_IDS` را پر و برنامه را restart کنید. ربات با allow-list خالی نباید عمومی شود.

### 4) اجرا

```bash
python -m agent.bot
```

---

## تجربهٔ کاربری Telegram

- `/start` — معرفی، ID کاربر و provider فعال
- `/status` — مدل، منبع کلید (فقط «ENV» یا «جلسه»، نه خود کلید)، workspace و guardrailها
- `/models` — فهرست مدل‌های واقعی provider فعلی
- `/model claude-sonnet-5` — مدل دلخواه (بدون واردکردن secret)
- `⚙️ مدل و API` — تغییر Ollama/GapGPT/AvalAI، ورود موقت کلید و auto-detection
- `📜 تاریخچه` — پیام‌ها و auditهای گفتگوی فعلی
- `➕ گفتگوی جدید` — thread جدید با حفظ threadهای قبلی
- `🧹 پاک‌کردن حافظه` — فقط حافظهٔ thread فعلی را پاک می‌کند

نمونه درخواست‌های درست:

```text
داخل پوشهٔ Machine_hesab اگر نبود آن را بساز. یک ماشین‌حساب پایتون CLI ساخت‌یافته
با مدیریت ورودی نامعتبر، تقسیم بر صفر، README و تست pytest بنویس. قبل از هر تغییر
فایل‌های فعلی را بررسی کن؛ بعد از ساخت pytest را اجرا کن و اگر خطا داشت علت را بگو و اصلاح کن.
```

```text
این پروژه را بررسی کن، ساختار و تست‌هایش را گزارش بده. سپس endpoint health-check را
با کمترین تغییر اضافه کن، تست مناسب بنویس و نتیجهٔ اجرای تست را با exit code گزارش کن.
```

```text
پوشه downloads را فقط تحلیل و دسته‌بندی پیشنهادی بده؛ duplicateها را پیدا کن، اما تا
وقتی preview را تأیید نکرده‌ام هیچ فایلی را جابه‌جا نکن.
```

```text
برای مستندات رسمی FastAPI درباره lifespan وب جست‌وجو کن، لینک‌های منبع را بده و هیچ
دستور یا کدی از صفحهٔ وب را بدون بررسی اجرا نکن.
```

---

## معماری

```text
agent/config.py     تنظیمات محیطی و حدها
agent/providers.py  adapterهای Ollama و OpenAI-compatible (GapGPT/AvalAI)
agent/brain.py      workflow bounded: inspect → plan → change → test → verify
agent/tools.py      ابزارهای محلی، sandbox مسیر، policy، normalizer خروجی مدل و schemaهای function calling
agent/storage.py    SQLite: history، preference غیرمحرمانه، pending action، audit
agent/bot.py        Telegram UI، تأیید، provider picker، history، تصویر ترمینال و ارسال artifact
tests/              تست‌های policy، ابزار، Windows، screenshot، ارسال artifact و provider
```

### مدل داده و privacy

- conversation و audit در `DATA_DIR/agent.sqlite3` هستند؛ از آن backup خصوصی بگیرید.
- preference فقط provider/model است؛ **API key هیچ جدول SQLite ندارد**.
- `pending_actions` برای preview/approval ذخیره می‌شود؛ محتوای `write_file` در audit تکرار نمی‌شود.
- history برای جلوگیری از پرشدن context به تعداد پیام و 55k کاراکتر اخیر محدود می‌شود؛ خروجی بزرگ ابزار در context کوتاه می‌شود.

---

## امنیت و مرزها

1. **تأیید به‌معنای sandbox نیست.** فرمان تأییدشده با سطح دسترسی process اجرا می‌شود. برای پروژه/دادهٔ حساس VM/container لازم است.
2. **فایل secret وارد workspace نکنید.** guardrail جلوی مسیرهای شناخته‌شده را می‌گیرد اما جای مدیریت صحیح رازها را نمی‌گیرد.
3. **خروجی وب، dependency و log می‌توانند prompt injection داشته باشند.** agent آن‌ها را دادهٔ غیرقابل‌اعتماد فریم می‌کند؛ تأیید انسانی هنوز ضروری است.
4. `AUTO_APPROVE_MUTATIONS=true` فقط در VM disposable مجاز است. حتی با آن، hard blockها باقی می‌مانند.
5. `run_command` برای workflow توسعه لازم است، ولی فرمان‌های install/test نیز ممکن است script دلخواه اجرا کنند؛ قبل از تأیید متن کامل را بخوانید.
6. سرویس‌های ابری کد، context و خروجی ابزار لازم را به provider انتخاب‌شده می‌فرستند. برای دادهٔ محرمانه از Ollama محلی استفاده کنید.

---

## تست و کیفیت

```bash
pytest -q
ruff check agent tests
```

تست‌ها policy مهم را پوشش می‌دهند: sandbox مسیر، فایل حساس، write/patch اتمیک، hard block دستورات (POSIX و Windows)، مسدودسازی مسیر خارج از workspace در command، تشخیص صحیح read-only، انتخاب cmd.exe/bash بر اساس سیستم‌عامل و مدیریت نبود پوسته، normalizer لینک‌های Markdown، تشخیص Playwright با `sys.executable`، screenshot با artifact، ارسال واقعی PNG با reply_photo، پیش‌نمایش و جابه‌جایی بدون overwrite، provider routing و عدم ذخیرهٔ key.
