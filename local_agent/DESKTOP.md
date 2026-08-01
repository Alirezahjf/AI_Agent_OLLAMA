# اپ دسکتاپ

یک برنامهٔ **بومی** — نه ترمینال، نه تب مرورگر. پنجرهٔ خودش،
آیکون کنار ساعت، کلید میان‌بر سراسری، و نوتیفیکیشن.

**ویندوز** (پنجرهٔ pywebview، tray، کلید سراسری، اجرای خودکار):

```powershell
python local_agent_setup.py desktop
```

**لینوکس** (پنجرهٔ pywebview یا مرورگر، tray اختیاری):

```bash
python local_agent_setup.py desktop
# یا اگر بدون pywebview:
python local_agent_setup.py desktop --browser
```

**سرور لینوکس بدون نمایشگر** (رابط وب در شبکه):

```bash
python local_agent_setup.py web --host 0.0.0.0 --port 7824
```

> ⚠️ هشدار امنیتی: اگر آدرس غیرمحلی (مثل `0.0.0.0`) انتخاب کنید،
> یک توکن احراز هویت لازم است. توکن در `<DATA_DIR>/bridge.token`
> ذخیره می‌شود.

![پنجرهٔ اپ دسکتاپ](../docs/images/desktop-window.png)

---

## فهرست

- [نصب](#نصب)
- [اجرا](#اجرا)
- [ویژگی‌ها](#ویژگیها)
- [معماری](#معماری)
- [تنظیمات](#تنظیمات)
- [ساخت فایل exe](#ساخت-فایل-exe)
- [ساخت اینستالر](#ساخت-اینستالر)
- [عیب‌یابی](#عیبیابی)
- [تست](#تست)

---

## نصب

```powershell
python local_agent_setup.py install-all
```

یا فقط وابستگی‌های دسکتاپ:

```powershell
pip install pywebview pystray
```

### پیش‌نیازها

| مورد | لازم؟ | توضیح |
|---|---|---|
| Windows 10/11 | ✅ | نسخهٔ ۱۸۰۹ به بالا |
| Python 3.11+ | ✅ | برای اجرا از سورس (نه برای `.exe`) |
| Edge WebView2 | ✅ | روی ویندوز ۱۱ و ویندوز ۱۰ به‌روز از پیش نصب است |
| `pywebview` | ✅ | پنجرهٔ بومی |
| `pystray` | ⬜ | آیکون tray — بدونش هم اجرا می‌شود |

بررسی نصب:

```powershell
python local_agent_setup.py doctor
```

---

## اجرا

```powershell
python local_agent_setup.py desktop              # پنجرهٔ عادی
python local_agent_setup.py desktop --hidden     # مینیمایز در tray
python local_agent_setup.py desktop --browser    # در مرورگر (بدون pywebview)
python local_agent_setup.py desktop --debug      # با devtools
```

معادل‌ها:

```powershell
persian-local-desktop          # پس از pip install -e .
python -m local_agent.desktop
```

گزینه‌های کامل `persian-local-desktop`:

| گزینه | کار |
|---|---|
| `--port N` | پورت سرور داخلی (پیش‌فرض ۷۸۲۴، اگر اشغال بود آزاد بعدی) |
| `--hotkey SPEC` | تغییر کلید میان‌بر، مثلاً `ctrl+shift+space` |
| `--hidden` | شروع در tray |
| `--no-tray` | بدون آیکون tray |
| `--no-updates` | بدون بررسی به‌روزرسانی |
| `--browser` | مرورگر به‌جای پنجرهٔ بومی |
| `--debug` | باز کردن devtools |

---

## ویژگی‌ها

### پنجره

- ۱۲۰۰×۸۰۰ پیش‌فرض، قابل تغییر اندازه، حداقل ۸۰۰×۶۰۰
- عنوان پنجره مسیر پوشهٔ کاری را نشان می‌دهد
- پس‌زمینهٔ تیره از همان لحظهٔ باز شدن (بدون فلاش سفید)
- محتوا دقیقاً همان [رابط وب](WEB_UI.md) است — یک front-end، دو پوسته

### آیکون tray

راست‌کلیک روی آیکون کنار ساعت:

```
نمایش پنجره        ← پیش‌فرض (کلیک چپ هم همین)
پنهان کردن
──────────────
باز کردن پوشهٔ کاری  ← Explorer
تنظیمات             ← پنجره را می‌آورد و مودال را باز می‌کند
بررسی به‌روزرسانی
درباره
──────────────
خروج
```

<img src="../docs/images/app-icon.png" alt="آیکون برنامه" width="72">

آیکون در زمان اجرا با Pillow کشیده می‌شود — هیچ فایل باینری در مخزن
نیست و همیشه با برند رابط وب هماهنگ است.

### کلید میان‌بر سراسری

<kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>A</kbd> از **هر جای ویندوز** پنجره
را می‌آورد یا پنهان می‌کند.

با `RegisterHotKey` از `user32.dll` و فقط با `ctypes` پیاده شده — بدون
وابستگی جدید و بدون keylogger.

پشتیبانی: `ctrl` `alt` `shift` `win` + حروف، ارقام، `f1`–`f24`،
`space`، `enter`، `tab`، `esc`، کلیدهای جهت و…

```powershell
$env:LOCAL_AGENT_HOTKEY = "ctrl+shift+space"
```

اگر برنامهٔ دیگری کلید را گرفته باشد، اپ بدون کرش بالا می‌آید و در لاگ
دلیل را می‌نویسد.

### نوتیفیکیشن

Toast ویندوز وقتی:
- دستیار **تأیید** می‌خواهد
- کار طولانی **تمام** می‌شود
- **خطا** رخ می‌دهد
- به‌روزرسانی موجود است

ترتیب fallback: `win10toast` → بالن tray → لاگ. هیچ‌وقت کرش نمی‌کند.

### مینیمایز به tray

دکمهٔ **X** برنامه را نمی‌بندد، در tray پنهان می‌کند. خروج واقعی از
منوی tray است. برای غیرفعال کردن:

```powershell
$env:LOCAL_AGENT_MINIMIZE_TO_TRAY = "false"
```

### تک‌نمونه

اجرای دوم برنامه، پنجرهٔ نمونهٔ اول را جلو می‌آورد و بی‌صدا خارج
می‌شود. قفل با bind روی `127.0.0.1:7825` انجام می‌شود — اتمیک، و
هم‌زمان کانال IPC هم هست.

روی ویندوز از `SO_EXCLUSIVEADDRUSE` استفاده می‌شود تا پورت قابل دزدیدن
نباشد؛ روی سایر سیستم‌ها `SO_REUSEADDR` برای ری‌استارت سریع.

### اجرای خودکار با ویندوز

از مودال تنظیمات قابل روشن/خاموش کردن است. یک مقدار در
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` می‌نویسد —
بدون نیاز به دسترسی ادمین و فقط برای همان کاربر.

در نسخهٔ سورس از `pythonw.exe` استفاده می‌شود تا پنجرهٔ کنسول باز نشود.

**لینوکس:** فایل `~/.config/autostart/persian-local-assistant.desktop` ساخته
می‌شود. محیط‌های دسکتاپ (GNOME, KDE, ...) این فایل را به‌طور خودکار
می‌خوانند.

### به‌روزرسانی

آخرین release مخزن GitHub بررسی می‌شود، با فاصلهٔ حداقل ۲۴ ساعت.
مقایسهٔ نسخه‌ها درست انجام می‌شود: `1.10.0` از `1.9.0` جدیدتر است و
`1.0.0` از `1.0.0-rc1` بالاتر. نبود اینترنت یعنی «به‌روزرسانی نیست»،
نه پیام خطا.

### سایر

- **دیالوگ بومی فایل** برای انتخاب فایل و پوشه
- **درگ‌اند‌دراپ** از Explorer روی پنجره
- **نوار پیشرفت taskbar** هنگام کار طولانی (با `comtypes`)
- **پل JavaScript**: `window.pywebview.api` شامل `show` `hide`
  `minimize` `quit` `notify` `set_progress` `open_workspace`
  `pick_file` `pick_folder` `get_autostart` `set_autostart`
  `get_info` `check_updates`

---

## معماری

```
┌─ پنجرهٔ pywebview (Edge WebView2) ──────────────┐
│   http://127.0.0.1:7824  ← همان رابط وب        │
└────────────────────────────────────────────────┘
                 │  پل JS: window.pywebview.api
┌────────────────▼───────────────────────────────┐
│  DesktopApp                                    │
│  tray · hotkey · نوتیفیکیشن · تک‌نمونه ·        │
│  اجرای خودکار · به‌روزرسانی                     │
└────────────────┬───────────────────────────────┘
                 │  in-process
┌────────────────▼───────────────────────────────┐
│  WebServer (FastAPI) → BridgeClient → Bridge   │
└────────────────────────────────────────────────┘
```

نکتهٔ کلیدی: **هیچ front-end دومی وجود ندارد.** همان HTML/CSS/JS که
مرورگر می‌گیرد، داخل پنجره هم لود می‌شود.

### فایل‌ها

| فایل | نقش |
|---|---|
| `app.py` | `DesktopApp` و `DesktopApi` — قلب برنامه |
| `tray.py` | آیکون tray و منو (pystray + Pillow) |
| `hotkey.py` | کلید سراسری با `RegisterHotKey` |
| `single_instance.py` | قفل TCP + کانال فعال‌سازی |
| `autostart.py` | کلید Run در رجیستری |
| `updater.py` | بررسی release در GitHub |
| `build.py` | تولید spec و اجرای PyInstaller |
| `installer.iss` | اسکریپت Inno Setup |

### تنزل تدریجی

هیچ ویژگی بومی‌ای اجباری نیست:

| نبود | نتیجه |
|---|---|
| `pywebview` | اجرا در مرورگر سیستم |
| `pystray` | بدون tray، بقیه سر جایش |
| غیر ویندوز | tray و هات‌کی غیرفعال، بقیه کار می‌کند |
| بدون نمایشگر (سرور) | خودکار به حالت سرور وب می‌رود |
| `wmctrl`/`xdotool` | ابزارهای پنجره روی لینوکس غیرفعال |
| `xclip`/`xsel` | کلیپ‌بورد روی لینوکس غیرفعال |
| بدون اینترنت | بررسی به‌روزرسانی بی‌صدا رد می‌شود |

به همین دلیل تست‌های دسکتاپ روی لینوکس هم سبز هستند.

---

## تنظیمات

| متغیر محیطی | پیش‌فرض | کار |
|---|---|---|
| `LOCAL_AGENT_WEB_PORT` | `7824` | پورت رابط |
| `LOCAL_AGENT_WEB_HOST` | `127.0.0.1` | هاست |
| `LOCAL_AGENT_LOCK_PORT` | `7825` | پورت قفل تک‌نمونه |
| `LOCAL_AGENT_HOTKEY` | `ctrl+alt+a` | کلید میان‌بر |
| `LOCAL_AGENT_WINDOW_WIDTH` | `1200` | عرض پنجره |
| `LOCAL_AGENT_WINDOW_HEIGHT` | `800` | ارتفاع پنجره |
| `LOCAL_AGENT_MINIMIZE_TO_TRAY` | `true` | رفتار دکمهٔ X |
| `LOCAL_AGENT_START_HIDDEN` | `false` | شروع در tray |
| `LOCAL_AGENT_CHECK_UPDATES` | `true` | بررسی به‌روزرسانی |
| `LOCAL_AGENT_DESKTOP_DEBUG` | `false` | devtools |

بقیهٔ تنظیمات (مدل، پوشهٔ کاری، حالت تأیید) از همان
`%USERPROFILE%\.local_assistant\config.json` خوانده می‌شود.

---

## ساخت فایل exe

```powershell
pip install pyinstaller
python local_agent_setup.py build-desktop
```

خروجی: `dist\PersianLocalAssistant.exe` — یک فایل، بدون نیاز به
Python روی سیستم مقصد.

| گزینه | کار |
|---|---|
| `--onedir` | پوشه به‌جای تک‌فایل (شروع سریع‌تر) |
| `--console` | نگه‌داشتن کنسول برای دیباگ |
| `--spec-only` | فقط تولید spec |
| `--installer` | اجرای Inno Setup بعد از بیلد |

اسکریپت خودش آیکون `.ico` را می‌سازد و قالب‌ها، CSS، JS، کتابخانه‌های
vendor و فونت فارسی را داخل باینری بسته‌بندی می‌کند.

> بیلد باید **روی ویندوز** انجام شود. PyInstaller کراس‌کامپایل نمی‌کند.

---

## ساخت اینستالر

[Inno Setup 6](https://jrsoftware.org/isdl.php) را نصب کنید، سپس:

```powershell
python local_agent_setup.py build-desktop --installer
```

خروجی:
`dist\installer\PersianLocalAssistant-Setup-2.0.0.exe`

اینستالر:
- **بدون نیاز به ادمین** نصب می‌کند (per-user)
- شورتکات دسکتاپ، منوی استارت و اجرای خودکار (اختیاری)
- وجود WebView2 را بررسی می‌کند و در صورت نبود **هشدار** می‌دهد (نه توقف)
- هنگام حذف، کلید Run و فایل‌ها را پاک می‌کند

---

## عیب‌یابی

**پنجره باز نمی‌شود / صفحه سفید است**
WebView2 نصب نیست. از
[اینجا](https://developer.microsoft.com/microsoft-edge/webview2/)
نصب کنید. یا موقتاً: `python local_agent_setup.py desktop --browser`

**آیکون tray نیست**
`pip install pystray pillow`. اگر باز هم نبود، لاگ
`%USERPROFILE%\.local_assistant\logs\` را ببینید.

**کلید میان‌بر کار نمی‌کند**
برنامهٔ دیگری آن را گرفته است. کلید دیگری امتحان کنید:

```powershell
python local_agent_setup.py desktop --hotkey "ctrl+shift+f12"
```

**می‌گوید از قبل در حال اجراست**
یک نمونهٔ دیگر باز است (احتمالاً در tray). اگر مطمئنید نیست، پروسهٔ
`PersianLocalAssistant.exe` را در Task Manager ببندید یا
`%USERPROFILE%\.local_assistant\desktop.pid` را پاک کنید.

**پورت ۷۸۲۴ اشغال است**
اپ خودکار پورت آزاد بعدی را می‌گیرد. برای تعیین دستی: `--port 7830`

**آنتی‌ویروس به exe گیر می‌دهد**
هشدار کاذب رایج PyInstaller است. یا استثنا اضافه کنید، یا از سورس
اجرا کنید، یا `.exe` را امضای دیجیتال کنید.

**اجرای خودکار کار نمی‌کند**
مقدار رجیستری را بررسی کنید:

```powershell
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v PersianLocalAssistant
```

**لینوکس: pywebview باز نمی‌شود**
وابستگی‌های GTK را نصب کنید:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-webkit2-4.1
pip install pywebview[gtk]
```

**لینوکس: کلیپ‌بورد کار نمی‌کند**
`xclip` یا `xsel` را نصب کنید:

```bash
sudo apt install xclip
```

**لینوکس: ابزارهای پنجره کار نمی‌کنند**
`wmctrl` یا `xdotool` را نصب کنید:

```bash
sudo apt install wmctrl xdotool
```

**سرور: اتصال از راه دور رد می‌شود**
توکن احراز هویت لازم است. توکن در `<DATA_DIR>/bridge.token` است.
یا متغیر `LOCAL_AGENT_BRIDGE_TOKEN` را تنظیم کنید.

---

## تست

```powershell
python -m pytest tests_local_agent/test_desktop.py -v
```

۶۸ تست، همه مستقل از سیستم‌عامل:

- ساختار ماژول‌ها و فایل‌های لازم
- ابعاد پنجره و override با متغیر محیطی
- انتخاب پورت آزاد وقتی پورت پیش‌فرض اشغال است
- تجزیه و مقایسهٔ نسخه (pre-release، ترتیب عددی، ورودی خراب)
- رفتار آپدیتر: نسخهٔ جدید، به‌روز بودن، قطعی شبکه، cooldown
- تجزیهٔ همهٔ انواع کلید میان‌بر و رد کردن ورودی نامعتبر
- قفل تک‌نمونه: خطای نمونهٔ دوم، قفل مجدد پس از آزادسازی، سیگنال
  فعال‌سازی، ثبت PID
- کشیدن و ذخیرهٔ آیکون tray
- بوت واقعی بک‌اند و سرو رابط روی HTTP
- تولید spec و صحت `installer.iss`
