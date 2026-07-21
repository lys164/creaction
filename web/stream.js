/* 發現流：聚合所有角色的第三方帖子，按站內 ins 式解剖展示。
   卡片(配圖+一行標題) → 點開詳情(正文) → 點評論按鈕(評論面板)。 */
const $ = (s) => document.querySelector(s);

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(url, opts = {}) {
  const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function imgUrl(u, w) {
  if (!u) return null;
  return w ? u + (u.includes("?") ? "&" : "?") + "w=" + w : u;
}

let LANG = "zh";
let POSTS = [];
let CURRENT = null; // 當前詳情帖（規整後）

/* 回覆對象：{ci, replyTo, targetId}。targetId="main"=發新主評論；
   否則="ci"（回覆主評論）或"ci:ri"（回覆某條回覆，視覺仍平鋪在主評論下）。 */
let REPLY = { ci: -1, replyTo: "", targetId: "main" };
const DRAFTS = {};   // 以「帖子 × 評論對象」為維度隔離的未發送草稿
let SENDING = false;

window.avFail = (img, icon) => { const p = img.parentNode; if (p) p.innerHTML = `<i class="ti ti-${icon}"></i>`; };
window.heroFail = (img, sel) => { const box = img.closest(sel); if (box) box.remove(); };

function B(field) {
  if (field == null) return "";
  if (typeof field === "string") return field;
  if (typeof field === "object") return field[LANG] ?? field.ko ?? field.zh ?? "";
  return String(field);
}

function num(n) {
  const v = Number(n);
  if (!isFinite(v) || v <= 0) return "";
  return v >= 10000 ? (v / 10000).toFixed(1).replace(/\.0$/, "") + "만" : v.toLocaleString();
}

function clip(s, n) {
  s = String(s ?? "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function relTime(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  const [m, h, d] = LANG === "ko" ? ["분 전", "시간 전", "일 전"] : ["分鐘前", "小時前", "天前"];
  if (diff < 3600) return Math.max(1, Math.floor(diff / 60)) + m;
  if (diff < 86400) return Math.floor(diff / 3600) + h;
  return Math.floor(diff / 86400) + d;
}

function postImageSrc(post) {
  const img = post.image;
  if (!img || img.error) return null;
  return img.local_path ? "/img/" + img.local_path.split("/").pop() : img.url || null;
}

/* ---------- 規整：新舊 schema 統一成 {acct,title,content,comments,stats,…} ---------- */
function flattenComments(list, isOpKey) {
  return (list || []).map((c) => ({
    author: c.author, content: c.content, likes: c.likes,
    is_op: !!c[isOpKey], char_self: !!c.char_self, badge: c.badge,
    is_user: !!c.is_user, created: c.created || 0,
    replies: (c.replies || []).map((r) => ({
      author: r.author, content: r.content, likes: r.likes,
      is_op: !!r[isOpKey], char_self: !!r.char_self,
      is_user: !!r.is_user, reply_to: r.reply_to || "", created: r.created || 0,
    })),
  }));
}

function normPost(post) {
  const d = post.data || {};
  const n = { post, kind: post.kind, image: postImageSrc(post) };
  if (post.kind === "event") {        // 熱搜事件（新增類，T2 不變）：聚合頁展示
    n.acct = { name: post.char_name };
    n.comments = [];
    n.eventData = post.data || {};
    n.visLabel = "";
    n.canComment = false;
    n.likes = Number(((post.data || {}).topic || {}).heat) || 0;
    n.commentCount = ((post.data || {}).posts || []).length;
    return n;
  }
  if (post.kind === "own") {          // 角色本人的圖文帖
    n.acct = { name: post.char_name };
    n.content = (post.data || {}).content;
    n.comments = flattenComments(d.comments, "is_op");
    n.subtype = post.subtype;
    n.visLabel = "";
    const st = d.stats || {};
    n.likes = Number(st.likes) || 0;
    n.commentCount = Number(st.comment_count) || n.comments.length;
    n.canComment = false;  // own 帖暫只展示平台評論，不開放使用者續寫
    return n;
  }
  n.commentKey = null;   // 後端可回覆的評論列表鍵（新 schema 才 1:1 對得上索引）
  if (post.kind === "t1") {
    if (d.thread) {           // 舊 schema：論壇帖 → 映射到 app 解剖
      const t = d.thread, op = t.op || {};
      n.acct = d.media_account || {};
      n.titleLegacy = t.title;
      n.content = op.content;
      n.comments = flattenComments(t.floors, "is_op").concat(flattenComments(d.outer_comments));
    } else {                   // 新 schema：account/content/comments/image
      n.acct = d.account || {};
      n.titleLegacy = d.title; // 過渡期帖子可能還帶 title
      n.content = d.content;
      n.comments = flattenComments(d.comments, "is_op");
      n.commentKey = "comments";
    }
    n.verified = true;
    n.visLabel = LANG === "ko" ? "전체 공개" : "全部可見";
  } else {
    n.acct = d.account || {};
    n.titleLegacy = d.title;
    n.content = d.content;
    n.subtype = d.subtype || post.subtype;
    n.timeline = d.timeline || [];   // 舊帖的年表陣列；新帖年表寫在正文裡
    n.comments = flattenComments(d.outer_comments);
    n.commentKey = "outer_comments";
    n.dm = d.char_dm || [];
    n.mention = d.mention_user;
    n.visLabel = LANG === "ko" ? "나만 보기" : "只我可見";
  }
  n.canComment = post.kind !== "own" && !!n.commentKey;
  // 帖子級點讚數（mock demo；舊論壇帖用 op.upvotes 兜底）
  const st = d.stats || {};
  n.likes = Number(st.likes) || Number(d.thread?.op?.upvotes) || 0;
  n.commentCount = Number(st.comment_count) || n.comments.length;
  return n;
}

/* @token 渲染：@{char}=角色連結(跳落地頁，demo 先指向聊天頁)；@{user}=紅點提及 */
function renderRich(text, n) {
  let s = escapeHtml(text);
  const cname = escapeHtml(n.post.char_name || "본인");
  s = s.replaceAll("@{char}", `<a class="mention char" href="/chat.html?char=${encodeURIComponent(n.post.char_id)}" target="_blank" rel="noopener">@${cname}</a>`);
  // @{user} 只在 T2 生效；T1 是公開拉新帖，不指向具體使用者，違規 token 一律清掉
  s = (n.kind === "t2" || n.kind === "event")
    ? s.replaceAll("@{user}", `<span class="mention user">@${LANG === "ko" ? "나" : "我"}</span>`)
    : s.replaceAll("@{user}", "");
  return s;
}

function hasUserMention(post) {
  if (post.kind !== "t2" && post.kind !== "event") return false;   // 紅點提及屬於 T2 與熱搜事件
  try { return JSON.stringify(post.data || {}).includes("@{user}"); } catch (e) { return false; }
}

/* content 第一行＝卡片鉤子行；舊帖用 title 當鉤子行 */
function hookAndBody(n) {
  if (n.titleLegacy && B(n.titleLegacy)) return [B(n.titleLegacy), B(n.content)];
  const s = B(n.content);
  const i = s.indexOf("\n");
  return i < 0 ? [s, ""] : [s.slice(0, i).trim(), s.slice(i + 1).trim()];
}

/* ---------- 載入 ---------- */
async function load() {
  const feed = $("#feed");
  feed.innerHTML = '<p class="empty">載入中…</p>';
  try {
    const data = await api("/api/feed_stream?limit=100");
    POSTS = data.posts || [];
  } catch (e) {
    feed.innerHTML = `<p class="empty">載入失敗：${escapeHtml(e.message)}</p>`;
    return;
  }
  render();
}

function avatarHtml(n) {
  if (n.kind === "t1") return '<i class="ti ti-news"></i>';
  if (n.kind === "own") return n.post.char?.cover_url
    ? `<img src="${escapeHtml(imgUrl(n.post.char.cover_url, 120))}" onerror="avFail(this,'user')" />`
    : '<i class="ti ti-user"></i>';
  return n.post.char?.cover_url
    ? `<img src="${escapeHtml(imgUrl(n.post.char.cover_url, 120))}" onerror="avFail(this,'eye')" />`
    : '<i class="ti ti-eye"></i>';
}

const SRC_LABELS = { hater: "黑粉", fan: "粉絲", passerby: "路人", marketing: "營銷號", insider: "知情人", official: "官方", char: "本人" };

/* ---------- 熱搜組件卡（feed 裡的入口） ---------- */
function eventCard(n, i) {
  const ev = n.eventData, topic = ev.topic || {}, posts = ev.posts || [];
  const srcs = [...new Set(posts.map((p) => p.source_type))].slice(0, 5)
    .map((t) => `<span class="src-chip src-${escapeHtml(t || "")}">${SRC_LABELS[t] || t || ""}</span>`).join("");
  const el = document.createElement("article");
  el.className = "pcard event";
  el.innerHTML = `
    <div class="ev-head"><span class="ev-flame"><i class="ti ti-flame"></i> 熱搜</span>
      <span class="ev-heat">${num(topic.heat) || ""} 熱度</span></div>
    <div class="ev-tag">${escapeHtml(B(topic.tag))}</div>
    ${B(topic.sub) ? `<div class="ev-sub">${escapeHtml(B(topic.sub))}</div>` : ""}
    <div class="ev-meta">${posts.length} 條帖子 · ${srcs}</div>
    <div class="pfoot"><span>${escapeHtml(relTime(n.post.created))}${hasUserMention(n.post) ? ` <span class="mention-flag"><span class="reddot"></span>提到了我</span>` : ""}</span>
      <span class="more">看事件全貌 <i class="ti ti-chevron-right"></i></span></div>`;
  el.addEventListener("click", () => openDetail(i));
  return el;
}

/* ---------- 事件聚合頁（時間正序：來龍去脈） ---------- */
function eventPostHtml(p, n) {
  const isChar = p.source_type === "char";
  const name = B((p.account || {}).name) || "匿名";
  const av = isChar && n.post.char?.cover_url
    ? `<img src="${escapeHtml(imgUrl(n.post.char.cover_url, 80))}" onerror="avFail(this,'user')" />`
    : escapeHtml(name.replace(/^@/, "").slice(0, 1));
  const st = p.stats || {};
  const cmts = (p.comments || []).slice(0, 3).map((c) =>
    `<div class="evp-cmt"><b>${escapeHtml(B(c.author))}</b>：${escapeHtml(B(c.content))}${c.likes ? ` <span class="dcmt-like">♥ ${num(c.likes)}</span>` : ""}</div>`).join("");
  return `<div class="evp${isChar ? " char" : ""}">
    <div class="evp-time">${escapeHtml(B(p.time_label))}</div>
    <div class="evp-card">
      <div class="evp-top"><div class="dcmt-av">${av}</div>
        <span class="evp-name">${escapeHtml(name)}</span>
        <span class="src-chip src-${escapeHtml(p.source_type || "")}">${SRC_LABELS[p.source_type] || ""}</span>
        <span class="evp-stats">♥ ${num(st.likes) || 0} · 💬 ${num(st.comment_count) || 0}</span></div>
      <div class="evp-body">${renderRich(B(p.content), n)}</div>
      ${p.image_rendered && !p.image_rendered.error ? `<div class="d-img"><img src="${p.image_rendered.local_path ? "/img/" + escapeHtml(p.image_rendered.local_path.split("/").pop()) : escapeHtml(p.image_rendered.url || "")}" loading="lazy" /></div>` : ""}
      ${cmts ? `<div class="evp-cmts">${cmts}</div>` : ""}
    </div></div>`;
}

function eventDetailHtml(n) {
  const ev = n.eventData, topic = ev.topic || {}, posts = ev.posts || [];
  const dm = ev.char_dm || {};
  const quoted = dm.quote_post_index != null ? posts[dm.quote_post_index] : null;
  const dmBox = (dm.bubbles || []).length ? `<div class="dm-box">
      <div class="dm-label"><i class="ti ti-send"></i> ${escapeHtml(n.post.char_name || "")} 把這場風波裡的一條帖子轉發給了你</div>
      ${quoted ? `<div class="ev-quote">${escapeHtml(clip(B(quoted.content), 70))}</div>` : ""}
      ${dm.bubbles.map((m) => `<div><span class="dm-bubble">${renderRich(B(m), n)}</span></div>`).join("")}
      <a class="dm-cta" href="/chat.html?char=${encodeURIComponent(n.post.char_id)}" target="_blank" rel="noopener">去回覆 TA →</a></div>` : "";
  return `<div class="ev-dhead">
      <div class="ev-tag big">${escapeHtml(B(topic.tag))}</div>
      ${B(topic.sub) ? `<div class="ev-sub" style="padding-left:0">${escapeHtml(B(topic.sub))}</div>` : ""}
      <div class="ev-meta" style="padding-left:0">${num(topic.heat) || ""} 熱度 · ${posts.length} 條帖子 · 按時間順序</div>
    </div>
    <div class="ev-line">${posts.map((p) => eventPostHtml(p, n)).join("")}</div>
    ${dmBox}`;
}

/* ---------- 卡片：配圖 + 帳號行 + 一行標題（和站內 ins 帖同構） ---------- */
function card(n, i) {
  const el = document.createElement("article");
  el.className = "pcard " + n.kind;
  const hero = n.image
    ? `<div class="hero"><img src="${escapeHtml(n.image)}" loading="lazy" onerror="heroFail(this,'.hero')" /></div>`
    : "";
  const [hook, body] = hookAndBody(n);
  const canDel = n.kind === "t1" || n.kind === "t2";
  el.innerHTML = `
    ${canDel ? `<button class="card-del" data-del title="刪除這條帖子" aria-label="刪除"><i class="ti ti-trash"></i></button>` : ""}
    ${hero}
    <div class="phead">
      <div class="pav ${n.kind === "t1" ? "news" : "eye"}">${avatarHtml(n)}</div>
      <div class="pmeta">
        <div class="pname">${escapeHtml(B(n.acct.name) || n.post.char_name || "")}
          ${n.verified ? '<i class="ti ti-rosette-discount-check verified"></i>' : ""}</div>
        <div class="psub">${n.visLabel ? `<span class="vis ${n.kind === "t1" ? "pub" : "pri"}">${n.kind === "t2" ? '<i class="ti ti-lock"></i> ' : ""}${escapeHtml(n.visLabel)}</span>` : `<span>${escapeHtml(relTime(n.post.created))}</span>`}</div>
      </div>
      ${n.kind === "own"
        ? `<span class="chip own">${LANG === "ko" ? "채팅" : "聊天"}</span>`
        : n.kind === "t2"
          ? `<span class="chip pri">${LANG === "ko" ? "너만 보기" : "只你可見"}</span>`
          : `<span class="chip pub">${LANG === "ko" ? "화제" : "話題"}</span>`}
    </div>
    <div class="ptitle">${renderRich(clip(hook, 60), n)}</div>
    ${body ? `<div class="pcap">${renderRich(clip(body, 140), n)}</div>` : ""}
    <div class="pfoot">
      <span>${escapeHtml(relTime(n.post.created))}${hasUserMention(n.post) ? ` <span class="mention-flag"><span class="reddot"></span>${LANG === "ko" ? "나를 언급했어요" : "提到了我"}</span>` : ""}</span>
      <span class="pfoot-acts"><i class="ti ti-message-circle"></i> ${n.commentCount ? num(n.commentCount) : n.comments.length}&nbsp;&nbsp;<i class="ti ti-heart"></i> ${n.likes ? num(n.likes) : ""}</span>
    </div>`;
  el.addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    if (e.target.closest("[data-del]")) { e.stopPropagation(); deletePost(n.post, el); return; }
    openDetail(i);
  });
  return el;
}

async function deletePost(post, el) {
  if (!confirm(`刪除這條帖子？（${post.kind.toUpperCase()} · ${post.char_name || ""}）`)) return;
  try {
    await api(`/api/feed_posts/${encodeURIComponent(post.char_id)}/${encodeURIComponent(post.post_id)}`, { method: "DELETE" });
    POSTS = POSTS.filter((p) => p.post_id !== post.post_id);
    if (el) el.remove(); else render();
    if (CURRENT && CURRENT.post.post_id === post.post_id) closeDetail();
  } catch (e) {
    alert("刪除失敗：" + e.message);
  }
}

function render() {
  const feed = $("#feed");
  feed.innerHTML = "";
  if (!POSTS.length) {
    feed.innerHTML = '<p class="empty">還沒有第三方帖子。先去生產台生成幾條。</p>';
    return;
  }
  POSTS.forEach((post, i) => {
    const n = normPost(post);
    feed.appendChild(n.kind === "event" ? eventCard(n, i) : card(n, i));
  });
}

/* ---------- 詳情：正文層（評論收進面板） ---------- */
function detailHtml(n) {
  const [hook, body] = hookAndBody(n);
  const timeline = (n.timeline || []).length
    ? `<div class="timeline">${n.timeline.map((it) => `<div class="tl-item">
        <div class="tl-date">${escapeHtml(B(it.date))}</div><div class="tl-text">${escapeHtml(B(it.text))}</div></div>`).join("")}</div>` : "";
  const dm = (n.dm || []).length
    ? `<div class="dm-box"><div class="dm-label"><i class="ti ti-send"></i> ${escapeHtml(n.post.char_name || "")} ${LANG === "ko" ? "님이 이 게시물을 너에게 보냈어요" : "把這條帖子轉發給了你"}</div>
        ${n.dm.map((m) => `<div><span class="dm-bubble">${escapeHtml(B(m))}</span></div>`).join("")}
        <a class="dm-cta" href="/chat.html?char=${encodeURIComponent(n.post.char_id)}" target="_blank" rel="noopener">${LANG === "ko" ? "답장하러 가기 →" : "去回覆 TA →"}</a></div>` : "";
  return `
    <div class="d-acct">
      <div class="d-acct-av ${n.kind === "t1" ? "news" : "eye"}">${avatarHtml(n)}</div>
      <div><div class="d-acct-name">${escapeHtml(B(n.acct.name) || n.post.char_name || "")} ${n.verified ? '<i class="ti ti-rosette-discount-check verified"></i>' : ""}</div>
        <div class="d-acct-sub">${escapeHtml(n.acct.handle || "")}${B(n.acct.bio || n.acct.relation) ? " · " + escapeHtml(B(n.acct.bio || n.acct.relation)) : ""} · <span class="vis ${n.kind === "t1" ? "pub" : "pri"}">${escapeHtml(n.visLabel)}</span></div></div>
      <span style="margin-left:auto;font-size:11.5px;color:var(--mut)">${escapeHtml(relTime(n.post.created))}</span>
    </div>
    ${n.kind === "t2" && n.subtype ? `<span class="subtype-chip">${escapeHtml(n.subtype)}${n.mention ? " · @너" : ""}</span>` : ""}
    <div class="d-title">${renderRich(hook, n)}</div>
    ${n.image ? `<div class="d-img"><img src="${escapeHtml(n.image)}" /></div>` : ""}
    ${body ? `<div class="d-content">${renderRich(body, n)}</div>` : ""}
    ${timeline}${dm}`;
}

/* ---------- 評論面板（樓中樓：主評論 + 扁平回覆列表） ----------
   ci = 主評論在後端列表裡的索引；回覆一律平鋪在主評論下，顯示「回覆 @xxx：」。 */
const INIT_REPLIES = 2;   // 每個主評論默認展示最早 x 條
const STEP_REPLIES = 5;   // 點「查看更多回覆」每次再展開的條數
const REPLY_SHOWN = {};   // { [ci]: 已展開的回覆條數 }

function tt(zh, ko) { return LANG === "ko" ? ko : zh; }

function avatarFor(c, n) {
  const coverUrl = n.post.char?.cover_url;
  const initial = escapeHtml((B(c.author) || "?").replace(/^@/, "").slice(0, 1));
  return c.char_self && coverUrl
    ? `<img src="${escapeHtml(imgUrl(coverUrl, 80))}" onerror="avFail(this,'user')" />` : initial;
}

/* 單條氣泡（主評論或回覆共用）。reply=true 時前綴「回覆 @xxx：」 */
function bubbleHtml(c, n, ci, ri) {
  const isSelf = !!c.char_self, isUser = !!c.is_user;
  const cls = isUser ? "user" : (isSelf ? "self" : "");
  const badge = c.badge ? B(c.badge)
    : (isSelf ? tt("本人", "본인") : (c.is_op ? tt("樓主", "글쓴이") : (isUser ? tt("我", "나") : "")));
  const prefix = c.reply_to
    ? `<span class="reply-prefix">${tt("回覆", "답글")} @${escapeHtml(c.reply_to)}：</span>` : "";
  const canReply = n.canComment && ci != null;
  const target = ri == null ? `${ci}` : `${ci}:${ri}`;
  return `<div class="dcmt${isSelf ? " self" : ""}${isUser ? " user" : ""}">
      <div class="dcmt-av">${avatarFor(c, n)}</div>
      <div class="dcmt-main">
        <div class="dcmt-top"><span class="dcmt-au">${escapeHtml(B(c.author))}</span>
          ${badge ? `<span class="dcmt-badge ${cls}">${escapeHtml(badge)}</span>` : ""}</div>
        <div class="dcmt-body">${prefix}${renderRich(B(c.content), n)}</div>
        ${canReply ? `<button class="reply-btn" data-reply="${escapeHtml(target)}">${tt("回覆", "답글")}</button>` : ""}
      </div>
      ${c.likes ? `<span class="dcmt-like">♥ ${num(c.likes)}</span>` : ""}
    </div>`;
}

/* 一個主評論塊：主評論 + 已展開的扁平回覆 + 「查看 N 條回覆 / 查看更多回覆」 */
function commentBlock(c, n, ci) {
  const replies = c.replies || [];
  const total = replies.length;
  const shown = Math.min(REPLY_SHOWN[ci] != null ? REPLY_SHOWN[ci] : INIT_REPLIES, total);
  let inner = "";
  if (total) {
    inner += replies.slice(0, shown).map((r, ri) => bubbleHtml(r, n, ci, ri)).join("");
    const rest = total - shown;
    if (rest > 0) {
      const step = Math.min(STEP_REPLIES, rest);
      inner += `<button class="expand-btn" data-expand="${ci}" data-step="${step}">${
        shown <= INIT_REPLIES ? tt(`查看 ${rest} 條回覆`, `답글 ${rest}개 보기`)
                              : tt(`查看更多回覆（還有 ${rest} 條）`, `답글 더 보기 (${rest}개)`)}</button>`;
    }
  }
  return `<div class="cmt-block">${bubbleHtml(c, n, ci, null)}
    ${inner ? `<div class="dcmt-replies">${inner}</div>` : ""}</div>`;
}

function renderCommentBody() {
  const n = CURRENT;
  if (!n) return;
  $("#cmtTitle").textContent = tt("評論 ", "댓글 ") + (n.comments.length || 0);
  $("#cmtBody").innerHTML = n.comments.length
    ? n.comments.map((c, ci) => commentBlock(c, n, ci)).join("")
    : `<p class="empty">${tt("還沒有評論，來說第一句", "아직 댓글이 없어요")}</p>`;
}

function openComments() {
  if (!CURRENT) return;
  Object.keys(REPLY_SHOWN).forEach((k) => delete REPLY_SHOWN[k]);
  renderCommentBody();
  $("#cmtSheet").classList.add("show");
  restoreDraft();
}

function closeComments() {
  saveDraft();
  $("#cmtSheet").classList.remove("show");
  clearReplyTarget();
}

/* ---------- 開合 ---------- */
function openDetail(i) {
  CURRENT = normPost(POSTS[i]);
  REPLY = { ci: -1, replyTo: "", targetId: "main" };
  $("#detailBody").innerHTML = CURRENT.kind === "event" ? eventDetailHtml(CURRENT) : detailHtml(CURRENT);
  $("#detailBody").scrollTop = 0;
  $("#cmtCount").textContent = CURRENT.commentCount ? num(CURRENT.commentCount) : (CURRENT.comments.length || "0");
  $("#likeCount").textContent = CURRENT.likes ? num(CURRENT.likes) : "";
  const can = CURRENT.canComment;
  $("#openCompose").style.display = can ? "" : "none";
  $("#cmtInput").disabled = !can;
  $("#btnSend").disabled = !can;
  closeComments();
  $("#overlay").classList.add("show");
  document.body.style.overflow = "hidden";
}

function closeDetail() {
  $("#overlay").classList.remove("show");
  closeComments();
  document.body.style.overflow = "";
  CURRENT = null;
}

/* ---------- 草稿：以「帖子 × 評論對象」為維度隔離 ---------- */
function draftKey() {
  const pid = CURRENT?.post?.post_id || "?";
  return `${pid}::${REPLY.targetId}`;
}
function saveDraft() {
  if (!CURRENT) return;
  const v = $("#cmtInput").value;
  if (v.trim()) DRAFTS[draftKey()] = v; else delete DRAFTS[draftKey()];
}
function restoreDraft() {
  const input = $("#cmtInput");
  input.value = DRAFTS[draftKey()] || "";   // 複用同一對象回填草稿；切換對象則為空
}

/* ---------- 回覆對象 ---------- */
function setReplyTarget(targetId) {
  saveDraft();                 // 切走前先存當前對象的草稿
  const n = CURRENT;
  if (!n || targetId === "main") { clearReplyTarget(); return; }
  const [ci, ri] = targetId.split(":").map(Number);
  const main = n.comments[ci];
  if (!main) { clearReplyTarget(); return; }
  const targetC = ri == null || Number.isNaN(ri) ? main : (main.replies || [])[ri];
  REPLY = { ci, replyTo: B(targetC?.author) || "", targetId };
  $("#replyName").textContent = REPLY.replyTo;
  $("#replyChip").style.display = "";
  restoreDraft();              // 回填新對象的草稿（不同對象則清空）
  const input = $("#cmtInput");
  input.placeholder = tt(`回覆 @${REPLY.replyTo}`, `@${REPLY.replyTo} 님에게 답글`);
  input.focus();
}
function clearReplyTarget() {
  saveDraft();
  REPLY = { ci: -1, replyTo: "", targetId: "main" };
  const chip = $("#replyChip");
  if (chip) chip.style.display = "none";
  const input = $("#cmtInput");
  if (input) { input.placeholder = tt("說點什麼吧...", "댓글을 남겨보세요..."); restoreDraft(); }
}

/* ---------- 發送：使用者評論落檔 → 輪詢 NPC 續寫 ---------- */
async function pollReply(taskId) {
  for (;;) {
    await new Promise((r) => setTimeout(r, 2000));
    const t = await api("/api/tasks/" + taskId);
    if (t.status === "done") return t.result;
    if (t.status === "error") throw new Error(t.error || tt("生成失敗", "생성 실패"));
  }
}

function refreshCurrentFrom(post) {
  const idx = POSTS.findIndex((p) => p.post_id === post.post_id);
  if (idx >= 0) POSTS[idx] = post;
  const scrollTop = $("#cmtBody").scrollTop;
  CURRENT = normPost(post);
  renderCommentBody();
  $("#cmtCount").textContent = CURRENT.commentCount ? num(CURRENT.commentCount) : (CURRENT.comments.length || "0");
  $("#cmtBody").scrollTop = scrollTop;
}

async function sendComment() {
  if (SENDING || !CURRENT || !CURRENT.canComment) return;
  const input = $("#cmtInput");
  const text = input.value.trim();
  if (!text) return;
  const commentIndex = REPLY.targetId === "main" ? -1 : REPLY.ci;
  const replyTo = REPLY.targetId === "main" ? "" : REPLY.replyTo;
  const body = { comment_index: commentIndex, text, reply_to: replyTo,
                 user_name: tt("我", "나") };
  SENDING = true;
  $("#btnSend").disabled = true;
  input.value = "";
  delete DRAFTS[draftKey()];
  const typing = document.createElement("div");
  typing.className = "npc-typing";
  typing.innerHTML = `<span class="spinner"></span> ${tt("評論區有人在回你…", "누군가 답글을 다는 중…")}`;
  $("#cmtBody").appendChild(typing);
  $("#cmtBody").scrollTop = $("#cmtBody").scrollHeight;
  try {
    const { char_id, post_id } = CURRENT.post;
    const { task_id } = await api(
      `/api/feed_posts/${encodeURIComponent(char_id)}/${encodeURIComponent(post_id)}/reply`,
      { method: "POST", body: JSON.stringify(body) });
    const post = await pollReply(task_id);
    // 展開被回覆主評論的回覆區，讓使用者的話與 NPC 續寫都可見
    const ci = commentIndex === -1 ? (post.data?.[CURRENT.commentKey]?.length || 1) - 1 : commentIndex;
    REPLY_SHOWN[ci] = 999;
    refreshCurrentFrom(post);
    clearReplyTarget();
    $("#cmtBody").scrollTop = $("#cmtBody").scrollHeight;
  } catch (e) {
    typing.remove();
    input.value = text;   // 失敗回填，別丟使用者的話
    alert(tt("發送失敗：", "전송 실패: ") + e.message);
  } finally {
    SENDING = false;
    $("#btnSend").disabled = false;
  }
}

/* ---------- 事件 ---------- */
$("#cmtBody").addEventListener("click", (e) => {
  const exp = e.target.closest("[data-expand]");
  if (exp) {
    const ci = Number(exp.dataset.expand), step = Number(exp.dataset.step) || STEP_REPLIES;
    const cur = REPLY_SHOWN[ci] != null ? REPLY_SHOWN[ci] : INIT_REPLIES;
    REPLY_SHOWN[ci] = cur + step;
    renderCommentBody();
    return;
  }
  const rep = e.target.closest("[data-reply]");
  if (rep) { setReplyTarget(rep.dataset.reply); }
});
$("#btnSend").addEventListener("click", sendComment);
$("#cmtInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); sendComment(); }
});
$("#cmtInput").addEventListener("input", saveDraft);
$("#replyCancel").addEventListener("click", clearReplyTarget);
$("#openCompose").addEventListener("click", () => { openComments(); $("#cmtInput").focus(); });

$("#close").addEventListener("click", closeDetail);
$("#overlay").addEventListener("click", (e) => { if (e.target.id === "overlay") closeDetail(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($("#cmtSheet").classList.contains("show")) closeComments(); else closeDetail();
});
$("#btnComments").addEventListener("click", openComments);
$("#cmtClose").addEventListener("click", closeComments);
$("#langSwitch").addEventListener("click", (e) => {
  const btn = e.target.closest(".lang-btn");
  if (!btn) return;
  LANG = btn.dataset.lang;
  document.querySelectorAll(".lang-btn").forEach((b) => b.classList.toggle("active", b === btn));
  render();
  closeDetail();
});

load();
