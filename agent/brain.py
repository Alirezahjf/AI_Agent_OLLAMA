"""Ollama planning loop with a small, inspectable JSON tool protocol."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .config import Settings
from .storage import Storage
from .tools import LocalTools, ToolError, ToolResult

SYSTEM = """تو «مهندس کدنویسی محلی» هستی. فقط به فارسی پاسخ بده مگر کاربر صریحاً زبان دیگری بخواهد.
کار تو بررسی، توسعه، تست و نگهداری پروژه‌های نرم‌افزاری در WORKSPACE_ROOT است. دقیق، مرحله‌به‌مرحله و کوتاه باش.
هرگز درباره دسترسی‌ای که نداری ادعا نکن. قبل از تغییر، فایل‌های مرتبط را بخوان و پس از تغییر تست/دیف مناسب را پیشنهاد یا اجرا کن.
برای استفاده از ابزار، فقط یک شیء JSON خالص (بدون markdown و متن اضافی) با این فرم برگردان:
{"tool":"list_files","args":{"path":".","depth":2}}
{"tool":"read_file","args":{"path":"src/x.py","start_line":1,"end_line":200}}
{"tool":"write_file","args":{"path":"src/x.py","content":"کل محتوای فایل"}}
{"tool":"run_command","args":{"command":"pytest -q","cwd":"."}}
فقط یک ابزار در هر پاسخ. وقتی کار تمام شد، پاسخ نهایی معمولی و فارسی بده؛ ابزار نساز.
نتیجه ابزارها معتبرند. دستورات مخرب، خارج از workspace، یا دریافت و اجرای کد ناشناس را پیشنهاد نده.
"""


@dataclass
class Pending:
    tool: str
    args: dict[str, Any]


class OllamaAgent:
    def __init__(self, settings: Settings, storage: Storage, tools: LocalTools) -> None:
        self.settings, self.storage, self.tools = settings, storage, tools

    def _model(self, messages: list[dict[str, str]]) -> str:
        try:
            response = requests.post(
                f"{self.settings.ollama_base_url}/api/chat",
                json={
                    "model": self.settings.ollama_model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.15, "num_ctx": 16384},
                },
                timeout=180,
            )
            response.raise_for_status()
            return response.json()["message"]["content"].strip()
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise RuntimeError(
                f"ارتباط با Ollama ناموفق بود ({exc}). آیا `ollama serve` و مدل {self.settings.ollama_model} آماده‌اند؟"
            ) from exc

    @staticmethod
    def _tool_call(text: str) -> Pending | None:
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if (
            isinstance(value, dict)
            and isinstance(value.get("tool"), str)
            and isinstance(value.get("args", {}), dict)
        ):
            return Pending(value["tool"], value["args"])
        return None

    def run(
        self,
        chat_id: int,
        user_text: str | None,
        progress: Callable[[str], None],
        resume: dict[str, Any] | None = None,
    ) -> tuple[str | None, Pending | None, ToolResult | None]:
        """Run up to 8 tool turns. A pending mutating action is returned to Telegram for consent."""
        if user_text:
            self.storage.add(chat_id, "user", user_text)
        if resume:
            try:
                result = self.tools.invoke(resume["tool"], resume["args"])
                self.storage.add(
                    chat_id, "user", "نتیجه ابزار " + resume["tool"] + ":\n" + result.text
                )
                progress("✅ دستور تأییدشده اجرا شد؛ در حال بررسی نتیجه…")
            except ToolError as exc:
                self.storage.add(chat_id, "user", f"خطای ابزار: {exc}")
                result = ToolResult(f"خطای ابزار: {exc}")
        else:
            result = None

        for turn in range(8):
            messages = [
                {
                    "role": "system",
                    "content": SYSTEM + f"\nWORKSPACE_ROOT: {self.settings.workspace_root}",
                }
            ] + self.storage.history(chat_id)
            progress("🧠 در حال تحلیل درخواست…" if turn == 0 else "🧠 در حال ادامهٔ بررسی…")
            answer = self._model(messages)
            call = self._tool_call(answer)
            if not call:
                self.storage.add(chat_id, "assistant", answer)
                return answer, None, result
            if (
                self.tools.requires_approval(call.tool, call.args)
                and not self.settings.auto_approve_mutations
            ):
                return None, call, result
            progress(f"🔧 اجرای ابزار: {call.tool}")
            try:
                result = self.tools.invoke(call.tool, call.args)
                self.storage.add(chat_id, "user", "نتیجه ابزار " + call.tool + ":\n" + result.text)
            except (ToolError, TypeError) as exc:
                self.storage.add(chat_id, "user", f"خطای ابزار {call.tool}: {exc}")
        final = "برای جلوگیری از اجرای بی‌پایان، پس از ۸ مرحله متوقف شدم. لطفاً نتیجهٔ فعلی را بررسی و درخواست را ادامه دهید."
        self.storage.add(chat_id, "assistant", final)
        return final, None, result
