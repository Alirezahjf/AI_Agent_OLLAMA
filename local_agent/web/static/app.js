// Local Windows Assistant — Web UI
const messages = document.getElementById("chat");
const composer = document.getElementById("composer");
const input = document.getElementById("input");
const status = document.getElementById("status");
const actions = document.getElementById("actions");
const clearBtn = document.getElementById("clear");
const screenshotBtn = document.getElementById("screenshot");

let ws = null;
let reconnectTimer = null;
let pendingConfirms = new Map();

function appendMessage(role, content) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = content;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}

function appendConfirm(requestId, name, argsText) {
  const div = document.createElement("div");
  div.className = "msg confirm";
  div.innerHTML = `⚠️ تأیید لازم: <b>${name}</b><br>${argsText}<br>`;
  const yes = document.createElement("button");
  yes.className = "approve";
  yes.textContent = "✅ تأیید";
  yes.onclick = () => {
    ws.send(JSON.stringify({ type: "confirm", request_id: requestId, approved: true }));
    div.remove();
  };
  const no = document.createElement("button");
  no.className = "deny";
  no.textContent = "✖️ لغو";
  no.onclick = () => {
    ws.send(JSON.stringify({ type: "confirm", request_id: requestId, approved: false }));
    div.remove();
  };
  div.appendChild(yes);
  div.appendChild(no);
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

function setStatus(text) {
  status.textContent = text;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    setStatus("🟢 متصل");
    loadActions();
  };
  ws.onclose = () => {
    setStatus("🔴 قطع - تلاش برای اتصال مجدد…");
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2000);
  };
  ws.onerror = () => {
    setStatus("🔴 خطای اتصال");
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "event") {
      handleEvent(msg);
    } else if (msg.type === "confirm_result") {
      // already rendered inline
    } else if (msg.type === "error") {
      appendMessage("error", msg.message || "error");
    } else if (msg.type === "pong") {
      // ignore
    }
  };
}

function handleEvent(msg) {
  const p = msg.payload || {};
  switch (msg.event_type) {
    case "chat_started":
      appendMessage("system", "💬 گفتگو شروع شد");
      break;
    case "turn_started":
      appendMessage("system", `🧠 turn ${p.turn}/${p.max_turns}`);
      break;
    case "assistant_final":
      appendMessage("assistant", p.text || "");
      break;
    case "tool_proposed":
      appendMessage("tool", `🔧 ${p.name}(${JSON.stringify(p.arguments).slice(0, 200)})`);
      break;
    case "tool_confirm_requested":
      appendConfirm(p.request_id, p.name, JSON.stringify(p.arguments, null, 2).slice(0, 600));
      break;
    case "tool_result":
      appendMessage("tool", `↪ ${p.name}: ${(p.text || "").slice(0, 400)}`);
      break;
    case "chat_done":
      appendMessage("system", "✅ تمام شد");
      break;
    case "chat_failed":
      appendMessage("error", `❌ ${p.error || p.reason || "chat failed"}`);
      break;
  }
}

async function loadActions() {
  try {
    const r = await fetch("/api/actions");
    const list = await r.json();
    actions.innerHTML = "";
    for (const desc of list) {
      const li = document.createElement("li");
      // Highlight risk
      const m = desc.match(/\[risk=([^\]]+)\]/);
      if (m) {
        const risk = m[1];
        const span = document.createElement("span");
        span.className = `risk-${risk}`;
        span.textContent = risk;
        li.appendChild(span);
        li.appendChild(document.createTextNode(" " + desc.replace(/\[risk=[^\]]+\]\s*/, "")));
      } else {
        li.textContent = desc;
      }
      actions.appendChild(li);
    }
  } catch (err) {
    actions.textContent = "could not load actions: " + err;
  }
}

composer.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  appendMessage("user", text);
  ws.send(JSON.stringify({ type: "chat", message: text }));
  input.value = "";
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

clearBtn.onclick = async () => {
  await fetch("/api/clear", { method: "POST" });
  messages.innerHTML = "";
  appendMessage("system", "🧹 حافظه پاک شد");
};

screenshotBtn.onclick = async () => {
  const r = await fetch("/api/invoke", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "screen_capture", arguments: { filename: "webui.png" } }),
  });
  const j = await r.json();
  if (j.success) appendMessage("system", j.text);
  else appendMessage("error", j.error || j.text);
};

connect();
