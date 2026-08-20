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

  // Explicitly mirrors the backend's operation allow-list. Sensitive writes
  // (secret values and webhook secrets) intentionally have dedicated forms.
  const GITHUB_CONSOLE_READ_OPERATIONS = Object.freeze([
    "account", "installations", "installation_repositories", "repositories", "repository", "contents",
    "file_text", "repository_tree", "commits", "commit", "compare", "languages", "contributors", "branches",
    "branch_protection", "branch_rules", "rulesets", "ruleset", "ruleset_history", "tags", "issues", "issue",
    "issue_comments", "pulls", "pull", "pull_files", "pull_reviews", "discussion_categories", "discussions",
    "discussion", "check_runs", "check_run", "check_run_annotations", "check_suites", "check_suite",
    "check_suite_runs", "workflows", "workflow", "workflow_runs", "workflow_run", "workflow_run_jobs",
    "artifacts", "actions_secrets", "actions_variables", "organization_actions_secrets",
    "organization_actions_variables", "environment_actions_secrets", "environment_actions_variables",
    "actions_caches", "actions_cache_usage", "self_hosted_runners", "releases", "release", "deployments",
    "deployment_statuses", "environments", "collaborators", "webhooks", "webhook", "webhook_deliveries",
    "repository_codespaces", "codespaces", "codespace", "codespace_machines", "codespace_secrets", "packages",
    "package_versions", "dependabot_alerts", "code_scanning_alerts", "secret_scanning_alerts",
    "security_advisories", "organizations", "organization_repositories", "organization_members",
    "organization_runners", "organization_webhooks", "notifications", "notification_thread",
    "notification_subscription", "search", "projects", "project", "local_repositories", "local_status",
    "local_branches", "local_log", "local_remotes", "local_diff",
  ]);
  const GITHUB_CONSOLE_WRITE_OPERATIONS = Object.freeze([
    "repository_create", "repository_update", "repository_delete", "repository_transfer", "repository_topics",
    "fork", "file_upsert", "file_delete", "branch_create", "branch_delete", "branch_protection_update",
    "branch_protection_delete", "ruleset_create", "ruleset_update", "ruleset_delete", "issue_create",
    "issue_update", "issue_comment", "issue_lock", "issue_unlock", "pull_create", "pull_update", "pull_review",
    "pull_merge", "discussion_create", "discussion_update", "discussion_delete", "discussion_comment",
    "discussion_comment_update", "discussion_comment_delete", "discussion_close", "discussion_reopen",
    "check_run_create", "check_run_update", "check_run_rerequest", "check_suite_rerequest", "workflow_dispatch",
    "workflow_run_rerun", "workflow_run_cancel", "workflow_run_delete", "artifact_delete", "actions_secret_delete",
    "actions_variable_set", "actions_variable_delete", "organization_actions_secret_repositories_set",
    "organization_actions_secret_delete", "organization_actions_variable_set", "organization_actions_variable_delete",
    "environment_actions_secret_delete", "environment_actions_variable_set", "environment_actions_variable_delete",
    "workflow_enable", "workflow_disable", "actions_cache_delete", "runner_remove", "runner_labels_set",
    "release_create", "release_update", "release_delete", "release_asset_update", "release_asset_delete",
    "deployment_create", "deployment_status", "environment_update", "environment_delete", "collaborator_add",
    "collaborator_remove", "organization_membership_set", "organization_membership_remove", "notification_mark",
    "notification_subscription_set", "notification_subscription_delete", "webhook_delete", "webhook_ping",
    "webhook_redeliver", "codespace_create", "codespace_update", "codespace_start", "codespace_stop",
    "codespace_delete", "codespace_secret_repositories_set", "codespace_secret_delete", "package_version_delete",
    "package_version_restore", "dependabot_alert_update", "code_scanning_alert_update",
    "secret_scanning_alert_update", "project_create", "project_update", "project_delete", "project_add_item",
    "project_add_draft_issue", "project_update_draft_issue", "project_archive_item", "project_unarchive_item",
    "project_delete_item", "project_update_item_field", "project_clear_item_field", "project_update_item_position",
    "local_clone", "local_pull", "local_push", "local_branch_create", "local_branch_switch",
    "local_branch_delete", "local_commit", "local_tag",
  ]);

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
    { name: "تلگرام", icon: "✈️", match: /^(send_telegram|telegram\.)/ },
    { name: "GitHub", icon: "●", match: /^github\./ },
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
      purging: false,
      purgeArmed: false,
      purgeDone: false,
      purgeMessage: "",
      form: {
        provider: "ollama",
        model: "",
        openai_base_url: "",
        openai_api_key: "",
        confirm_mode: "destructive",
        work_dir: "",
        full_system_access: false,
        autostart: false,
        telegram: { enabled: false, api_id: "", api_hash: "", phone: "", confirm_send: true },
        github: {
          enabled: false, client_id: "", broker_url: "", callback_url: "",
          api_url: "https://api.github.com", web_url: "https://github.com",
          graphql_url: "https://api.github.com/graphql", selected_repositories: [],
          local_clone_root: "", allowed_origins: [],
        },
        gmail: { enabled: false, username: "", credentials_file: "", token_file: "", app_password: "", confirm_send: true },
      },
      fullAccessWanted: false,
      fullAccessArmed: false,
      elevating: false,
      elevation: "",
      telegramState: "disabled",
      telegramConnected: false,
      telegramBusy: false,
      telegramCode: "",
      telegramPassword: "",
      telegramAccounts: [],
      activeTelegramAccount: "",
      telegramTargetAccount: "",
      switchingTelegram: false,
      telegramBrowserOpen: false,
      telegramBrowserLoading: false,
      telegramBrowserError: "",
      telegramBrowserAccount: "",
      telegramBrowserQuery: "",
      telegramChatKind: "all",
      telegramChatScope: "all",
      telegramTab: "chats",
      telegramBrowserOffset: 0,
      telegramBrowserHasMore: false,
      telegramBrowserItems: [],
      telegramStats: null,
      telegramSelectedChat: null,
      telegramHistory: [],
      telegramHistoryLoading: false,
      gmailConnected: false,
      gmailBusy: false,
      githubConnected: false,
      githubBusy: false,
      githubAccount: null,
      githubRepositories: [],
      githubInstallations: [],
      githubError: "",
      githubCsrfToken: "",
      githubAllowedOriginsText: "",
      githubOperationsRepository: "",
      githubActionsEntry: {
        kind: "secret", scope: "repository", name: "", value: "", org: "", environment: "",
        visibility: "private", selected_repository_ids: "",
      },
      githubReleaseAsset: { release_id: "", label: "", file: null },
      githubWebhookDraft: {
        hook_id: "", url: "", content_type: "json", events: "push", secret: "", active: true,
      },
      githubConsoleMode: "read",
      githubConsoleOperation: "repository",
      githubConsoleParams: "{}",
      githubConsoleResult: "",
      githubConsoleLastRun: null,
      githubConsoleUseRepository: true,
      githubWorkspaceRepo: "",
      githubWorkspacePath: "",
      githubWorkspaceLoading: false,
      githubWorkspace: null,
      githubLocalPath: "",
      githubLocalResult: "",
      githubCommitMessage: "",
      githubCommitPaths: "",
      githubBranchName: "",
      githubPullRequest: { title: "", head: "", base: "main", body: "" },
      githubFileEdit: { path: "", branch: "", content: "", message: "", sha: "" },
      githubIssueDraft: { title: "", body: "", labels: "" },
      githubWorkflowDispatch: { workflow_id: "", ref: "main", inputs: "{}" },
      githubReleaseDraft: { tag_name: "", name: "", body: "", draft: true, prerelease: false },
      githubProjectOwner: "",
      githubProjectOwnerType: "user",
      githubProjectOwnerId: "",
      githubProjectLoadedOwner: "",
      githubProjectLoadedOwnerType: "",
      githubProjects: [],
      githubProjectId: "",
      githubProject: null,
      githubProjectTitle: "",
      githubNewRepository: { name: "", description: "", private: true, auto_init: true },

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
        window.addEventListener("message", (event) => {
          if (event.origin !== location.origin || !event.data || event.data.source !== "pla-github-oauth") return;
          if (event.data.ok) {
            this.githubBusy = false;
            this.refreshGitHubStatus(true);
            this.toast("ok", "✅", "حساب GitHub متصل شد");
          }
        });
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
          case "telegram_state":
            this.applyTelegramState(p.telegram || {});
            if (p.accounts) this.telegramAccounts = p.accounts;
            if (p.telegram && p.telegram.account) this.activeTelegramAccount = p.telegram.account;
            this.refreshStatus();
            break;
          case "scheduled_fired": {
            const job = p.job || {};
            const kind = job.type === "task" ? "کار زمان‌بندی‌شده" : "یادآوری";
            const text = job.message || (job.action_name ? "اجرای " + job.action_name : "");
            this.notifyDesktop("⏰ " + kind, text || p.result || "");
            this.pushNote("system", "⏰ " + kind + (text ? ": " + text : "") +
              (p.success === false ? " — خطا: " + (p.result || "") : ""));
            this.beep("done");
            break;
          }
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
          case "tool_confirm_resolved": {
            // Close the approval card once a decision is in (matches by
            // request_id so a late answer doesn't leave the card hanging).
            for (let i = this.messages.length - 1; i >= 0; i -= 1) {
              const m = this.messages[i];
              if (m.role === "approval" && m.request_id === p.request_id) {
                m.resolved = true;
                m.approved = Boolean(p.approved);
                break;
              }
            }
            break;
          }
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
        // F4: each tab's conversation is its own session so tabs never share
        // history.  The session id is the conversationId persisted locally.
        const chatMsg = { type: "chat", message: full };
        if (this.conversationId) chatMsg.session_id = this.conversationId;
        this.ws.send(JSON.stringify(chatMsg));
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
        const payload = { request_id: message.request_id, approved };
        // Prefer the live socket, but if it is closed/half-alive (the B1
        // bug: a confirm typed mid-run used to be buffered and ignored)
        // fall back to the plain HTTP endpoint so the approval is never lost.
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify(Object.assign({ type: "confirm" }, payload)));
        } else {
          this.api("/api/confirm", { method: "POST", body: JSON.stringify(payload) })
            .catch(() => this.toast("bad", "⚠️", "ثبت تأیید ناموفق بود"));
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
        if (!response.ok) {
          let detail = "HTTP " + response.status;
          try {
            const body = await response.json();
            const payload = body.detail || body;
            detail = (payload && (payload.message || payload.detail)) || (typeof payload === "string" ? payload : detail);
          } catch (_) { /* plain response */ }
          throw new Error(detail);
        }
        return response.json();
      },

      async ensureGitHubSecurity(force) {
        if (this.githubCsrfToken && !force) return this.githubCsrfToken;
        const response = await fetch("/api/github/security", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
        });
        if (!response.ok) throw new Error("ایجاد نشست امن GitHub ناموفق بود");
        const data = await response.json();
        this.githubCsrfToken = data.csrf_token || "";
        return this.githubCsrfToken;
      },

      async githubApi(path, options) {
        const csrf = await this.ensureGitHubSecurity(false);
        const opts = options || {};
        const headers = Object.assign({ "Content-Type": "application/json", "X-CSRF-Token": csrf }, opts.headers || {});
        return this.api(path, Object.assign({}, opts, { headers }));
      },

      applyGitHubStatus(state) {
        if (!state) return;
        this.githubConnected = Boolean(state.connected);
        this.githubAccount = state.account || (this.githubConnected ? this.githubAccount : null);
        if (this.githubAccount && !this.githubProjectOwner) {
          this.githubProjectOwner = this.githubAccount.login || "";
          this.githubProjectOwnerId = this.githubAccount.node_id || "";
          this.githubProjectLoadedOwner = this.githubProjectOwner;
          this.githubProjectLoadedOwnerType = "user";
        }
        if (state.error) this.githubError = String(state.error);
        if (!this.githubConnected) {
          this.githubRepositories = [];
          this.githubInstallations = [];
          this.githubProjects = [];
          this.githubProject = null;
          this.githubProjectOwnerId = "";
          this.githubProjectLoadedOwner = "";
          this.githubProjectLoadedOwnerType = "";
        }
        if (Array.isArray(state.selected_repositories)) {
          this.form.github.selected_repositories = state.selected_repositories.slice();
        }
      },

      normalizeGitHubSettings(value) {
        const defaults = {
          enabled: false, client_id: "", broker_url: "", callback_url: "",
          api_url: "https://api.github.com", web_url: "https://github.com",
          graphql_url: "https://api.github.com/graphql", selected_repositories: [],
          local_clone_root: "", allowed_origins: [],
        };
        const result = Object.assign(defaults, value || {});
        result.enabled = Boolean(result.enabled);
        result.selected_repositories = Array.isArray(result.selected_repositories) ? result.selected_repositories.slice() : [];
        result.allowed_origins = Array.isArray(result.allowed_origins) ? result.allowed_origins.slice() : [];
        return result;
      },

      async refreshGitHubStatus(verify) {
        try {
          const data = await this.githubApi("/api/github/status?verify=" + (verify ? "true" : "false"), {
            method: "POST", body: "{}",
          });
          this.applyGitHubStatus(data);
          if (data.configuration) {
            this.form.github = this.normalizeGitHubSettings(data.configuration);
            this.githubAllowedOriginsText = this.form.github.allowed_origins.join("\n");
          }
          this.githubError = data.error || "";
          if (this.githubConnected && verify) {
            await Promise.all([this.loadGitHubRepositories(), this.loadGitHubInstallations()]);
          }
          return data;
        } catch (err) {
          this.githubError = err.message;
          throw err;
        }
      },

      async saveGitHubConfiguration() {
        this.form.github.allowed_origins = String(this.githubAllowedOriginsText || "")
          .split(/[\n,]+/).map((item) => item.trim()).filter(Boolean);
        const saved = await this.saveSettings(false);
        if (!saved) throw new Error("ذخیرهٔ پیکربندی GitHub ناموفق بود");
        await this.ensureGitHubSecurity(true);
      },

      async connectGitHub() {
        if (this.connection === "offline" || this.githubBusy) return;
        this.githubBusy = true;
        this.githubError = "";
        let popup = null;
        const desktopWebview = Boolean(window.pywebview && window.pywebview.api);
        try {
          // Open synchronously to avoid popup blocking in a normal browser.
          // Desktop uses an in-webview redirect so its loopback session cookie
          // returns with the OAuth callback instead of being lost in an external browser.
          if (!desktopWebview) {
            try { popup = window.open("about:blank", "pla-github-oauth", "popup,width=720,height=780"); } catch (_) { popup = null; }
          }
          this.form.github.enabled = true;
          await this.saveGitHubConfiguration();
          const data = await this.githubApi("/api/github/oauth/start", {
            method: "POST", body: JSON.stringify({ origin: location.origin }),
          });
          if (!data.authorization_url) throw new Error("نشانی مجوز GitHub دریافت نشد");
          if (popup && !popup.closed) {
            popup.location.replace(data.authorization_url);
            const started = Date.now();
            const poll = window.setInterval(async () => {
              if (!this.githubBusy || Date.now() - started > 120000) {
                window.clearInterval(poll);
                this.githubBusy = false;
                return;
              }
              try {
                const status = await this.refreshGitHubStatus(true);
                if (status.connected) { window.clearInterval(poll); this.githubBusy = false; }
              } catch (_) { /* authorization may still be pending */ }
              if (popup.closed && this.githubBusy) { window.clearInterval(poll); this.githubBusy = false; }
            }, 1500);
          } else {
            location.assign(data.authorization_url);
          }
        } catch (err) {
          if (popup && !popup.closed) popup.close();
          this.githubBusy = false;
          this.githubError = err.message;
          this.toast("bad", "⚠️", "اتصال GitHub ناموفق بود — " + err.message);
        }
      },

      async disconnectGitHub() {
        if (!window.confirm("اتصال GitHub قطع و توکن امن باطل شود؟")) return;
        this.githubBusy = true;
        this.githubError = "";
        try {
          const data = await this.githubApi("/api/github/disconnect", { method: "POST", body: "{}" });
          this.applyGitHubStatus(data);
          this.githubAccount = null;
          this.githubRepositories = [];
          this.toast("info", "ℹ️", "اتصال GitHub قطع شد");
        } catch (err) {
          this.githubError = err.message;
        } finally { this.githubBusy = false; }
      },

      async loadGitHubRepositories() {
        if (!this.githubConnected) return;
        this.githubBusy = true;
        try {
          const data = await this.githubApi("/api/github/read", {
            method: "POST", body: JSON.stringify({ operation: "repositories", params: { limit: 500 } }),
          });
          const items = data && (data.items || data.repositories || data.result || data);
          this.githubRepositories = (Array.isArray(items) ? items : []).map((repo) => ({
            full_name: repo.full_name || ((repo.owner && repo.owner.login ? repo.owner.login + "/" : "") + (repo.name || "")),
            private: Boolean(repo.private),
            description: repo.description || "",
            language: repo.language || "",
            default_branch: repo.default_branch || "",
            open_issues_count: Number(repo.open_issues_count || 0),
            updated_at: repo.updated_at || "",
          })).filter((repo) => repo.full_name);
        } catch (err) {
          this.githubError = err.message;
        } finally { this.githubBusy = false; }
      },

      async loadGitHubInstallations() {
        if (!this.githubConnected) return;
        try {
          const data = await this.githubApi("/api/github/read", {
            method: "POST", body: JSON.stringify({ operation: "installations", params: { limit: 100 } }),
          });
          const items = data && (data.items || data.installations || data);
          this.githubInstallations = (Array.isArray(items) ? items : []).map((installation) => ({
            id: installation.id,
            account: installation.account && installation.account.login ? installation.account.login : "—",
            target_type: installation.target_type || "",
            repository_selection: installation.repository_selection || "",
          }));
        } catch (err) {
          this.githubError = err.message;
        }
      },

      githubRepositoryParams(fullName) {
        const value = fullName || this.githubWorkspaceRepo;
        const separator = String(value || "").indexOf("/");
        if (separator < 1 || separator === String(value).length - 1) {
          throw new Error("ابتدا یک مخزن انتخاب کنید");
        }
        return { owner: value.slice(0, separator), repo: value.slice(separator + 1) };
      },

      async githubRead(operation, params) {
        return this.githubApi("/api/github/read", {
          method: "POST",
          body: JSON.stringify({ operation, params: params || {} }),
        });
      },

      async githubWrite(operation, params, message) {
        if (!window.confirm(message || "اجرای این عملیات تغییردهنده در GitHub را تأیید می‌کنید؟")) return null;
        return this.githubApi("/api/github/write", {
          method: "POST",
          body: JSON.stringify({ operation, params: params || {}, confirm: true }),
        });
      },

      get githubConsoleOperations() {
        return this.githubConsoleMode === "write"
          ? GITHUB_CONSOLE_WRITE_OPERATIONS
          : GITHUB_CONSOLE_READ_OPERATIONS;
      },

      resetGitHubConsoleOperation() {
        const available = this.githubConsoleOperations;
        if (!available.includes(this.githubConsoleOperation)) {
          this.githubConsoleOperation = available[0] || "";
        }
        this.githubConsoleResult = "";
      },

      async runGitHubConsole(repeat) {
        const previous = repeat ? this.githubConsoleLastRun : null;
        const mode = previous ? previous.mode : this.githubConsoleMode;
        const available = mode === "write" ? GITHUB_CONSOLE_WRITE_OPERATIONS : GITHUB_CONSOLE_READ_OPERATIONS;
        const operation = previous ? previous.operation : String(this.githubConsoleOperation || "");
        if (!available.includes(operation)) {
          this.githubError = "عملیات کنسول در فهرست مجاز نیست";
          return;
        }
        let params;
        try {
          params = previous
            ? Object.assign({}, previous.params)
            : JSON.parse(String(this.githubConsoleParams || "{}"));
        } catch (_) { this.githubError = "پارامترهای کنسول باید JSON معتبر باشند"; return; }
        if (!params || Array.isArray(params) || typeof params !== "object") {
          this.githubError = "پارامترهای کنسول باید یک شیء JSON باشند";
          return;
        }
        if (this.githubConsoleUseRepository && this.githubOperationsRepository) {
          let selected;
          try { selected = this.githubRepositoryParams(this.githubOperationsRepository); }
          catch (err) { this.githubError = err.message; return; }
          params.owner = selected.owner;
          params.repo = selected.repo;
        }
        this.githubBusy = true;
        this.githubError = "";
        try {
          const result = mode === "write"
            ? await this.githubWrite(operation, params, "اجرای عملیات «" + operation + "» با این پارامترها را تأیید می‌کنید؟")
            : await this.githubRead(operation, params);
          if (result === null) return;
          this.githubConsoleResult = JSON.stringify(result, null, 2);
          this.githubConsoleLastRun = { mode, operation, params: Object.assign({}, params) };
          if (mode === "write") {
            await this.refreshGitHubStatus(false);
            if (params.owner && params.repo && this.githubWorkspaceRepo === params.owner + "/" + params.repo) {
              await this.inspectGitHubRepository();
            }
          }
          this.toast("ok", "✅", "عملیات GitHub انجام شد");
        } catch (err) {
          this.githubError = err.message;
          this.githubConsoleResult = JSON.stringify({ error: err.message }, null, 2);
        } finally { this.githubBusy = false; }
      },

      async inspectGitHubRepository(fullName) {
        if (fullName) this.githubWorkspaceRepo = fullName;
        let repo;
        try { repo = this.githubRepositoryParams(); } catch (err) { this.githubError = err.message; return; }
        this.githubWorkspaceLoading = true;
        this.githubError = "";
        try {
          const path = String(this.githubWorkspacePath || "").replace(/^\/+|\/+$/g, "");
          const jobs = {
            repository: this.githubRead("repository", repo),
            contents: this.githubRead("contents", Object.assign({}, repo, { path })),
            commits: this.githubRead("commits", Object.assign({}, repo, { limit: 15 })),
            branches: this.githubRead("branches", Object.assign({}, repo, { limit: 100 })),
            issues: this.githubRead("issues", Object.assign({}, repo, { state: "open", limit: 20 })),
            pulls: this.githubRead("pulls", Object.assign({}, repo, { state: "open", limit: 20 })),
            languages: this.githubRead("languages", repo),
            workflows: this.githubRead("workflows", Object.assign({}, repo, { limit: 50 })),
          };
          const keys = Object.keys(jobs);
          const settled = await Promise.allSettled(keys.map((key) => jobs[key]));
          const workspace = { errors: {} };
          settled.forEach((result, index) => {
            const key = keys[index];
            if (result.status === "fulfilled") workspace[key] = result.value;
            else workspace.errors[key] = result.reason && result.reason.message ? result.reason.message : "خطای ناشناخته";
          });
          if (workspace.contents && workspace.contents.type === "file") {
            try {
              workspace.file = await this.githubRead("file_text", Object.assign({}, repo, { path, max_bytes: 524288 }));
              this.githubFileEdit.path = workspace.file.path || path;
              this.githubFileEdit.content = workspace.file.text || "";
              this.githubFileEdit.sha = workspace.file.sha || "";
            } catch (err) { workspace.errors.file = err.message; }
          }
          this.githubWorkspace = workspace;
        } catch (err) {
          this.githubError = err.message;
        } finally { this.githubWorkspaceLoading = false; }
      },

      async inspectCompleteGitHubTree() {
        let repo;
        try { repo = this.githubRepositoryParams(); } catch (err) { this.githubError = err.message; return; }
        this.githubWorkspaceLoading = true;
        try {
          const tree = await this.githubRead("repository_tree", Object.assign({}, repo, { limit: 2000 }));
          this.githubWorkspace = Object.assign({}, this.githubWorkspace || {}, { tree });
        } catch (err) { this.githubError = err.message; }
        finally { this.githubWorkspaceLoading = false; }
      },

      openGitHubWorkspacePath(path, type) {
        this.githubWorkspacePath = path || "";
        if (type === "dir" || type === "file") this.inspectGitHubRepository();
      },

      async createGitHubRepository() {
        const draft = this.githubNewRepository;
        const name = String(draft.name || "").trim();
        if (!/^[A-Za-z0-9_.-]{1,100}$/.test(name) || name === "." || name === "..") {
          this.githubError = "نام مخزن نامعتبر است";
          return;
        }
        this.githubBusy = true;
        this.githubError = "";
        try {
          const created = await this.githubWrite(
            "repository_create",
            { name, description: String(draft.description || "").trim(), private: Boolean(draft.private), auto_init: Boolean(draft.auto_init) },
            "ساخت مخزن جدید «" + name + "» در GitHub را تأیید می‌کنید؟",
          );
          if (!created) return;
          const fullName = created.full_name || ((created.owner && created.owner.login ? created.owner.login + "/" : "") + (created.name || name));
          if (fullName.indexOf("/") > 0) {
            const selected = new Set(this.form.github.selected_repositories || []);
            selected.add(fullName);
            this.form.github.selected_repositories = Array.from(selected);
            await this.saveGitHubConfiguration();
            this.githubWorkspaceRepo = fullName;
          }
          draft.name = "";
          draft.description = "";
          await this.loadGitHubRepositories();
          if (this.githubWorkspaceRepo) await this.inspectGitHubRepository();
          this.toast("ok", "✅", "مخزن GitHub ساخته شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async runGitHubLocal(operation) {
        let repo;
        try { repo = this.githubRepositoryParams(); } catch (err) { this.githubError = err.message; return; }
        const path = String(this.githubLocalPath || "").trim();
        const params = operation === "local_clone" ? Object.assign({}, repo, path ? { destination: path } : {}) : { path };
        if (operation !== "local_clone" && !path) { this.githubError = "مسیر clone محلی را وارد کنید"; return; }
        const labels = { local_clone: "Clone", local_pull: "Pull", local_push: "Push" };
        this.githubBusy = true;
        this.githubError = "";
        try {
          const result = await this.githubWrite(operation, params, labels[operation] + " مخزن را تأیید می‌کنید؟");
          if (!result) return;
          if (result.path) this.githubLocalPath = result.path;
          this.githubLocalResult = JSON.stringify(result, null, 2);
          this.toast("ok", "✅", labels[operation] + " با موفقیت انجام شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async inspectGitHubLocal(operation) {
        const allowedOperations = ["local_repositories", "local_status", "local_branches", "local_log", "local_remotes", "local_diff"];
        const selected = operation || "local_status";
        if (!allowedOperations.includes(selected)) { this.githubError = "عملیات بررسی محلی نامعتبر است"; return; }
        const path = String(this.githubLocalPath || "").trim();
        if (selected !== "local_repositories" && !path) { this.githubError = "مسیر clone محلی را وارد کنید"; return; }
        this.githubBusy = true;
        this.githubError = "";
        try {
          const params = selected === "local_repositories" ? {} : { path };
          if (selected === "local_log") params.limit = 50;
          const result = await this.githubApi("/api/github/read", {
            method: "POST",
            body: JSON.stringify({ operation: selected, params }),
          });
          this.githubLocalResult = JSON.stringify(result, null, 2);
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async commitGitHubLocal() {
        const path = String(this.githubLocalPath || "").trim();
        const message = String(this.githubCommitMessage || "").trim();
        if (!path || !message) { this.githubError = "مسیر clone و پیام Commit الزامی است"; return; }
        const paths = String(this.githubCommitPaths || "").split(/[\n,]+/).map((item) => item.trim()).filter(Boolean);
        const params = { path, message, all_tracked: paths.length === 0 };
        if (paths.length) params.paths = paths;
        this.githubBusy = true;
        try {
          const result = await this.githubWrite("local_commit", params, "ثبت Commit محلی با پیام «" + message + "» را تأیید می‌کنید؟");
          if (!result) return;
          this.githubLocalResult = JSON.stringify(result, null, 2);
          this.githubCommitMessage = "";
          this.toast("ok", "✅", "Commit ثبت شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async changeGitHubLocalBranch(operation) {
        const allowedOperations = ["local_branch_create", "local_branch_switch"];
        if (!allowedOperations.includes(operation)) { this.githubError = "عملیات Branch نامعتبر است"; return; }
        const path = String(this.githubLocalPath || "").trim();
        const branch = String(this.githubBranchName || "").trim();
        if (!path || !branch) { this.githubError = "مسیر clone و نام Branch الزامی است"; return; }
        this.githubBusy = true;
        try {
          const label = operation === "local_branch_create" ? "ساخت" : "تعویض";
          const result = await this.githubWrite(operation, { path, branch }, label + " Branch «" + branch + "» را تأیید می‌کنید؟");
          if (!result) return;
          this.githubLocalResult = JSON.stringify(result, null, 2);
          this.toast("ok", "✅", "Branch آماده شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async createGitHubPullRequest() {
        let repo;
        try { repo = this.githubRepositoryParams(); } catch (err) { this.githubError = err.message; return; }
        const draft = this.githubPullRequest;
        if (!String(draft.title || "").trim() || !String(draft.head || "").trim() || !String(draft.base || "").trim()) {
          this.githubError = "عنوان، branch مبدأ و branch مقصد Pull Request الزامی است";
          return;
        }
        this.githubBusy = true;
        try {
          const params = Object.assign({}, repo, {
            title: draft.title.trim(), head: draft.head.trim(), base: draft.base.trim(), body: String(draft.body || ""),
          });
          const result = await this.githubWrite("pull_create", params, "ساخت Pull Request «" + params.title + "» را تأیید می‌کنید؟");
          if (!result) return;
          draft.title = "";
          draft.body = "";
          this.githubLocalResult = JSON.stringify(result, null, 2);
          await this.inspectGitHubRepository();
          this.toast("ok", "✅", "Pull Request ساخته شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async saveGitHubFile(remove) {
        let repo;
        try { repo = this.githubRepositoryParams(); } catch (err) { this.githubError = err.message; return; }
        const edit = this.githubFileEdit;
        const path = String(edit.path || "").trim().replace(/^\/+/, "");
        const message = String(edit.message || "").trim();
        if (!path || !message || (remove && !edit.sha)) {
          this.githubError = remove ? "مسیر، SHA فایل و پیام Commit الزامی است" : "مسیر و پیام Commit الزامی است";
          return;
        }
        const params = Object.assign({}, repo, { path, message });
        if (edit.branch) params.branch = String(edit.branch).trim();
        if (edit.sha) params.sha = String(edit.sha).trim();
        if (!remove) params.content = String(edit.content || "");
        this.githubBusy = true;
        this.githubError = "";
        try {
          const operation = remove ? "file_delete" : "file_upsert";
          const result = await this.githubWrite(operation, params, (remove ? "حذف" : "ثبت") + " فایل «" + path + "» را تأیید می‌کنید؟");
          if (!result) return;
          edit.message = "";
          if (remove) { edit.content = ""; edit.sha = ""; }
          this.githubWorkspacePath = path;
          await this.inspectGitHubRepository();
          this.toast("ok", "✅", remove ? "فایل حذف شد" : "فایل و Commit ثبت شدند");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async createGitHubIssue() {
        let repo;
        try { repo = this.githubRepositoryParams(); } catch (err) { this.githubError = err.message; return; }
        const draft = this.githubIssueDraft;
        const title = String(draft.title || "").trim();
        if (!title) { this.githubError = "عنوان Issue الزامی است"; return; }
        const labels = String(draft.labels || "").split(",").map((item) => item.trim()).filter(Boolean);
        const params = Object.assign({}, repo, { title, body: String(draft.body || "") });
        if (labels.length) params.labels = labels;
        this.githubBusy = true;
        try {
          const result = await this.githubWrite("issue_create", params, "ساخت Issue «" + title + "» را تأیید می‌کنید؟");
          if (!result) return;
          this.githubIssueDraft = { title: "", body: "", labels: "" };
          await this.inspectGitHubRepository();
          this.toast("ok", "✅", "Issue ساخته شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async dispatchGitHubWorkflow() {
        let repo;
        try { repo = this.githubRepositoryParams(); } catch (err) { this.githubError = err.message; return; }
        const draft = this.githubWorkflowDispatch;
        if (!String(draft.workflow_id || "").trim() || !String(draft.ref || "").trim()) {
          this.githubError = "شناسهٔ Workflow و ref الزامی است"; return;
        }
        let inputs;
        try { inputs = JSON.parse(String(draft.inputs || "{}")); } catch (_) { this.githubError = "ورودی Workflow باید JSON معتبر باشد"; return; }
        if (!inputs || Array.isArray(inputs) || typeof inputs !== "object") { this.githubError = "ورودی Workflow باید یک شیء JSON باشد"; return; }
        const params = Object.assign({}, repo, { workflow_id: String(draft.workflow_id).trim(), ref: String(draft.ref).trim(), inputs });
        this.githubBusy = true;
        try {
          const result = await this.githubWrite("workflow_dispatch", params, "اجرای Workflow «" + params.workflow_id + "» روی «" + params.ref + "» را تأیید می‌کنید؟");
          if (!result) return;
          this.toast("ok", "✅", "درخواست اجرای Workflow ثبت شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async createGitHubRelease() {
        let repo;
        try { repo = this.githubRepositoryParams(); } catch (err) { this.githubError = err.message; return; }
        const draft = this.githubReleaseDraft;
        const tagName = String(draft.tag_name || "").trim();
        if (!tagName) { this.githubError = "Tag انتشار الزامی است"; return; }
        const params = Object.assign({}, repo, {
          tag_name: tagName, name: String(draft.name || "").trim(), body: String(draft.body || ""),
          draft: Boolean(draft.draft), prerelease: Boolean(draft.prerelease), generate_release_notes: true,
        });
        this.githubBusy = true;
        try {
          const result = await this.githubWrite("release_create", params, "ساخت Release برای tag «" + tagName + "» را تأیید می‌کنید؟");
          if (!result) return;
          this.githubReleaseDraft = { tag_name: "", name: "", body: "", draft: true, prerelease: false };
          this.toast("ok", "✅", "Release ساخته شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      selectGitHubReleaseAsset(event) {
        const files = event && event.target ? event.target.files : null;
        this.githubReleaseAsset.file = files && files.length ? files[0] : null;
      },

      async uploadGitHubReleaseAsset() {
        let repo;
        try { repo = this.githubRepositoryParams(); } catch (err) { this.githubError = err.message; return; }
        const draft = this.githubReleaseAsset;
        const file = draft.file;
        const releaseId = Number(draft.release_id);
        if (!Number.isSafeInteger(releaseId) || releaseId < 1) {
          this.githubError = "شناسهٔ عددی Release الزامی است";
          return;
        }
        if (!file || file.size < 1 || file.size > 256 * 1024 * 1024) {
          this.githubError = "یک فایل ۱ بایت تا ۲۵۶ مگابایت انتخاب کنید";
          return;
        }
        if (!window.confirm("آپلود فایل «" + file.name + "» در Release شمارهٔ " + releaseId + " را تأیید می‌کنید؟")) return;
        const query = new URLSearchParams({
          owner: repo.owner, repo: repo.repo, release_id: String(releaseId), name: file.name,
        });
        if (String(draft.label || "").trim()) query.set("label", String(draft.label).trim());
        this.githubBusy = true;
        this.githubError = "";
        try {
          const result = await this.githubApi("/api/github/release-asset?" + query.toString(), {
            method: "POST",
            headers: {
              "Content-Type": file.type || "application/octet-stream",
              "X-GitHub-Confirm": "true",
            },
            body: file,
          });
          this.githubConsoleResult = JSON.stringify(result, null, 2);
          this.githubReleaseAsset = { release_id: "", label: "", file: null };
          if (this.$refs.githubReleaseAssetFile) this.$refs.githubReleaseAssetFile.value = "";
          this.toast("ok", "✅", "فایل Release آپلود شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async loadGitHubProjects() {
        const owner = String(this.githubProjectOwner || "").trim();
        if (!owner) { this.githubError = "مالک Projects را وارد کنید"; return; }
        this.githubBusy = true;
        this.githubError = "";
        this.githubProjectOwnerId = "";
        this.githubProjectLoadedOwner = "";
        this.githubProjectLoadedOwnerType = "";
        this.githubProjects = [];
        try {
          const result = await this.githubRead("projects", { owner, owner_type: this.githubProjectOwnerType, limit: 100 });
          const container = result && result.data ? (result.data.user || result.data.organization) : null;
          this.githubProjectOwnerId = container && container.id ? container.id : (this.githubProjectOwnerType === "user" && this.githubAccount && owner === this.githubAccount.login ? this.githubAccount.node_id || "" : "");
          this.githubProjectLoadedOwner = this.githubProjectOwnerId ? owner : "";
          this.githubProjectLoadedOwnerType = this.githubProjectOwnerId ? this.githubProjectOwnerType : "";
          this.githubProjects = container && container.projectsV2 && Array.isArray(container.projectsV2.nodes) ? container.projectsV2.nodes : [];
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async inspectGitHubProject(projectId) {
        const selected = String(projectId || this.githubProjectId || "").trim();
        if (!selected) { this.githubError = "ابتدا یک Project انتخاب کنید"; return; }
        this.githubBusy = true;
        try {
          this.githubProjectId = selected;
          this.githubProject = await this.githubRead("project", { project_id: selected, limit: 100 });
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async createGitHubProject() {
        const ownerId = String(this.githubProjectOwnerId || "").trim();
        const title = String(this.githubProjectTitle || "").trim();
        const ownerMatches = this.githubProjectLoadedOwner === String(this.githubProjectOwner || "").trim()
          && this.githubProjectLoadedOwnerType === this.githubProjectOwnerType;
        if (!ownerId || !ownerMatches || !title) { this.githubError = "ابتدا Projects همین مالک را دریافت کنید و عنوان را وارد کنید"; return; }
        this.githubBusy = true;
        try {
          const result = await this.githubWrite("project_create", { owner_id: ownerId, title }, "ساخت Project «" + title + "» را تأیید می‌کنید؟");
          if (!result) return;
          this.githubProjectTitle = "";
          await this.loadGitHubProjects();
          this.toast("ok", "✅", "Project ساخته شد");
        } catch (err) { this.githubError = err.message; }
        finally { this.githubBusy = false; }
      },

      async submitGitHubActionsEntry(remove) {
        const entry = this.githubActionsEntry;
        const scope = String(entry.scope || "repository");
        if (scope === "codespace" && entry.kind !== "secret") {
          this.githubError = "Codespaces فقط Secret را در این API پشتیبانی می‌کند";
          return;
        }
        if (!String(entry.name || "").trim() || (!remove && !entry.value)) {
          this.githubError = "نام و مقدار را کامل کنید";
          return;
        }
        const operationPrefix = {
          repository: "actions_",
          organization: "organization_actions_",
          environment: "environment_actions_",
          codespace: "codespace_",
        }[scope];
        if (!operationPrefix) { this.githubError = "دامنهٔ Secret/Variable نامعتبر است"; return; }
        const fullName = this.githubOperationsRepository || (this.form.github.selected_repositories || [])[0];
        const params = { name: String(entry.name).trim() };
        let target = scope === "codespace" ? "حساب Codespaces" : "";
        if (scope === "repository" || scope === "environment") {
          if (!fullName || fullName.indexOf("/") < 1) {
            this.githubError = "ابتدا یک مخزن انتخاب و ذخیره کنید";
            return;
          }
          const parts = fullName.split("/");
          params.owner = parts[0];
          params.repo = parts[1];
          target = fullName;
        }
        if (scope === "organization") {
          params.org = String(entry.org || "").trim();
          params.visibility = String(entry.visibility || "private");
          target = params.org;
          if (!params.org) { this.githubError = "نام سازمان الزامی است"; return; }
        }
        if (scope === "environment") {
          params.environment = String(entry.environment || "").trim();
          target += " / " + params.environment;
          if (!params.environment) { this.githubError = "نام Environment الزامی است"; return; }
        }
        const selectedIds = String(entry.selected_repository_ids || "")
          .split(/[\s,]+/).filter(Boolean).map((value) => Number(value));
        if (selectedIds.some((value) => !Number.isSafeInteger(value) || value < 1)) {
          this.githubError = "شناسه‌های مخزن باید اعداد مثبت باشند";
          return;
        }
        if ((scope === "organization" && params.visibility === "selected") || scope === "codespace") {
          if (selectedIds.length) params.selected_repository_ids = selectedIds;
          else if (scope === "organization") { this.githubError = "برای visibility انتخابی، شناسهٔ مخزن لازم است"; return; }
        }
        const label = entry.kind === "secret" ? "Secret" : "Variable";
        const operation = operationPrefix + entry.kind + (remove ? "_delete" : "_set");
        if (!window.confirm((remove ? "حذف " : "ثبت ") + label + " در " + target + " را تأیید می‌کنید؟")) return;
        if (!remove) params.value = entry.value;
        this.githubBusy = true;
        this.githubError = "";
        try {
          const sensitive = entry.kind === "secret" && !remove;
          await this.githubApi(sensitive ? "/api/github/sensitive" : "/api/github/write", {
            method: "POST",
            headers: sensitive ? { "X-GitHub-Confirm": "true" } : {},
            body: JSON.stringify(sensitive ? { operation, params } : { operation, params, confirm: true }),
          });
          entry.name = "";
          entry.value = "";
          this.toast("ok", "✅", label + (remove ? " حذف شد" : " ثبت شد"));
        } catch (err) {
          entry.value = "";
          this.githubError = err.message;
        } finally { this.githubBusy = false; }
      },

      async submitGitHubWebhook(update) {
        let repo;
        try { repo = this.githubRepositoryParams(this.githubOperationsRepository); }
        catch (err) { this.githubError = err.message; return; }
        const draft = this.githubWebhookDraft;
        const params = Object.assign({}, repo, {
          active: Boolean(draft.active),
          content_type: String(draft.content_type || "json"),
          events: String(draft.events || "push").split(/[\s,]+/).map((item) => item.trim()).filter(Boolean),
        });
        if (String(draft.url || "").trim()) params.url = String(draft.url).trim();
        if (draft.secret) params.secret = draft.secret;
        if (update) {
          const hookId = Number(draft.hook_id);
          if (!Number.isSafeInteger(hookId) || hookId < 1) { this.githubError = "شناسهٔ عددی Webhook الزامی است"; return; }
          params.hook_id = hookId;
        } else if (!params.url) { this.githubError = "URL امن HTTPS برای Webhook الزامی است"; return; }
        const operation = update ? "webhook_update" : "webhook_create";
        if (!window.confirm((update ? "ویرایش" : "ساخت") + " Webhook در " + repo.owner + "/" + repo.repo + " را تأیید می‌کنید؟")) return;
        this.githubBusy = true;
        this.githubError = "";
        try {
          const result = await this.githubApi("/api/github/sensitive", {
            method: "POST",
            headers: { "X-GitHub-Confirm": "true" },
            body: JSON.stringify({ operation, params }),
          });
          this.githubConsoleResult = JSON.stringify(result, null, 2);
          draft.secret = "";
          if (!update) draft.hook_id = result && result.id ? String(result.id) : "";
          this.toast("ok", "✅", "Webhook ثبت شد");
        } catch (err) {
          draft.secret = "";
          this.githubError = err.message;
        } finally { this.githubBusy = false; }
      },

      async toggleGitHubRepository(fullName, checked) {
        const selected = new Set(this.form.github.selected_repositories || []);
        if (checked) selected.add(fullName); else selected.delete(fullName);
        this.form.github.selected_repositories = Array.from(selected);
        if (checked) {
          if (!this.githubOperationsRepository) this.githubOperationsRepository = fullName;
          if (!this.githubWorkspaceRepo) this.githubWorkspaceRepo = fullName;
        }
        const saved = await this.saveSettings(false);
        if (!saved) {
          if (checked) selected.delete(fullName); else selected.add(fullName);
          this.form.github.selected_repositories = Array.from(selected);
        }
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
          if (s.work_dir) this.form.work_dir = s.work_dir;
          if (typeof s.full_system_access === "boolean") {
            this.form.full_system_access = s.full_system_access;
            this.fullAccessWanted = s.full_system_access;
          }
          this.elevation = s.elevation || "";
          this.applyTelegramState({
            enabled: s.telegram_enabled,
            connected: s.telegram_connected,
            state: s.telegram_state,
            phone: s.telegram_phone,
          });
          this.form.telegram.enabled = Boolean(s.telegram_enabled);
          if (s.telegram_phone) this.form.telegram.phone = s.telegram_phone;
          if (typeof s.telegram_confirm_send === "boolean") this.form.telegram.confirm_send = s.telegram_confirm_send;
          this.telegramAccounts = (s.telegram_accounts && s.telegram_accounts.accounts) || [];
          this.activeTelegramAccount = s.telegram_active_account || "";
          this.gmailConnected = Boolean(s.gmail_connected);
          this.form.gmail.enabled = Boolean(s.gmail_enabled);
          if (typeof s.gmail_confirm_send === "boolean") this.form.gmail.confirm_send = s.gmail_confirm_send;
          this.form.github.enabled = Boolean(s.github_enabled);
          if (data.settings.github) this.applyGitHubStatus(data.settings.github);
        } catch (_) { /* keep previous values */ }
      },

      /* --------------------------------------------- telegram / gmail flow */

      applyTelegramState(state) {
        if (!state) return;
        this.telegramState = state.state || this.telegramState;
        this.telegramConnected = Boolean(state.connected);
        if (state.phone && !this.form.telegram.phone) this.form.telegram.phone = state.phone;
        if (this.telegramState === "connected") {
          this.telegramCode = "";
          this.telegramPassword = "";
        }
      },

      get telegramStateLabel() {
        return this.accountStateLabel(this.telegramState);
      },

      accountStateLabel(state) {
        const labels = {
          disabled: "غیرفعال",
          disconnected: "وصل نیست",
          await_code: "منتظر کد…",
          await_2fa: "منتظر رمز 2FA…",
          connected: "✅ متصل",
        };
        return labels[state] || "";
      },

      get gmailStateLabel() {
        return this.gmailConnected ? "✅ متصل" : "وصل نیست";
      },

      get githubStateLabel() {
        if (!this.form.github.enabled) return "غیرفعال";
        if (this.githubBusy) return "در حال بررسی…";
        return this.githubConnected ? "✅ متصل" : "وصل نیست";
      },

      get elevationLabel() {
        return { admin: "administrator", root: "root", user: "user" }[this.elevation] || this.elevation || "نامشخص";
      },

      async connectTelegram(account) {
        if (this.connection === "offline") return;
        this.telegramTargetAccount = account || this.activeTelegramAccount || "";
        this.telegramBusy = true;
        try {
          const result = await this.api("/api/telegram/connect", {
            method: "POST",
            body: JSON.stringify(this.telegramTargetAccount ? { account: this.telegramTargetAccount } : {}),
          });
          this.applyTelegramState(result);
          if (result.state === "connected") {
            this.toast("ok", "✅", "تلگرام متصل شد");
            this.refreshStatus();
          }
        } catch (err) {
          this.toast("bad", "⚠️", "اتصال تلگرام ناموفق بود — " + err.message);
        } finally {
          this.telegramBusy = false;
        }
      },

      async switchAccount(name) {
        if (this.connection === "offline") return;
        this.switchingTelegram = true;
        try {
          const result = await this.api("/api/telegram/switch", {
            method: "POST",
            body: JSON.stringify({ name }),
          });
          this.toast("ok", "✅", "اکانت فعال تلگرام: " + name);
          this.refreshStatus();
        } catch (err) {
          this.toast("bad", "⚠️", "تعویض اکانت ناموفق بود — " + err.message);
        } finally {
          this.switchingTelegram = false;
        }
      },

      async toggleAccountEnabled(acc) {
        if (this.connection === "offline") return;
        this.switchingTelegram = true;
        try {
          await this.api("/api/telegram/account", {
            method: "POST",
            body: JSON.stringify({ name: acc.account, enabled: !acc.enabled }),
          });
          this.toast("ok", "✅", "وضعیت «فعال» اکانت " + acc.account + " تغییر کرد");
          this.refreshStatus();
        } catch (err) {
          this.toast("bad", "⚠️", "تغییر وضعیت اکانت ناموفق بود — " + err.message);
        } finally {
          this.switchingTelegram = false;
        }
      },

      async submitTelegramCode() {
        if (!this.telegramCode.trim()) { this.toast("bad", "⚠️", "کد را وارد کنید"); return; }
        this.telegramBusy = true;
        try {
          const result = await this.api("/api/telegram/verify", {
            method: "POST",
            body: JSON.stringify({
              code: this.telegramCode.trim(),
              account: this.telegramTargetAccount || undefined,
            }),
          });
          this.applyTelegramState(result);
          if (result.state === "connected") this.toast("ok", "✅", "تلگرام متصل شد");
        } catch (err) {
          this.toast("bad", "⚠️", "کد نادرست است — " + err.message);
        } finally {
          this.telegramBusy = false;
        }
      },

      async submitTelegramPassword() {
        if (!this.telegramPassword) { this.toast("bad", "⚠️", "رمز 2FA را وارد کنید"); return; }
        this.telegramBusy = true;
        try {
          const result = await this.api("/api/telegram/verify", {
            method: "POST",
            body: JSON.stringify({
              password: this.telegramPassword,
              account: this.telegramTargetAccount || undefined,
            }),
          });
          this.applyTelegramState(result);
          if (result.state === "connected") this.toast("ok", "✅", "تلگرام متصل شد");
        } catch (err) {
          this.toast("bad", "⚠️", "رمز 2FA نادرست است — " + err.message);
        } finally {
          this.telegramBusy = false;
        }
      },

      async disconnectTelegram() {
        if (this.connection === "offline") return;
        try {
          const result = await this.api("/api/telegram/disconnect", { method: "POST" });
          this.applyTelegramState(result);
          this.toast("info", "ℹ️", "تلگرام قطع شد");
        } catch (_) {
          this.toast("bad", "❌", "قطع اتصال تلگرام ناموفق بود");
        }
      },

      async openTelegramBrowser() {
        this.telegramBrowserOpen = true;
        this.telegramBrowserAccount = this.activeTelegramAccount || (this.telegramAccounts[0] && this.telegramAccounts[0].account) || "";
        this.telegramSelectedChat = null;
        await this.loadTelegramBrowser();
      },

      telegramKindLabel(kind) {
        return { private: "خصوصی", bot: "ربات", group: "گروه", supergroup: "سوپرگروه", channel: "کانال" }[kind] || "";
      },

      telegramItemIcon(item) {
        return { private: "👤", bot: "🤖", group: "👥", supergroup: "👥", channel: "📣" }[item.kind] || "👤";
      },

      async loadTelegramBrowser(append) {
        if (this.connection === "offline") return;
        const loadMore = Boolean(append);
        if (!loadMore) this.telegramBrowserOffset = 0;
        this.telegramBrowserLoading = true;
        this.telegramBrowserError = "";
        if (!loadMore) {
          this.telegramSelectedChat = null;
          this.telegramHistory = [];
        }
        const account = this.telegramBrowserAccount ? "&account=" + encodeURIComponent(this.telegramBrowserAccount) : "";
        const query = "&query=" + encodeURIComponent(this.telegramBrowserQuery || "");
        const offset = "&offset=" + this.telegramBrowserOffset;
        let scope = "";
        if (this.telegramTab === "chats" && this.telegramChatScope === "unread") scope = "&unread_only=true";
        if (this.telegramTab === "chats" && this.telegramChatScope === "archive") scope = "&archived=true";
        if (this.telegramTab === "chats" && this.telegramChatScope === "main") scope = "&archived=false";
        try {
          const path = this.telegramTab === "contacts"
            ? "/api/telegram/contacts?limit=100" + account + query + offset
            : "/api/telegram/chats?limit=50&sort=recent&kind=" + encodeURIComponent(this.telegramChatKind) + account + query + offset + scope;
          const [result, stats] = await Promise.all([
            this.api(path),
            this.api("/api/telegram/stats?" + (this.telegramBrowserAccount ? "account=" + encodeURIComponent(this.telegramBrowserAccount) : "")),
          ]);
          const incoming = result.items || [];
          this.telegramBrowserItems = loadMore ? this.telegramBrowserItems.concat(incoming) : incoming;
          this.telegramBrowserOffset = result.next_offset || this.telegramBrowserItems.length;
          this.telegramBrowserHasMore = Boolean(result.has_more);
          this.telegramStats = stats || null;
        } catch (err) {
          if (!loadMore) this.telegramBrowserItems = [];
          this.telegramBrowserError = err.message || "دریافت اطلاعات تلگرام ناموفق بود";
        } finally {
          this.telegramBrowserLoading = false;
        }
      },

      async loadTelegramHistory(chat) {
        this.telegramSelectedChat = chat;
        this.telegramHistory = [];
        this.telegramHistoryLoading = true;
        try {
          const account = this.telegramBrowserAccount ? "&account=" + encodeURIComponent(this.telegramBrowserAccount) : "";
          const result = await this.api("/api/telegram/history?limit=50&target=" + encodeURIComponent(chat.id) + account);
          this.telegramHistory = result.items || [];
        } catch (err) {
          this.telegramBrowserError = err.message || "دریافت تاریخچه ناموفق بود";
        } finally {
          this.telegramHistoryLoading = false;
        }
      },

      async connectGmail() {
        if (this.connection === "offline") return;
        this.gmailBusy = true;
        try {
          const result = await this.api("/api/gmail/connect", { method: "POST" });
          this.gmailConnected = Boolean(result.connected);
          if (result.connected) this.toast("ok", "✅", "جیمیل متصل شد");
          else this.toast("info", "ℹ️", result.message || "اتصال جیمیل نیاز به تأیید در مرورگر دارد");
        } catch (err) {
          this.toast("bad", "⚠️", "اتصال جیمیل ناموفق بود — " + err.message);
        } finally {
          this.gmailBusy = false;
        }
      },

      async disconnectGmail() {
        if (this.connection === "offline") return;
        try {
          await this.api("/api/gmail/disconnect", { method: "POST" });
          this.gmailConnected = false;
          this.toast("info", "ℹ️", "جیمیل قطع شد");
        } catch (_) {
          this.toast("bad", "❌", "قطع اتصال جیمیل ناموفق بود");
        }
      },

      onFullAccessToggle() {
        if (this.fullAccessWanted) {
          this.fullAccessArmed = true;  // two-step confirm
        } else {
          this.fullAccessArmed = false;
          this.form.full_system_access = false;
        }
      },

      confirmFullAccess() {
        this.form.full_system_access = true;
        this.fullAccessArmed = false;
        this.toast("warn", "⚠️", "دسترسی کامل فعال شد — ذخیره را بزنید");
      },

      cancelFullAccess() {
        this.fullAccessWanted = this.form.full_system_access;
        this.fullAccessArmed = false;
      },

      async restartElevated() {
        if (this.connection === "offline") return;
        this.elevating = true;
        try {
          const result = await this.api("/api/elevate/restart", { method: "POST" });
          this.toast(result.elevated ? "ok" : "info", result.elevated ? "✅" : "ℹ️", result.message || "");
        } catch (_) {
          this.toast("bad", "❌", "اجرای دوباره ناموفق بود");
        } finally {
          this.elevating = false;
        }
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
        this.ensureGitHubSecurity(false)
          .then(() => this.refreshGitHubStatus(true))
          .catch((err) => { this.githubError = err.message; });
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

      /* ---------------------------------------------------- full purge */

      async purgeEverything() {
        if (this.connection === "offline") {
          this.purgeArmed = false;
          this.toast("info", "ℹ️", "در حالت نمایش آفلاین پاک‌سازی در دسترس نیست");
          return;
        }
        this.purging = true;
        try {
          await this.ensureGithubSecurity();
          const result = await this.api("/api/purge", {
            method: "POST",
            headers: this.githubHeaders(),
            body: JSON.stringify({ confirm: true, shutdown: true }),
          });
          // Wipe the browser-side traces as well (prefs + conversations).
          try {
            localStorage.removeItem(STORAGE_PREFS);
            localStorage.removeItem(STORAGE_CONVERSATIONS);
          } catch (_) { /* private mode */ }
          this.conversations = [];
          this.conversationId = null;
          this.messages = [];
          this.settingsOpen = false;
          this.purgeArmed = false;
          // Stop the socket before the process exits so no reconnect storm
          // (or error toast) appears while the server shuts itself down.
          if (this.ws) {
            try { this.ws.onclose = null; this.ws.onerror = null; this.ws.close(); } catch (_) { /* ignore */ }
            this.ws = null;
          }
          if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null; }
          this.connection = "offline";
          this.purgeMessage = (result && result.message) || "";
          this.purgeDone = true;
        } catch (_) {
          this.purgeArmed = false;
          this.toast("bad", "❌", "پاک‌سازی کامل ناموفق بود — برنامه هنوز فعال است");
        } finally {
          this.purging = false;
        }
      },

      async saveSettings(closeModal) {
        const shouldClose = closeModal !== false;
        if (this.connection === "offline") {
          if (shouldClose) this.settingsOpen = false;
          this.toast("info", "ℹ️", "در حالت نمایش آفلاین تنظیمات ذخیره نمی‌شود");
          return null;
        }
        try {
          this.form.github = this.normalizeGitHubSettings(this.form.github);
          this.form.github.allowed_origins = String(this.githubAllowedOriginsText || "")
            .split(/[\n,]+/).map((item) => item.trim()).filter(Boolean);
          const result = await this.githubApi("/api/settings", {
            method: "POST",
            body: JSON.stringify(this.form),
          });
          if (shouldClose) {
            this.settingsOpen = false;
            this.toast("ok", "✅", "تنظیمات ذخیره شد" + (result && result.model ? " — " + result.model : ""));
          }
          this.refreshStatus();
          return result;
        } catch (err) {
          this.githubError = err.message;
          this.toast("bad", "❌", "ذخیرهٔ تنظیمات ناموفق بود — " + err.message);
          return null;
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
          try { await this.api("/api/clear" + (this.conversationId ? "?session_id=" + encodeURIComponent(this.conversationId) : ""), { method: "POST" }); } catch (_) { /* ignore */ }
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
          this.api("/api/clear" + (this.conversationId ? "?session_id=" + encodeURIComponent(this.conversationId) : ""), { method: "POST" }).catch(() => {});
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
