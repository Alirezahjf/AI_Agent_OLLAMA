# یکپارچه‌سازی امن GitHub

این سند پیکربندی و مرزهای یکپارچه‌سازی GitHub در رابط مشترک Web/Desktop را توضیح می‌دهد. این قابلیت برای ورود کاربر از **GitHub App + Authorization Code + PKCE S256** استفاده می‌کند. Client Secret هرگز وارد برنامهٔ محلی نمی‌شود؛ یک کارگزار OAuth کوچک و جداگانه exchange/refresh/revoke را انجام می‌دهد.

## معماری و پیش‌نیازها

1. یک **GitHub App** بسازید و «Expire user authorization tokens» را فعال نگه دارید.
2. Client ID عمومی App را در تنظیمات برنامه وارد کنید.
3. Callback URL برنامه را در GitHub App ثبت کنید. مقدار پیش‌فرض برنامه:
   `https://APP-ORIGIN/api/github/oauth/callback`
   است. برای اجرای محلی معمولاً `http://127.0.0.1:PORT/api/github/oauth/callback` است.
4. کارگزار OAuth را پشت HTTPS اجرا کنید و Client Secret را فقط در secret manager محیط استقرار آن بگذارید.
5. افزونه را نصب کنید:

```bash
pip install -e ".[github,web]"
```

برنامه توکن‌های کاربر را فقط در vault سیستم‌عامل نگه می‌دارد:

- Windows Credential Manager
- macOS Keychain
- Secret Service/keyring امن در Linux

اگر backend امن keyring موجود نباشد، اتصال fail-closed است و هیچ fallback متنی ساخته نمی‌شود.

## اجرای کارگزار OAuth

متغیرهای محیط کارگزار:

```dotenv
GITHUB_CLIENT_ID=Iv1.example
GITHUB_CLIENT_SECRET=secret-from-github-app
GITHUB_CALLBACK_URLS=https://assistant.example/api/github/oauth/callback,http://127.0.0.1:8765/api/github/oauth/callback
# فقط برای GitHub Enterprise Server این دو مقدار را از حالت comment خارج کنید:
# GITHUB_WEB_URL=https://github.example.com
# GITHUB_API_URL=https://github.example.com/api/v3
HOST=0.0.0.0
PORT=8080
```

سپس:

```bash
persian-github-oauth-broker
# یا
python -m local_agent.github.broker
```

کارگزار باید پشت reverse proxy معتبر HTTPS باشد. مسیرهای آن `/exchange`، `/refresh`، `/revoke` و `/health` هستند. پاسخ‌های حساس `no-store`، callbackها exact allow-list و درخواست‌ها rate-limited هستند. کارگزار کد یا توکن را ذخیره نمی‌کند. `GITHUB_CLIENT_SECRET` را هرگز در config برنامه، رابط کاربری یا فایل `.env` دستگاه کاربر قرار ندهید.

## تنظیم برنامه

در Web یا Desktop به «تنظیمات → اتصال‌ها → GitHub» بروید و این موارد را وارد کنید:

- **GitHub App Client ID**
- **Broker URL**، مانند `https://oauth.example.com`
- Callback URL در صورت نیاز؛ خالی یعنی محاسبه از Origin برنامه
- ریشهٔ clone محلی اختیاری
- برای GitHub Enterprise: REST API URL، Web URL و GraphQL URL
- Originهای اضافه فقط در استقرارهای چند-Origin

سپس «اتصال به GitHub» را بزنید. در مرورگر، برنامه یک popup متعارف باز می‌کند و اگر popup مسدود باشد همان صفحه را هدایت می‌کند. Desktop عمداً از redirect همان webview استفاده می‌کند تا cookie نشست loopback در یک مرورگر خارجی گم نشود. بعد از callback کاربر به برنامه برمی‌گردد. `state` یک‌بارمصرف ده‌دقیقه‌ای به نشست امضاشدهٔ مرورگر متصل است و code verifier فقط در RAM می‌ماند. Host/port مربوط به callback محلی Desktop باید ثابت، در GitHub App و allow-list کارگزار ثبت‌شده، و با Origin صفحه یکسان باشد.

بعد از اتصال، نصب‌های قابل‌مشاهده و مخزن‌های در دسترس نمایش داده می‌شوند. حداقل یک مخزن را انتخاب و تنظیمات را ذخیره کنید. همهٔ عملیات مخزن—remote یا clone محلی—تا آن زمان رد می‌شوند و بعد از آن نیز فقط `owner/repo`های انتخاب‌شده مجازند.

همین تنظیم‌ها را می‌توان با override محیطی عمومی برنامه نیز داد؛ مسیر با `__` جدا می‌شود. فهرست‌ها باید JSON معتبر باشند:

```dotenv
LOCAL_AGENT_GITHUB__ENABLED=true
LOCAL_AGENT_GITHUB__CLIENT_ID=Iv1.example
LOCAL_AGENT_GITHUB__BROKER_URL=https://oauth.example.com
LOCAL_AGENT_GITHUB__CALLBACK_URL=https://assistant.example/api/github/oauth/callback
LOCAL_AGENT_GITHUB__SELECTED_REPOSITORIES='["owner/repo"]'
LOCAL_AGENT_GITHUB__ALLOWED_ORIGINS='["https://assistant.example"]'
```

## مجوزهای GitHub App

فقط مجوزهای لازم برای قابلیت‌هایی که استفاده می‌کنید فعال کنید. GitHub دسترسی واقعی را از اشتراک مجوز App، مخزن‌های نصب‌شده و دسترسی خود کاربر محاسبه می‌کند. endpointها خطای permission-specific فارسی برمی‌گردانند.

| حوزه | دسترسی پیشنهادی |
|---|---|
| حساب، نصب‌ها، فهرست مخزن | Metadata: read |
| کد، commit، branch، tag، release | Contents: read/write |
| ویرایش workflow file | Contents: write و Workflows: write |
| Issue و comment | Issues: read/write |
| Pull Request، review و merge | Pull requests: read/write؛ merge ممکن است Contents: write هم بخواهد |
| Workflow، run، cache، artifact | Actions: read/write |
| Actions Secret در مخزن | Secrets: read/write |
| Actions Secret در سازمان | Organization secrets: read/write |
| Actions Variable | Variables یا Organization variables: read/write |
| Secret/Variable در Environment | Environments: read/write |
| Branch protection، ruleset، collaborator و runner مخزن | Administration: read/write |
| Discussion | Discussions: read/write |
| Check run/suite | Checks: read/write؛ معمولاً ساخت Check Run فقط برای GitHub App مجاز است |
| Webhook مخزن | Webhooks: read/write |
| Codespaces و secretهای آن | Codespaces: read/write؛ نوع توکن و policy سازمان باید endpoint را مجاز کند |
| Package و نسخه‌ها | Packages: read/write |
| Dependabot، Code scanning و Secret scanning alert | مجوز read/write متناظر هر قابلیت امنیتی |
| Deployment و environment | Deployments و Environments: read/write |
| سازمان و اعضا | Organization members: read/write |
| notification | Notifications: read/write |
| Projects v2 | Projects: read/write و GraphQL پشتیبانی‌شدهٔ GitHub |

GitHub ممکن است برای endpoint یا سیاست سازمان شما مجوز بیشتری بخواهد. پیام API شامل نام مجوز مورد انتظار، status و GitHub request ID است، اما توکن را نمایش نمی‌دهد.

## قابلیت‌های پیاده‌سازی‌شده

رابط backend و ابزارهای تایپ‌شدهٔ ایجنت فقط عملیات allow-listed زیر را می‌پذیرند؛ URL، متد REST، shell command یا GraphQL دلخواه از caller قبول نمی‌شود:

- حساب، نصب‌ها، مخزن‌های نصب و مخزن‌های کاربر
- مشخصات مخزن، محتوا، commit، branch، tag، compare، contributor و زبان‌ها
- ساخت/ویرایش/حذف/انتقال/fork مخزن، topic، branch و فایل؛ branch protection، effective branch rules و ruleset/history
- Issue، comment، lock/unlock و Pull Request، فایل‌ها، review و merge؛ چون endpoint فهرست Issue در GitHub ذاتاً PRها را هم برمی‌گرداند، پاسخ `issues` آن‌ها را حذف و تعداد حذف‌شده را در `excluded_pull_requests` شفاف گزارش می‌کند
- Discussions و commentهای آن، close/reopen و حذف‌های allow-listed
- Check Run/Suite، annotationها، ساخت/ویرایش و rerequest
- Workflow و run، enable/disable، dispatch/rerun/cancel/delete، log، artifact، cache و runner/labelهای مخزن
- Actions Secrets و Variables در سطح repository، organization و environment؛ Secret حساب Codespaces نیز پشتیبانی می‌شود. **مقدار Secret فقط در endpoint مستقیم UI پذیرفته می‌شود و در schema ابزار LLM یا کنسول عمومی وجود ندارد**
- ساخت/ویرایش/حذف Release، دانلود و ویرایش/حذف asset و آپلود فایل خام asset تا سقف ۲۵۶ مگابایت
- deployment/status و environment، collaborator و عضویت سازمان
- Webhook مخزن شامل فهرست، delivery، create/update/delete/ping/redeliver؛ Secret آن فقط از فرم مستقیم محافظت‌شده می‌گذرد
- Codespace شامل list/create/update/start/stop/delete، machine و metadata؛ Package و delete/restore نسخه
- Dependabot/Code-scanning/Secret-scanning alertها و تغییر وضعیت آن‌ها، و فهرست Repository Security Advisoryها
- سازمان‌ها، مخزن‌ها/اعضا/runnerها/Webhookهای سازمان به‌صورت خواندنی
- notification، thread، mark-read، subscribe/ignore/unsubscribe و search محدود GitHub
- Projects v2: فهرست/جزئیات و create/update/delete پروژه، افزودن item یا draft issue، update draft، archive/unarchive/delete item، set/clear field value و تغییر موقعیت item
- clone/status/branch/log/remote/diff/pull/push/commit/tag محلی در ریشهٔ محدودشده

در رابط مشترک Web/Desktop کنترل مستقیم برای بررسی جمعی مخزن‌ها و Projects v2، بررسی جزئی هر مخزن/Project، مرور درخت و فایل متنی، زبان‌ها و Workflowها، ساخت مخزن و Project، clone/status/branch/log/diff/pull/push/commit، ساخت Pull Request و Issue، ساخت/ویرایش/حذف فایل، dispatch کردن Workflow، ساخت Release و آپلود asset وجود دارد. فرم Secret/Variable چهار scope و فرم Webhook مسیر مستقیم و CSRF-protected دارند. «کنسول پیشرفته» فهرست صریح عملیات read/write را ارائه می‌کند، مخزن مجاز را تزریق می‌کند، JSON نتیجه را زنده نمایش می‌دهد و برای هر mutation تأیید مستقل می‌گیرد؛ این کنسول URL/متد/GraphQL دلخواه یا عملیات دارای plaintext Secret را نمی‌پذیرد. همین frontend بدون fork در Web و Desktop استفاده می‌شود.

صفحه‌بندی REST حداکثر ۲۰۰۰ مورد در هر درخواست و هر صفحه حداکثر ۱۰۰ مورد دارد. rate-limit آخرین پاسخ در status گزارش می‌شود. همهٔ بدنه‌های JSON عادی، چه از Web و چه مستقیماً از ابزار ایجنت، پیش از دریافت credential و ارسال شبکه از نظر JSON معتبر و سقف ۲ مگابایت بررسی می‌شوند. دانلود log/artifact/asset با `no-store`، filename پاک‌سازی‌شده و سقف ۲۵۶ مگابایت تحویل می‌شود؛ برای فایل بزرگ‌تر باید مستقیماً از رابط GitHub استفاده شود.

## سیاست ایمنی

- همهٔ readها `SAFE` هستند.
- همهٔ mutationهای remote و local Git در ابزارهای ایجنت `DESTRUCTIVE` و `force_human_confirmation` هستند.
- scheduler، `confirm_mode=never`، auto-approve عمومی و `auto_confirm` caller نمی‌توانند تأیید زنده را دور بزنند.
- API نوشتن Web نیز `confirm: true`، نشست HttpOnly امضاشده، Origin exact و CSRF معتبر می‌خواهد. آپلود asset و endpoint حساس علاوه بر این‌ها header تأیید جداگانه، Content-Length/stream محدودشده و allow-list مستقل دارند؛ عملیات حساس از API عمومی write رد می‌شوند.
- در bind غیر-loopback، **کل** Web UI/API/WebSocket فقط پشت HTTPS و پس از ورود با توکن Bridge کار می‌کند؛ HTTP رد می‌شود. توکن در فرم ورود با header ارسال و با cookie امن HttpOnly دوازده‌ساعته جایگزین می‌شود، نه query URL یا browser storage. reverse proxy باید در allow-list مورد اعتماد Uvicorn باشد و Host اصلی را حفظ کند.
- توکن در پاسخ frontend، prompt، history، log، config یا remote URL قرار نمی‌گیرد. Git با askpass موقت و prompt غیرفعال اجرا می‌شود؛ فایل credential موقت با مجوز محدود سیستم‌عامل ساخته و فوراً پاک می‌شود، config سراسری/سیستمی و متغیرهای `GIT_*` ارث‌رسیده نادیده گرفته می‌شوند، redirect/proxy/helper/hook/submodule غیرفعال‌اند و config محلی خطرناک پیش از دریافت توکن رد می‌شود.
- «قطع اتصال» و «پاک‌سازی کامل» ابتدا revoke را از کارگزار درخواست و در همهٔ حالت‌ها credential محلی را حذف می‌کنند.

## محدودیت‌ها و وابستگی‌های خارجی

- ساخت GitHub App، انتخاب permissionها، نصب App روی حساب/سازمان و استقرار HTTPS کارگزار باید توسط مدیر انجام شود؛ برنامه نمی‌تواند Client Secret یا policy سازمان را ایجاد کند.
- برنامه Webhook مخزن را در GitHub مدیریت می‌کند، اما **listener عمومی دریافت event**، اعتبارسنجی امضای ورودی و صف تحویل را میزبانی نمی‌کند؛ برای آن یک سرویس HTTPS همیشه‌روشن جداگانه لازم است.
- GitHub Marketplace billing، خرید plan، مدیریت صورتحساب و provision کردن زیرساخت خارج از API عملی این برنامه است.
- runnerهای self-hosted مخزن را می‌توان فهرست/حذف و labelهایشان را set کرد، اما دانلود runner، ساخت registration token و اجرای runner روی میزبان انجام نمی‌شود. runnerهای سازمان فقط خواندنی‌اند.
- Projects classic منسوخ پوشش داده نمی‌شود. Projects v2 فقط mutationهای صریح فهرست‌شده را دارد؛ automation داخلی Project، iteration/field creation و template management پوشش داده نشده است.
- Security coverage روی alert list/update و advisory list متمرکز است؛ upload SARIF، analysis deletion، autofix، secret-scanning location و create/publish advisory در این نسخه وجود ندارد.
- Packages فقط list/version list/delete/restore دارد؛ publish پکیج با ابزار package manager و registry credential همان ecosystem انجام می‌شود. delete/restore علاوه بر token سازگار به دسترسی admin همان package و قواعد registry نیاز دارد؛ GitHub حذف نسخهٔ public با بیش از ۵۰۰۰ دانلود را بدون پشتیبانی خود GitHub مجاز نمی‌کند.
- Webhookهای سازمان، runnerهای سازمان و security advisoryها در حال حاضر فقط خواندنی‌اند. API دریافت registration credential، webhook secret یا plaintext Secret را به ابزار LLM نمی‌دهد.
- رفتار popup در مرورگر به policy مرورگر وابسته است؛ fallback همان صفحه وجود دارد. Desktop برای حفظ نشست، همیشه redirect همان webview را به‌کار می‌برد.
- Secret ثبت‌شده قابل‌خواندن نیست (محدودیت GitHub). فقط نام/metadata قابل فهرست است؛ مقدار از فرم set می‌شود و پس از ارسال پاک می‌گردد.
- بعضی endpointها بر اساس نوع توکن متفاوت‌اند: برای نمونه GitHub ممکن است Check Run را فقط برای GitHub App و برخی Codespaces/notification endpointها را فقط برای user access token با scope/permission مناسب بپذیرد. user token این برنامه همچنان به اشتراک permissionهای App، repository selection، حقوق کاربر، SSO و policy سازمان محدود است.
- قابلیت‌هایی مانند branch protection/rulesets، environment، private repository، code/secret scanning، Codespaces و برخی Projects بسته به plan، فعال‌بودن قابلیت در مخزن، مالکیت سازمانی و policy مدیر ممکن است با ۴۰۳/۴۰۴ پاسخ دهند؛ برنامه این محدودیت خارجی را شبیه‌سازی یا دور نمی‌زند.
- سقف آپلود و دانلود این برنامه ۲۵۶ مگابایت است، حتی اگر endpoint یا plan گیت‌هاب سقف دیگری داشته باشد. برای فایل بزرگ‌تر باید از GitHub CLI/UI یا storage مناسب استفاده شود.

## عیب‌یابی

- **vault unavailable:** افزونهٔ `github` و backend امن سیستم‌عامل را نصب/فعال کنید.
- **callback mismatch:** مقدار callback در GitHub App، `GITHUB_CALLBACK_URLS` کارگزار و تنظیم برنامه باید دقیقاً یکسان باشد.
- **403/404:** نصب App، مخزن انتخابی و permission endpoint را بررسی کنید؛ برخی سازمان‌ها approval مدیر می‌خواهند.
- **rate limit:** زمان reset در status را بررسی و تا آن زمان صبر کنید.
- **مخزن رد می‌شود:** مخزن را در کارت GitHub انتخاب و تنظیمات را ذخیره کنید؛ تطبیق نام case-insensitive است.
