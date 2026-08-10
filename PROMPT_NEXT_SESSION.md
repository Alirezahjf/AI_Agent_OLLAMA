# Prompt برای Session بعدی — ۶ بهبود باقی‌مانده

## Context پروژه

این پروژه یک **AI Agent Platform فارسی** به نام `AI_Agent_OLLAMA` است.
شاخه فعلی: `arena/019fecce-ai-agent-ollama`
ریپو: `https://github.com/Alirezahjf/AI_Agent_OLLAMA`

پروژه شامل **۱۰۰+ ابزار** در **۱۳ Skill** است با Skill System (model routing + action filtering)،
Analytics Engine، و یکپارچگی با GitHub, Discord, Slack, Notion, Google Calendar, Home Assistant.
همه ابزارهای AI از **AvalAI API** (`https://api.avalai.ir/v1`) استفاده می‌کنند.

### فایل‌های کلیدی
- `local_agent/core/skills.py` — SkillManager (activate/deactivate/routing/filtering)
- `local_agent/bridge/api/handlers.py` — BridgeHandlers (_chat_loop, model routing)
- `local_agent/bridge/protocol.py` — Event types
- `local_agent/actions/ai_content.py` — AI tools (AvalAI)
- `local_agent/actions/analytics_actions.py` — Analytics
- `local_agent/actions/google_calendar.py` — Calendar
- `local_agent/telegram/client.py` — Telethon wrapper
- `local_agent/web/app.py` — FastAPI endpoints
- `local_agent/web/templates/index.html` — SPA
- `local_agent/web/static/app.js` — Frontend JS

---

## ۶ مورد برای پیاده‌سازی

### ۱. STT Streaming (Real-time Audio Transcription)

**مشکل فعلی:** `speech_to_text` فقط فایل کامل قبول می‌کند. کاربر نمی‌تواند live audio stream بفرستد.

**راه‌حل:**
- یک WebSocket endpoint در `web/app.py` اضافه کن: `WS /api/stt/stream`
- Audio chunks را از client دریافت کن
- هر chunk را به AvalAI `/v1/audio/transcriptions` بفرست (اگر ساپورت می‌کند) یا buffer کن تا ۵ ثانیه جمع شود
- نتیجه partial transcription را از همان WebSocket برگردان
- Frontend: دکمه «ضبط صدا» در index.html با MediaRecorder API (browser)

**فایل‌ها:**
- `local_agent/web/app.py` — WS endpoint
- `local_agent/actions/ai_content.py` — `stream_speech` action
- `local_agent/web/templates/index.html` — UI button
- `local_agent/web/static/app.js` — MediaRecorder integration

**نکته:** AvalAI ممکن است streaming STT ساپورت نکند. در این صورت از WebSocket با chunked file upload استفاده کن (هر ۵ ثانیه یک فایل موقت بفرست).

---

### ۲. Discord Webhook Receiver (دریافت پیام از Discord)

**مشکل فعلی:** `discord.create_webhook` webhook URL می‌سازد ولی پیام‌های دریافتی را process نمی‌کند.

**راه‌حل:**
- یک HTTP endpoint در `web/app.py` اضافه کن: `POST /api/discord/webhook/{webhook_id}`
- Discord webhook payload را دریافت و parse کن
- پیام را به عنوان event `DISCORD_MESSAGE` در event bus publish کن
- Frontend: نمایش پیام‌های Discord در real-time

**فایل‌ها:**
- `local_agent/web/app.py` — POST endpoint
- `local_agent/bridge/protocol.py` — `DISCORD_MESSAGE = "discord_message"` event type
- `local_agent/actions/integrations.py` — `discord.receive_messages` action (poll-based fallback)
- `local_agent/web/templates/index.html` — Discord messages panel

**نکته:** اگر Discord signature verification لازم است، `X-Signature-Ed25519` header را با `DISCORD_PUBLIC_KEY` validate کن.

**Fallback (بدون public URL):** یک `discord.poll_messages` action بساز که هر N ثانیه از Discord API پیام‌های جدید را fetch کند (نیاز به bot token با `MESSAGE_CONTENT` intent).

---

### ۳. Calendar OAuth Token Auto-Refresh

**مشکل فعلی:** `calendar_token.json` ذخیره می‌شود ولی وقتی `access_token` expire می‌شود، error می‌دهد. `refresh_token` استفاده نمی‌شود.

**راه‌حل:**
- در `_get_calendar_token()` (google_calendar.py)، قبل از return چک کن token expired شده
- اگر expired، از `refresh_token` + `client_id` + `client_secret` استفاده کن و `POST https://oauth2.googleapis.com/token` بزن
- Token جدید را در `calendar_token.json` ذخیره کن

**کد مورد نیاز:**

```python
def _refresh_calendar_token(data_dir: Path) -> str:
    token_file = data_dir / "calendar_token.json"
    data = json.loads(token_file.read_text())
    refresh_token = data.get("refresh_token", "")
    if not refresh_token:
        raise AssistantError("refresh_token موجود نیست. دوباره calendar.connect اجرا کنید.")

    # Load client credentials
    pending = data_dir / "_cal_oauth_pending.json"
    if pending.is_file():
        creds = json.loads(pending.read_text())
    else:
        raise AssistantError("client credentials پیدا نشد.")

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    new_data = resp.json()
    new_data["refresh_token"] = refresh_token  # preserve
    token_file.write_text(json.dumps(new_data))
    return new_data["access_token"]
```

- `_get_calendar_token()` را طوری تغییر بده که اگر `GET /calendarList` با 401 شکست خورد، auto-refresh کند و دوباره تلاش کند.

**فایل‌ها:**
- `local_agent/actions/google_calendar.py` — `_get_calendar_token`, `_refresh_calendar_token`

---

### ۴. Video Generation — Async Polling

**مشکل فعلی:** `generate_video` اگر ویدیو async باشد، فقط `task_id` برمی‌گرداند. polling ندارد.

**راه‌حل:**
- یک `video.check_status` action بساز که `task_id` بگیرد و status چک کند
- AvalAI endpoint: `GET /v1/videos/{task_id}` یا مشابه
- اگر complete، ویدیو را download و save کن
- اگر pending، status برگردان

همچنین یک **background polling** در `_chat_loop`:
- وقتی `generate_video` task_id برمی‌گرداند، یک daemon thread شروع کن
- هر ۳۰ ثانیه status چک کن
- وقتی ready شد، یک `Event(type=VIDEO_READY)` publish کن

**فایل‌ها:**
- `local_agent/actions/ai_content.py` — `video_check_status` action + background poller
- `local_agent/bridge/protocol.py` — `VIDEO_READY = "video_ready"` event type
- `local_agent/web/app.py` — `GET /api/video/status/{task_id}` endpoint

---

### ۵. Skill Prompt — Rebuild System Prompt Mid-Conversation

**مشکل فعلی:** وقتی `skill_set_prompt` صدا زده می‌شود، system prompt **chat‌های فعال** آپدیت نمی‌شود. فقط chat‌های جدید prompt تازه می‌گیرند.

**راه‌حل:**
- در `SkillManager._notify()`، وقتی event_type == "prompt" است، علاوه بر `SKILLS_CHANGED`، یک event جدید `SYSTEM_PROMPT_UPDATED` بفرست
- در `_chat_loop` (handlers.py)، قبل از هر LLM call، system prompt را rebuild کن:
  - `runtime.system_prompt = _build_system_prompt(...) + skill_fragment`
- این یعنی system message در `self._build_messages(runtime)` همیشه fresh است

**کد مورد نیاز در `_chat_loop`:**

```python
# Inside the turn loop, before calling client:
if skill_mgr is not None:
    # Rebuild system prompt (picks up prompt changes mid-conversation)
    skill_fragment = skill_mgr.build_system_prompt_fragment()
    base_prompt = _build_system_prompt(
        self.registry, self.settings, is_gui_available(),
        telegram_has_clients(self), skill_manager=skill_mgr,
    )
    new_prompt = base_prompt
    if skill_fragment:
        new_prompt += "\n\n# مهارت‌های فعال\n" + skill_fragment
    runtime.system_prompt = new_prompt
```

**فایل‌ها:**
- `local_agent/bridge/api/handlers.py` — `_chat_loop` turn-level prompt rebuild
- `local_agent/core/context.py` — ensure `runtime.system_prompt` is mutable

---

### ۶. Model Routing — Multi-Skill Priority System

**مشکل فعلی:** `detect_skill_for_message` فقط **یک** skill برمی‌گرداند. اگر پیام هم "github" داشته باشد هم "calendar"، فقط اولی انتخاب می‌شود.

**راه‌حل:**
- `detect_skill_for_message` را طوری تغییر بده که **همه** matching skills را با score برگرداند
- بالاترین score را انتخاب کن
- اگر هیچ model override نبود، default model استفاده شود

**کد جدید:**

```python
def detect_skills_for_message(self, user_message: str) -> list[tuple[str, int]]:
    """Return all matching skills with their keyword match count, sorted by score."""
    msg_lower = user_message.lower()
    matches: list[tuple[str, int]] = []
    for skill in self.active_skills():
        if not skill.model_override:
            continue
        score = sum(1 for kw in skill.trigger_keywords if kw.lower() in msg_lower)
        if score > 0:
            matches.append((skill.id, score))
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches

def detect_skill_for_message(self, user_message: str) -> str | None:
    """Return the best-matching skill with a model override."""
    matches = self.detect_skills_for_message(user_message)
    return matches[0][0] if matches else None
```

**فایل‌ها:**
- `local_agent/core/skills.py` — `detect_skills_for_message` (plural), `detect_skill_for_message` (backward compat)

---

## نکات فنی

### تست
بعد از هر تغییر:
```bash
python -c "import ast; ast.parse(open('FILE').read())"  # syntax check
python -m pytest tests_local_agent/ -x -q --no-header -k "not web"  # unit tests
```

### Commit و Push
```bash
git add -A
git commit -m "feat: DESCRIPTION"
git push origin arena/019fecce-ai-agent-ollama
```

### فایل‌های جدید احتمالی
- `local_agent/actions/video_actions.py` — اگر video polling بزرگ شد

### Dependencies جدید احتمالی
- `pynacl` — برای Discord webhook signature verification
- `sounddevice` / `pyaudio` — اگر STT streaming از mic لازم شد

### ترتیب پیشنهادی پیاده‌سازی
1. **Calendar OAuth Refresh** (ساده‌ترین، ۱ فایل)
2. **Skill Prompt Rebuild** (۱ فایل، مهم)
3. **Model Routing Multi-Skill** (۱ فایل، مهم)
4. **Video Async Polling** (۲ فایل)
5. **Discord Webhook Receiver** (۳ فایل)
6. **STT Streaming** (۴ فایل، پیچیده‌ترین)
