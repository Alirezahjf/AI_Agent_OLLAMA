/* =========================================================================
 * دستیار محلی ویندوز — Web UI controller
 *
 * A single Alpine.js component. It talks to the Bridge over the WebSocket
 * at /ws and the REST API under /api/*. When the page is opened directly
 * from disk (file://) it falls back to an offline showcase mode so the UI
 * can be reviewed — and screenshotted — without a running backend.
 * ========================================================================= */

(function () {
  "use strict";

  const STORAGE_PREFS = "assistant.prefs.v1";
  const STORAGE_CONVERSATIONS = "assistant.conversations.v1";

  /* ------------------------------------------------------------ helpers */

  const uid = () =>
    (Date.now().toString(36) + Math.random().toString(36).slice(2, 8)).toUpperCase();

  const escapeHtml = (value) =>
    String(value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  const isOffline = () => location.protocol === "file:";

  /** Tool categories, in display order. */
  const ACTION_GROUPS = [
    { name: "برنامه‌ها و پنجره‌ها", icon: "🪟", match: /^(open_application|close_application|list_applications|locate_application|focus_window|maximize_window|minimize_window|move_window|list_windows|open_task_manager)/ },
    { name: "فایل و پوشه", icon: "📁", match: /^(read_file|write_file|delete_path|move_path|make_directory|list_directory|search_files|open_path)/ },
    { name: "ماوس و کیبورد", icon: "🖱️", match: /^(mouse_|drag_to|scroll|type_text|key_press|hotkey|get_mouse_position|get_screen_size|find_controls|screen_capture)/ },
    { name: "تلگرام", icon: "✈️", match: /^send_telegram/ },
    { name: "وب", icon: "🌐", match: /^(web_search|web_fetch)/ },
    { name: "سیستم", icon: "⚙️", match: /^(run_shell|system_info|list_processes|kill_process|shutdown_computer|cancel_shutdown|clipboard_)/ },
  ];

  const STATUS_LABELS = {
    pending: "در انتظار",
    running: "در حال اجرا",
    done: "انجام شد",
    error: "خطا",
  };

  const RISK_LABELS = {
    safe: "امن",
    destructive: "خطرناک",
    system: "سیستمی",
  };

  const FILE_ICONS = [
    [/\.(png|jpe?g|gif|webp|bmp|svg)$/i, "🖼️"],
    [/\.(md|markdown|txt|log)$/i, "📄"],
    [/\.(json|ya?ml|toml|ini|cfg)$/i, "🧾"],
    [/\.(py|js|ts|tsx|jsx|rs|go|java|c|cpp|cs|rb|php|sh|ps1)$/i, "💻"],
    [/\.(zip|7z|rar|tar|gz)$/i, "🗜️"],
    [/\.(mp3|wav|ogg|flac|m4a)$/i, "🎵"],
    [/\.(mp4|mkv|mov|avi|webm)$/i, "🎬"],
    [/\.pdf$/i, "📕"],
    [/\.(xlsx?|csv)$/i, "📊"],
    [/\.(docx?)$/i, "📝"],
  ];

  /* ----------------------------------------------------- markdown setup */

  function configureMarked() {
    if (typeof window.marked === "undefined") return false;
    const renderer = new window.marked.Renderer();
    renderer.code = function (code, language) {
      const text = typeof code === "object" && code !== null ? code.text : code;
      const lang = (typeof code === "object" && code !== null ? code.lang : language) || "";
      let body = escapeHtml(text);
      let label = lang || "متن";
      if (window.hljs) {
        try {
          if (lang && window.hljs.getLanguage(lang)) {
            body = window.hljs.highlight(text, { language: lang }).value;
          } else {
            const auto = window.hljs.highlightAuto(text);
            body = auto.value;
            label = auto.language || label;
          }
        } catch (_) { /* keep escaped text */ }
      }
      return (
        '<div class="code-block" dir="ltr">' +
        '<div class="code-block__head"><span>' + escapeHtml(label) + "</span>" +
        '<button type="button" class="code-block__copy" data-copy>کپی</button></div>' +
        "<pre><code class=\"hljs\">" + body + "</code></pre></div>"
      );
    };
    renderer.link = function (href, title, text) {
      const url = typeof href === "object" && href !== null ? href.href : href;
      const label = typeof href === "object" && href !== null ? (href.text || "") : text;
      return '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener noreferrer">' +
        (label || escapeHtml(url)) + "</a>";
    };
    window.marked.setOptions({ renderer, breaks: true, gfm: true, headerIds: false, mangle: false });
    return true;
  }

  /* ---------------------------------------------------------- demo data */

  function demoMessages() {
    const now = Date.now();
    return [
      {
        id: uid(), role: "user", ts: now - 96000,
        content: "یک اسکرین‌شات از دسکتاپ بگیر و فهرست پنجره‌های باز را در یک فایل Markdown ذخیره کن.",
      },
      {
        id: uid(), role: "tool", name: "screen_capture", status: "done", expanded: false,
        risk: "safe", arguments: { filename: "desktop.png" },
        output: "تصویر ذخیره شد: C:\\Users\\Ali\\workspace\\desktop.png (1920x1080)",
        artifacts: ["desktop.png"], ts: now - 88000,
      },
      {
        id: uid(), role: "tool", name: "list_windows", status: "done", expanded: false,
        risk: "safe", arguments: {},
        output: "1. Visual Studio Code — local_agent\n2. Google Chrome — GitHub\n3. Telegram Desktop\n4. Task Manager",
        artifacts: [], ts: now - 80000,
      },
      {
        id: uid(), role: "tool", name: "write_file", status: "done", expanded: false,
        risk: "destructive", arguments: { path: "windows.md", content: "# پنجره‌های باز\n..." },
        output: "فایل نوشته شد: windows.md (۴ خط)", artifacts: ["windows.md"], ts: now - 72000,
      },
      {
        id: uid(), role: "assistant", ts: now - 64000,
        content:
          "انجام شد ✅\n\n**خلاصهٔ کار:**\n\n1. یک اسکرین‌شات از صفحهٔ اصلی گرفتم (`desktop.png`).\n" +
          "2. چهار پنجرهٔ باز پیدا شد.\n3. فهرست را در `windows.md` ذخیره کردم.\n\n" +
          "اگر بخواهید همین گزارش را در تلگرام دسکتاپ هم بفرستم، کافی است بگویید:\n\n" +
          "```python\nsend_telegram_desktop(\n    chat_name=\"Saved Messages\",\n    message=open(\"windows.md\").read(),\n    verify=True,\n)\n```\n\n" +
          "> نکته: ارسال پیام یک عملیات خطرناک است و قبل از اجرا از شما تأیید می‌گیرم.",
      },
      {
        id: uid(), role: "approval", name: "send_telegram_desktop", risk: "destructive",
        arguments: { chat_name: "Saved Messages", message: "گزارش پنجره‌های باز", verify: true },
        resolved: false, approved: false, request_id: "demo", ts: now - 20000,
      },
    ];
  }

  function demoActions() {
    return [
      ["open_application", "safe", "باز کردن یک برنامهٔ ویندوزی با نام یا مسیر"],
      ["close_application", "destructive", "بستن یک برنامهٔ در حال اجرا"],
      ["list_windows", "safe", "فهرست پنجره‌های باز به همراه عنوان و موقعیت"],
      ["focus_window", "safe", "آوردن یک پنجره به جلو"],
      ["read_file", "safe", "خواندن محتوای یک فایل متنی از پوشهٔ کاری"],
      ["write_file", "destructive", "نوشتن یا بازنویسی یک فایل"],
      ["delete_path", "destructive", "حذف فایل یا پوشه"],
      ["list_directory", "safe", "فهرست فایل‌های یک پوشه"],
      ["search_files", "safe", "جستجوی فایل بر اساس الگو"],
      ["screen_capture", "safe", "گرفتن اسکرین‌شات از صفحه"],
      ["mouse_click", "destructive", "کلیک ماوس در مختصات مشخص"],
      ["type_text", "destructive", "تایپ متن با صفحه‌کلید مجازی"],
      ["hotkey", "destructive", "فشردن ترکیب کلیدها مثل ctrl+c"],
      ["send_telegram_desktop", "destructive", "ارسال پیام از تلگرام دسکتاپ شما"],
      ["web_search", "safe", "جستجو در وب و بازگرداندن نتایج"],
      ["web_fetch", "safe", "دریافت محتوای یک صفحهٔ وب"],
      ["run_shell", "destructive", "اجرای یک دستور در PowerShell"],
      ["system_info", "safe", "اطلاعات سیستم: CPU، رم، دیسک"],
      ["list_processes", "safe", "فهرست پروسه‌های در حال اجرا"],
      ["kill_process", "system", "بستن اجباری یک پروسه"],
      ["shutdown_computer", "system", "خاموش یا ری‌استارت کردن ویندوز"],
      ["clipboard_read", "safe", "خواندن محتوای کلیپ‌بورد"],
    ].map(([name, risk, description]) => ({ name, risk, description, args: [] }));
  }

  /* ------------------------------------------------------------ component */

  function assistantApp() {
    return {
      /* ---- state ---- */
      messages: [],
      draft: "",
      busy: false,
      streamingText: "",
      runId: null,

      connection: "connecting", // connecting | connected | offline | error
      theme: "dark",
      soundEnabled: true,

      historyOpen: false,
      panelOpen: false,
      settingsOpen: false,
      exportOpen: false,
      shortcutsOpen: false,
      dragging: false,
      atBottom: true,

      conversations: [],
      conversationId: null,
      conversationQuery: "",

      actions: [],
      actionQuery: "",

      models: [],
      modelsLoading: false,
      detectingProvider: false,
      billingLoading: false,
      billing: null,
      billingOpen: false,
      form: {
        provider: "ollama",
        model: "",
        openai_base_url: "",
        openai_api_key: "",
        confirm_mode: "destructive",
        autostart: false,
      },

      status: {},
      warnings: [],
      doctor: {},
      doctorOpen: false,
      doctorLoading: false,
      toasts: [],
      attachments: [],

      listening: false,
      voiceSupported: false,
      recognition: null,
      desktop: false,
      _audio: null,

      ws: null,
      reconnectDelay: 1000,
      reconnectTimer: null,

      suggestions: [
        { icon: "🖼️", title: "اسکرین‌شات بگیر", text: "یک اسکرین‌شات از صفحه بگیر و توصیفش کن" },
        { icon: "📂", title: "پوشهٔ کاری را مرتب کن", text: "فایل‌های پوشهٔ کاری را فهرست کن و بگو کدام‌ها تکراری‌اند" },
        { icon: "🪟", title: "پنجره‌های باز", text: "چه برنامه‌هایی الان باز هستند؟ فهرستشان کن" },
        { icon: "✈️", title: "پیام تلگرام", text: "در تلگرام دسکتاپ به Saved Messages پیام «تست» بفرست" },
      ],

      /* ---------------------------------------------------------- init */

      init() {
        configureMarked();
        this.loadPrefs();
        this.applyTheme();
        this.loadConversations();
        this.setupVoice();
        this.setupCopyDelegation();
        this.desktop = Boolean(window.pywebview);

        if (isOffline()) {
          this.enterOfflineShowcase();
        } else {
          this.connect();
          this.refreshStatus();
          this.refreshActions();
        }

        this.$watch("messages", () => {
          this.queueScroll();
          this.persistConversation();
        });
        window.addEventListener("beforeunload", () => this.persistConversation());
      },

      /* ------------------------------------------------- offline showcase */

      enterOfflineShowcase() {
        this.connection = "offline";
        this.actions = demoActions();
        this.messages = demoMessages();
        this.status = {
          bridge: { hostname: "WIN-DESKTOP", user: "Ali", platform: "Windows-11" },
          settings: {
            settings: {
              data_dir: "C:\\Users\\Ali\\.local_assistant",
              work_dir: "C:\\Users\\Ali\\workspace",
              llm_provider: "ollama",
              llm_model: "qwen2.5:7b",
              confirm_mode: "destructive",
            },
          },
        };
        this.form.model = "qwen2.5:7b";
        if (this.conversations.length === 0) {
          this.conversations = [
            { id: "demo-1", title: "اسکرین‌شات و گزارش پنجره‌ها", updated_at: Date.now() - 60000, message_count: 6, messages: this.messages },
            { id: "demo-2", title: "مرتب‌سازی پوشهٔ دانلود", updated_at: Date.now() - 86400000, message_count: 12, messages: [] },
            { id: "demo-3", title: "نصب و تست Ollama", updated_at: Date.now() - 3 * 86400000, message_count: 4, messages: [] },
          ];
          this.conversationId = "demo-1";
        }
      },

      /* ------------------------------------------------------- websocket */

      connect() {
        try {
          const proto = location.protocol === "https:" ? "wss" : "ws";
          this.ws = new WebSocket(proto + "://" + location.host + "/ws");
        } catch (err) {
          this.connection = "error";
          return;
        }
        this.connection = "connecting";
        this.ws.onopen = () => {
          this.connection = "connected";
          this.reconnectDelay = 1000;
          this.refreshStatus();
          this.refreshActions();
        };
        this.ws.onclose = () => {
          this.connection = "error";
          this.busy = false;
          this.scheduleReconnect();
        };
        this.ws.onerror = () => { this.connection = "error"; };
        this.ws.onmessage = (raw) => {
          let msg;
          try { msg = JSON.parse(raw.data); } catch (_) { return; }
          this.handleSocketMessage(msg);
        };
      },

      scheduleReconnect() {
        if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
        this.reconnectTimer = setTimeout(() => this.connect(), this.reconnectDelay);
        this.reconnectDelay = Math.min(this.reconnectDelay * 1.6, 15000);
      },

      handleSocketMessage(msg) {
        if (msg.type === "event") return this.handleEvent(msg);
        if (msg.type === "error") {
          this.pushNote("error", msg.message || "خطای نامشخص");
          this.busy = false;
        }
      },

      handleEvent(msg) {
        const p = msg.payload || {};
        switch (msg.event_type) {
          case "chat_started":
            this.runId = msg.run_id;
            this.busy = true;
            break;
          case "turn_started":
            break;
          case "assistant_delta":
            this.streamingText += p.text || "";
            break;
          case "assistant_final":
            this.streamingText = "";
            if (p.text) this.pushMessage({ role: "assistant", content: p.text });
            break;
          case "tool_proposed":
            this.pushMessage({
              role: "tool", name: p.name, status: "running", expanded: false,
              arguments: p.arguments || {}, output: "", artifacts: [], risk: p.risk || "safe",
              call_id: p.call_id,
            });
            break;
          case "tool_confirm_requested":
            this.pushMessage({
              role: "approval", name: p.name, risk: p.risk || "destructive",
              arguments: p.arguments || {}, request_id: p.request_id,
              resolved: false, approved: false,
            });
            this.notifyDesktop("تأیید لازم است", "دستیار می‌خواهد " + p.name + " را اجرا کند");
            this.beep("warn");
            break;
          case "tool_result": {
            // Prefer a call_id match so a card updates live even when the
            // same tool name runs several times in one turn; fall back to
            // the most recent running card with that name.
            const card = p.call_id
              ? this.lastToolCard(p.name, p.call_id)
              : this.lastToolCard(p.name);
            const ok = p.success !== false && !p.refused;
            if (card) {
              card.status = ok ? "done" : "error";
              card.output = p.text || "";
              card.artifacts = p.artifacts || this.detectArtifacts(p.text || "");
            } else {
              this.pushMessage({
                role: "tool", name: p.name, status: ok ? "done" : "error", expanded: false,
                arguments: {}, output: p.text || "", artifacts: [], risk: "safe",
              });
            }
            break;
          }
          case "chat_done":
            this.busy = false;
            this.runId = null;
            this.beep("done");
            this.setTaskbarProgress(0);
            break;
          case "chat_failed":
            this.busy = false;
            this.runId = null;
            this.pushNote("error", "❌ " + (p.error || p.reason || "گفتگو ناتمام ماند"));
            this.notifyDesktop("خطا", String(p.error || p.reason || "گفتگو ناتمام ماند"));
            this.beep("error");
            this.setTaskbarProgress(0);
            break;
          default:
            break;
        }
      },

      /* ----------------------------------------------------------- chat */

      send() {
        const text = this.draft.trim();
        const files = this.attachments.map((a) => a.name);
        if (!text && files.length === 0) return;
        const full = files.length
          ? (text ? text + "\n\n" : "") + "فایل‌های پیوست: " + files.join("، ")
          : text;

        this.pushMessage({ role: "user", content: full });
        this.draft = "";
        this.attachments = [];
        if (this.$refs.input) this.$refs.input.style.height = "auto";

        if (this.connection === "offline") {
          this.simulateReply(full);
          return;
        }
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
          this.pushNote("error", "اتصال به Bridge برقرار نیست. در حال تلاش دوباره…");
          this.scheduleReconnect();
          return;
        }
        this.busy = true;
        this.setTaskbarProgress(-1);
        this.ws.send(JSON.stringify({ type: "chat", message: full }));
      },

      simulateReply(text) {
        this.busy = true;
        setTimeout(() => {
          this.busy = false;
          this.pushMessage({
            role: "assistant",
            content:
              "این نمایش آفلاین رابط کاربری است، بنابراین پیام شما به مدل ارسال نشد.\n\n" +
              "> «" + text.slice(0, 120) + "»\n\n" +
              "برای گفتگوی واقعی، Bridge را اجرا کنید:\n\n```powershell\npython local_agent_setup.py web\n```",
          });
          this.beep("done");
        }, 900);
      },

      resend(text) {
        this.draft = text;
        this.send();
      },

      useSuggestion(text) {
        this.draft = text;
        this.$nextTick(() => this.$refs.input && this.$refs.input.focus());
      },

      interrupt() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN && this.runId) {
          this.ws.send(JSON.stringify({ type: "interrupt", run_id: this.runId }));
        }
        this.busy = false;
        this.setTaskbarProgress(0);
        this.pushNote("system", "⏹️ اجرا متوقف شد");
      },

      respondApproval(message, approved) {
        message.resolved = true;
        message.approved = approved;
        if (this.connection === "offline") return;
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({
            type: "confirm", request_id: message.request_id, approved: approved,
          }));
        }
      },

      /* -------------------------------------------------------- messages */

      pushMessage(payload) {
        const message = Object.assign({ id: uid(), ts: Date.now() }, payload);
        this.messages.push(message);
        this.queueScroll();
        return message;
      },

      pushNote(role, content) {
        this.pushMessage({ role: role === "error" ? "error" : "system", content });
      },

      lastToolCard(name, callId) {
        for (let i = this.messages.length - 1; i >= 0; i -= 1) {
          const m = this.messages[i];
          if (m.role !== "tool" || m.name !== name || m.status !== "running") continue;
          if (callId && m.call_id && m.call_id === callId) return m;
          if (!callId) return m;
        }
        // call_id requested but not found on a running card — fall back to name
        if (callId) {
          for (let i = this.messages.length - 1; i >= 0; i -= 1) {
            const m = this.messages[i];
            if (m.role === "tool" && m.name === name && m.status === "running") return m;
          }
        }
        return null;
      },

      detectArtifacts(text) {
        // Match file tokens (relative or absolute Windows/POSIX paths). The
        // backend normally sends structured artifacts; this is a safe fallback
        // for older stored conversations.
        const matches = String(text).match(/[^\s"'<>]+\.(?:png|jpe?g|gif|webp|bmp|md|txt|json|csv|log|pdf|zip)/gi);
        const out = [];
        for (const m of (matches || [])) {
          const clean = String(m).replace(/[,;:)\]}]+$/g, "").trim();
          if (clean && out.indexOf(clean) === -1) out.push(clean);
          if (out.length >= 6) break;
        }
        return out;
      },

      renderMarkdown(text) {
        const value = String(text == null ? "" : text);
        if (window.marked && window.marked.parse) {
          try { return window.marked.parse(value); } catch (_) { /* fall through */ }
        }
        return "<p>" + escapeHtml(value).replace(/\n/g, "<br>") + "</p>";
      },

      pretty(value) {
        try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
      },

      statusLabel(status) { return STATUS_LABELS[status] || status; },
      riskLabel(risk) { return RISK_LABELS[risk] || risk || "نامشخص"; },

      /* ------------------------------------------------------------ REST */

      async api(path, options) {
        const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      },

      async refreshStatus() {
        if (this.connection === "offline") return;
        try {
          const data = await this.api("/api/status");
          this.status = data || {};
          this.warnings = (data && data.settings && data.settings.warnings) || [];
          const s = (data && data.settings && data.settings.settings) || {};
          this.form.provider = s.llm_provider || this.form.provider;
          this.form.model = s.llm_model || this.form.model;
          this.form.confirm_mode = s.confirm_mode || this.form.confirm_mode;
          if (s.openai_base_url) this.form.openai_base_url = s.openai_base_url;
        } catch (_) { /* keep previous values */ }
      },

      async refreshActions() {
        if (this.connection === "offline") return;
        try {
          this.actions = await this.api("/api/actions/detail");
        } catch (_) {
          try {
            const raw = await this.api("/api/actions");
            this.actions = (raw || []).map((line) => this.parseActionLine(line));
          } catch (__) { /* leave empty */ }
        }
      },

      parseActionLine(line) {
        const text = String(line);
        const name = (text.split(/\s+/)[0] || "").trim();
        const riskMatch = text.match(/\[risk=([^\]]+)\]/);
        const description = text.replace(/^\S+\s+\[risk=[^\]]+\]\s+args=\([^)]*\)\s*/, "").trim();
        return { name, risk: riskMatch ? riskMatch[1] : "safe", description, args: [] };
      },

      async refreshModels() {
        if (this.connection === "offline") {
          this.models = ["qwen2.5:7b", "llama3.1:8b", "gpt-4o-mini", "claude-sonnet-4"];
          return;
        }
        this.modelsLoading = true;
        try {
          this.models = await this.api("/api/models");
          this.toast("ok", "✅", "فهرست مدل‌ها به‌روزرسانی شد");
        } catch (_) {
          this.toast("bad", "⚠️", "دریافت فهرست مدل‌ها ناموفق بود");
        } finally {
          this.modelsLoading = false;
        }
      },

      get doctorVerdict() {
        return { ok: "همه‌چیز سالم است", warn: "قابل استفاده، با چند هشدار", fail: "نیاز به رفع اشکال" }[this.doctor.status] || "";
      },

      openDoctor() {
        this.doctorOpen = true;
        if (!this.doctor.results) this.runDoctor();
      },

      async runDoctor() {
        if (this.connection === "offline") {
          this.toast("info", "ℹ️", "در حالت نمایش آفلاین بررسی سلامت در دسترس نیست");
          return;
        }
        this.doctorLoading = true;
        try {
          this.doctor = await this.api("/api/doctor");
        } catch (_) {
          this.toast("bad", "❌", "بررسی سلامت ناموفق بود");
        } finally {
          this.doctorLoading = false;
        }
      },

      copyDoctor() {
        const lines = (this.doctor.results || []).map((r) => {
          const icon = r.status === "ok" ? "OK  " : (r.status === "warn" ? "WARN" : "FAIL");
          return `[${icon}] ${r.title} — ${r.detail}${r.hint ? "\n        ↳ " + r.hint : ""}`;
        });
        const text = "بررسی سلامت دستیار محلی\n" + lines.join("\n") + "\n" + (this.doctor.summary || "");
        try {
          navigator.clipboard.writeText(text);
          this.toast("ok", "✅", "گزارش کپی شد");
        } catch (_) {
          this.toast("bad", "❌", "کپی ناموفق بود");
        }
      },

      useAvalai() {
        this.form.provider = "openai_compatible";
        this.form.openai_base_url = this.form.openai_base_url || "https://api.avalai.ir/v1";
        if (!this.form.model || this.form.model.indexOf(":") !== -1) this.form.model = "gpt-4o-mini";
        this.toast("info", "ℹ️", "کلید API خود را وارد کنید و ذخیره بزنید");
      },

      async autoDetectProvider() {
        if (this.connection === "offline") {
          this.toast("info", "ℹ️", "در حالت نمایش آفلاین تشخیص خودکار در دسترس نیست");
          return;
        }
        this.detectingProvider = true;
        try {
          const result = await this.api("/api/provider/detect", {
            method: "POST",
            body: JSON.stringify({
              base_url: this.form.openai_base_url || "",
              api_key: this.form.openai_api_key || "",
            }),
          });
          this.form.provider = "openai_compatible";
          if (result.base_url) this.form.openai_base_url = result.base_url;
          this.form.model = (result.models && result.models[0]) || this.form.model;
          if (result.models && result.models.length) this.models = result.models;
          if (result.valid) {
            this.toast("ok", "✅", "ارائه‌دهنده تشخیص داده شد: " + (result.label || result.provider));
          } else {
            this.toast("bad", "⚠️", "تشخیص خودکار ناموفق بود — " + (result.error || "کلید یا آدرس معتبر نیست"));
          }
        } catch (_) {
          this.toast("bad", "❌", "تشخیص خودکار ناموفق بود");
        } finally {
          this.detectingProvider = false;
        }
      },

      openSettings() {
        this.settingsOpen = true;
        if (this.models.length === 0) this.refreshModels();
      },

      /* ------------------------------------------------ billing / tokens */

      async openBilling() {
        this.billingOpen = true;
        this.loadBilling();
      },

      async loadBilling() {
        if (this.connection === "offline") {
          this.billing = { available: false, error: "در حالت نمایش آفلاین در دسترس نیست" };
          return;
        }
        this.billingLoading = true;
        try {
          this.billing = await this.api("/api/billing");
        } catch (_) {
          this.billing = { available: false, error: "دریافت اطلاعات مالی ناموفق بود" };
        } finally {
          this.billingLoading = false;
        }
      },

      formatAmount(value) {
        if (value === null || value === undefined || value === "") return "—";
        const num = Number(value);
        if (!Number.isFinite(num)) return String(value);
        try {
          return new Intl.NumberFormat("fa-IR").format(num);
        } catch (_) {
          return String(num);
        }
      },

      formatExpiry(value) {
        if (!value) return "—";
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return String(value);
        try {
          return new Intl.DateTimeFormat("fa-IR", { year: "numeric", month: "long", day: "numeric" }).format(d);
        } catch (_) {
          return String(value);
        }
      },

      formatIsoTime(value) {
        // Short fa-IR timestamp for ISO strings (transactions, fetched_at).
        if (!value) return "—";
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return String(value);
        try {
          return new Intl.DateTimeFormat("fa-IR", {
            month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
          }).format(d);
        } catch (_) {
          return String(value);
        }
      },

      formatFineAmount(value) {
        // Tiny unit costs (0.0146689) need fraction digits, unlike IRT.
        if (value === null || value === undefined || value === "") return "—";
        const num = Number(value);
        if (!Number.isFinite(num)) return String(value);
        try {
          return new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 6 }).format(num);
        } catch (_) {
          return String(num);
        }
      },

      billingCreditSources() {
        const b = this.billing || {};
        return [...(b.packages || []), ...(b.grants || [])];
      },

      billingTransactions() {
        return (this.billing && this.billing.transactions) || [];
      },

      async saveSettings() {
        if (this.connection === "offline") {
          this.settingsOpen = false;
          this.toast("info", "ℹ️", "در حالت نمایش آفلاین تنظیمات ذخیره نمی‌شود");
          return;
        }
        try {
          const result = await this.api("/api/settings", {
            method: "POST",
            body: JSON.stringify(this.form),
          });
          this.settingsOpen = false;
          this.toast("ok", "✅", "تنظیمات ذخیره شد" + (result && result.model ? " — " + result.model : ""));
          this.refreshStatus();
        } catch (_) {
          this.toast("bad", "❌", "ذخیرهٔ تنظیمات ناموفق بود");
        }
      },

      async setAutostart() {
        try {
          if (window.pywebview && window.pywebview.api && window.pywebview.api.set_autostart) {
            await window.pywebview.api.set_autostart(this.form.autostart);
            this.toast("ok", "✅", this.form.autostart ? "اجرای خودکار فعال شد" : "اجرای خودکار غیرفعال شد");
          }
        } catch (_) {
          this.toast("bad", "❌", "تنظیم اجرای خودکار ناموفق بود");
        }
      },

      async runQuickAction(name, args) {
        if (this.connection === "offline") {
          this.pushMessage({
            role: "tool", name, status: "done", expanded: true, risk: "safe",
            arguments: args, output: "(نمایش آفلاین — عملیات واقعی اجرا نشد)", artifacts: [],
          });
          return;
        }
        const card = this.pushMessage({
          role: "tool", name, status: "running", expanded: true, risk: "safe",
          arguments: args, output: "", artifacts: [],
        });
        try {
          const result = await this.api("/api/invoke", {
            method: "POST",
            body: JSON.stringify({ name, arguments: args, auto_confirm: true }),
          });
          card.status = result.success ? "done" : "error";
          card.output = result.text || result.error || "";
          card.artifacts = result.artifacts || this.detectArtifacts(card.output);
        } catch (err) {
          card.status = "error";
          card.output = String(err);
        }
      },

      async clearMemory() {
        this.messages = [];
        if (this.connection !== "offline") {
          try { await this.api("/api/clear", { method: "POST" }); } catch (_) { /* ignore */ }
        }
        this.toast("ok", "🧹", "حافظهٔ گفتگو پاک شد");
      },

      /* --------------------------------------------------- conversations */

      loadConversations() {
        try {
          const raw = localStorage.getItem(STORAGE_CONVERSATIONS);
          const parsed = raw ? JSON.parse(raw) : [];
          this.conversations = Array.isArray(parsed) ? parsed : [];
        } catch (_) { this.conversations = []; }
        if (this.conversations.length && !isOffline()) {
          const first = this.conversations[0];
          this.conversationId = first.id;
          this.messages = first.messages || [];
        }
      },

      persistConversation() {
        if (isOffline()) return;
        if (this.messages.length === 0) return;
        const title = this.conversationTitle();
        const record = {
          id: this.conversationId || (this.conversationId = uid()),
          title,
          updated_at: Date.now(),
          message_count: this.messages.length,
          messages: this.messages.slice(-200),
        };
        const index = this.conversations.findIndex((c) => c.id === record.id);
        if (index >= 0) this.conversations.splice(index, 1, record);
        else this.conversations.unshift(record);
        this.conversations = this.conversations
          .sort((a, b) => b.updated_at - a.updated_at)
          .slice(0, 40);
        try {
          localStorage.setItem(STORAGE_CONVERSATIONS, JSON.stringify(this.conversations));
        } catch (_) { /* quota */ }
      },

      conversationTitle() {
        const first = this.messages.find((m) => m.role === "user");
        if (!first) return "گفتگوی بدون عنوان";
        const text = String(first.content).replace(/\s+/g, " ").trim();
        return text.length > 42 ? text.slice(0, 42) + "…" : text;
      },

      newConversation() {
        this.persistConversation();
        this.conversationId = uid();
        this.messages = [];
        this.historyOpen = false;
        if (this.connection !== "offline") {
          this.api("/api/clear", { method: "POST" }).catch(() => {});
        }
      },

      openConversation(id) {
        this.persistConversation();
        const found = this.conversations.find((c) => c.id === id);
        if (!found) return;
        this.conversationId = id;
        this.messages = found.messages || [];
        if (window.innerWidth < 860) this.historyOpen = false;
        this.queueScroll();
      },

      deleteConversation(id) {
        this.conversations = this.conversations.filter((c) => c.id !== id);
        try {
          localStorage.setItem(STORAGE_CONVERSATIONS, JSON.stringify(this.conversations));
        } catch (_) { /* ignore */ }
        if (this.conversationId === id) {
          this.conversationId = null;
          this.messages = [];
        }
      },

      get filteredConversations() {
        const q = this.conversationQuery.trim().toLowerCase();
        if (!q) return this.conversations;
        return this.conversations.filter((c) => String(c.title).toLowerCase().includes(q));
      },

      /* ---------------------------------------------------------- export */

      exportConversation(format) {
        const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
        let blob;
        let filename;
        if (format === "json") {
          filename = "conversation-" + stamp + ".json";
          blob = new Blob([JSON.stringify({
            exported_at: new Date().toISOString(),
            title: this.conversationTitle(),
            model: this.model,
            workspace: this.workspace,
            messages: this.messages,
          }, null, 2)], { type: "application/json" });
        } else {
          filename = "conversation-" + stamp + ".md";
          blob = new Blob([this.toMarkdown()], { type: "text/markdown" });
        }
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        this.exportOpen = false;
        this.toast("ok", "💾", "فایل " + filename + " ذخیره شد");
      },

      toMarkdown() {
        const lines = [
          "# " + this.conversationTitle(),
          "",
          "- تاریخ: " + new Date().toLocaleString("fa-IR"),
          "- مدل: " + this.model,
          "- پوشهٔ کاری: `" + this.workspace + "`",
          "",
          "---",
          "",
        ];
        for (const m of this.messages) {
          if (m.role === "user") lines.push("## 👤 شما", "", m.content, "");
          else if (m.role === "assistant") lines.push("## 🤖 دستیار", "", m.content, "");
          else if (m.role === "tool") {
            lines.push("### 🔧 " + m.name + " — " + this.statusLabel(m.status), "");
            lines.push("```json", this.pretty(m.arguments), "```", "");
            if (m.output) lines.push("```", String(m.output), "```", "");
          } else if (m.role === "approval") {
            lines.push("### ⚠️ تأیید: " + m.name + " — " + (m.resolved ? (m.approved ? "تأیید شد" : "لغو شد") : "بی‌پاسخ"), "");
          } else if (m.content) {
            lines.push("> " + m.content, "");
          }
        }
        return lines.join("\n");
      },

      /* ----------------------------------------------------------- files */

      onDrop(event) {
        this.dragging = false;
        const files = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
        if (files.length) this.acceptFiles(files);
      },

      onFilePicked(event) {
        const files = Array.from(event.target.files || []);
        if (files.length) this.acceptFiles(files);
        event.target.value = "";
      },

      acceptFiles(files) {
        for (const file of files) {
          this.attachments.push({ name: file.name, size: file.size });
          if (this.connection !== "offline") this.uploadFile(file);
        }
        this.toast("ok", "📎", files.length + " فایل پیوست شد");
      },

      uploadFile(file) {
        const reader = new FileReader();
        reader.onload = () => {
          const base64 = String(reader.result).split(",")[1] || "";
          this.api("/api/upload", {
            method: "POST",
            body: JSON.stringify({ name: file.name, content_base64: base64 }),
          }).catch(() => this.toast("bad", "⚠️", "بارگذاری " + file.name + " ناموفق بود"));
        };
        reader.readAsDataURL(file);
      },

      fileUrl(path) {
        if (this.connection === "offline") return "#";
        return "/api/file?path=" + encodeURIComponent(path);
      },

      baseName(path) {
        return String(path).split(/[\\/]/).pop() || path;
      },

      fileIcon(path) {
        const name = String(path);
        for (const [pattern, icon] of FILE_ICONS) if (pattern.test(name)) return icon;
        return "📎";
      },

      /* -------------------------------------- artifacts (screenshots & files) */

      artifactName(f) {
        if (typeof f === "object" && f !== null) return f.name || f.path || "";
        return this.baseName(f);
      },

      artifactPath(f) {
        if (typeof f === "object" && f !== null) return f.path || f.name || "";
        return String(f);
      },

      artifactUrl(f) {
        if (this.connection === "offline") return "#";
        const p = this.artifactPath(f);
        return "/api/artifact?path=" + encodeURIComponent(p);
      },

      artifactIcon(f) { return this.fileIcon(this.artifactPath(f)); },

      artifactKey(f) { return this.artifactPath(f) || this.artifactName(f) || "artifact"; },

      isImageArtifact(f) {
        if (typeof f === "object" && f !== null && f.kind) return f.kind === "image";
        return /\.(png|jpe?g|gif|webp|bmp)$/i.test(String(f));
      },

      /* ----------------------------------------------------------- voice */

      setupVoice() {
        const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        this.voiceSupported = Boolean(Recognition);
        if (!Recognition) return;
        const recognition = new Recognition();
        recognition.lang = "fa-IR";
        recognition.interimResults = true;
        recognition.continuous = false;
        recognition.onresult = (event) => {
          let text = "";
          for (let i = event.resultIndex; i < event.results.length; i += 1) {
            text += event.results[i][0].transcript;
          }
          this.draft = text;
        };
        recognition.onend = () => { this.listening = false; };
        recognition.onerror = () => {
          this.listening = false;
          this.toast("bad", "🎙️", "ورودی صوتی ناموفق بود");
        };
        this.recognition = recognition;
      },

      toggleVoice() {
        if (!this.voiceSupported) {
          this.toast("warn", "🎙️", "مرورگر شما از ورودی صوتی پشتیبانی نمی‌کند");
          return;
        }
        if (this.listening) {
          this.recognition.stop();
          this.listening = false;
        } else {
          try {
            this.recognition.start();
            this.listening = true;
          } catch (_) { this.listening = false; }
        }
      },

      /* --------------------------------------------------------- desktop */

      notifyDesktop(title, body) {
        try {
          if (window.pywebview && window.pywebview.api && window.pywebview.api.notify) {
            window.pywebview.api.notify(title, body);
            return;
          }
        } catch (_) { /* ignore */ }
        if (typeof Notification !== "undefined" && Notification.permission === "granted") {
          try { new Notification(title, { body }); } catch (_) { /* ignore */ }
        }
      },

      setTaskbarProgress(value) {
        try {
          if (window.pywebview && window.pywebview.api && window.pywebview.api.set_progress) {
            window.pywebview.api.set_progress(value);
          }
        } catch (_) { /* ignore */ }
      },

      /* ----------------------------------------------------------- sound */

      beep(kind) {
        if (!this.soundEnabled) return;
        try {
          const Ctx = window.AudioContext || window.webkitAudioContext;
          if (!Ctx) return;
          const ctx = this._audio || (this._audio = new Ctx());
          const notes = { done: [660, 880], warn: [520, 415], error: [330, 220] }[kind] || [660];
          notes.forEach((freq, index) => {
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = "sine";
            osc.frequency.value = freq;
            gain.gain.value = 0.0001;
            osc.connect(gain).connect(ctx.destination);
            const start = ctx.currentTime + index * 0.11;
            gain.gain.exponentialRampToValueAtTime(0.05, start + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.16);
            osc.start(start);
            osc.stop(start + 0.18);
          });
        } catch (_) { /* audio is a nicety */ }
      },

      /* ---------------------------------------------------------- toasts */

      toast(tone, icon, text) {
        const item = { id: uid(), tone, icon, text };
        this.toasts.push(item);
        setTimeout(() => {
          this.toasts = this.toasts.filter((t) => t.id !== item.id);
        }, 3600);
      },

      copyText(text) {
        const value = String(text);
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(value)
            .then(() => this.toast("ok", "📋", "کپی شد"))
            .catch(() => this.toast("bad", "❌", "کپی ناموفق بود"));
          return;
        }
        const area = document.createElement("textarea");
        area.value = value;
        document.body.appendChild(area);
        area.select();
        try { document.execCommand("copy"); this.toast("ok", "📋", "کپی شد"); } catch (_) { /* ignore */ }
        area.remove();
      },

      setupCopyDelegation() {
        document.addEventListener("click", (event) => {
          const button = event.target.closest && event.target.closest("[data-copy]");
          if (!button) return;
          const block = button.closest(".code-block");
          const code = block && block.querySelector("code");
          if (code) this.copyText(code.innerText);
        });
      },

      /* ------------------------------------------------------ preferences */

      loadPrefs() {
        let prefs = {};
        try { prefs = JSON.parse(localStorage.getItem(STORAGE_PREFS) || "{}"); } catch (_) { prefs = {}; }
        this.theme = prefs.theme === "light" ? "light" : "dark";
        this.soundEnabled = prefs.soundEnabled !== false;
        this.historyOpen = Boolean(prefs.historyOpen) && window.innerWidth >= 1180;
      },

      persistPrefs() {
        try {
          localStorage.setItem(STORAGE_PREFS, JSON.stringify({
            theme: this.theme,
            soundEnabled: this.soundEnabled,
            historyOpen: this.historyOpen,
          }));
        } catch (_) { /* ignore */ }
      },

      applyTheme() {
        document.documentElement.setAttribute("data-theme", this.theme);
        const dark = document.getElementById("hljs-dark-theme");
        const light = document.getElementById("hljs-light-theme");
        if (dark) dark.disabled = this.theme !== "dark";
        if (light) light.disabled = this.theme !== "light";
        const meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.setAttribute("content", this.theme === "dark" ? "#070b18" : "#f4f6fd");
      },

      toggleTheme() {
        this.theme = this.theme === "dark" ? "light" : "dark";
        this.applyTheme();
        this.persistPrefs();
      },

      /* ------------------------------------------------------- shortcuts */

      onComposerKey(event) {
        if (event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
          event.preventDefault();
          this.send();
        }
      },

      onGlobalKey(event) {
        const ctrl = event.ctrlKey || event.metaKey;
        if (ctrl && event.key === "Enter") { event.preventDefault(); this.send(); return; }
        if (ctrl && event.key.toLowerCase() === "l") { event.preventDefault(); this.messages = []; return; }
        if (ctrl && event.key.toLowerCase() === "k") { event.preventDefault(); this.historyOpen = !this.historyOpen; this.persistPrefs(); return; }
        if (ctrl && event.key === ",") { event.preventDefault(); this.openSettings(); return; }
        if (ctrl && event.key === "/") { event.preventDefault(); this.shortcutsOpen = !this.shortcutsOpen; return; }
        if (event.key === "Escape") {
          if (this.settingsOpen || this.exportOpen || this.shortcutsOpen) {
            this.settingsOpen = this.exportOpen = this.shortcutsOpen = false;
          } else if (this.busy) {
            this.interrupt();
          }
          return;
        }
        const typing = ["INPUT", "TEXTAREA", "SELECT"].includes((event.target && event.target.tagName) || "");
        if (!typing && (event.key === "y" || event.key === "n")) {
          const pending = this.messages.filter((m) => m.role === "approval" && !m.resolved).pop();
          if (pending) this.respondApproval(pending, event.key === "y");
        }
      },

      /* --------------------------------------------------------- scrolling */

      autoGrow(event) {
        const el = event.target;
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 200) + "px";
      },

      onScroll(event) {
        const el = event.target;
        this.atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
      },

      queueScroll() {
        this.$nextTick(() => { if (this.atBottom) this.scrollToBottom(false); });
      },

      scrollToBottom(smooth) {
        const el = this.$refs.chatScroll;
        if (!el) return;
        el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
        this.atBottom = true;
      },

      /* ------------------------------------------------------- formatting */

      formatTime(ts) {
        try {
          return new Intl.DateTimeFormat("fa-IR", { hour: "2-digit", minute: "2-digit" }).format(new Date(ts));
        } catch (_) { return ""; }
      },

      formatDate(ts) {
        const diff = Date.now() - Number(ts || 0);
        if (diff < 60000) return "همین حالا";
        if (diff < 3600000) return Math.floor(diff / 60000) + " دقیقه پیش";
        if (diff < 86400000) return Math.floor(diff / 3600000) + " ساعت پیش";
        if (diff < 7 * 86400000) return Math.floor(diff / 86400000) + " روز پیش";
        try {
          return new Intl.DateTimeFormat("fa-IR", { month: "short", day: "numeric" }).format(new Date(ts));
        } catch (_) { return ""; }
      },

      /* ---------------------------------------------------- derived state */

      get settingsBlock() {
        return (this.status && this.status.settings && this.status.settings.settings) || {};
      },

      get workspace() {
        return this.settingsBlock.work_dir || "—";
      },

      get workspaceLabel() {
        const path = this.workspace;
        if (!path || path === "—") return "بدون پوشهٔ کاری";
        const parts = String(path).split(/[\\/]/).filter(Boolean);
        return parts.length > 2 ? "…\\" + parts.slice(-2).join("\\") : path;
      },

      get model() {
        return this.settingsBlock.llm_model || this.form.model || "بدون مدل";
      },

      get providerLabel() {
        const provider = this.settingsBlock.llm_provider || this.form.provider;
        return { ollama: "Ollama (محلی)", openai_compatible: "سازگار با OpenAI", auto: "خودکار" }[provider] || provider || "—";
      },

      get confirmModeLabel() {
        const mode = this.settingsBlock.confirm_mode || this.form.confirm_mode;
        return { destructive: "فقط کارهای خطرناک", always: "همیشه بپرس", never: "بدون تأیید" }[mode] || mode;
      },

      get hostname() {
        const bridge = (this.status && this.status.bridge) || {};
        return bridge.hostname || (this.connection === "offline" ? "WIN-DESKTOP" : "—");
      },

      get connectionTone() {
        return { connected: "ok", connecting: "warn", offline: "warn", error: "bad" }[this.connection] || "warn";
      },

      get connectionLabel() {
        return {
          connected: "متصل",
          connecting: "در حال اتصال…",
          offline: "نمایش آفلاین",
          error: "قطع — تلاش مجدد",
        }[this.connection] || this.connection;
      },

      get connectionTitle() {
        return this.connection === "connected"
          ? "اتصال به Bridge برقرار است"
          : "اتصال به Bridge برقرار نیست";
      },

      get actionCount() { return this.actions.length; },

      get placeholder() {
        if (this.busy) return "دستیار در حال کار است…";
        if (typeof window !== "undefined" && window.innerWidth < 860) return "پیام خود را بنویسید…";
        return "پیام خود را بنویسید…  (Enter ارسال، Shift+Enter خط جدید)";
      },

      get filteredActionGroups() {
        const q = this.actionQuery.trim().toLowerCase();
        const items = q
          ? this.actions.filter((a) =>
              a.name.toLowerCase().includes(q) || String(a.description).toLowerCase().includes(q))
          : this.actions;
        const groups = ACTION_GROUPS.map((g) => ({
          name: g.name,
          icon: g.icon,
          items: items.filter((a) => g.match.test(a.name)),
        }));
        const grouped = new Set(groups.flatMap((g) => g.items.map((a) => a.name)));
        const rest = items.filter((a) => !grouped.has(a.name));
        if (rest.length) groups.push({ name: "سایر", icon: "✨", items: rest });
        return groups.filter((g) => g.items.length > 0);
      },
    };
  }

  window.assistantApp = assistantApp;
})();
