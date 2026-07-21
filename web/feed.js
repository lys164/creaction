/* 第三方視角帖子 Demo：選角色 → 生成 T1 論壇體宣傳帖 / T2 角色綁定帖 → 論壇體渲染 */
const $ = (s) => document.querySelector(s);

function toast(msg, cls = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show " + cls;
  setTimeout(() => (el.className = "toast"), 2600);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function imgUrl(coverUrl) {
  if (!coverUrl) return null;
  return coverUrl + (coverUrl.includes("?") ? "&" : "?") + "w=200";
}

let CHARS = [];
let ACTIVE_CHAR = null;
let ACTIVE_META = null;
let LANG = "zh";
let POSTS = [];
let EVENTS = [];
const PENDING = {};   // char_id → [{id, kind, subtype, err}]，支援並行生成 + 生成中占位卡
let GEN_SEQ = 0;
function pendingOf(charId) { return PENDING[charId] || (PENDING[charId] = []); }
function removePending(charId, gid) {
  if (PENDING[charId]) PENDING[charId] = PENDING[charId].filter((x) => x.id !== gid);
}

/* ---------- 雙語欄位 ---------- */
function B(field) {
  if (field == null) return "";
  if (typeof field === "string") return field;
  if (typeof field === "object") return field[LANG] ?? field.ko ?? field.zh ?? "";
  return String(field);
}

function num(n) {
  const v = Number(n);
  if (!isFinite(v)) return "";
  return v >= 10000 ? (v / 10000).toFixed(1).replace(/\.0$/, "") + "만" : v.toLocaleString();
}

/* 當前選中的生圖模型（image-2 / banana），供各生成/重出鏈路透傳 */
function selectedImageModel() {
  return $("#imgModel")?.value || "image-2";
}

/* 生圖模型顯示名（帖子存的 image_model_choice → 人類可讀標籤） */
function imageModelLabel(choice) {
  return { "image-2": "image-2", banana: "nanobanana" }[choice] || null;
}

/* ---------- 角色列表 ---------- */
async function init() {
  try {
    CHARS = await api("/api/feed_characters");
  } catch (e) {
    toast("角色列表載入失敗", "err");
    return;
  }
  renderCharList();
  const wanted = new URLSearchParams(location.search).get("char");
  if (wanted && CHARS.some((c) => c.char_id === wanted)) return selectChar(wanted);
  const shown = filterChars();
  if (shown.length) selectChar(shown[0].char_id);
}

function filterChars() {
  const q = ($("#search").value || "").trim().toLowerCase();
  return CHARS.filter((c) => !q || `${c.name || ""} ${c.char_id || ""}`.toLowerCase().includes(q));
}

function renderCharList() {
  const box = $("#charList");
  box.innerHTML = "";
  const list = filterChars();
  if (!list.length) {
    box.innerHTML = '<p style="color:var(--mut);padding:10px;font-size:13px">沒有匹配的角色。</p>';
    return;
  }
  list.forEach((c) => {
    const item = document.createElement("div");
    item.className = "char-item" + (c.char_id === ACTIVE_CHAR ? " active" : "");
    const cover = c.cover_url
      ? `<img src="${imgUrl(c.cover_url)}" loading="lazy" />`
      : `<span>${escapeHtml((c.name || "?").slice(0, 1))}</span>`;
    const langTag = c.lang_name ? `<span class="lang-badge">${escapeHtml(c.lang_name)}</span>` : "";
    item.innerHTML = `<div class="char-avatar">${cover}</div>
      <div class="char-meta"><div class="char-name">${langTag}${escapeHtml(c.name || "(未命名)")}</div>
      <div class="char-id">${escapeHtml(c.char_id || "")}</div></div>`;
    item.addEventListener("click", () => selectChar(c.char_id));
    box.appendChild(item);
  });
}

async function selectChar(charId) {
  ACTIVE_CHAR = charId;
  ACTIVE_META = CHARS.find((c) => c.char_id === charId) || null;
  renderCharList();
  $("#empty").style.display = "none";
  $("#panel").classList.add("show");
  const av = $("#avatar");
  av.innerHTML = ACTIVE_META?.cover_url
    ? `<img src="${imgUrl(ACTIVE_META.cover_url)}" />`
    : escapeHtml((ACTIVE_META?.name || "?").slice(0, 1));
  $("#title").textContent = ACTIVE_META?.name || charId;
  $("#sub").textContent = charId + (ACTIVE_META?.lang_name ? ` · ${ACTIVE_META.lang_name}` : "");
  $("#chatLink").href = "/chat.html?char=" + encodeURIComponent(charId);
  $("#feed").innerHTML = '<p style="color:var(--mut)">載入帖子…</p>';
  try {
    const data = await api("/api/feed_posts/" + charId);
    POSTS = data.posts || [];
    EVENTS = data.events || [];
  } catch (e) {
    POSTS = [];
    EVENTS = [];
  }
  renderFeed();
}

/* ---------- 生成 ---------- */
async function pollTask(taskId) {
  for (;;) {
    await new Promise((r) => setTimeout(r, 2500));
    const t = await api("/api/tasks/" + taskId);
    if (t.status === "done") return t.result;
    if (t.status === "error") throw new Error(t.error || "生成失敗");
  }
}

/* 並行生成：不禁用按鈕，每次生成在 feed 頂部插一張「生成中」占位卡；
   完成後替換成真帖。占位卡按角色暫存，切換角色再切回也還在。 */
async function generate(kind) {
  if (!ACTIVE_CHAR) return;
  const charId = ACTIVE_CHAR;
  const body = { char_id: charId, kind, image_model: selectedImageModel() };
  if (kind === "t1") {
    body.hint = $("#t1Hint").value.trim();
  } else {
    body.subtype = $("#t2Subtype").value;
    body.user_name = $("#t2UserName").value.trim();
    body.hint = $("#t2Hint").value.trim();
    body.schedule_text = $("#t2Schedule").value.trim();
  }
  const gid = ++GEN_SEQ;
  pendingOf(charId).push({ id: gid, kind, subtype: kind === "t2" ? body.subtype : null, err: null });
  if (charId === ACTIVE_CHAR) { renderFeed(); $("#feed").scrollTop = 0; }
  try {
    const { task_id } = await api("/api/feed_posts", { method: "POST", body: JSON.stringify(body) });
    const post = await pollTask(task_id);
    removePending(charId, gid);
    if (charId === ACTIVE_CHAR) {
      if (!POSTS.some((p) => p.post_id === post.post_id)) POSTS.unshift(post);
      renderFeed();
    }
    toast("已生成", "ok");
  } catch (e) {
    const pd = pendingOf(charId).find((x) => x.id === gid);
    if (pd) pd.err = e.message;
    if (charId === ACTIVE_CHAR) renderFeed();
    toast("生成失敗：" + e.message, "err");
    setTimeout(() => { removePending(charId, gid); if (charId === ACTIVE_CHAR) renderFeed(); }, 8000);
  }
}

async function rerenderImage(postId, btn) {
  btn.disabled = true;
  btn.textContent = "出圖中…";
  try {
    const { task_id } = await api(`/api/feed_posts/${ACTIVE_CHAR}/${postId}/image?image_model=${encodeURIComponent(selectedImageModel())}`, { method: "POST" });
    const post = await pollTask(task_id);
    const idx = POSTS.findIndex((p) => p.post_id === postId);
    if (idx >= 0) POSTS[idx] = post;
    renderFeed();
    toast("配圖已更新", "ok");
  } catch (e) {
    toast("出圖失敗：" + e.message, "err");
    btn.disabled = false;
    btn.textContent = "重出配圖";
  }
}

async function deletePost(postId) {
  if (!confirm("刪除這條帖子？")) return;
  try {
    await api(`/api/feed_posts/${ACTIVE_CHAR}/${postId}`, { method: "DELETE" });
    POSTS = POSTS.filter((p) => p.post_id !== postId);
    renderFeed();
  } catch (e) {
    toast("刪除失敗：" + e.message, "err");
  }
}

/* ---------- 渲染 ---------- */
const SRC_LABELS = { hater: "黑粉", fan: "粉絲", passerby: "路人", marketing: "營銷號", insider: "知情人", official: "官方", char: "本人" };

async function generateEvent() {
  if (!ACTIVE_CHAR) return;
  const charId = ACTIVE_CHAR;
  const gid = ++GEN_SEQ;
  pendingOf(charId).push({ id: gid, kind: "event", subtype: null, err: null });
  if (charId === ACTIVE_CHAR) { renderFeed(); $("#feed").scrollTop = 0; }
  try {
    const { task_id } = await api("/api/feed_events", { method: "POST",
      body: JSON.stringify({ char_id: charId, hint: $("#evtHint").value.trim(), with_images: $("#evtImages").checked, schedule_text: $("#evtSchedule").value.trim(), image_model: selectedImageModel() }) });
    const ev = await pollTask(task_id);
    removePending(charId, gid);
    if (ev && ev.abstain) {
      if (charId === ACTIVE_CHAR) renderFeed();
      toast("無法生成熱搜：" + (ev.reason || "今天沒有撐得起熱搜的事件"), "err");
      return;
    }
    if (ACTIVE_CHAR === charId) {
      if (!EVENTS.some((e) => e.event_id === ev.event_id)) EVENTS.unshift(ev);
      renderFeed();
    }
    toast("熱搜事件已生成", "ok");
  } catch (e) {
    const pd = pendingOf(charId).find((x) => x.id === gid);
    if (pd) pd.err = e.message;
    if (charId === ACTIVE_CHAR) renderFeed();
    toast("生成失敗：" + e.message, "err");
    setTimeout(() => { removePending(charId, gid); if (charId === ACTIVE_CHAR) renderFeed(); }, 8000);
  }
}

async function deleteEvent(eventId) {
  if (!confirm("刪除這場熱搜事件？")) return;
  try {
    await api(`/api/feed_events/${ACTIVE_CHAR}/${eventId}`, { method: "DELETE" });
    EVENTS = EVENTS.filter((e) => e.event_id !== eventId);
    renderFeed();
  } catch (e) { toast("刪除失敗：" + e.message, "err"); }
}

function renderEventShell(ev) {
  const d = ev.data || {};
  const topic = d.topic || {};
  const posts = d.posts || [];
  const dm = d.char_dm || {};
  const quoted = dm.quote_post_index != null ? posts[dm.quote_post_index] : null;
  const when = ev.created ? new Date(ev.created * 1000).toLocaleString() : "";
  const shell = document.createElement("div");
  shell.className = "post-shell";
  const postBlocks = posts.map((p) => `
    <div class="floor">
      <div class="floor-top"><span class="floor-no">${escapeHtml(B(p.time_label))}</span>
        <span style="font-weight:700">${escapeHtml(B((p.account || {}).name))}</span>
        <span class="floor-op-badge" style="background:${p.source_type === "char" ? "var(--accent)" : "var(--mut)"}">${escapeHtml(SRC_LABELS[p.source_type] || p.source_type || "")}</span>
        <span class="floor-likes">♥ ${num((p.stats || {}).likes)} · 💬 ${num((p.stats || {}).comment_count)}</span></div>
      <div class="floor-body">${renderRich(B(p.content))}</div>
      ${p.image_rendered && !p.image_rendered.error ? `<div class="post-img"><img src="${p.image_rendered.local_path ? "/img/" + escapeHtml(p.image_rendered.local_path.split("/").pop()) : escapeHtml(p.image_rendered.url || "")}" loading="lazy" /></div>` : ""}
      ${(p.comments || []).length ? `<div class="floor-quote">${p.comments.slice(0, 2).map((c) => `${escapeHtml(B(c.author))}：${escapeHtml(B(c.content))}`).join("　|　")}</div>` : ""}
    </div>`).join("");
  const dmBox = (dm.bubbles || []).length ? `<div class="dm-box">
      <div class="dm-label">💬 ${escapeHtml(ev.char_name || "")} 把這場風波裡的一條帖子轉發給了你</div>
      ${quoted ? `<div class="floor-quote">${escapeHtml(B(quoted.content)).slice(0, 80)}…</div>` : ""}
      ${dm.bubbles.map((m) => `<div><span class="dm-bubble">${renderRich(B(m))}</span></div>`).join("")}
    </div>` : "";
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <div class="forum-head" style="border-bottom:1px solid var(--line)">
      <span class="forum-board">🔥 熱搜 · ${num(topic.heat)} 熱度</span>
      <div class="forum-title">${escapeHtml(B(topic.tag))}</div>
      ${B(topic.sub) ? `<div class="forum-meta">${escapeHtml(B(topic.sub))}</div>` : ""}
    </div>
    ${postBlocks}${dmBox}${hooksBlock(d.chat_hooks)}`;
  const evModel = imageModelLabel(ev.image_model_choice);
  shell.innerHTML = `<div class="post-toolbar"><span>熱搜事件 · ${posts.length} 條帖子${ev.chat_material_used ? " · 已用真實聊天素材" : ""}</span>
    <span class="spacer"></span>
    ${evModel ? `<span class="img-model-tag">🖼 ${escapeHtml(evModel)}</span>` : ""}
    <span>${escapeHtml(when)}</span>
    <button class="danger-link" data-delev="${escapeHtml(ev.event_id)}">刪除</button></div>`;
  shell.appendChild(card);
  shell.appendChild(renderRawLog(ev));
  shell.querySelector("[data-delev]").addEventListener("click", () => deleteEvent(ev.event_id));
  return shell;
}

function pendingCard(pd) {
  const shell = document.createElement("div");
  shell.className = "post-shell";
  const label = pd.kind === "t1" ? "T1 · 平臺媒體號"
    : pd.kind === "event" ? "熱搜 · 事件（多方發帖）"
    : `T2 · 角色綁定號${pd.subtype ? " · " + escapeHtml(pd.subtype) : ""}`;
  shell.innerHTML = `<div class="post-toolbar"><span>${label}</span><span class="spacer"></span></div>
    <div class="card" style="padding:22px;text-align:center;color:var(--mut);font-size:13px">
      ${pd.err ? "⚠ 生成失敗：" + escapeHtml(pd.err) : '<span class="spinner"></span> 生成中（帶配圖約 2-4 分鐘）…'}
    </div>`;
  return shell;
}

function renderFeed() {
  const feed = $("#feed");
  feed.innerHTML = "";
  const pend = PENDING[ACTIVE_CHAR] || [];
  if (!POSTS.length && !pend.length && !EVENTS.length) {
    feed.innerHTML = '<p style="color:var(--mut)">還沒有帖子。點上方按鈕生成第一條（可連點多次並行生成）。</p>';
    return;
  }
  pend.forEach((pd) => feed.appendChild(pendingCard(pd)));
  EVENTS.forEach((ev) => feed.appendChild(renderEventShell(ev)));
  POSTS.forEach((post) => {
    const shell = document.createElement("div");
    shell.className = "post-shell";
    const when = post.created ? new Date(post.created * 1000).toLocaleString() : "";
    const kindLabel = post.kind === "t1"
      ? "T1 · 平臺媒體號（全體可見）"
      : `T2 · 角色綁定號（僅本人可見）${post.subtype ? " · " + escapeHtml(post.subtype) : ""}`;
    const matNote = post.kind === "t2"
      ? (post.chat_material_used ? " · 已用真實聊天素材" : " · 無聊天記錄，冷啟動")
      : "";
    const hasImgSpec = post.data?.image && post.data.image.kind && post.data.image.kind !== "none";
    const imgModel = imageModelLabel(post.image_model_choice);
    shell.innerHTML = `<div class="post-toolbar"><span>${kindLabel}${matNote}</span>
      <span class="spacer"></span>
      ${imgModel ? `<span class="img-model-tag">🖼 ${escapeHtml(imgModel)}</span>` : ""}
      <span>${escapeHtml(when)}</span>
      ${hasImgSpec ? `<button class="danger-link" data-reimg="${escapeHtml(post.post_id)}">重出配圖</button>` : ""}
      <button class="danger-link" data-del="${escapeHtml(post.post_id)}">刪除</button></div>`;
    try {
      shell.appendChild(post.kind === "t1" ? renderT1(post) : renderT2(post));
    } catch (e) {
      const err = document.createElement("div");
      err.className = "card";
      err.innerHTML = `<div style="padding:14px;font-size:12px;color:var(--mut)">渲染失敗（${escapeHtml(e.message)}），可展開原始輸出檢視。</div>`;
      shell.appendChild(err);
    }
    shell.appendChild(renderRawLog(post));
    shell.querySelector("[data-del]").addEventListener("click", () => deletePost(post.post_id));
    const reimgBtn = shell.querySelector("[data-reimg]");
    if (reimgBtn) reimgBtn.addEventListener("click", () => rerenderImage(post.post_id, reimgBtn));
    feed.appendChild(shell);
  });
}

function renderRich(text) {
  let s = escapeHtml(text);
  const cname = escapeHtml(ACTIVE_META?.name || "본인");
  s = s.replaceAll("@{char}", `<a href="/chat.html?char=${encodeURIComponent(ACTIVE_CHAR)}" target="_blank" rel="noopener" style="color:var(--blue);font-weight:700;text-decoration:none">@${cname}</a>`);
  s = s.replaceAll("@{user}", `<span style="color:var(--hot);font-weight:700">@${LANG === "ko" ? "나" : "我"}</span>`);
  return s;
}

function commentHtml(c, { selfBadgeDefault } = {}) {
  const isSelf = !!c.char_self;
  const initial = escapeHtml((B(c.author) || "?").replace(/^@/, "").slice(0, 1));
  const badge = c.badge ? B(c.badge) : (isSelf ? (selfBadgeDefault || "본인") : "");
  const replies = (c.replies || []).map((r) => commentHtml(r, { selfBadgeDefault })).join("");
  return `<div class="comment${isSelf ? " self" : ""}">
      <div class="cmt-avatar">${isSelf && ACTIVE_META?.cover_url ? `<img src="${imgUrl(ACTIVE_META.cover_url)}" style="width:100%;height:100%;border-radius:50%;object-fit:cover" />` : initial}</div>
      <div class="cmt-main">
        <div class="cmt-top"><span class="cmt-author">${escapeHtml(B(c.author))}</span>
          ${badge ? `<span class="cmt-badge">${escapeHtml(badge)}</span>` : ""}</div>
        <div class="cmt-body">${renderRich(B(c.content))}</div>
      </div>
      ${c.likes ? `<span class="cmt-likes">♥ ${num(c.likes)}</span>` : ""}
    </div>` + (replies ? `<div class="comment reply" style="display:block;padding:0">${replies}</div>` : "");
}

function commentsBlock(comments, label) {
  if (!comments?.length) return "";
  return `<div class="comments"><div class="comments-label">${escapeHtml(label)}</div>
    ${comments.map((c) => commentHtml(c)).join("")}</div>`;
}

function hooksBlock(hooks) {
  if (!hooks?.length) return "";
  return `<details class="hooks"><summary>🎣 埋進帖子的聊天鉤子（運營視角）</summary>
    <ul>${hooks.map((h) => `<li>${escapeHtml(B(h))}</li>`).join("")}</ul></details>`;
}

function postImageHtml(post) {
  const img = post.image;
  if (!img) return "";
  if (img.error) return `<div class="post-img-err">配圖失敗：${escapeHtml(img.error)}</div>`;
  const src = img.local_path ? "/img/" + escapeHtml(img.local_path.split("/").pop()) : img.url;
  if (!src) return "";
  return `<div class="post-img"><img src="${src}" loading="lazy" /></div>`;
}

function digestBlock(post) {
  const d = post.day_digest;
  if (!d) return "";
  const threads = (d.goal_threads || []).map((t) =>
    `<li><b>${escapeHtml(t.name || t.id || "")}</b>（${escapeHtml(String(t.progress_before ?? "?"))}%→${escapeHtml(String(t.progress_after ?? "?"))}%）${escapeHtml(t.today_step || "")}</li>`).join("");
  const segs = (d.segments || []).map((s) =>
    `<li>${escapeHtml(s.time || "")} · ${escapeHtml(s.location || "")} · ${escapeHtml(s.activity || "")} — ${escapeHtml(s.detail || "")}${s.echo ? ` <span class="echo-tag">echo: ${escapeHtml(s.echo)}</span>` : ""}</li>`).join("");
  return `<details class="hooks"><summary>📅 當日日程依據（伴隨日程生產）· ${escapeHtml(d.day_summary || "")}</summary>
    ${threads ? `<div class="digest-label">目標線推進</div><ul>${threads}</ul>` : ""}
    ${segs ? `<div class="digest-label">日程片段</div><ul>${segs}</ul>` : ""}
    ${d.highlight ? `<div class="digest-label">高光瞬間</div><ul><li>${escapeHtml(d.highlight)}</li></ul>` : ""}</details>`;
}

function renderT1(post) {
  const d = post.data || {};
  if (!d.thread) return renderT1App(post);   // 新 schema：站內 ins 式解剖
  const t = d.thread || {};
  const op = t.op || {};
  const card = document.createElement("div");
  card.className = "card";
  const acct = d.media_account || {};
  const floors = (t.floors || []).map((f) => {
    const quoted = f.reply_to != null
      ? (t.floors || []).find((x) => x.no === f.reply_to)
      : null;
    return `<div class="floor">
      <div class="floor-top"><span class="floor-no">${escapeHtml(String(f.no ?? ""))}층</span>
        <span>${escapeHtml(B(f.author))}</span>
        ${f.is_op ? '<span class="floor-op-badge">글쓴이</span>' : ""}
        <span class="floor-likes">${f.likes ? "👍 " + num(f.likes) : ""}</span></div>
      ${quoted ? `<div class="floor-quote">${escapeHtml(String(quoted.no))}층: ${escapeHtml(B(quoted.content).slice(0, 60))}</div>` : ""}
      <div class="floor-body">${escapeHtml(B(f.content))}</div>
    </div>`;
  }).join("");
  card.innerHTML = `
    <div class="acct">
      <div class="acct-avatar">📰</div>
      <div><div class="acct-name">${escapeHtml(B(acct.name))} <span class="verified">✔</span></div>
        <div class="acct-sub">${escapeHtml(acct.handle || "")} · ${escapeHtml(B(acct.bio))}</div></div>
      <span class="acct-chip">팔로우</span>
    </div>
    <div class="caption">${escapeHtml(B(d.caption))}</div>
    <div class="hashtags">${(d.hashtags || []).map((h) => `<span>${escapeHtml(B(h).startsWith("#") ? B(h) : "#" + B(h))}</span>`).join("")}</div>
    <div class="forum-shot">
      <div class="forum-head">
        <span class="forum-board">${escapeHtml(B(t.board))}</span>
        <div class="forum-title">${escapeHtml(B(t.title))}</div>
        <div class="forum-meta"><span>${escapeHtml(B(op.author) || "익명")}</span>
          <span>${escapeHtml(op.time || "")}</span>
          <span>조회 ${num(op.views)}</span><span>댓글 ${num(op.comment_count)}</span></div>
      </div>
      <div class="forum-op">${escapeHtml(B(op.content))}${postImageHtml(post)}</div>
      <div class="vote-row">
        <div class="vote up"><span class="pill">추천</span><span>${num(op.upvotes)}</span></div>
        <div class="vote down"><span class="pill">반대</span><span>${num(op.downvotes)}</span></div>
      </div>
      <div class="floors-label">댓글 ${num(op.comment_count || (t.floors || []).length)}</div>
      ${floors}
    </div>
    ${commentsBlock(d.outer_comments, LANG === "ko" ? "이 게시물에 달린 댓글" : "帖子下的平臺評論")}
    ${hooksBlock(d.chat_hooks)}`;
  return card;
}

function renderT1App(post) {
  const d = post.data || {};
  const acct = d.account || {};
  const full = B(d.title) ? B(d.title) + "\n" + B(d.content) : B(d.content);
  const nl = full.indexOf("\n");
  const hook = nl < 0 ? full : full.slice(0, nl).trim();
  const body = nl < 0 ? "" : full.slice(nl + 1).trim();
  const card = document.createElement("div");
  card.className = "card";
  const comments = (d.comments || []).map((c) => ({
    ...c, badge: c.badge || (c.is_op ? { ko: "글쓴이", zh: "發帖人" } : null),
    replies: (c.replies || []).map((r) => ({ ...r, badge: r.badge || (r.is_op ? { ko: "글쓴이", zh: "發帖人" } : null) })),
  }));
  card.innerHTML = `
    <div class="acct">
      <div class="acct-avatar">📰</div>
      <div><div class="acct-name">${escapeHtml(B(acct.name))} <span class="verified">✔</span></div>
        <div class="acct-sub">${escapeHtml(acct.handle || "")} · ${escapeHtml(B(acct.bio))}</div></div>
      <span class="acct-chip">全體可見</span>
    </div>
    <div class="t2-title">${renderRich(hook)}</div>
    <div class="t2-content">${renderRich(body)}${postImageHtml(post)}</div>
    <div class="floors-label" style="padding:10px 14px 0">❤ ${num((d.stats || {}).likes)} · 評論 ${num((d.stats || {}).comment_count || comments.length)}（站內收進評論面板）</div>
    ${commentsBlock(comments, LANG === "ko" ? "댓글" : "評論")}
    ${hooksBlock(d.chat_hooks)}`;
  return card;
}

function renderT2(post) {
  const d = post.data || {};
  const acct = d.account || {};
  const card = document.createElement("div");
  card.className = "card";
  const timeline = (d.timeline || []).length
    ? `<div class="timeline">${d.timeline.map((it) => `
        <div class="tl-item"><div class="tl-date">${escapeHtml(B(it.date))}</div>
        <div class="tl-text">${escapeHtml(B(it.text))}</div></div>`).join("")}</div>`
    : "";
  const dm = (d.char_dm || []).length
    ? `<div class="dm-box">
        <div class="dm-label">💬 ${escapeHtml(post.char_name || "")} ${LANG === "ko" ? "님이 이 게시물을 너에게 보냈어요" : "把這條帖子轉發給了你"}</div>
        ${d.char_dm.map((m) => `<div><span class="dm-bubble">${renderRich(B(m))}</span></div>`).join("")}
        <a class="dm-cta" href="/chat.html?char=${encodeURIComponent(post.char_id)}" target="_blank" rel="noopener">${LANG === "ko" ? "답장하러 가기 →" : "去回覆 TA →"}</a>
      </div>` : "";
  card.innerHTML = `
    <div class="acct">
      <div class="acct-avatar" style="background:var(--accent-soft);color:var(--accent)">${escapeHtml((B(acct.name) || "?").slice(0, 1))}</div>
      <div><div class="acct-name">${escapeHtml(B(acct.name))}</div>
        <div class="acct-sub">${escapeHtml(acct.handle || "")} · ${escapeHtml(B(acct.relation))}</div></div>
      <span class="acct-chip" style="color:var(--accent);border-color:var(--accent)">${LANG === "ko" ? "나만 보임" : "僅自己可見"}</span>
    </div>
    ${d.subtype ? `<span class="subtype-chip">${escapeHtml(d.subtype)}${d.mention_user ? " · @너" : ""}</span>` : ""}
    <div class="t2-content">${renderRich(B(d.content))}${postImageHtml(post)}</div>
    ${timeline}
    ${commentsBlock(d.outer_comments, LANG === "ko" ? "댓글" : "評論")}
    ${dm}
    ${digestBlock(post)}
    ${hooksBlock(d.chat_hooks)}`;
  return card;
}

function renderRawLog(post) {
  const det = document.createElement("details");
  det.className = "rawlog";
  const log = post.call_log || {};
  det.innerHTML = `<summary>原始輸出 / prompt</summary>
    <pre>${escapeHtml(JSON.stringify(post.data, null, 2))}</pre>
    <pre>${escapeHtml((log.messages || []).map((m) => `--- ${m.role} ---\n${m.content}`).join("\n\n"))}</pre>`;
  return det;
}

/* ---------- 事件 ---------- */
$("#search").addEventListener("input", renderCharList);
$("#btnT1").addEventListener("click", () => generate("t1"));
$("#btnT2").addEventListener("click", () => generate("t2"));
$("#btnEvt").addEventListener("click", generateEvent);
$("#langSwitch").addEventListener("click", (e) => {
  const btn = e.target.closest(".lang-btn");
  if (!btn) return;
  LANG = btn.dataset.lang;
  document.querySelectorAll(".lang-btn").forEach((b) => b.classList.toggle("active", b === btn));
  renderFeed();
});

init();
