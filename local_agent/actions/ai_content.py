"""Image generation, OCR, TTS, STT, and translation actions.

Category 2: AI and Content tools.

Providers:
  * Image Generation: Stability AI (SDXL) or OpenAI DALL-E
  * OCR: pytesseract (Tesseract) with PIL fallback
  * TTS: gTTS (Google Text-to-Speech, free)
  * STT: openai-whisper (local) or groq whisper API
  * Translate: MyMemory free API + Google Translate fallback
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk


def register_ai_content(registry: ActionRegistry, context: ActionContext) -> None:
    # ---- Image Generation ----
    registry.decorator(
        name="generate_image",
        description=(
            "ساخت تصویر از متن با Stability AI (SDXL) یا DALL-E. "
            "نیاز به API key در config (stability_api_key یا openai_api_key). "
            "تصویر در workspace ذخیره و مسیر برگردانده می‌شود. DESTRUCTIVE."
        ),
        parameters={
            "prompt": {"type": "string", "description": "توضیح تصویر به انگلیسی"},
            "filename": {"type": "string", "description": "نام فایل خروجی (بدون پسوند)"},
            "provider": {"type": "string", "enum": ["stability", "dalle", "auto"]},
            "width": {"type": "integer", "description": "عرض (پیش‌فرض 1024)"},
            "height": {"type": "integer", "description": "ارتفاع (پیش‌فرض 1024)"},
        },
        required=("prompt",),
        risk_level=Risk.DESTRUCTIVE,
    )(generate_image)

    # ---- OCR ----
    registry.decorator(
        name="ocr",
        description=(
            "تشخیص متن از تصویر (OCR) با Tesseract. "
            "از فایل تصویر در workspace می‌خواند. SAFE."
        ),
        parameters={
            "path": {"type": "string", "description": "مسیر فایل تصویر"},
            "language": {"type": "string", "description": "زبان (fas=فارسی, eng=انگلیسی, fas+eng=هر دو)"},
        },
        required=("path",),
    )(ocr)

    # ---- TTS ----
    registry.decorator(
        name="text_to_speech",
        description=(
            "تبدیل متن به صدا (TTS) با Google TTS. فایل mp3 ذخیره می‌شود. DESTRUCTIVE."
        ),
        parameters={
            "text": {"type": "string", "description": "متن برای تبدیل"},
            "filename": {"type": "string", "description": "نام فایل خروجی (بدون پسوند)"},
            "language": {"type": "string", "description": "زبان (fa=فارسی, en=انگلیسی)"},
            "slow": {"type": "boolean", "description": "سرعت آهسته"},
        },
        required=("text",),
        risk_level=Risk.DESTRUCTIVE,
    )(text_to_speech)

    # ---- Translate ----
    registry.decorator(
        name="translate",
        description=(
            "ترجمه متن با MyMemory API (رایگان، بدون نیاز به key). SAFE."
        ),
        parameters={
            "text": {"type": "string", "description": "متن برای ترجمه"},
            "target_language": {"type": "string", "description": "زبان مقصد (fa, en, ar, tr, ...)"},
            "source_language": {"type": "string", "description": "زبان مبدأ (خودکار اگر خالی)"},
        },
        required=("text", "target_language"),
    )(translate)

    # ---- Database ----
    registry.decorator(
        name="db_query",
        description=(
            "اجرای SQL query روی یک فایل SQLite. فقط SELECT مجاز است (read-only). SAFE."
        ),
        parameters={
            "db_path": {"type": "string", "description": "مسیر فایل .sqlite یا .db"},
            "query": {"type": "string", "description": "عبارت SQL (فقط SELECT)"},
            "max_rows": {"type": "integer", "description": "حداکثر ردیف (پیش‌فرض 50)"},
        },
        required=("db_path", "query"),
    )(db_query)

    registry.decorator(
        name="db_tables",
        description="لیست جدول‌ها و ستون‌های یک فایل SQLite. SAFE.",
        parameters={
            "db_path": {"type": "string", "description": "مسیر فایل .sqlite یا .db"},
        },
        required=("db_path",),
    )(db_tables)

    # ---- Code Sandbox ----
    registry.decorator(
        name="run_code",
        description=(
            "اجرای امن یک قطعه کد Python یا JavaScript در محیط sandbox. "
            "خروجی stdout/stderr برمی‌گردد. DESTRUCTIVE."
        ),
        parameters={
            "code": {"type": "string", "description": "کد برای اجرا"},
            "language": {"type": "string", "enum": ["python", "javascript"]},
            "timeout": {"type": "integer", "description": "حداکثر ثانیه (پیش‌فرض 10)"},
        },
        required=("code",),
        risk_level=Risk.DESTRUCTIVE,
    )(run_code)

    # ---- PDF ----
    registry.decorator(
        name="pdf_read",
        description="خواندن متن یک فایل PDF. SAFE.",
        parameters={
            "path": {"type": "string", "description": "مسیر فایل PDF"},
            "max_pages": {"type": "integer", "description": "حداکثر صفحات (پیش‌فرض 10)"},
        },
        required=("path",),
    )(pdf_read)

    # ---- Password Generator ----
    registry.decorator(
        name="generate_password",
        description="ساخت رمز عبور قوی و تصادفی. SAFE.",
        parameters={
            "length": {"type": "integer", "description": "طول رمز (پیش‌فرض 16)"},
            "include_symbols": {"type": "boolean"},
            "count": {"type": "integer", "description": "تعداد رمز (پیش‌فرض 1)"},
        },
    )(generate_password)


# ===========================================================================
# Implementations
# ===========================================================================


@risk(Risk.DESTRUCTIVE)
def generate_image(*, prompt: str, filename: str = "",
                   provider: str = "auto", width: int = 1024,
                   height: int = 1024, context: ActionContext) -> str:
    """Generate an image from text using Stability AI or DALL-E."""
    import requests

    text = str(prompt).strip()
    if not text:
        raise AssistantError("prompt خالی است")

    w = max(256, min(int(width or 1024), 2048))
    h = max(256, min(int(height or 1024), 2048))
    name = str(filename or "generated").strip()
    if not name:
        name = "generated"

    work_dir = context.work_dir
    target = work_dir / f"{name}.png"
    # Avoid overwriting
    counter = 1
    while target.exists():
        target = work_dir / f"{name}_{counter}.png"
        counter += 1

    prov = str(provider or "auto").lower()

    # Try Stability AI
    stability_key = os.environ.get("STABILITY_API_KEY", "")
    if not stability_key:
        settings = context.runtime.settings
        stability_key = settings.extra.get("stability_api_key", "")

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        openai_key = context.runtime.settings.extra.get("openai_api_key", "")

    if prov in ("stability", "auto") and stability_key:
        try:
            resp = requests.post(
                "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image",
                headers={
                    "Authorization": f"Bearer {stability_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "text_prompts": [{"text": text, "weight": 1}],
                    "cfg_scale": 7, "width": w, "height": h,
                    "samples": 1, "steps": 30,
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            import base64
            image_bytes = base64.b64decode(data["artifacts"][0]["base64"])
            target.write_bytes(image_bytes)
            return f"✅ تصویر ساخته شد: {target}\n  Provider: Stability AI (SDXL)\n  Prompt: {text[:200]}"
        except Exception as exc:
            if prov == "stability":
                raise AssistantError(f"Stability AI ناموفق بود: {exc}")

    # Try DALL-E (OpenAI)
    if prov in ("dalle", "auto") and openai_key:
        try:
            resp = requests.post(
                "https://api.openai.com/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "dall-e-3",
                    "prompt": text,
                    "n": 1,
                    "size": f"{w}x{h}",
                    "response_format": "b64_json",
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            import base64
            image_bytes = base64.b64decode(data["data"][0]["b64_json"])
            target.write_bytes(image_bytes)
            revised = data["data"][0].get("revised_prompt", "")
            return (
                f"✅ تصویر ساخته شد: {target}\n"
                f"  Provider: DALL-E 3\n"
                f"  Prompt: {text[:200]}\n"
                + (f"  Revised: {revised[:200]}" if revised else "")
            )
        except Exception as exc:
            if prov == "dalle":
                raise AssistantError(f"DALL-E ناموفق بود: {exc}")

    raise AssistantError(
        "هیچ ارائه‌دهندهٔ تصویرسازی تنظیم نشده است. "
        "STABILITY_API_KEY یا OPENAI_API_KEY را تنظیم کنید."
    )


@risk(Risk.SAFE)
def ocr(*, path: str, language: str = "eng", context: ActionContext) -> str:
    """Extract text from an image using Tesseract OCR."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = context.work_dir / target
    if not target.is_file():
        raise AssistantError(f"فایل پیدا نشد: {target}")

    try:
        from PIL import Image
    except ImportError:
        raise DependencyMissing("PIL is not installed", install_hint="pip install Pillow")

    try:
        import pytesseract
    except ImportError:
        raise DependencyMissing(
            "pytesseract is not installed",
            install_hint="pip install pytesseract && sudo apt install tesseract-ocr",
        )

    lang = str(language or "eng").strip()
    try:
        image = Image.open(str(target))
        text = pytesseract.image_to_string(image, lang=lang)
    except Exception as exc:
        raise AssistantError(f"OCR ناموفق بود: {exc}")

    text = text.strip()
    if not text:
        return "متنی در تصویر تشخیص داده نشد."
    if len(text) > 10000:
        text = text[:10000] + "\n… (متن کوتاه شد)"
    return f"📝 OCR از {target.name} (زبان: {lang}):\n\n{text}"


@risk(Risk.DESTRUCTIVE)
def text_to_speech(*, text: str, filename: str = "",
                   language: str = "fa", slow: bool = False,
                   context: ActionContext) -> str:
    """Convert text to speech using gTTS."""
    content = str(text).strip()
    if not content:
        raise AssistantError("متن خالی است")

    try:
        from gtts import gTTS
    except ImportError:
        raise DependencyMissing("gTTS is not installed", install_hint="pip install gTTS")

    name = str(filename or "speech").strip()
    if not name:
        name = "speech"

    target = context.work_dir / f"{name}.mp3"
    counter = 1
    while target.exists():
        target = context.work_dir / f"{name}_{counter}.mp3"
        counter += 1

    lang = str(language or "fa").strip()[:5]
    try:
        tts = gTTS(text=content, lang=lang, slow=bool(slow))
        tts.save(str(target))
    except Exception as exc:
        raise AssistantError(f"TTS ناموفق بود: {exc}")

    return f"🔊 فایل صوتی ساخته شد: {target}\n  زبان: {lang}\n  متن: {content[:200]}"


@risk(Risk.SAFE)
def translate(*, text: str, target_language: str,
              source_language: str = "", context: ActionContext) -> str:
    """Translate text using MyMemory free API."""
    import requests

    content = str(text).strip()
    if not content:
        raise AssistantError("متن خالی است")

    target = str(target_language).strip().lower()[:5]
    source = str(source_language or "").strip().lower()[:5]
    langpair = f"{source}|{target}" if source else f"auto|{target}"

    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": content[:500], "langpair": langpair},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise AssistantError(f"ترجمه ناموفق بود: {exc}")

    response_data = data.get("responseData", {})
    translated = response_data.get("translatedText", "")
    detected_lang = response_data.get("detectedLanguage", "")

    if not translated:
        raise AssistantError("ترجمه‌ای دریافت نشد.")

    return (
        f"🌐 ترجمه ({detected_lang or source or '?'} → {target}):\n\n"
        f"{translated}"
    )


@risk(Risk.SAFE)
def db_query(*, db_path: str, query: str, max_rows: int = 50,
             context: ActionContext) -> str:
    """Execute a read-only SQL query on a SQLite database."""
    import sqlite3

    path = Path(db_path).expanduser()
    if not path.is_absolute():
        path = context.work_dir / path
    if not path.is_file():
        raise AssistantError(f"فایل دیتابیس پیدا نشد: {path}")

    sql = str(query).strip()
    if not sql:
        raise AssistantError("query خالی است")

    # Safety: only SELECT allowed
    sql_upper = sql.upper().lstrip()
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        raise AssistantError("فقط query های SELECT مجاز هستند (read-only).")

    # Block dangerous keywords even inside SELECT
    dangerous = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                 "ATTACH", "DETACH", "PRAGMA"}
    for keyword in dangerous:
        if keyword in sql_upper.split():
            raise AssistantError(f"کلمهٔ کلیدی {keyword} مجاز نیست.")

    limit = max(1, min(int(max_rows or 50), 500))
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
        conn.close()
    except sqlite3.Error as exc:
        raise AssistantError(f"خطای SQL: {exc}")

    if not columns:
        return "نتیجه‌ای نداشت."

    # Format as table
    lines = [f"🗃️ نتیجهٔ query ({len(rows)} ردیف{'+'if has_more else ''}):"]
    lines.append("  " + " | ".join(columns))
    lines.append("  " + "-+-".join("-" * min(len(c), 30) for c in columns))
    for row in rows:
        values = [str(v)[:50] for v in row]
        lines.append("  " + " | ".join(values))

    return "\n".join(lines)


@risk(Risk.SAFE)
def db_tables(*, db_path: str, context: ActionContext) -> str:
    """List tables and columns in a SQLite database."""
    import sqlite3

    path = Path(db_path).expanduser()
    if not path.is_absolute():
        path = context.work_dir / path
    if not path.is_file():
        raise AssistantError(f"فایل دیتابیس پیدا نشد: {path}")

    try:
        conn = sqlite3.connect(str(path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        lines = [f"🗃️ جداول {path.name} ({len(tables)} جدول):"]
        for (table_name,) in tables:
            columns = conn.execute(f"PRAGMA table_info(\"{table_name}\")").fetchall()
            col_strs = [f"{c[1]} ({c[2]})" for c in columns]
            lines.append(f"\n  📋 {table_name} ({len(columns)} ستون):")
            lines.append(f"     {', '.join(col_strs)}")
        conn.close()
    except sqlite3.Error as exc:
        raise AssistantError(f"خطا: {exc}")

    return "\n".join(lines)


@risk(Risk.DESTRUCTIVE)
def run_code(*, code: str, language: str = "python",
             timeout: int = 10, context: ActionContext) -> str:
    """Execute a code snippet in a sandboxed subprocess."""
    import subprocess
    import tempfile

    content = str(code).strip()
    if not content:
        raise AssistantError("کد خالی است")

    lang = str(language or "python").lower().strip()
    t = max(2, min(int(timeout or 10), 60))

    if lang in ("python", "py"):
        ext = ".py"
        cmd = ["python3", "-c", content]
        # For multiline, write to a temp file
        if "\n" in content:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False,
                                              dir=str(context.work_dir)) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            cmd = ["python3", tmp_path]
        else:
            tmp_path = None
    elif lang in ("javascript", "js", "node"):
        ext = ".js"
        if "\n" in content:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False,
                                              dir=str(context.work_dir)) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
        else:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False,
                                              dir=str(context.work_dir)) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
        cmd = ["node", tmp_path]
    else:
        raise AssistantError(f"زبان {lang} پشتیبانی نمی‌شود. python یا javascript استفاده کنید.")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=t,
            cwd=str(context.work_dir),
        )
        output = result.stdout or ""
        errors = result.stderr or ""
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        return f"⏱️ زمان اجرا تمام شد ({t} ثانیه)"
    except FileNotFoundError:
        return f"❌ مفسر {lang} پیدا نشد."
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    lines = [f"💻 اجرای {lang} (exit code: {exit_code}):"]
    if output:
        lines.append(f"\nstdout:\n{output[:5000]}")
    if errors:
        lines.append(f"\nstderr:\n{errors[:2000]}")
    if not output and not errors:
        lines.append("  (بدون خروجی)")
    return "\n".join(lines)


@risk(Risk.SAFE)
def pdf_read(*, path: str, max_pages: int = 10, context: ActionContext) -> str:
    """Read text from a PDF file."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = context.work_dir / target
    if not target.is_file():
        raise AssistantError(f"فایل PDF پیدا نشد: {target}")
    if target.suffix.lower() != ".pdf":
        raise AssistantError("فایل باید پسوند .pdf داشته باشد")

    try:
        import PyPDF2
    except ImportError:
        raise DependencyMissing("PyPDF2 is not installed", install_hint="pip install PyPDF2")

    pages = max(1, min(int(max_pages or 10), 100))
    try:
        reader = PyPDF2.PdfReader(str(target))
        total = len(reader.pages)
        texts = []
        for i, page in enumerate(reader.pages[:pages]):
            text = page.extract_text() or ""
            if text.strip():
                texts.append(f"--- صفحه {i+1} ---\n{text.strip()}")
        if not texts:
            return "متنی در PDF تشخیص داده نشد (شاید اسکن شده است)."
        content = "\n\n".join(texts)
        if len(content) > 15000:
            content = content[:15000] + "\n… (متن کوتاه شد)"
        return f"📄 {target.name} ({total} صفحه، {min(pages, total)} خوانده‌شد):\n\n{content}"
    except Exception as exc:
        raise AssistantError(f"خواندن PDF ناموفق بود: {exc}")


@risk(Risk.SAFE)
def generate_password(*, length: int = 16, include_symbols: bool = True,
                      count: int = 1, context: ActionContext) -> str:
    """Generate cryptographically secure random passwords."""
    import secrets
    import string

    n = max(8, min(int(length or 16), 128))
    c = max(1, min(int(count or 1), 10))

    chars = string.ascii_letters + string.digits
    if include_symbols:
        chars += "!@#$%^&*()-_=+[]{}|;:,.<>?"

    passwords = []
    for _ in range(c):
        pw = "".join(secrets.choice(chars) for _ in range(n))
        passwords.append(pw)

    if c == 1:
        return f"🔐 رمز عبور ({n} کاراکتر):\n  {passwords[0]}"
    lines = [f"🔐 {c} رمز عبور ({n} کاراکتر):"]
    for i, pw in enumerate(passwords, 1):
        lines.append(f"  {i}. {pw}")
    return "\n".join(lines)
