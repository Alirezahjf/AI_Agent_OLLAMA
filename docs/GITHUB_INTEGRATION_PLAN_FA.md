# طرح پیشنهادی: اتصال به GitHub و مدیریت پروژه (بررسی + پیشنهاد)

> این فقط **طرح و بررسی** است. بعد از تأیید شما و اعمال بازخوردهایتان کد واقعی نوشته و پوش می‌شود.

---

## ۱) یافته‌های بررسی معماری (پروژه چطور کار می‌کند)

برای اینکه اتصال گیتهاب **دقیقاً هم‌سبک** بقیهٔ پروژه باشد، آن را بررسی کردم:

| بخش | الگوی موجود که دنبال می‌کنیم |
|---|---|
| تنظیمات | دیتاکلاس‌های frozen در `core/config.py` (مدل: `GmailSettings`). یک `GitHubSettings` اضافه می‌شود. |
| پنهان‌سازی راز | خودکار! `config_actions._SECRET_SUFFIXES` شامل `token`, `secret`, `api_key`, `password`, `api_hash` است. پس `github.token` و `github.client_secret` **هرگز** در خروجی چاپ نمی‌شوند. |
| کلاینت | یک کلاس در `local_agent/github/client.py` (مدل: `PersonalTelegram` / بک‌اند gmail). در `context.extra["github"]` تزریق می‌شود. |
| اکشن‌ها | `actions/github_actions.py` با `register_github(registry, context)`؛ در `handlers.py` کنار `register_gmail`/`register_telegram` صدا زده می‌شود. |
| endpointهای وب | در `web/app.py` مثل `/api/telegram/connect` و `/api/gmail/connect`. |
| رابط کاربری | یک بخش در مودال تنظیمات `index.html` + توابع `connectGitHub`/`disconnectGitHub` در `app.js`. |
| وابستگی | پروژه از قبل `requests` دارد → برای API و تبادل OAuth کافی است. برای clone/push/pull از `git` واقعی (subprocess) استفاده می‌کنیم (مثل `inspect_git` در بستهٔ `agent/`). |

**هیچ کد git/github موجودی در `local_agent` نیست** — این کاملاً جدید است.

---

## ۲) فلوی احراز هویت (دقیقاً مثل چیزی که خواستی: ریدایرکت به صفحهٔ گیتهاب)

GitHub **OAuth Web Application Flow** — همان روشی که Cursor/Copilot استفاده می‌کنند:

```
کاربر کلید «اتصال به GitHub» را می‌زند
        │
        ▼
عامل: state تصادفی می‌سازد + URL زیر را برمی‌گرداند:
  https://github.com/login/oauth/authorize
      ?client_id=...
      &redirect_uri=http://localhost:<PORT>/api/github/callback
      &scope=repo workflow read:user
      &state=<random>
        │
        ▼
مرورگر به github.com می‌رود (ریدایرکت) → کاربر تأیید می‌کند
        │
        ▼
گیتهاب برمی‌گردد به:  http://localhost:<PORT>/api/github/callback?code=...&state=...
        │
        ▼
عامل: state را چک می‌کند → code را با POST به
  https://github.com/login/oauth/access_token (با client_secret)
  به access_token تبدیل می‌کند
        │
        ▼
token در فایل جدا ذخیره می‌شود (مثل gmail_token.json) + پروفایل کاربر گرفته می‌شود
        │
        ▼
صفحهٔ موفقیت → «به‌عنوان @user وصل شدی» → برگرد به اپ
```

GitHub اجازه می‌دهد `redirect_uri` روی `http://localhost` باشد (نیازی به HTTPS نیست) — درست مثل `flow.run_local_server` که gmail الان استفاده می‌کند.

### دو حالت احراز هویت (پیشنهاد من)
1. **OAuth ریدایرکت (اصلی — همان چیزی که خواستی)**: کاربر یک OAuth App خودش می‌سازد و `client_id`/`client_secret` را در تنظیمات می‌گذارد. سپس دکمهٔ اتصال، ریدایرکت می‌شود.
2. **PAT (توکن دسترسی شخصی) — سریع‌ترین**: کاربر یک توکن fine-grained می‌سازد و مستقیم Paste می‌کند. برای کسانی که نمی‌خواهند OAuth App بسازند.

> هر دو **واقعی** هستند. توکن/PAT هرگز در خروجی چاپ نمی‌شود و در فایل جدا (نه متن config) ذخیره می‌شود.

---

## ۳) فایل‌هایی که اضافه/تغییر می‌یابند

| فایل | کار |
|---|---|
| `local_agent/github/__init__.py`, `client.py` | کلاینت واقعی GitHub: احراز هویت (OAuth + PAT)، فراخوانی REST API، اجرای git واقعی. |
| `local_agent/actions/github_actions.py` | اکشن‌های `github.*` برای LLM. |
| `local_agent/core/config.py` | افزودن `GitHubSettings`. |
| `local_agent/bridge/api/handlers.py` | ثبت کلاینت + اکشن‌ها؛ متدهای connect/callback/disconnect/status. |
| `local_agent/web/app.py` | endpointهای `/api/github/*`. |
| `local_agent/web/templates/index.html` | بخش GitHub در مودال تنظیمات. |
| `local_agent/web/static/app.js` | `connectGitHub`/`disconnectGitHub` + نمایش وضعیت. |
| `pyproject.toml` | بدون وابستگی جدید (requests هست؛ git از سیستم). |
| تست‌ها | `tests_local_agent/test_github.py` (با بک‌اند fake فقط برای تست واحد — کد تولید واقعی است). |

---

## ۴) فهرست کامل قابلیت‌ها (اکشن‌های `github.*`)

| اکشن | کار | Risk |
|---|---|---|
| `github.whoami` | کاربر/سازمان متصل | SAFE |
| `github.list_repos` | مخازن کاربر | SAFE |
| `github.get_repo` | جزئیات یک مخزن (همکاران، branches، آخرین release) | SAFE |
| `github.create_repo` | ساخت مخزن روی گیتهاب (API) + اختیاری: init محلی و set remote | DESTRUCTIVE |
| `github.clone` | clone واقعی با git به داخل work_dir | SAFE |
| `github.init` | `git init` + تنظیم remote (با توکن، نه در فایل) | DESTRUCTIVE |
| `github.status` | `git status` خواندنی | SAFE |
| `github.diff` | `git diff` خلاصه | SAFE |
| `github.add_commit` | stage + commit با پیام | DESTRUCTIVE |
| `github.push` | push واقعی به remote | DESTRUCTIVE |
| `github.pull` | pull/rebase | DESTRUCTIVE |
| `github.branch` | ساخت/تعویض/لیست شاخه‌ها | DESTRUCTIVE (ساخت) / SAFE (لیست) |
| `github.merge` | ادغام یک شاخه | DESTRUCTIVE |
| `github.create_pr` | باز کردن Pull Request (API) | DESTRUCTIVE |
| `github.list_prs` / `github.merge_pr` | مدیریت PR (API) | SAFE / DESTRUCTIVE |
| `github.create_issue` / `github.list_issues` | issue (API) | DESTRUCTIVE / SAFE |
| `github.create_release` | ساخت release (API) | DESTRUCTIVE |
| `github.fetch_url` | هر URL گیتهاب را به دادهٔ ساخت‌یافته تبدیل کن (ضد prompt-injection) | SAFE |
| `github.run_action` | راه‌اندازی/مشاهدهٔ GitHub Actions | SAFE |

---

## ۵) ملاحظات امنیتی (مهم)

1. **توکن هرگز در فایل‌های repo یا git config نوشته نمی‌شود.** برای push/pull از متغیر محیطی یا credential helper موقت استفاده می‌کنیم تا توکن در `.git/config` یا log نشت نکند.
2. **`state` تصادفی CSRF**: هر اتصال یک `state` یکبار مصرف می‌سازد و در callback چک می‌شود.
3. **پنهان‌سازی خودکار**: `github.token` و `github.client_secret` به‌خاطر پسوندشان در همهٔ پاسخ‌ها ماسک می‌شوند (مثل `telegram.api_hash`).
4. **force-push به‌صورت پیش‌فرض مسدود** است (مثل hard-blockهای بستهٔ `agent/`)؛ فقط با تأیید صریح.
5. ** gating**: push/merge/create_repo با `confirm_mode` و دروازهٔ تأیید موجود رفتار می‌کنند.
6. **توکن در فایل جدا**: `data_dir/github_token.json` (مثل gmail_token.json)، نه در متن config.

---

## ۶) پیشنهادهای خلاقانه برای «بی‌رقیب‌تر شدن»

1. **`github.fetch_url` هوشمند**: هر لینک گیتهاب که LLM می‌بیند (issue، PR، فایل، release) را به‌صورت ساخت‌یافته می‌خواند تا مدل کد/دستور وب را کورکورانه اجرا نکند.
2. **پراکسای توکن امن**: یک credential helper درون‌حافظه‌ای که توکن را فقط برای مدت کوتاه git در دسترس می‌گذارد و هرگز روی دیسک می‌نویسد.
3. **اتصال چنداکانت** (مثل تلگرام): چند توکن/اکانت با تعویض سریع.
4. **نمودار زندهٔ وضعیت repo** در رابط وب (branches/recent PRs).
5. **Device Flow به‌عنوان گزینهٔ سوم**: برای دسترسی از ماشین دیگر (بدون localhost callback) — فقط با client_id و بدون secret.
6. **ادغام با حلقهٔ عامل موجود**: بعد از هر push/commit، `git status` واقعی برای گزارش صادقانه (الگوی `inspect_git`).

---

## ۷) محدودیت‌های صادقانه

1. **client_secret در اپ محلی**: تبادل OAuth به `client_secret` نیاز دارد. چون عامل روی ماشین خود کاربر اجرا می‌شود، رازِ OAuth App خودِ کاربر روی همان ماشین می‌ماند (امن برای اپ محلی؛ دقیقاً مثل gmail که کاربر credentials.json خودش را می‌گذارد). این ذاتِ اپ‌های محلی است.
2. **callback روی localhost**: فقط وقتی مرورگر روی همان ماشین عامل باشد کار می‌کند. برای دسترسی از ماشین دیگر، PAT یا Device Flow پیشنهاد می‌دهم.
3. **نیاز به git نصب‌شده**: clone/push/pull به `git` روی سیستم وابسته‌اند؛ اگر نباشد، پیام فارسی روشن می‌دهیم (API همچنان کار می‌کند).
4. **اعتبارسنجی زنده**: تست‌های واقعی به OAuth App/توکن واقعی و دسترسی شبکه نیاز دارند که در سندباکس ندارم. کد ۱۰۰٪ واقعی است و با fake فقط در سطح واحد تست می‌شود.

---

## ۸) سؤالاتی که برای نهایی‌کردن نیاز دارم

1. **حالت احراز هویت اصلی** کدام باشد؟ (الف) OAuth ریدایرکت با OAuth App کاربر — همان‌طور که خواستی، یا (ب) PAT ساده، یا (ج) هر دو؟
2. آیا **Device Flow** را هم به‌عنوان گزینهٔ سوم اضافه کنم (برای دسترسی از ماشین دیگر)؟
3. **scope پیش‌فرض** چه باشد؟ `repo,workflow` (دسترسی کامل) یا فقط fine-grained محدود؟
4. آیا با **چنداکانت** موافقی، یا فعلاً یک اکانت کافی است؟
5. آیا اکشن‌های **PR/Issue/Release/Actions** (قسمت ۴) را همه می‌خواهی، یا اول زیرمجموعهٔ core (clone/commit/push/pull/branch/merge/create_repo) را بیاورم؟

بعد از پاسخ‌هایت، نسخهٔ نهایی را کدنویسی و پوش می‌کنم.
