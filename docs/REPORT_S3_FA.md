# گزارش نهایی — رفع ۱۰ باگ + ویژگی زمان‌بندی (سشن ۳)

**تاریخ:** ۲۰۲۶-۰۸-۰۶ · **برنچ:** `arena/019fd774-ai-agent-ollama` · **PR:** [#18](https://github.com/Alirezahjf/AI_Agent_OLLAMA/pull/18) به `main` (بدون merge)

---

## ۱) چه چیزی انجام شد

هر باگ/ویژگی در یک **کامیت فارسی جدا** (۱۳ کامیت) ثبت شد؛ `pytest -q`
کاملاً سبز و **آفلاین** است: **۶۱۲ passed / ۱ skipped**. فقط فایل‌های
لمس‌شده ruff-clean شدند (بازفرمت سراسری انجام نشد). هیچ رازی
(api_id/api_hash/توکن/پسورد) در چت، لاگ، کامیت‌ها یا خروجی endpointها
چاپ نمی‌شود.

### الف) باگ‌ها

| # | کامیت | خلاصهٔ ریشه‌یابی واقعی و رفع |
|---|---|---|
| ۱ | `5653351` | **ایمیل HTML به‌صورت متن ساده می‌رفت.** ریشه تأیید شد: `_build_mime`/`_build_mime_reply` همیشه `set_content` (text/plain) صدا می‌زدند. رفع: تشخیص خودکار HTML (شروع با `<html`/`<!DOCTYPE` یا تگ معنادار) و ساخت **multipart/alternative** با بخش `text/plain` سلب‌شده + `text/html`؛ در `send`/`reply` هر دو بکند. |
| ۲ | `6d43a2d` | **موضوع‌ها base64/مُخ می‌شدند.** ریشه تأیید شد: هدرهای RFC 2047 مستقیم استفاده می‌شدند. رفع: `_decode_header_value` با `email.header.decode_header` و استفاده در همهٔ محل‌های subject/sender/نام پیوست (OAuth + IMAP). |
| ۳ | `3ba4ce2` | **گیرندهٔ Markdown.** ریشه تأیید شد: اعتبارسنجی فقط `"@" in to` بود. رفع: `_extract_email` لینک `[x](mailto:y)` را تمیز و با regex اعتبارسنجی می‌کند؛ خطای فارسی؛ قانون پرامپت «فقط name@domain خام». |
| ۴ | `46d1ee4` | **خطای خام «FETCH command error».** ریشه تأیید شد: `fetch(str(msg_id))` بدون اعتبارسنجی. رفع: `_require_numeric_id` با پیام فارسی «شناسهٔ ایمیل باید عددی باشد»؛ همهٔ `imaplib.IMAP4.error`/`smtplib.SMTPException`/`OSError` در هر دو بکند به `GmailError` فارسی تبدیل می‌شوند (دیگر «action ... crashed» در ERROR نداریم)؛ توضیح اکشن `download_attachment` اصلاح شد. |
| ۵ | `f4325aa` | **گیجی فایل‌های ضمیمهٔ چت.** ریشه: مدل به‌جای ارسال مستقیم، search/download می‌کرد. رفع: قانون پرامپت صریح + `_resolve_attachments` (مسیر نسبی از work_dir، مطلق عادی). |
| ۶ | `1d1b74e` | **اکانت تلگرام disabled می‌ماند و reconnect نمی‌شد.** ریشه تأیید شد: فلوی اتصال `enabled=True` را persist نمی‌کرد و `switch_account` فقط active را عوض می‌کرد. رفع: `_mark_account_enabled` در `start_telegram_login`/`submit_telegram_code`/`submit_telegram_password`/`connect_telegram` هنگام `connected`؛ `switch_telegram_account` اکانت هدف را فعال می‌کند؛ `telegram_status` فیلدهای `enabled` (هر اکانت) و `feature_enabled` (سراسری) را جدا می‌دهد. |
| ۶-ب | `8d02f4c` | **«Connection to Telegram failed 5 time(s)».** ریشه: خطای شبکه/فیلترشکن با خطای config قاطی می‌شد. رفع: `_is_telegram_network_error` (ConnectionError/timeout/DNS/عبارت «connection to telegram failed») → پیام فارسی جدا: «اتصال به سرور تلگرام برقرار نشد؛ اتصال اینترنت را بررسی کنید و در صورت نیاز از فیلترشکن/VPN استفاده کنید». |
| ۷ | `1ac3048` | **منوی اکانت‌ها در تنظیمات وب نمی‌آمد.** ریشه: با ساخت مستقیم config، `accounts` خالی بود و هیچ سوییچ/اکشنی وجود نداشت. رفع: سینتز ردیف «اکانت فعال» در `telegram_accounts_status`، endpoint جدید `POST /api/telegram/account` (فقط name+enabled؛ بدون ردوبدل اعتبارنامه)، سوییچ «فعال»، برچسب فارسی وضعیت، دکمه‌های «اتصال» و «فعال کن/تعویض»، و نمایش اکانت فعال در هدر. |
| ۸ | `bf3556c` | **تنظیمات بعد از ری‌استارت پاک می‌شد (مهم‌ترین).** ریشه تأیید شد: مسیر config ثابت بود در حالی که فایل واقعی کاربر در پوشهٔ پروژه است. رفع: `_default_config_path` با اولویت LOCAL_AGENT_CONFIG → فایل پیش‌فرض موجود → جست‌وجوی config واقعی در LOCAL_AGENT_DATA_DIR/پوشهٔ جاری/پوشهٔ پروژه (اولین فایلی که «تنظیمات واقعی» دارد؛ config خارجی فیلتر می‌شود) → fallback؛ **مهاجرت قوی** از همهٔ جاهای قبلی با لاگ فارسی؛ بعد از مهاجرت همان اجرا دوباره می‌خواند؛ نوشتن‌ها همیشه به همان مسیر خوانده‌شده (config_path روی settings می‌ماند)؛ `check_config_consistency` دربارهٔ config سرگردان و قابل‌نوشتن‌بودن هشدار/خطا می‌دهد. |
| ۹ | `700afbb` | **«پورت 7824 مشغول است» در doctor در حالی که سرویس خودمان است.** ریشه: پروب ضعیف. رفع: `_is_our_web_server` پاسخ کامل `/healthz` را می‌خواند و JSON `{"ok": true}` را دقیق چک می‌کند؛ bind شکست‌خورده + پروب موفق → **OK** با پیام «پورت X توسط همین دستیار در حال استفاده است»؛ فقط سرویسِ دیگر → WARN/--port. |
| ۱۰ | `3290f09` | **اسپم «gmail client not built ...».** ریشه: WARNING تکراری در هر شروع با username خالی. رفع: لاگ DEBUG + پیام فارسی روشن در `_warnings()`/بنر UI: «برای اتصال جیمیل، username و App Password (یا credentials.json) را ست کنید...». |

### ب) ویژگی: یادآوری و اجرای زمان‌بندی‌شده (`c345d7b`)

- `local_agent/core/scheduler.py`: ریسمان دیمون (چک هر ~۳۰ ثانیه)، ذخیره در `data_dir/scheduled.json` (بقا بعد از ری‌استارت)، قفل thread-safe، نوشتن اتمیک.
- `parse_at`: ISO (`2026-08-06T18:30`)، «در HH:MM» (امروز/فردا)، نسبی فارسی («تا ۵ دقیقه دیگر»، «یک ساعت دیگر» — اعداد فارسی و کلمات عددی هم قبول).
- اکشن‌ها: `schedule_reminder` (Safe)، `schedule_task` (Destructive؛ هنگام ثبت تأیید می‌گیرد، سرِ موعد اکشن را با auto_confirm اجرا و نتیجه را رویداد می‌کند)، `list_scheduled_jobs`، `cancel_scheduled_job`.
- اعلان: رویداد `scheduled_fired` روی event_bus → پخش سراسری WebSocket (رویدادهای run_id خالی به همهٔ کلاینت‌ها) → Notification API مرورگر + اعلان دسکتاپ (plyer/win10toast ویندوز، notify-send لینوکس) + پیام سیستمی در چت.
- قانون ۱۵ در سیستم‌پرامپت + بخش‌های README به‌روز شد (`7a70c58`).

## ۲) دقیقاً چه چیزی تست شده (همه آفلاین)

| موضوع | تست |
|---|---|
| HTML ایمیل | MIME شامل multipart/alternative + text/html برای ۴ نمونه HTML؛ text/plain ساده برای متن ساده؛ fallback متنی بدون `<`؛ reply و attachments |
| RFC 2047 | دیکد `=?UTF-8?B?...?=` (همان رشتهٔ لاگ کاربر) → «هشدار امنیتی»؛ Q-encoding؛ هدر خراب بدون کرش |
| گیرندهٔ Markdown | `[x](mailto:y)` → آدرس واقعی؛ `not-an-email`/`a@`/خالی → خطای فارسی |
| id غیرعددی | read/reply/download با `content-bottom_1.png` → GmailError فارسی و بدون fetch؛ بدون لاگ ERROR |
| پیوست نسبی | فایل در work_dir با `attachments=[name]` ضمیمه می‌شود؛ مسیر مطلق دست‌نخورده |
| تلگرام reconnect | اتصال موفق (fake) → `enabled=True` و persist در config؛ ساخت دوبارهٔ handlers از همان config → کلاینت ساخته می‌شود؛ switch اکانت را فعال می‌کند |
| خطای شبکهٔ تلگرام | fake با ConnectionError → پیام «فیلترشکن/VPN»؛ خطای غیرشبکه → پیام عمومی |
| منوی اکانت‌ها | /api/status شامل telegram_accounts؛ toggle از طریق HTTP بدون ازدست‌رفتن api_hash/phone؛ 400 برای اکانت ناشناخته؛ عناصر HTML |
| بقای تنظیمات | config واقعی در پوشهٔ پروژه → POST /api/settings → لود مجدد (شبیه‌سازی ری‌استارت) → همهٔ مقادیر باقی‌ماند؛ مهاجرت فایل قدیمی پُر؛ فایل خارجی نادیده گرفته می‌شود؛ هشدار doctor |
| پورت doctor | mock بایند شکست‌خورده + پروب موفق → OK؛ نمونهٔ واقعی سرور خودمان → شناسایی؛ JSON خارجی/غیرJSON → رد |
| هشدار گیمیل | با config ناقص هیچ WARNING در لاگ نمی‌آید؛ پیام فارسی در warnings هست |
| زمان‌بند | ثبت → در scheduled.json؛ `_tick()` با ساعت fake → رویداد reminder و status=fired؛ schedule_task با system_info → اجرا و نتیجه به‌صورت رویداد؛ cancel؛ persist بین ساخت مجدد؛ رسیدن رویداد به WebSocket واقعی (uvicorn) |

## ۳) چه چیزی نیازمند تأیید روی ویندوز ۱۱ واقعی است (صادقانه)

روی لینوکس توسعه داده شد؛ این موارد با تست واحد **mock** شده‌اند و باید
روی ویندوز ۱۱ واقعی تأیید شوند:

1. **اعلان دسکتاپ/toast** (plyer یا win10toast) برای یادآوری‌ها — در تست‌ها
   callback/مسیر جایگزین است.
2. **لاگین واقعی تلگرام** (Telethon + SMS/2FA و شبکه/فیلترشکن) — تست‌ها
   fake هستند؛ فقط مسیر خطا/موفقیت mock شده.
3. **OAuth جیمیل** (باز شدن مرورگر و تأیید یک‌بار) — تست‌ها بکند جعلی دارند.
4. **UAC/elevation** و **بیلد PyInstaller** — در این سشن ساخته/اجرا نشد.
5. خودِ پروب `/healthz` در doctor روی بایند ویندوز (SO_REUSEADDR رفتار
   متفاوت دارد) قابل بررسی نهایی است.

## ۴) نمونهٔ لاگ (پیام‌های واقعی سیستم پس از رفع — ترکیبی)

```
INFO  local_assistant.config:  تنظیمات قدیمی از C:\Users\alireza.jafarzadeh\Downloads\New folder\AI_Agent_OLLAMA\config.json به C:\Users\alireza.jafarzadeh\.local_assistant\config.json منتقل شد تا پس از ری‌استارت از بین نروند.
DEBUG local_assistant.bridge.handlers:  gmail client not built: برای حالت App Password، آدرس ایمیل (username) لازم است   ← فقط debug، دیگر اسپم WARNING نیست
INFO  local_assistant.bridge.handlers:  telegram submit_password → state=connected؛ اکانت «اصلی» enabled=True شد و در config.json ذخیره شد
INFO  local_assistant.scheduler:  یادآوری ثبت شد: «خرید نان» در 2026-08-06T18:30 (شناسه: 4f2a91c0e1)
INFO  local_assistant.actions:  running action gmail.send with to='sajjadbul313@gmail.com' subject='گزارش هفتگی' body='<html>...'
INFO  local_assistant.gmail:  ایمیل با بدنهٔ HTML به‌صورت multipart/alternative ارسال شد
WARN  local_assistant.bridge.handlers:  telegram start_login network failure: Connection to Telegram failed 5 time(s)
       → پیام فارسی به کاربر: «اتصال به سرور تلگرام برقرار نشد؛ اتصال اینترنت را بررسی کنید و در صورت نیاز از فیلترشکن/VPN استفاده کنید، سپس دوباره تلاش کنید.»
INFO  local_assistant.actions:  running action schedule_task with at='تا ۱ ساعت دیگر' action_name='telegram.send_message' ...
INFO  local_assistant.scheduler:  scheduled task 9c2e1ab4 اجرا شد → «✅ پیام به Saved Messages ارسال شد»
INFO  local_assistant.web:  websocket: رویداد سراسری scheduled_fired به همهٔ کلاینت‌ها پخش شد
```

## ۵) کامیت‌ها (۱۳ عدد، همه فارسی)

```
7a70c58 مستندات: به‌روزرسانی README برای زمان‌بندی، رفع HTML جیمیل و چند-اکانتی تلگرام
615e523 تمیزکاری: حذف noqa بی‌استفاده و ترکیب with در تست گیمیل (ruff)
c345d7b ویژگی: یادآوری و اجرای زمان‌بندی‌شده (scheduler)
700afbb رفع: تشخیص پورت مشغولِ خودِ دستیار در doctor (پروب /healthz)
bf3556c رفع: تنظیمات بعد از ری‌استارت پاک نمی‌شود (منبع حقیقت واحد برای config)
1ac3048 رفع: منوی اکانت‌های تلگرام در تنظیمات وب (لیست، سوییچ فعال، هدر)
8d02f4c رفع: پیام فارسی جدا برای خطای شبکهٔ تلگرام (Connection to Telegram failed)
1d1b74e رفع: اکانت تلگرام بعد از اتصال enabled می‌شود و بعد از ری‌استارت reconnect می‌شود
3290f09 رفع: هشدار اسپم «gmail client not built» و پیام فارسی در UI
f4325aa رفع: بازکردن مسیر نسبی پیوست‌ها از پوشهٔ کاری + راهنمای مدل
46d1ee4 رفع: اعتبارسنجی شناسهٔ عددی ایمیل و خطاهای فارسی IMAP/SMTP
3ba4ce2 رفع: تمیزکردن و اعتبارسنجی آدرس گیرندهٔ ایمیل (Markdown mailto)
6d43a2d رفع: دیکد هدرهای RFC 2047 موضوع/فرستندهٔ ایمیل
5653351 رفع: تشخیص HTML در ارسال ایمیل و ساخت multipart/alternative
```
