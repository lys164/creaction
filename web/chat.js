const LANG_NAMES_FULL = { zh: "简体中文", ja: "日本語", ko: "한국어", en: "English" };

const $ = (s, r = document) => r.querySelector(s);

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  setTimeout(() => (t.className = "toast " + kind), 2600);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function localized(obj, lang = "zh") {
  if (obj == null) return "";
  if (typeof obj === "string") return obj;
  if (typeof obj === "object") {
    if (obj[lang] != null) return obj[lang];
    if (obj.zh != null) return obj.zh;
    return JSON.stringify(obj, null, 1);
  }
  return String(obj);
}

function imgUrl(local_path, url) {
  const bust = `_t=${Date.now()}`;
  if (local_path) {
    const name = local_path.split("/").pop();
    return "/img/" + name + "?" + bust;
  }
  if (url && url.startsWith("/img/")) {
    return url + (url.includes("?") ? "&" : "?") + bust;
  }
  return url;
}

let CHARS = [];
let ACTIVE_CHAR = null;
let ACTIVE_REC = null;
let SESSION_ID = null;
let MESSAGES = [];
let DEFAULT_TPL = "";
let MODE = "normal";
let DEFAULT_TPLS = {};

async function init() {
  try {
    CHARS = await api("/api/characters");
  } catch (e) {
    toast("角色列表加载失败", "err");
    return;
  }
  renderCharList();
  const shown = filterChars();
  if (shown.length) selectChar(shown[0].char_id);
}

function filterChars() {
  const q = ($("#search").value || "").trim().toLowerCase();
  return CHARS.filter((c) => {
    if (!q) return true;
    return `${c.name || ""} ${c.char_id || ""}`.toLowerCase().includes(q);
  });
}

function renderCharList() {
  const box = $("#charList");
  box.innerHTML = "";
  const list = filterChars();
  if (!list.length) {
    box.innerHTML = '<p style="color:var(--mut);padding:10px;font-size:13px">没有匹配的角色。</p>';
    return;
  }
  list.forEach((c) => {
    const item = document.createElement("div");
    item.className = "char-item" + (c.char_id === ACTIVE_CHAR ? " active" : "");
    const cover = c.cover_url
      ? `<img src="${imgUrl(null, c.cover_url)}" />`
      : `<span>${escapeHtml((c.name || "?").slice(0, 1))}</span>`;
    const langTag = c.lang_name ? `<span class="lang-badge">${escapeHtml(c.lang_name)}</span>` : "";
    item.innerHTML = `<div class="char-avatar">${cover}</div>
      <div class="char-meta"><div class="char-name">${langTag}${escapeHtml(c.name || "(未命名)")}</div>
      <div class="char-id">${escapeHtml(c.char_id || "")}</div></div>`;
    item.addEventListener("click", () => selectChar(c.char_id));
    box.appendChild(item);
  });
}

function markActiveChar() {
  document.querySelectorAll(".char-item").forEach((x) => x.classList.remove("active"));
  renderCharList();
}

async function selectChar(charId, opts = {}) {
  if (!charId) return;
  ACTIVE_CHAR = charId;
  markActiveChar();
  $("#empty").classList.add("hidden");
  $("#panel").classList.remove("hidden");
  $("#messages").innerHTML = "";
  $("#status").innerHTML = `<span class="spinner"></span> 正在载入角色…`;
  try {
    const [rec, latest] = await Promise.all([
      api("/api/character/" + charId),
      api("/api/chat/" + charId + "/latest?mode=" + MODE),
    ]);
    ACTIVE_REC = rec;
    DEFAULT_TPL = latest.default_template || DEFAULT_TPL || "";
    DEFAULT_TPLS = latest.default_templates || DEFAULT_TPLS;
    const p = rec.persona || {};
    const summary = p.profile || (p.personality && p.personality.summary) || "";
    $("#title").innerHTML = `${rec.lang ? `<span class="lang-badge">${LANG_NAMES_FULL[rec.lang] || rec.lang}</span>` : ""}${escapeHtml(localized(p.name) || rec.char_id)}`;
    $("#sub").textContent = summary ? localized(summary) : rec.char_id;
    renderAvatar(rec);

    const session = latest.session;
    if (opts.forceNew) {
      SESSION_ID = null;
      setTemplate("");
      MESSAGES = openingMessages(latest.opening);
    } else if (session && session.messages && session.messages.length) {
      SESSION_ID = session.session_id;
      setTemplate(session.prompt_template || "");
      MESSAGES = session.messages;
    } else {
      SESSION_ID = null;
      setTemplate("");
      MESSAGES = openingMessages(latest.opening);
    }
    renderMessages();
    $("#status").innerHTML = SESSION_ID ? "已载入最近一次对话。" : "已载入角色开场白，可直接开始聊天。";
  } catch (e) {
    $("#status").innerHTML = "载入失败：" + e.message;
    toast("角色载入失败", "err");
  }
}

function openingMessages(opening) {
  return (opening || []).length
    ? [{ role: "assistant", items: opening, is_opening: true, created: Math.floor(Date.now() / 1000) }]
    : [];
}

function renderAvatar(rec) {
  const av = $("#avatar");
  const name = localized((rec.persona || {}).name) || rec.char_id || "?";
  const cover = rec.cover && (rec.cover.local_path || rec.cover.url)
    ? imgUrl(rec.cover.local_path, rec.cover.url)
    : null;
  av.innerHTML = cover ? `<img src="${cover}" />` : "";
  if (!cover) av.textContent = name.slice(0, 1);
}

function setTemplate(tpl) {
  $("#promptTpl").value = tpl || "";
  updateTplHint();
}

function updateTplHint() {
  const custom = $("#promptTpl").value.trim().length > 0;
  $("#tplHint").textContent = custom
    ? (SESSION_ID ? "本会话使用自定义模板" : "将用自定义模板开始新对话")
    : "当前使用默认模板";
}

const CTX_MAP = {
  relationship: "ctxRelationship", user_persona: "ctxUserPersona",
  user_impression: "ctxUserImpression", plot_summary: "ctxPlotSummary",
  location: "ctxLocation", weather: "ctxWeather",
  day_summary: "ctxDaySummary", day_schedule: "ctxDaySchedule",
};

function contextPayload() {
  const out = {};
  Object.entries(CTX_MAP).forEach(([k, id]) => { out[k] = ($("#" + id).value || "").trim(); });
  return out;
}

function fillContext(ctx) {
  Object.entries(CTX_MAP).forEach(([k, id]) => { $("#" + id).value = (ctx || {})[k] || ""; });
}

function renderMessages() {
  const box = $("#messages");
  box.innerHTML = "";
  if (!MESSAGES.length) {
    box.innerHTML = '<div class="placeholder">暂无消息，发一句开始。</div>';
    return;
  }
  MESSAGES.forEach((m) => {
    if (m.role === "user") {
      box.appendChild(userBubble(m.content || ""));
      return;
    }
    const items = Array.isArray(m.items) ? m.items : [];
    if (m.is_opening) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "角色开场白";
      box.appendChild(note);
    }
    items.forEach((it) => box.appendChild(assistantItem(it)));
    if (m.call_log) box.appendChild(callLogRow(m.call_log));
  });
  box.scrollTop = box.scrollHeight;
}

function prettyJson(raw) {
  if (typeof raw !== "string") return JSON.stringify(raw, null, 2);
  try { return JSON.stringify(JSON.parse(raw), null, 2); } catch (e) { return raw; }
}

function callLogRow(log) {
  const sections = [];
  const meta = [log.model && `model: ${log.model}`, log.temperature != null && `temperature: ${log.temperature}`, log.max_tokens != null && `max_tokens: ${log.max_tokens}`].filter(Boolean).join("   ");
  if (meta) sections.push(`<div class="log-meta">${escapeHtml(meta)}</div>`);
  (log.messages || []).forEach((msg) => {
    const label = msg.role === "system" ? "SYSTEM PROMPT" : msg.role === "user" ? "INPUT · user" : "INPUT · assistant";
    sections.push(`<div class="log-block"><div class="log-label">${escapeHtml(label)}</div><pre>${escapeHtml(prettyJson(msg.content))}</pre></div>`);
  });
  sections.push(`<div class="log-block"><div class="log-label">OUTPUT</div><pre>${escapeHtml(prettyJson(log.output))}</pre></div>`);
  const row = document.createElement("div");
  row.className = "row assistant";
  const det = document.createElement("details");
  det.className = "raw-output";
  det.innerHTML = `<summary>模型调用日志</summary><div class="log-body">${sections.join("")}</div>`;
  row.appendChild(det);
  return row;
}

function userBubble(content) {
  const row = document.createElement("div");
  row.className = "row user";
  row.innerHTML = `<div class="bubble user-bubble">${escapeHtml(content)}</div>`;
  return row;
}

function assistantItem(item) {
  const type = item && item.type ? item.type : "text";
  const data = (item && item.data) || {};
  const row = document.createElement("div");
  row.className = "row assistant";
  if (type === "voice") {
    row.innerHTML = `<div class="bubble assistant-bubble voice-bubble"><span class="type-label">VOICE</span>${escapeHtml(data.content || "")}${data.emotion ? `<div class="extra">${escapeHtml(data.emotion)}</div>` : ""}</div>`;
    return row;
  }
  if (type === "sticker") {
    row.innerHTML = `<div class="bubble assistant-bubble sticker-bubble"><span class="type-label">STICKER</span><div>${escapeHtml(data.scene || "sticker")}</div><div class="extra">${escapeHtml(data.desc || data.emotion || "")}</div></div>`;
    return row;
  }
  if (type === "image") {
    row.innerHTML = `<div class="bubble assistant-bubble image-bubble"><span class="type-label">IMAGE · ${escapeHtml(data.category || "photo")}</span><div>${escapeHtml(data.description || "")}</div></div>`;
    return row;
  }
  if (type === "html_file") {
    const wrap = document.createElement("div");
    wrap.className = "bubble assistant-bubble html-bubble";
    wrap.innerHTML = `<span class="type-label">HTML</span><div class="html-title">${escapeHtml(data.file_name || "공유")}</div><div>${escapeHtml(data.description || "HTML")}</div><button class="ghost open-html" type="button">预览 HTML</button>`;
    wrap.querySelector(".open-html").addEventListener("click", () => {
      const w = window.open("", "_blank");
      w.document.open();
      w.document.write(data.html || "");
      w.document.close();
    });
    row.appendChild(wrap);
    return row;
  }
  if (type === "state_update") {
    row.className = "row state";
    const parts = [];
    if (data.status) parts.push(`<span class="state-status">${escapeHtml(data.status)}</span>`);
    if (data.emotion) parts.push(`<span class="state-emotion">${escapeHtml(data.emotion)}</span>`);
    row.innerHTML = `<div class="state-chip">${parts.join("") || "状态已更新"}</div>`;
    return row;
  }
  if (type === "music") {
    row.innerHTML = `<div class="bubble assistant-bubble music-bubble"><span class="type-label">MUSIC</span>${escapeHtml(data.content || "")}</div>`;
    return row;
  }
  if (type === "dating_card") {
    const meta = [data.location, data.status, data.outfit, data.emotion].filter(Boolean).map(escapeHtml).join(" · ");
    row.innerHTML = `<div class="bubble assistant-bubble dating-bubble"><span class="type-label">约会邀请</span><div class="dating-title">${escapeHtml(data.title || "见一面")}</div>${meta ? `<div class="extra">${meta}</div>` : ""}${data.description ? `<div>${escapeHtml(data.description)}</div>` : ""}${data.button ? `<button class="ghost" type="button">${escapeHtml(data.button)}</button>` : ""}</div>`;
    return row;
  }
  if (type === "match_action") {
    const greeting = data.greeting || data.content || "";
    row.innerHTML = `<div class="bubble assistant-bubble match-bubble"><span class="type-label">加好友</span><div>对方同意后的第一句</div>${greeting ? `<div class="extra">${escapeHtml(greeting)}</div>` : ""}</div>`;
    return row;
  }
  const emotionTag = data.emotion && data.emotion !== "default" ? `<div class="extra">${escapeHtml(data.emotion)}</div>` : "";
  row.innerHTML = `<div class="bubble assistant-bubble">${escapeHtml(data.content || "")}${emotionTag}</div>`;
  return row;
}

async function sendMessage() {
  if (!ACTIVE_CHAR) return toast("请先选择角色", "err");
  const input = $("#input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  MESSAGES.push({ role: "user", content: text, created: Math.floor(Date.now() / 1000) });
  renderMessages();
  const btn = $("#btnSend");
  btn.disabled = true;
  $("#status").innerHTML = `<span class="spinner"></span> 角色正在输入…`;
  try {
    const payload = {
      char_id: ACTIVE_CHAR,
      message: text,
      session_id: SESSION_ID,
      context: contextPayload(),
      mode: MODE,
    };
    if (!SESSION_ID) {
      const tpl = ($("#promptTpl").value || "").trim();
      if (tpl) payload.prompt_template = tpl;
    }
    const r = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    SESSION_ID = r.session.session_id;
    MESSAGES = r.session.messages || [];
    if (r.session.prompt_template !== undefined) setTemplate(r.session.prompt_template || "");
    renderMessages();
    $("#status").innerHTML = "";
  } catch (e) {
    MESSAGES.push({ role: "assistant", items: [{ type: "text", data: { content: "发送失败：" + e.message } }], created: Math.floor(Date.now() / 1000) });
    renderMessages();
    $("#status").innerHTML = "失败：" + e.message;
    toast("聊天失败", "err");
  } finally {
    btn.disabled = false;
    input.focus();
  }
}

async function loadHistory() {
  const list = $("#historyList");
  list.innerHTML = '<p style="color:var(--mut);font-size:13px">加载中…</p>';
  try {
    const r = await api("/api/chat/" + ACTIVE_CHAR + "/sessions?mode=" + MODE);
    const sessions = r.sessions || [];
    if (!sessions.length) {
      list.innerHTML = '<p style="color:var(--mut);font-size:13px">暂无历史对话。</p>';
      return;
    }
    list.innerHTML = "";
    sessions.forEach((s) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "history-item" + (s.session_id === SESSION_ID ? " active" : "");
      const when = s.updated ? new Date(s.updated * 1000).toLocaleString() : "";
      const tag = s.has_custom_template ? '<span class="history-tag">自定义</span>' : "";
      item.innerHTML = `<div class="history-top">${when}${tag}<span class="history-count">${s.message_count} 条</span></div>
        <div class="history-preview">${escapeHtml(s.preview || "(无内容)")}</div>`;
      item.addEventListener("click", () => openSession(s.session_id));
      list.appendChild(item);
    });
  } catch (e) {
    list.innerHTML = '<p style="color:var(--mut);font-size:13px">加载失败：' + escapeHtml(e.message) + "</p>";
  }
}

async function openSession(sessionId) {
  if (!ACTIVE_CHAR || !sessionId) return;
  $("#status").innerHTML = `<span class="spinner"></span> 载入历史对话…`;
  try {
    const r = await api("/api/chat/" + ACTIVE_CHAR + "/session/" + sessionId);
    const session = r.session;
    SESSION_ID = session.session_id;
    MESSAGES = session.messages || [];
    setTemplate(session.prompt_template || "");
    fillContext(session.context || {});
    renderMessages();
    await loadHistory();
    $("#status").innerHTML = "已载入该历史对话，可继续聊天。";
  } catch (e) {
    $("#status").innerHTML = "载入失败：" + e.message;
    toast("历史对话载入失败", "err");
  }
}

$("#search").addEventListener("input", renderCharList);
$("#form").addEventListener("submit", (e) => { e.preventDefault(); sendMessage(); });
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
$("#promptTpl").addEventListener("input", updateTplHint);
$("#btnTplReset").addEventListener("click", () => { setTemplate(""); toast("已恢复默认模板（新对话生效）", "ok"); });
$("#btnNew").addEventListener("click", () => {
  SESSION_ID = null; MESSAGES = [];
  if (ACTIVE_CHAR) selectChar(ACTIVE_CHAR, { forceNew: true });
  toast("已开始新对话", "ok");
});
$("#btnHistory").addEventListener("click", async () => {
  const box = $("#historyBox");
  if (!ACTIVE_CHAR) return;
  box.hidden = false;
  box.open = true;
  await loadHistory();
});

$("#modeSwitch").addEventListener("click", (e) => {
  const btn = e.target.closest(".mode-btn");
  if (!btn || btn.dataset.mode === MODE) return;
  MODE = btn.dataset.mode;
  document.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === MODE));
  SESSION_ID = null;
  MESSAGES = [];
  if (ACTIVE_CHAR) selectChar(ACTIVE_CHAR, { forceNew: true });
  toast(MODE === "anonymous" ? "已切换到匿名聊天模式" : "已切换到普通聊天模式", "ok");
});

init();


