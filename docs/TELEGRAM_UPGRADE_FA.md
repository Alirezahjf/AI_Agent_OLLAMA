# گزارش نهایی ارتقای تلگرام شخصی

این سند قرارداد نهایی بخش Telegram شخصی اپ را ثبت می‌کند. پیاده‌سازی روی `PersonalTelegram`، اکشن‌های `telegram.*`، Bridge، Web/Desktop و CLI انجام شده و معماری چنداکانتی قبلی حفظ شده است.

## اصل دادهٔ زنده

- `list_chats` در هر فراخوانی dialogها را از Telegram پیمایش می‌کند.
- فیلتر نوع، جست‌وجو، archive و unread پیش از limit/offset اعمال می‌شوند.
- `list_contacts` و `search_contacts` در هر فراخوانی `GetContactsRequest(hash=0)` می‌زنند.
- تاریخچه، آمار، export و دانلود رسانه از پیام‌های همان فراخوانی استفاده می‌کنند.
- هیچ دیتابیس یا snapshot برنامه‌ای مرجع پاسخ چت، مخاطب، unread یا آخرین پیام نیست.
- cache داخلی session در Telethon فقط برای اطلاعات فنی entity/access hash است و جای پاسخ زنده را نمی‌گیرد.

## Resolver امن

`telegram.resolve_target` و resolver داخلی از ID، marked ID، username، شماره، نام مخاطب، عنوان چت و Saved Messages پشتیبانی می‌کنند. ترتیب تطابق exact قبل از partial است. نتیجه هم‌نام هیچ‌وقت خودکار برای ارسال انتخاب نمی‌شود و `target_ambiguous` همراه گزینه‌های دارای ID برمی‌گردد.

## مدل‌ها

- Chat: نوع دقیق، marked ID، username، phone، unread، آخرین پیام/تاریخ، pin، mute، archive/folder، forum، verified، members و deleted.
- Contact: ID، نام، username، phone، contact/mutual، bot، verified، deleted، status و last_seen.
- Message: ID، marked chat ID، sender ID/name، type، reply ID، views، forwards، media، متن و تاریخ.

## قابلیت‌ها

- چت خصوصی/گروه/سوپرگروه/کانال/ربات، جست‌وجو و صفحه‌بندی
- مخاطبین و resolve بدون ابهام
- تاریخچه، جست‌وجوی پیام و پروفایل کامل User/Chat/Channel
- آمار کلی، unread و آمار یک چت
- export به JSON/TXT
- دانلود گروهی رسانه با فیلتر نوع
- ارسال و فوروارد گروهی تأییدشده با سقف ۲۰ مقصد
- چنداکانتی در تمام اکشن‌ها و APIهای مستقیم
- مرورگر مستقیم چت/مخاطب/تاریخچه در Web و Desktop
- دستورات chats/contacts/stats/resolve در CLI

## قرارداد خطا

`TelegramError` شامل `code`، `message`، `retryable` و `retry_after` است. کدهای اصلی:

- `network`, `timeout`, `flood_wait`
- `session_revoked`, `authorization_required`, `account_restricted`
- `privacy_restricted`, `admin_required`, `write_forbidden`
- `peer_invalid`, `target_ambiguous`, `message_invalid`, `media_invalid`
- `not_connected`, `invalid_input`, `local_file_missing`, `rpc_error`

Web API فیلد انسانی و سازگار `detail` و داده ساختاریافته `error` را هم‌زمان برمی‌گرداند. FloodWait دارای `Retry-After` است. readهای idempotent فقط یک بار در خطای گذرای network/timeout retry می‌شوند؛ FloodWait و عملیات destructive هرگز خودکار تکرار نمی‌شوند.

## امنیت

- api_hash، کد ورود، 2FA و token در status یا خروجی ابزار قرار نمی‌گیرند.
- لاگ خطاهای Telegram فقط نوع خطا/کد امن را ثبت می‌کند و متن خام exception را چاپ نمی‌کند.
- ارسال، forward، bulk و تغییرات حساب زیر Safety Gate باقی مانده‌اند.
- bulk مقصد تکراری را حذف و بیش از ۲۰ مقصد را رد می‌کند.

## سطوح دسترسی اپ

- Web/Desktop: endpointهای chats، contacts، stats، history و resolve؛ فیلتر، pagination و refresh زنده.
- CLI: `/telegram chats`, `/telegram contacts`, `/telegram stats`, `/telegram resolve`.
- Agent: اکشن‌های typed و دستور صریح برای resolve پیش از عملیات دارای نام مبهم.
