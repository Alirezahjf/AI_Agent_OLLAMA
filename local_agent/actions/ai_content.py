"""AI content tools powered by AvalAI API.

All AI capabilities use the SAME AvalAI API key and base URL that
the rest of the project uses for LLM chat completions:

  * Image Generation: POST /v1/images/generations
    Models: gpt-image-1, dall-e-3, flux-pro, stability.sd3-5-large, qwen-image
  * OCR: POST /v1/ocr
    Models: mistral-ocr-latest
  * Text-to-Speech: POST /v1/audio/speech
    Models: tts-1, tts-1-hd
  * Speech-to-Text: POST /v1/audio/transcriptions
    Models: whisper-1, whisper-large-v3
  * Translate: POST /v1/chat/completions (vision + translation)
  * Vision (image analysis): POST /v1/chat/completions with image_url

Each tool auto-detects the AvalAI credentials from the project config,
and lists available models dynamically via GET /v1/models.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import requests

from ..core.errors import AssistantError, DependencyMissing
from .registry import ActionContext, ActionRegistry, Risk, risk


# ===========================================================================
# AvalAI credential helpers
# ===========================================================================


def _get_avalai_config(context: ActionContext) -> tuple[str, str]:
    """Return (base_url, api_key) for AvalAI from project settings."""
    settings = context.runtime.settings

    # Try AvalAI first, then any OpenAI-compatible provider
    base_url = ""
    api_key = ""

    # From settings
    if hasattr(settings, "avalai_base_url") and settings.avalai_base_url:
        base_url = settings.avalai_base_url
        api_key = settings.avalai_api_key
    elif hasattr(settings, "llm"):
        llm = settings.llm
        if llm.openai_base_url and llm.openai_api_key:
            base_url = llm.openai_base_url
            api_key = llm.openai_api_key

    # From env (override)
    base_url = os.environ.get("AVALAI_BASE_URL", base_url) or os.environ.get("OPENAI_BASE_URL", base_url)
    api_key = os.environ.get("AVALAI_API_KEY", api_key) or os.environ.get("OPENAI_API_KEY", api_key)

    # From extra config
    if not base_url:
        base_url = settings.extra.get("avalai_base_url", "") or settings.extra.get("openai_base_url", "")
    if not api_key:
        api_key = settings.extra.get("avalai_api_key", "") or settings.extra.get("openai_api_key", "")

    if not base_url or not api_key:
        raise DependencyMissing(
            "AvalAI API is not configured",
            install_hint="AVALAI_BASE_URL و AVALAI_API_KEY را در config.json یا environment تنظیم کنید.",
        )

    return base_url.rstrip("/"), api_key


def _avalai_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _list_models_by_category(context: ActionContext) -> dict[str, list[str]]:
    """List all available models from the provider, grouped by category."""
    base_url, api_key = _get_avalai_config(context)
    try:
        resp = requests.get(
            f"{base_url}/models",
            headers=_avalai_headers(api_key),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return {"error": [str(exc)]}

    models = [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    categories: dict[str, list[str]] = {
        "image": [],
        "ocr": [],
        "tts": [],
        "stt": [],
        "video": [],
        "chat": [],
        "embedding": [],
        "other": [],
    }

    for m in models:
        ml = m.lower()
        if any(k in ml for k in ["dall-e", "gpt-image", "flux", "imagen", "stable", "sd3", "sdxl", "qwen-image", "seedream"]):
            categories["image"].append(m)
        elif "ocr" in ml or "mistral-ocr" in ml:
            categories["ocr"].append(m)
        elif any(k in ml for k in ["tts", "speech", "voice"]):
            categories["tts"].append(m)
        elif any(k in ml for k in ["whisper", "transcri", "stt"]):
            categories["stt"].append(m)
        elif any(k in ml for k in ["sora", "veo", "video"]):
            categories["video"].append(m)
        elif any(k in ml for k in ["embed"]):
            categories["embedding"].append(m)
        elif any(k in ml for k in ["gpt", "claude", "gemini", "qwen", "llama", "deepseek", "kimi", "grok", "mistral"]):
            categories["chat"].append(m)
        else:
            categories["other"].append(m)

    return categories


# ===========================================================================
# Registration
# ===========================================================================


def register_ai_content(registry: ActionRegistry, context: ActionContext) -> None:
    registry.decorator(
        name="generate_image",
        description=(
            "ساخت تصویر از متن با AvalAI API (DALL-E 3, GPT-Image, FLUX, Stability AI, Qwen-Image). "
            "مدل‌های موجود از /v1/models خوانده می‌شوند. DESTRUCTIVE."
        ),
        parameters={
            "prompt": {"type": "string", "description": "توضیح تصویر"},
            "model": {"type": "string", "description": "مدل (خالی=پیش‌فرض: dall-e-3)"},
            "filename": {"type": "string", "description": "نام فایل خروجی"},
            "size": {"type": "string", "description": "اندازه (1024x1024, 1792x1024, ...)"},
            "quality": {"type": "string", "enum": ["standard", "hd"]},
        },
        required=("prompt",),
        risk_level=Risk.DESTRUCTIVE,
    )(generate_image)

    registry.decorator(
        name="ocr",
        description=(
            "تشخیص متن از تصویر/PDF با AvalAI OCR API (Mistral OCR). "
            "خروجی Markdown ساخت‌یافته. SAFE."
        ),
        parameters={
            "path": {"type": "string", "description": "مسیر فایل تصویر یا PDF"},
            "model": {"type": "string", "description": "مدل OCR (پیش‌فرض: mistral-ocr-latest)"},
        },
        required=("path",),
    )(ocr)

    registry.decorator(
        name="text_to_speech",
        description=(
            "تبدیل متن به صدا با AvalAI TTS API. SAFE."
        ),
        parameters={
            "text": {"type": "string", "description": "متن برای تبدیل"},
            "model": {"type": "string", "description": "مدل TTS (tts-1, tts-1-hd)"},
            "voice": {"type": "string", "description": "صدا (alloy, echo, fable, onyx, nova, shimmer)"},
            "filename": {"type": "string", "description": "نام فایل خروجی"},
            "speed": {"type": "number", "description": "سرعت (0.25 تا 4.0)"},
        },
        required=("text",),
    )(text_to_speech)

    registry.decorator(
        name="speech_to_text",
        description=(
            "تبدیل صدا به متن با AvalAI STT API (Whisper). SAFE."
        ),
        parameters={
            "path": {"type": "string", "description": "مسیر فایل صوتی"},
            "model": {"type": "string", "description": "مدل (whisper-1, whisper-large-v3)"},
            "language": {"type": "string", "description": "زبان (fa, en, ...)"},
        },
        required=("path",),
    )(speech_to_text)

    registry.decorator(
        name="translate",
        description=(
            "ترجمه متن با AvalAI Chat API (هر مدل چت). SAFE."
        ),
        parameters={
            "text": {"type": "string", "description": "متن برای ترجمه"},
            "target_language": {"type": "string", "description": "زبان مقصد (fa, en, ar, tr, ...)"},
            "source_language": {"type": "string", "description": "زبان مبدأ (خودکار اگر خالی)"},
            "model": {"type": "string", "description": "مدل ترجمه (خالی=پیش‌فرض)"},
        },
        required=("text", "target_language"),
    )(translate)

    registry.decorator(
        name="analyze_image",
        description=(
            "تحلیل تصویر با Vision API (GPT-4o, Claude Vision, Gemini). "
            "توضیح، OCR، استخراج اطلاعات از تصویر. SAFE."
        ),
        parameters={
            "path": {"type": "string", "description": "مسیر فایل تصویر"},
            "question": {"type": "string", "description": "سؤال درباره تصویر (پیش‌فرض: توصیف کامل)"},
            "model": {"type": "string", "description": "مدل vision (gpt-4o, claude-sonnet-5, ...)"},
        },
        required=("path",),
    )(analyze_image)

    registry.decorator(
        name="list_ai_models",
        description=(
            "لیست مدل‌های AI موجود در AvalAI API، دسته‌بندی‌شده "
            "(image/ocr/tts/stt/video/chat). SAFE."
        ),
        parameters={},
    )(list_ai_models)

    registry.decorator(
        name="edit_image",
        description=(
            "ویرایش تصویر با AvalAI API (ماسک + prompt). DESTRUCTIVE."
        ),
        parameters={
            "image_path": {"type": "string", "description": "مسیر تصویر اصلی"},
            "mask_path": {"type": "string", "description": "مسیر ماسک (PNG شفاف)"},
            "prompt": {"type": "string", "description": "توضیح تغییرات"},
            "model": {"type": "string", "description": "مدل (gpt-image-1, dall-e-2)"},
            "filename": {"type": "string"},
        },
        required=("image_path", "prompt"),
        risk_level=Risk.DESTRUCTIVE,
    )(edit_image)

    registry.decorator(
        name="generate_video",
        description=(
            "ساخت ویدیو از متن با AvalAI Video API (Sora, Veo). DESTRUCTIVE."
        ),
        parameters={
            "prompt": {"type": "string", "description": "توضیح ویدیو"},
            "model": {"type": "string", "description": "مدل (sora, veo-3, ...)"},
            "duration": {"type": "integer", "description": "مدت ثانیه (پیش‌فرض 5)"},
            "filename": {"type": "string"},
        },
        required=("prompt",),
        risk_level=Risk.DESTRUCTIVE,
    )(generate_video)

    registry.decorator(
        name="run_code",
        description="اجرای امن کد Python/JavaScript. DESTRUCTIVE.",
        parameters={
            "code": {"type": "string"},
            "language": {"type": "string", "enum": ["python", "javascript"]},
            "timeout": {"type": "integer"},
        },
        required=("code",),
        risk_level=Risk.DESTRUCTIVE,
    )(run_code)

    registry.decorator(
        name="pdf_read",
        description="خواندن متن PDF. SAFE.",
        parameters={
            "path": {"type": "string"},
            "max_pages": {"type": "integer"},
        },
        required=("path",),
    )(pdf_read)

    registry.decorator(
        name="generate_password",
        description="ساخت رمز عبور قوی. SAFE.",
        parameters={
            "length": {"type": "integer"},
            "include_symbols": {"type": "boolean"},
            "count": {"type": "integer"},
        },
    )(generate_password)

    registry.decorator(
        name="db_query",
        description="اجرای SELECT روی SQLite. SAFE.",
        parameters={
            "db_path": {"type": "string"},
            "query": {"type": "string"},
            "max_rows": {"type": "integer"},
        },
        required=("db_path", "query"),
    )(db_query)

    registry.decorator(
        name="db_tables",
        description="لیست جدول‌های SQLite. SAFE.",
        parameters={"db_path": {"type": "string"}},
        required=("db_path",),
    )(db_tables)


# ===========================================================================
# Implementations
# ===========================================================================


@risk(Risk.DESTRUCTIVE)
def generate_image(*, prompt: str, model: str = "",
                   filename: str = "", size: str = "1024x1024",
                   quality: str = "standard",
                   context: ActionContext) -> str:
    """Generate image via AvalAI /v1/images/generations."""
    base_url, api_key = _get_avalai_config(context)
    text = str(prompt).strip()
    if not text:
        raise AssistantError("prompt خالی است")

    m = str(model or "dall-e-3").strip()
    sz = str(size or "1024x1024").strip()

    target = _unique_filename(context.work_dir, filename or "generated", ".png")

    payload: dict[str, Any] = {
        "model": m,
        "prompt": text,
        "n": 1,
        "size": sz,
        "response_format": "b64_json",
    }
    if "dall-e" in m or "gpt-image" in m:
        payload["quality"] = str(quality or "standard")

    try:
        resp = requests.post(
            f"{base_url}/images/generations",
            headers=_avalai_headers(api_key),
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise AssistantError(f"Image generation ناموفق بود: {exc}")

    image_data = data.get("data", [{}])[0]
    if "b64_json" in image_data:
        image_bytes = base64.b64decode(image_data["b64_json"])
        target.write_bytes(image_bytes)
    elif "url" in image_data:
        img_resp = requests.get(image_data["url"], timeout=60)
        img_resp.raise_for_status()
        target.write_bytes(img_resp.content)
    else:
        raise AssistantError("خروجی تصویر نامعتبر بود.")

    revised = image_data.get("revised_prompt", "")
    return (
        f"✅ تصویر ساخته شد: {target}\n"
        f"  مدل: {m} | اندازه: {sz}\n"
        f"  Prompt: {text[:200]}\n"
        + (f"  Revised: {revised[:200]}\n" if revised else "")
        + f"  API: {base_url}"
    )


@risk(Risk.SAFE)
def ocr(*, path: str, model: str = "mistral-ocr-latest",
        context: ActionContext) -> str:
    """OCR via AvalAI /v1/ocr (Mistral OCR) with Tesseract fallback."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = context.work_dir / target
    if not target.is_file():
        raise AssistantError(f"فایل پیدا نشد: {target}")

    m = str(model or "mistral-ocr-latest").strip()
    base_url, api_key = _get_avalai_config(context)

    # Read file and base64 encode
    file_bytes = target.read_bytes()
    b64 = base64.b64encode(file_bytes).decode("utf-8")

    # Detect MIME type
    suffix = target.suffix.lower()
    mime = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp",
        ".pdf": "application/pdf", ".tiff": "image/tiff",
    }.get(suffix, "image/png")

    image_url = f"data:{mime};base64,{b64}"

    try:
        resp = requests.post(
            f"{base_url}/ocr",
            headers=_avalai_headers(api_key),
            json={
                "model": m,
                "document": {
                    "type": "image_url",
                    "image_url": image_url,
                },
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        # Fallback to local Tesseract
        return _ocr_tesseract(target)

    # Parse Mistral OCR response
    pages = data.get("pages", [])
    if not pages:
        # Try direct text extraction
        text = data.get("text", "") or data.get("content", "")
        if text:
            return f"📝 OCR ({m} via {base_url}):\n\n{text[:15000]}"
        return _ocr_tesseract(target)

    lines = [f"📝 OCR با {m} ({len(pages)} صفحه، via {base_url}):\n"]
    for i, page in enumerate(pages):
        page_text = page.get("markdown", "") or page.get("text", "")
        if page_text.strip():
            lines.append(f"--- صفحه {i+1} ---\n{page_text.strip()}")

    result = "\n\n".join(lines)
    if len(result) > 15000:
        result = result[:15000] + "\n… (متن کوتاه شد)"
    return result


def _ocr_tesseract(target: Path) -> str:
    """Fallback OCR using local Tesseract."""
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        raise DependencyMissing(
            "OCR fallback requires pytesseract",
            install_hint="pip install pytesseract Pillow",
        )
    try:
        image = Image.open(str(target))
        text = pytesseract.image_to_string(image, lang="fas+eng")
    except Exception as exc:
        raise AssistantError(f"OCR ناموفق بود: {exc}")
    text = text.strip()
    if not text:
        return "متنی در تصویر تشخیص داده نشد."
    return f"📝 OCR (Tesseract local fallback):\n\n{text[:10000]}"


@risk(Risk.SAFE)
def text_to_speech(*, text: str, model: str = "tts-1",
                   voice: str = "alloy", filename: str = "",
                   speed: float = 1.0,
                   context: ActionContext) -> str:
    """TTS via AvalAI /v1/audio/speech."""
    base_url, api_key = _get_avalai_config(context)
    content = str(text).strip()
    if not content:
        raise AssistantError("متن خالی است")

    m = str(model or "tts-1").strip()
    v = str(voice or "alloy").strip()
    spd = max(0.25, min(float(speed or 1.0), 4.0))

    target = _unique_filename(context.work_dir, filename or "speech", ".mp3")

    try:
        resp = requests.post(
            f"{base_url}/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": m,
                "input": content,
                "voice": v,
                "speed": spd,
                "response_format": "mp3",
            },
            timeout=60,
            stream=True,
        )
        resp.raise_for_status()
        with open(str(target), "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
    except requests.RequestException as exc:
        raise AssistantError(f"TTS ناموفق بود: {exc}")

    return (
        f"🔊 فایل صوتی ساخته شد: {target}\n"
        f"  مدل: {m} | صدا: {v} | سرعت: {spd}x\n"
        f"  API: {base_url}\n"
        f"  متن: {content[:200]}"
    )


@risk(Risk.SAFE)
def speech_to_text(*, path: str, model: str = "whisper-1",
                   language: str = "",
                   context: ActionContext) -> str:
    """STT via AvalAI /v1/audio/transcriptions."""
    base_url, api_key = _get_avalai_config(context)
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = context.work_dir / target
    if not target.is_file():
        raise AssistantError(f"فایل صوتی پیدا نشد: {target}")

    m = str(model or "whisper-1").strip()

    try:
        with open(str(target), "rb") as f:
            files = {"file": (target.name, f)}
            data_payload: dict[str, Any] = {"model": m}
            if language:
                data_payload["language"] = str(language).strip()
            resp = requests.post(
                f"{base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data=data_payload,
                timeout=120,
            )
        resp.raise_for_status()
        result = resp.json()
    except requests.RequestException as exc:
        raise AssistantError(f"STT ناموفق بود: {exc}")

    text = result.get("text", "")
    if not text:
        return "متنی در صدا تشخیص داده نشد."
    return (
        f"🎙️ متن تشخیص‌شده ({m} via {base_url}):\n\n{text[:10000]}"
    )


@risk(Risk.SAFE)
def translate(*, text: str, target_language: str,
              source_language: str = "", model: str = "",
              context: ActionContext) -> str:
    """Translate using AvalAI chat completions."""
    base_url, api_key = _get_avalai_config(context)
    content = str(text).strip()
    if not content:
        raise AssistantError("متن خالی است")

    target = str(target_language).strip()
    source = str(source_language or "auto-detect").strip()
    m = str(model or "").strip()
    if not m:
        # Use the project's default model from settings
        if hasattr(context.runtime.settings, "llm"):
            llm = context.runtime.settings.llm
            m = llm.openai_model or llm.ollama_model or "gpt-4o-mini"
        else:
            m = "gpt-4o-mini"

    system_prompt = (
        f"You are a professional translator. Translate the following text to {target}. "
        f"Source language: {source}. "
        "Preserve formatting, tone, and meaning. Output ONLY the translation, nothing else."
    )

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=_avalai_headers(api_key),
            json={
                "model": m,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                "temperature": 0.3,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise AssistantError(f"ترجمه ناموفق بود: {exc}")

    translated = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    if not translated:
        raise AssistantError("ترجمه‌ای دریافت نشد.")

    return (
        f"🌐 ترجمه ({source} → {target}) با {m}:\n\n{translated}"
    )


@risk(Risk.SAFE)
def analyze_image(*, path: str, question: str = "",
                  model: str = "",
                  context: ActionContext) -> str:
    """Analyze an image using Vision API (GPT-4o, Claude, Gemini)."""
    base_url, api_key = _get_avalai_config(context)
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = context.work_dir / target
    if not target.is_file():
        raise AssistantError(f"فایل تصویر پیدا نشد: {target}")

    m = str(model or "gpt-4o").strip()
    q = str(question or "Describe this image in detail. Identify all text, objects, people, and context. Respond in Persian (فارسی).").strip()

    # Base64 encode
    file_bytes = target.read_bytes()
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    suffix = target.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp"}.get(suffix, "image/png")

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=_avalai_headers(api_key),
            json={
                "model": m,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": q},
                        {"type": "image_url", "image_url": {
                            "url": f"data:{mime};base64,{b64}",
                        }},
                    ],
                }],
                "max_tokens": 2000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise AssistantError(f"تحلیل تصویر ناموفق بود: {exc}")

    result = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    if not result:
        return "تحلیلی دریافت نشد."
    return f"🖼️ تحلیل تصویر ({m} via {base_url}):\n\n{result[:10000]}"


@risk(Risk.SAFE)
def list_ai_models(*, context: ActionContext) -> str:
    """List all available AI models grouped by category."""
    categories = _list_models_by_category(context)

    if "error" in categories:
        return f"❌ خطا در دریافت مدل‌ها: {categories['error'][0]}"

    base_url, _ = _get_avalai_config(context)
    labels = {
        "image": "🖼️ Image Generation",
        "ocr": "👁️ OCR",
        "tts": "🔊 Text-to-Speech",
        "stt": "🎙️ Speech-to-Text",
        "video": "🎬 Video Generation",
        "chat": "💬 Chat / Reasoning",
        "embedding": "📐 Embeddings",
        "other": "📦 Other",
    }

    lines = [f"🧠 مدل‌های AI موجود در {base_url}:\n"]
    total = 0
    for cat, label in labels.items():
        models = categories.get(cat, [])
        if models:
            total += len(models)
            lines.append(f"  {label} ({len(models)}):")
            for m in models[:20]:
                lines.append(f"    • {m}")
            if len(models) > 20:
                lines.append(f"    … و {len(models) - 20} مدل دیگر")
            lines.append("")

    lines.append(f"  مجموع: {total} مدل")
    return "\n".join(lines)


# ===========================================================================
# Non-AI tools (kept from previous version)
# ===========================================================================


@risk(Risk.DESTRUCTIVE)
def edit_image(*, image_path: str, mask_path: str = "",
               prompt: str = "", model: str = "gpt-image-1",
               filename: str = "",
               context: ActionContext) -> str:
    """Edit an image via AvalAI /v1/images/edits."""
    base_url, api_key = _get_avalai_config(context)

    img = Path(image_path).expanduser()
    if not img.is_absolute():
        img = context.work_dir / img
    if not img.is_file():
        raise AssistantError(f"تصویر پیدا نشد: {img}")

    text = str(prompt).strip()
    if not text:
        raise AssistantError("prompt خالی است")

    m = str(model or "gpt-image-1").strip()
    target = _unique_filename(context.work_dir, filename or "edited", ".png")

    try:
        with open(str(img), "rb") as img_file:
            files: dict[str, Any] = {"image": (img.name, img_file, "image/png")}
            mask_file_handle = None
            if mask_path:
                msk = Path(mask_path).expanduser()
                if not msk.is_absolute():
                    msk = context.work_dir / msk
                if msk.is_file():
                    mask_file_handle = open(str(msk), "rb")
                    files["mask"] = (msk.name, mask_file_handle, "image/png")

            try:
                resp = requests.post(
                    f"{base_url}/images/edits",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files=files,
                    data={"model": m, "prompt": text, "response_format": "b64_json", "n": "1"},
                    timeout=120,
                )
            finally:
                if mask_file_handle:
                    mask_file_handle.close()

        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise AssistantError(f"Image edit ناموفق بود: {exc}")

    image_data = data.get("data", [{}])[0]
    if "b64_json" in image_data:
        target.write_bytes(base64.b64decode(image_data["b64_json"]))
    elif "url" in image_data:
        img_resp = requests.get(image_data["url"], timeout=60)
        img_resp.raise_for_status()
        target.write_bytes(img_resp.content)
    else:
        raise AssistantError("خروجی نامعتبر.")

    return f"✅ تصویر ویرایش شد: {target}\n  مدل: {m}\n  Prompt: {text[:200]}"


@risk(Risk.DESTRUCTIVE)
def generate_video(*, prompt: str, model: str = "",
                   duration: int = 5, filename: str = "",
                   context: ActionContext) -> str:
    """Generate video via AvalAI /v1/videos."""
    base_url, api_key = _get_avalai_config(context)
    text = str(prompt).strip()
    if not text:
        raise AssistantError("prompt خالی است")

    m = str(model or "sora").strip()
    dur = max(2, min(int(duration or 5), 20))
    target = _unique_filename(context.work_dir, filename or "video", ".mp4")

    try:
        resp = requests.post(
            f"{base_url}/videos",
            headers=_avalai_headers(api_key),
            json={
                "model": m,
                "prompt": text,
                "duration": dur,
                "response_format": "url",
            },
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise AssistantError(f"Video generation ناموفق بود: {exc}")

    # Video may be async — check for URL or task ID
    video_data = data.get("data", [{}])[0] if "data" in data else data
    if "url" in video_data:
        vid_resp = requests.get(video_data["url"], timeout=120)
        vid_resp.raise_for_status()
        target.write_bytes(vid_resp.content)
        return f"✅ ویدیو ساخته شد: {target}\n  مدل: {m} | مدت: {dur}s\n  Prompt: {text[:200]}"
    elif "id" in data or "task_id" in video_data:
        task_id = data.get("id", video_data.get("task_id", "?"))
        return (
            f"⏳ ویدیو در حال ساخت (task: {task_id})\n"
            f"  مدل: {m} | مدت: {dur}s\n"
            f"  Prompt: {text[:200]}\n"
            f"  نتیجه بعداً آماده می‌شود."
        )
    else:
        return f"📹 پاسخ API:\n{json.dumps(data, ensure_ascii=False, indent=2)[:2000]}"


@risk(Risk.DESTRUCTIVE)
def run_code(*, code: str, language: str = "python",
             timeout: int = 10, context: ActionContext) -> str:
    import subprocess, tempfile
    content = str(code).strip()
    if not content:
        raise AssistantError("کد خالی است")
    lang = str(language or "python").lower().strip()
    t = max(2, min(int(timeout or 10), 60))
    tmp_path = None
    try:
        if lang in ("python", "py"):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=str(context.work_dir)) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            cmd = ["python3", tmp_path]
        elif lang in ("javascript", "js", "node"):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, dir=str(context.work_dir)) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            cmd = ["node", tmp_path]
        else:
            raise AssistantError(f"زبان {lang} پشتیبانی نمی‌شود.")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=t, cwd=str(context.work_dir))
        output = result.stdout or ""
        errors = result.stderr or ""
        lines = [f"💻 اجرای {lang} (exit: {result.returncode}):"]
        if output:
            lines.append(f"\nstdout:\n{output[:5000]}")
        if errors:
            lines.append(f"\nstderr:\n{errors[:2000]}")
        if not output and not errors:
            lines.append("  (بدون خروجی)")
        return "\n".join(lines)
    except subprocess.TimeoutExpired:
        return f"⏱️ زمان اجرا تمام شد ({t}s)"
    except FileNotFoundError:
        return f"❌ مفسر {lang} پیدا نشد."
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@risk(Risk.SAFE)
def pdf_read(*, path: str, max_pages: int = 10, context: ActionContext) -> str:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = context.work_dir / target
    if not target.is_file():
        raise AssistantError(f"فایل PDF پیدا نشد: {target}")
    try:
        import PyPDF2
    except ImportError:
        raise DependencyMissing("PyPDF2 not installed", install_hint="pip install PyPDF2")
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
            return "متنی در PDF یافت نشد. از ocr استفاده کنید."
        content = "\n\n".join(texts)
        if len(content) > 15000:
            content = content[:15000] + "\n… (کوتاه شد)"
        return f"📄 {target.name} ({total} صفحه، {min(pages, total)} خوانده‌شد):\n\n{content}"
    except Exception as exc:
        raise AssistantError(f"خواندن PDF ناموفق: {exc}")


@risk(Risk.SAFE)
def generate_password(*, length: int = 16, include_symbols: bool = True,
                      count: int = 1, context: ActionContext) -> str:
    import secrets, string
    n = max(8, min(int(length or 16), 128))
    c = max(1, min(int(count or 1), 10))
    chars = string.ascii_letters + string.digits
    if include_symbols:
        chars += "!@#$%^&*()-_=+[]{}|;:,.<>?"
    passwords = ["".join(secrets.choice(chars) for _ in range(n)) for _ in range(c)]
    if c == 1:
        return f"🔐 رمز عبور ({n} کاراکتر):\n  {passwords[0]}"
    lines = [f"🔐 {c} رمز عبور ({n} کاراکتر):"]
    for i, pw in enumerate(passwords, 1):
        lines.append(f"  {i}. {pw}")
    return "\n".join(lines)


@risk(Risk.SAFE)
def db_query(*, db_path: str, query: str, max_rows: int = 50,
             context: ActionContext) -> str:
    import sqlite3
    path = Path(db_path).expanduser()
    if not path.is_absolute():
        path = context.work_dir / path
    if not path.is_file():
        raise AssistantError(f"فایل دیتابیس پیدا نشد: {path}")
    sql = str(query).strip()
    if not sql:
        raise AssistantError("query خالی است")
    sql_upper = sql.upper().lstrip()
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        raise AssistantError("فقط SELECT مجاز است.")
    dangerous = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH", "DETACH", "PRAGMA"}
    for kw in dangerous:
        if kw in sql_upper.split():
            raise AssistantError(f"{kw} مجاز نیست.")
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
    lines = [f"🗃️ ({len(rows)} ردیف{'+'if has_more else ''}):"]
    lines.append("  " + " | ".join(columns))
    lines.append("  " + "-+-".join("-" * min(len(c), 30) for c in columns))
    for row in rows:
        lines.append("  " + " | ".join(str(v)[:50] for v in row))
    return "\n".join(lines)


@risk(Risk.SAFE)
def db_tables(*, db_path: str, context: ActionContext) -> str:
    import sqlite3
    path = Path(db_path).expanduser()
    if not path.is_absolute():
        path = context.work_dir / path
    if not path.is_file():
        raise AssistantError(f"فایل پیدا نشد: {path}")
    try:
        conn = sqlite3.connect(str(path))
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        lines = [f"🗃️ {path.name} ({len(tables)} جدول):"]
        for (name,) in tables:
            cols = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            col_strs = [f"{c[1]} ({c[2]})" for c in cols]
            lines.append(f"\n  📋 {name} ({len(cols)} ستون):\n     {', '.join(col_strs)}")
        conn.close()
    except sqlite3.Error as exc:
        raise AssistantError(f"خطا: {exc}")
    return "\n".join(lines)


# ===========================================================================
# Helpers
# ===========================================================================


def _unique_filename(work_dir: Path, name: str, ext: str) -> Path:
    target = work_dir / f"{name}{ext}"
    counter = 1
    while target.exists():
        target = work_dir / f"{name}_{counter}{ext}"
        counter += 1
    return target
