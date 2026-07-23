const LANGS = ["zh", "ja", "ko", "en"];
const LANG_NAMES = { zh: "中", ja: "日", ko: "韓", en: "EN" };
const LANG_NAMES_FULL = { zh: "簡體中文", ja: "日本語", ko: "한국어", en: "English" };
// 業務 style 標籤（real|cute|fantasy）的顯示名，用於角色卡徽章與批量打標籤。
const STYLE_NAMES = { real: "real", cute: "cute", fantasy: "fantasy" };

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

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

// 輪詢後臺任務直到完成。onProgress(done,total) 可選。返回任務 result。
async function pollTask(taskId, onProgress) {
  let netRetries = 0;
  while (true) {
    await new Promise((r) => setTimeout(r, 2000));
    let t;
    try {
      t = await api("/api/tasks/" + taskId);
    } catch (e) {
      if (/\b404\b|not found/i.test(e.message)) {
        throw new Error("任務已失效（可能服務已重啟），請重試");
      }
      if (++netRetries > 5) throw new Error("網路異常，任務輪詢中斷：" + e.message);
      continue;
    }
    netRetries = 0;
    if (onProgress) onProgress(t.done_count || 0, t.total || 0);
    if (t.status === "done") return t.result;
    if (t.status === "error") throw new Error(t.error || "任務失敗");
  }
}

// 提交一個返回 {task_id} 的介面並輪詢到完成。
async function runTask(path, opts, onProgress) {
  const r = await api(path, opts);
  if (!r || !r.task_id) return r; // 相容仍同步返回的介面
  return pollTask(r.task_id, onProgress);
}

// 角色語種篩選：各檢視獨立儲存當前選中語種（""=全部）。
const LANG_FILTER = { char: "", ig: "", post: "", ld: "", chat: "" };
const LANG_ORDER = ["zh", "ja", "ko", "en"];

// 渲染語種篩選條。containerId 對應 HTML 裡的 .lang-filter，chars 為完整角色列表，
// onChange 在使用者切換語種時回撥（用於重渲染對應列表）。
function renderLangFilter(containerId, key, chars, onChange) {
  const box = document.getElementById(containerId);
  if (!box) return;
  const present = LANG_ORDER.filter((lg) => chars.some((c) => c.lang === lg));
  // 只剩一種或沒有語種時不顯示篩選條
  if (present.length <= 1) {
    box.innerHTML = "";
    LANG_FILTER[key] = "";
    return;
  }
  const counts = {};
  chars.forEach((c) => { counts[c.lang] = (counts[c.lang] || 0) + 1; });
  const opts = [{ v: "", label: `全部 (${chars.length})` }].concat(
    present.map((lg) => ({ v: lg, label: `${LANG_NAMES_FULL[lg] || lg} (${counts[lg]})` }))
  );
  box.innerHTML = opts
    .map((o) => `<button class="lang-chip${LANG_FILTER[key] === o.v ? " on" : ""}" data-v="${o.v}">${o.label}</button>`)
    .join("");
  box.querySelectorAll(".lang-chip").forEach((b) => {
    b.addEventListener("click", () => {
      LANG_FILTER[key] = b.dataset.v;
      box.querySelectorAll(".lang-chip").forEach((x) =>
        x.classList.toggle("on", x.dataset.v === b.dataset.v)
      );
      onChange();
    });
  });
}

// 按當前篩選語種過濾角色列表。
function filterByLang(chars, key) {
  const lg = LANG_FILTER[key];
  return lg ? chars.filter((c) => c.lang === lg) : chars;
}

// 各檢視獨立儲存當前來源篩選值。"" = 全部，"__none__" = 無來源。
const SOURCE_FILTER = { char: "", ig: "", post: "", ld: "" };

// 渲染來源篩選條：從角色列表裡收集所有 source 值（含"無來源"）。
// key 與 SOURCE_FILTER 對應，各檢視獨立記憶選中來源。
function renderSourceFilter(containerId, key, chars, onChange) {
  const box = document.getElementById(containerId);
  if (!box) return;
  const counts = {};
  let noneN = 0;
  chars.forEach((c) => {
    const s = (c.source || "").trim();
    if (s) counts[s] = (counts[s] || 0) + 1;
    else noneN += 1;
  });
  const sources = Object.keys(counts).sort();
  // 沒有任何顯式來源時不顯示篩選條
  if (!sources.length) {
    box.innerHTML = "";
    SOURCE_FILTER[key] = "";
    return;
  }
  const opts = [{ v: "", label: `全部來源 (${chars.length})` }]
    .concat(sources.map((s) => ({ v: s, label: `${s} (${counts[s]})` })));
  if (noneN) opts.push({ v: "__none__", label: `無來源 (${noneN})` });
  box.innerHTML = opts
    .map((o) => `<button class="lang-chip${SOURCE_FILTER[key] === o.v ? " on" : ""}" data-v="${escapeHtml(o.v)}">${escapeHtml(o.label)}</button>`)
    .join("");
  box.querySelectorAll(".lang-chip").forEach((b) => {
    b.addEventListener("click", () => {
      SOURCE_FILTER[key] = b.dataset.v;
      box.querySelectorAll(".lang-chip").forEach((x) =>
        x.classList.toggle("on", x.dataset.v === b.dataset.v)
      );
      onChange();
    });
  });
}

// 按當前來源篩選過濾角色列表。
function filterBySource(chars, key) {
  const sf = SOURCE_FILTER[key];
  if (!sf) return chars;
  if (sf === "__none__") return chars.filter((c) => !(c.source || "").trim());
  return chars.filter((c) => (c.source || "").trim() === sf);
}

// 不再每次渲染都拼 Date.now() 時間戳——那會讓每張圖的 URL 每次都變、快取永遠命不中。
// 後端 /img 已帶內容版本 ?v=<mtime> 並支援 ETag 協商快取：內容不變時瀏覽器直接命中
// 本地快取（不發請求或僅 304），重繪覆蓋後 mtime/版本變化會自動取到新圖。
// w（選填）：請求縮圖寬度，後端僅接受 200/400/800，其餘忽略回退原圖。
// 列表/卡片用縮圖，詳情/大圖用原圖（不傳 w）。
function imgUrl(local_path, url, w) {
  if (local_path) {
    const name = local_path.split("/").pop();
    return "/img/" + name + (w ? "?w=" + w : "");
  }
  // url 常常已是本服務的 /img/xxx.png（封面等）；此前直接原樣返回，w 被丟棄，
  // 結果下發 3MB+ 原圖 → 列表/帖子頁極慢。這裡對本服務圖片補上縮略寬度。
  if (w && url && /^\/(img|upload)\//.test(url)) {
    return url + (url.includes("?") ? "&" : "?") + "w=" + w;
  }
  return url;
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

// Render persona fields that may be string / string[] / object[].
const SUBFIELD_LABELS = {
  summary: "概述", decisive_event: "關鍵經歷", imprint: "核心印記",
  response: "應對方式", cost: "代價/矛盾", desire_outer: "聲稱要的",
  desire_inner: "真正要的", desire_bottom_line: "底線", healing: "治癒條件",
  note: "註釋", messages: "開場白",
};

function fmtField(v) {
  if (v == null) return "";
  if (typeof v === "string") return escapeHtml(v);
  if (Array.isArray(v)) {
    const items = v.map((it) => {
      if (typeof it === "string") return `<li>${escapeHtml(it)}</li>`;
      if (it && typeof it === "object") {
        // backstory {stage,detail} or family/social {name,relation,info,dynamic}
        if (it.stage || it.detail) {
          return `<li><b>${escapeHtml(it.stage || "")}</b>：${escapeHtml(it.detail || "")}</li>`;
        }
        if (it.type || (it.content && !it.relation) || (it.data && it.data.content)) {
          const isVoice = it.type === "voice";
          const text = (it.data && it.data.content != null) ? it.data.content : (it.content || "");
          const tag = isVoice ? `<span class="muted">🎤</span> ` : "";
          return `<li>${tag}${escapeHtml(text)}</li>`;
        }
        const head = [it.name, it.relation].filter(Boolean).map(escapeHtml).join(" · ");
        const tail = [it.info, it.dynamic].filter(Boolean).map(escapeHtml).join("；");
        return `<li>${head ? `<b>${head}</b>` : ""}${tail ? "：" + tail : ""}</li>`;
      }
      return `<li>${escapeHtml(String(it))}</li>`;
    });
    return `<ul class="pf-list">${items.join("")}</ul>`;
  }
  if (typeof v === "object") {
    const rows = Object.entries(v)
      .filter(([, val]) => val != null && val !== "")
      .map(([k, val]) => {
        const lbl = SUBFIELD_LABELS[k] || k;
        return `<div class="pf-sub"><span class="sk">${escapeHtml(lbl)}</span>${fmtField(val)}</div>`;
      });
    return `<div class="pf-obj">${rows.join("")}</div>`;
  }
  return escapeHtml(String(v));
}

// ---------- view switching ----------
$$(".step").forEach((s) =>
  s.addEventListener("click", (e) => {
    if (!s.dataset.view) return; // 外鏈(如 POPOP ↗)不攔截，走瀏覽器預設行為
    e.preventDefault();
    const v = s.dataset.view;
    $$(".step").forEach((x) => x.classList.toggle("active", x === s));
    $$(".view").forEach((x) => x.classList.toggle("active", x.id === "view-" + v));
    if (v === "upload") initCreateCoverStyle();
    if (v === "characters") loadCharacters();
    if (v === "posts") initPostsView();
    if (v === "igposts") initIgView();
    if (v === "landing") initLandingView();
    if (v === "chat") initChatView();
    if (v === "styles") loadStylesEditor();
  })
);

// ========== UPLOAD VIEW ==========
let pendingFiles = [];
let pendingJson = [];
const dropzone = $("#dropzone");
const fileInput = $("#fileInput");

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("drag");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("drag");
  addFiles(e.dataTransfer.files);
});
fileInput.addEventListener("change", () => addFiles(fileInput.files));

// 直接貼上圖片（Ctrl/Cmd+V）：把剪貼簿裡的圖片當作上傳檔案，不落本地磁碟。
// 僅在「上傳」檢視啟用時響應，且避免幹擾在輸入框裡貼上文字。
function handlePasteImages(e) {
  const uploadActive = document.getElementById("view-upload")?.classList.contains("active");
  if (!uploadActive) return;
  const tag = (e.target && e.target.tagName) || "";
  const isTextInput = /^(INPUT|TEXTAREA)$/.test(tag) || (e.target && e.target.isContentEditable);
  const items = (e.clipboardData || window.clipboardData)?.items || [];
  const files = [];
  for (const it of items) {
    if (it.kind === "file" && it.type.startsWith("image/")) {
      const f = it.getAsFile();
      if (f) files.push(f);
    }
  }
  if (!files.length) return;        // 沒有圖片就放行（比如在輸入框貼上文字）
  if (isTextInput && !files.length) return;
  e.preventDefault();
  addFiles(files);
  toast(`已貼上 ${files.length} 張圖片`, "ok");
}
document.addEventListener("paste", handlePasteImages);

// language multi-select on creation
let CREATE_LANGS = ["zh", "ja", "ko", "en"];
let LANG_LIST = [];
let STYLES = [];
async function initLangPick() {
  if (!LANG_LIST.length) LANG_LIST = await api("/api/languages");
  const box = $("#langPick");
  box.innerHTML = "";
  LANG_LIST.forEach((l) => {
    const lab = document.createElement("label");
    lab.className = "lang-chip";
    const checked = CREATE_LANGS.includes(l.id) ? "checked" : "";
    lab.innerHTML = `<input type="checkbox" value="${l.id}" ${checked}/> ${l.name}`;
    box.appendChild(lab);
  });
}
initLangPick();

async function initCreateCoverStyle() {
  await ensureStyles();
  const sel = $("#createCoverStyle");
  if (!sel) return;
  const prev = sel.value;
  // 非人物鏈路：給一個"不套畫風（寫實底座）"空值預設項，畫風變可選。
  const track = $("#createTrack") && $("#createTrack").value;
  const noStyle = track === "nonhuman"
    ? `<option value="">不套畫風（預設·寫實底座）</option>` : "";
  sel.innerHTML = noStyle +
    STYLES.map((s) => `<option value="${s.id}">${s.name}</option>`).join("");
  if (prev !== null && (prev === "" || STYLES.some((s) => s.id === prev))) sel.value = prev;
}
initCreateCoverStyle();

// 非人物（nonhuman）鏈路：畫風可選（預設不套畫風）。欄位保持可見，重建選項即可。
function syncCreateTrackStyleUI() {
  const field = $("#createCoverStyleField");
  if (field) field.style.display = "";
  initCreateCoverStyle();
}
if ($("#createTrack")) {
  $("#createTrack").addEventListener("change", syncCreateTrackStyleUI);
  syncCreateTrackStyleUI();
}

function refreshStyleSelects() {
  initCreateCoverStyle();
  const bs = $("#batchStyle");
  if (bs) {
    const prev = bs.value;
    bs.innerHTML = STYLES.map((s) => `<option value="${s.id}">${s.name}</option>`).join("");
    if (prev && STYLES.some((s) => s.id === prev)) bs.value = prev;
  }
}

function addFiles(fl) {
  for (const f of fl) {
    const isJson = f.type === "application/json" || /\.json$/i.test(f.name || "");
    if (isJson) pendingJson.push(f);
    else if (f.type.startsWith("image/")) pendingFiles.push(f);
  }
  renderThumbs();
}
// 每個待傳檔案複用同一個 blob URL（WeakMap 快取），不再每次重渲染都新建；
// 檔案從 pendingFiles 移除後（上傳成功/清空）對應 URL 會被 revoke，避免 Blob 記憶體洩漏。
const THUMB_URL_CACHE = new WeakMap();
function renderThumbs() {
  const box = $("#thumbs");
  box.innerHTML = "";
  pendingFiles.forEach((f) => {
    let url = THUMB_URL_CACHE.get(f);
    if (!url) {
      url = URL.createObjectURL(f);
      THUMB_URL_CACHE.set(f, url);
    }
    const img = document.createElement("img");
    img.src = url;
    box.appendChild(img);
  });
  pendingJson.forEach((f) => {
    const chip = document.createElement("span");
    chip.className = "json-chip";
    chip.textContent = "📄 " + (f.name || "characters.json");
    box.appendChild(chip);
  });
  // JSON 匯入時顯示"下載源圖"開關
  const row = $("#dlImageRow");
  if (row) row.style.display = pendingJson.length ? "" : "none";
}
// 清空 pendingFiles 前呼叫：revoke 所有已分配的 blob URL，防止記憶體洩漏。
function revokeThumbUrls(files) {
  files.forEach((f) => {
    const url = THUMB_URL_CACHE.get(f);
    if (url) {
      URL.revokeObjectURL(url);
      THUMB_URL_CACHE.delete(f);
    }
  });
}

$("#btnPersona").addEventListener("click", async () => {
  const langs = $$("#langPick input:checked").map((i) => i.value);
  if (!langs.length) return toast("請至少選擇一種語言", "err");
  const hintText = $("#userHint").value.trim();
  if (!pendingFiles.length && !pendingJson.length && !hintText)
    return toast("請上傳圖片 / 角色 JSON，或在補充要求裡填寫文字", "err");

  const btn = $("#btnPersona");
  btn.disabled = true;
  const st = $("#uploadStatus");
  const withCover = $("#withCoverOnCreate").checked;

  try {
    // JSON 匯入分支：把已有角色 JSON 擴寫成 POPOP 人設
    if (pendingJson.length) {
      st.innerHTML = `<span class="spinner"></span> 正在解析 JSON，併為 ${langs.length} 種語言各自擴寫人設${
        withCover ? " + 封面圖" : ""
      }…（條數多時較慢）`;
      const fd = new FormData();
      pendingJson.forEach((f) => fd.append("files", f));
      fd.append("user_hint", $("#userHint").value);
      fd.append("langs", langs.join(","));
      fd.append("download_image", $("#downloadImage").checked);
      fd.append("with_cover", withCover);
      fd.append("cover_style_id", withCover ? $("#createCoverStyle").value : "");
      fd.append("track", $("#createTrack").value);
      fd.append("style", $("#createStyle").value);
      fd.append("source", $("#createSource").value.trim());
      const r = await runTask("/api/personas/import_json", { method: "POST", body: fd }, (done, total) => {
        st.innerHTML = `<span class="spinner"></span> 匯入中… ${done}/${total} 個角色`;
      });
      const errN = Object.keys(r.cover_errors || {}).length;
      const failN = Object.keys(r.errors || {}).length;
      st.innerHTML = `已匯入 ${r.count} 個角色（按語言拆分）${
        failN ? `，擴寫失敗 ${failN} 個` : ""
      }${withCover ? `，封面失敗 ${errN} 個` : ""}。前往「② 角色」檢視。`;
      toast(`匯入成功${failN || errN ? "（部分失敗）" : ""}`, failN || errN ? "err" : "ok");
      pendingJson = [];
      renderThumbs();
      return;
    }

    // 圖片分支（也相容純文字：無圖時按補充要求生成）
    const textOnly = !pendingFiles.length;
    st.innerHTML = `<span class="spinner"></span> ${textOnly ? "正在按文字" : "正在上傳，並"}為 ${langs.length} 種語言各自生成本土化人設${
      withCover ? " + 封面圖" : ""
    }…（封面圖會額外耗時）`;
    const fd = new FormData();
    pendingFiles.forEach((f) => fd.append("files", f));
    fd.append("user_hint", $("#userHint").value);
    fd.append("one_per_image", $("#onePerImage").checked);
    fd.append("langs", langs.join(","));
    fd.append("with_cover", withCover);
    fd.append("cover_style_id", withCover ? $("#createCoverStyle").value : "");
    fd.append("track", $("#createTrack").value);
    fd.append("style", $("#createStyle").value);
    fd.append("source", $("#createSource").value.trim());
    const r = await runTask("/api/personas", { method: "POST", body: fd }, (done, total) => {
      st.innerHTML = `<span class="spinner"></span> 生成中… ${done}/${total} 組`;
    });
    const errN = Object.keys(r.cover_errors || {}).length;
    const gErrN = (r.group_errors || []).length;
    st.innerHTML = `已生成 ${r.count} 個角色（按語言拆分）${
      withCover ? `，封面失敗 ${errN} 個` : ""
    }${gErrN ? `，${gErrN} 組生成失敗(詳見控制檯)` : ""}。前往「② 角色」檢視。`;
    if (gErrN) console.warn("[personas] 組失敗:", r.group_errors);
    toast(`人設生成${gErrN ? "部分" : ""}成功${errN ? `，${errN} 個封面失敗` : ""}${gErrN ? `，${gErrN} 組失敗` : ""}`,
          (errN || gErrN) ? "err" : "ok");
    revokeThumbUrls(pendingFiles);
    pendingFiles = [];
    renderThumbs();
  } catch (e) {
    st.innerHTML = "失敗：" + e.message;
    toast("生成失敗", "err");
  } finally {
    btn.disabled = false;
  }
});

// ========== CHARACTERS VIEW ==========

let CHAR_LIST = [];

async function loadCharacters() {
  await ensureStyles();
  const bs = $("#batchStyle");
  if (bs) {
    const prev = bs.value;
    bs.innerHTML = STYLES.map((s) => `<option value="${s.id}">${s.name}</option>`).join("");
    if (prev && STYLES.some((s) => s.id === prev)) bs.value = prev;
  }
  CHAR_LIST = await api("/api/characters");
  renderLangFilter("charLangFilter", "char", CHAR_LIST, renderCharList);
  renderSourceFilter("charSourceFilter", "char", CHAR_LIST, renderCharList);
  renderCharList();
}

// 單張角色卡的 HTML。資料取自當前篩選列表，char_id 存到 data 屬性供事件委託用。
function charCardHtml(c) {
  const cover = c.cover_url
    ? `<img class="cover" loading="lazy" decoding="async" src="${imgUrl(null, c.cover_url, 400)}" />`
    : `<div class="cover">無封面</div>`;
  const langTag = c.lang_name
    ? `<span class="lang-badge ${c.lang}">${c.lang_name}</span>`
    : "";
  const exportTag = c.exported
    ? `<span class="export-badge done">已匯出</span>`
    : `<span class="export-badge todo">未匯出</span>`;
  const arcaTag = c.arca_synced
    ? `<span class="export-badge done" title="該角色已同步到 arca-i18n">☁️ 已同步</span>`
    : "";
  const styleTag = c.style
    ? `<span class="style-badge ${esc(c.style)}" title="業務 style 標籤">${STYLE_NAMES[c.style] || esc(c.style)}</span>`
    : `<span class="style-badge none" title="尚未打 style 標籤">style?</span>`;
  const arcaDelBtn = c.arca_synced
    ? `<button class="card-arca-del" title="從POPOP刪除此角色（軟刪，本地資料不受影響）">☁️🗑</button>`
    : "";
  return `<div class="char-card" data-char-id="${esc(c.char_id)}">
    <label class="char-pick" title="多選"><input type="checkbox" class="csel" value="${esc(c.char_id)}" /></label>
    ${arcaDelBtn}${cover}<div class="meta"><div class="name">${langTag}${
    esc(c.name) || "(未命名)"
  }</div><div class="tag">${c.has_identity ? "已生成外貌DNA" : "未生成外貌"}${exportTag}${arcaTag}${styleTag}</div></div>
  </div>`;
}

// 增量渲染狀態：角色多達上千時，一次性 innerHTML 會長時間阻塞主執行緒且瞬間發起
// 上千縮圖請求。改為按批 append，並用哨兵 + IntersectionObserver 滾動到底再續。
const CHAR_RENDER_BATCH = 60;
let _charFiltered = [];
let _charRendered = 0;
let _charObserver = null;

function renderCharBatch() {
  const box = $("#charList");
  const end = Math.min(_charRendered + CHAR_RENDER_BATCH, _charFiltered.length);
  if (end <= _charRendered) return;
  const html = _charFiltered.slice(_charRendered, end).map(charCardHtml).join("");
  const sentinel = $("#charSentinel");
  const frag = document.createRange().createContextualFragment(html);
  if (sentinel) box.insertBefore(frag, sentinel);
  else box.appendChild(frag);
  _charRendered = end;
  if (_charRendered >= _charFiltered.length && _charObserver) {
    _charObserver.disconnect();
    $("#charSentinel")?.remove();
  }
}

function renderCharList() {
  _charFiltered = filterBySource(filterByLang(CHAR_LIST, "char"), "char");
  _charRendered = 0;
  if (_charObserver) { _charObserver.disconnect(); _charObserver = null; }
  const box = $("#charList");
  box.innerHTML = "";
  if (!_charFiltered.length) {
    box.innerHTML = '<p class="muted">沒有符合當前篩選條件的角色。</p>';
    updateSelCount();
    return;
  }
  // 尾部哨兵：進入視口就渲染下一批
  const sentinel = document.createElement("div");
  sentinel.id = "charSentinel";
  sentinel.style.cssText = "grid-column:1/-1;height:1px";
  box.appendChild(sentinel);
  renderCharBatch();
  _charObserver = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) renderCharBatch();
  }, { rootMargin: "600px" });
  _charObserver.observe(sentinel);
  updateSelCount();
}

// 事件委託：整個列表只掛 2 個監聽器，取代每卡 3 個（1500 卡 = 數千監聽器）。
(function bindCharListDelegation() {
  const box = $("#charList");
  if (!box) return;
  box.addEventListener("click", (e) => {
    const card = e.target.closest(".char-card");
    if (!card) return;
    const cid = card.dataset.charId;
    if (e.target.closest(".char-pick")) return; // 勾選不開啟詳情
    const delBtn = e.target.closest(".card-arca-del");
    if (delBtn) {
      const c = CHAR_LIST.find((x) => x.char_id === cid);
      if (c) arcaDeleteOne(c, delBtn);
      return;
    }
    if (cid) showCharDetail(cid);
  });
  box.addEventListener("change", (e) => {
    if (e.target.classList.contains("csel")) updateSelCount();
  });
})();

function selectedCharIds() {
  return $$("#charList .csel:checked").map((i) => i.value);
}
function updateSelCount() {
  const n = selectedCharIds().length;
  $("#selCount").textContent = n ? `已選 ${n} 個` : "";
}

$("#btnSelAll").addEventListener("click", () => {
  $$("#charList .csel").forEach((b) => (b.checked = true));
  updateSelCount();
});

$("#btnSelNone").addEventListener("click", () => {
  $$("#charList .csel").forEach((b) => (b.checked = false));
  updateSelCount();
});

$("#btnBatchCover").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  const styleId = $("#batchStyle").value;
  if (!styleId) return toast("請選擇封面畫風", "err");
  const mode = $("#batchCoverMode").value || "fill_missing";
  if (mode === "image_only" && !confirm("只生圖會複用已有 identity + cover_spec，不會補缺失。缺欄位的角色會失敗。繼續？")) return;
  const btn = $("#btnBatchCover");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "生成封面中…";
  const modeName = { fill_missing: "補缺失+生圖", full: "全套重跑+生圖", image_only: "只生圖" }[mode] || "生成封面";
  toast(`正在為 ${ids.length} 個角色生成封面（${modeName}）…`);
  try {
    const r = await runTask("/api/characters/batch_cover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids, style_id: styleId, mode }),
    }, (done, total) => {
      btn.textContent = `生成封面中… ${done}/${total}`;
    });
    const errN = Object.keys(r.errors || {}).length;
    toast(`已生成 ${r.covered.length} 個封面${errN ? `，${errN} 個失敗` : ""}`, errN ? "err" : "ok");
    loadCharacters();
  } catch (e) {
    toast("批次生成封面失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

// 上一次刪除失敗的角色 id，供「重試失敗項」按鈕使用
let LAST_DELETE_FAILED = [];

function syncRetryDeleteBtn() {
  const btn = $("#btnRetryDelete");
  if (!btn) return;
  if (LAST_DELETE_FAILED.length) {
    btn.classList.remove("hidden");
    btn.textContent = `↻ 重試失敗項 (${LAST_DELETE_FAILED.length})`;
  } else {
    btn.classList.add("hidden");
  }
}

// 執行一次批次刪除並處理進度/結果。btn 用於顯示進度，返回失敗 id 列表。
async function runBatchDelete(ids, btn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "刪除中…";
  try {
    const r = (await runTask("/api/characters/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids }),
    }, (done, total) => {
      btn.textContent = `刪除中… ${done}/${total}`;
    })) || {};
    const deleted = Array.isArray(r.deleted) ? r.deleted : [];
    const failed = Object.keys(r.errors || {});
    LAST_DELETE_FAILED = failed;
    toast(`已刪除 ${deleted.length} 個${failed.length ? `，${failed.length} 個失敗(可重試)` : ""}`,
          failed.length ? "err" : "ok");
    if (failed.length) console.warn("[delete] 失敗:", r.errors);
    $("#charDetail").classList.add("hidden");
    loadCharacters();
  } catch (e) {
    toast("刪除失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    syncRetryDeleteBtn();
  }
}

$("#btnBatchDelete").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  if (!confirm(`刪除 ${ids.length} 個角色？連同其封面/帖子/落地頁一併刪除，不可恢復。`)) return;
  await runBatchDelete(ids, $("#btnBatchDelete"));
});

$("#btnRetryDelete").addEventListener("click", async () => {
  const ids = LAST_DELETE_FAILED.slice();
  if (!ids.length) return;
  if (!confirm(`重試刪除上次失敗的 ${ids.length} 個角色？`)) return;
  await runBatchDelete(ids, $("#btnRetryDelete"));
});

$("#btnBatchExport").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  const btn = $("#btnBatchExport");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "匯出中…";
  try {
    // 非同步任務：後臺並行打包落盤，前端輪詢進度，完成後憑 token 下載 zip。
    const result = await runTask("/api/characters/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids }),
    }, (done, total) => {
      btn.textContent = `打包中… ${done}/${total}`;
    });
    if (!result || !result.download_url) throw new Error("匯出未返回下載連結");
    btn.textContent = "下載中…";
    const a = document.createElement("a");
    a.href = result.download_url;
    a.download = result.filename || "characters_export.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast(`已匯出 ${result.count || ids.length} 個角色`, "ok");
    loadCharacters();
  } catch (e) {
    toast("匯出失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

$("#btnExportTable").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  const btn = $("#btnExportTable");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "導出中…";
  try {
    const res = await fetch("/api/characters/export_table", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids }),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) {}
      throw new Error(detail);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `characters_${ids.length}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast(`已導出 ${ids.length} 個角色的表格`, "ok");
  } catch (e) {
    toast("導出表格失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

$("#btnBatchStyle").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  const style = $("#batchStyleTag").value;
  const btn = $("#btnBatchStyle");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "打標籤中…";
  try {
    const r = await api("/api/characters/set_style", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids, style }),
    });
    const okN = (r.updated || []).length;
    const errN = Object.keys(r.errors || {}).length;
    // 本地列表即時更新 style，免整表重載
    (r.updated || []).forEach((cid) => {
      const c = CHAR_LIST.find((x) => x.char_id === cid);
      if (c) c.style = r.style;
    });
    renderCharList();
    if (errN) {
      console.warn("打 style 標籤失敗明細:", r.errors);
      toast(`已打 style=${r.style}：成功 ${okN}，失敗 ${errN}。詳見主控台。`, "err");
    } else {
      toast(`已給 ${okN} 個角色打上 style=${r.style}`, "ok");
    }
  } catch (e) {
    toast("打 style 標籤失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

$("#btnBatchImport").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  if (!confirm(`按 SKILL 契約直推 ${ids.length} 個角色到後端 /internal/import/character？\n每個角色會上傳資產並做全量契約校驗，(provider, external_character_id) 幂等：重複導入命中同一角色不重複建。`)) return;
  const btn = $("#btnBatchImport");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "導入中…";
  try {
    const result = await runTask("/api/characters/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids }),
    }, (done, total) => {
      btn.textContent = `導入中… ${done}/${total}`;
    });
    const okN = (result.imported || []).length;
    const errEntries = Object.entries(result.errors || {});
    const created = (result.imported || []).filter((x) => x.new_created).length;
    if (errEntries.length) {
      console.warn("契約導入失敗明細:", result.errors);
      toast(`導入完成：成功 ${okN}（新建 ${created}），失敗 ${errEntries.length}。詳見主控台。`, "err");
    } else {
      toast(`已導入 ${okN} 個角色（新建 ${created}，更新 ${okN - created}）`, "ok");
    }
    loadCharacters();
  } catch (e) {
    toast("契約導入失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

async function runArcaSync(btn, ids, { syncPosts = false, force = false } = {}) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "同步中…";
  toast(`正在同步 ${ids.length} 個角色到 arca…`);
  try {
    const rows = await runTask("/api/arca/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids, sync_posts: syncPosts, force }),
    }, (done, total) => {
      btn.textContent = `同步中… ${done}/${total}`;
    });
    // rows: [{char_id, arca_character_id, posts, landing_url, skipped, updated, errors}]
    const list = Array.isArray(rows) ? rows : [];
    const ok = list.filter((r) => r.arca_character_id && !(r.errors || []).length);
    const skipped = list.filter((r) => r.skipped);
    const updated = list.filter((r) => r.updated);
    const failed = list.filter((r) => (r.errors || []).length);
    const nPosts = list.reduce((s, r) => s + (r.posts || []).length, 0);
    let msg = `同步完成：${ok.length} 成功`;
    if (updated.length) msg += `（${updated.length} 為原地更新）`;
    if (syncPosts) msg += `，共 ${nPosts} 條帖子`;
    if (skipped.length) msg += `，${skipped.length} 無變化(跳過)`;
    if (failed.length) msg += `，${failed.length} 有錯誤`;
    toast(msg, failed.length ? "err" : "ok");
    if (failed.length) {
      // 逐角色錯誤列印到控制檯，便於排查（含未配置 base_url/uid 等）
      failed.forEach((r) => console.warn(`[arca-sync] ${r.char_id}:`, (r.errors || []).join("; ")));
    }
    loadCharacters(); // 重新整理「☁️ 已同步」標籤
  } catch (e) {
    toast("同步失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

$("#btnArcaSync").addEventListener("click", () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  const dlg = $("#arcaSyncDialog");
  $("#arcaSyncDialogCount").textContent = `已勾選 ${ids.length} 個角色`;
  $("#arcaOptForce").checked = false;
  $("#arcaOptPosts").checked = false;
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "ok") return;
    runArcaSync($("#btnArcaSync"), ids, {
      force: $("#arcaOptForce").checked,
      syncPosts: $("#arcaOptPosts").checked,
    });
  }, { once: true });
});

$("#btnStorageMigrate").addEventListener("click", async () => {
  if (!confirm("把本地全部存量資料遷移到 arca 雲端儲存？\nJSON 記錄→儲存中臺，圖片→OSS。冪等可重跑，不影響本地資料。")) return;
  const btn = $("#btnStorageMigrate");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "遷移中…";
  try {
    const stats = await runTask("/api/arca/storage/migrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }, (done) => {
      btn.textContent = `遷移中… ${done}`;
    });
    const s = stats || {};
    const parts = ["personas", "post_batches", "ig_batches", "landings", "chats", "styles", "images", "uploads"]
      .filter((k) => s[k]).map((k) => `${k}:${s[k]}`);
    const nErr = (s.errors || []).length;
    toast(`遷移完成 ${parts.join(" ")}${nErr ? `，${nErr} 條失敗(見控制檯)` : ""}`, nErr ? "err" : "ok");
    if (nErr) console.warn("[storage-migrate]", s.errors);
  } catch (e) {
    toast("遷移失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

async function arcaDeleteOne(c, btn) {
  // 角色卡右上角「☁️🗑」：刪除該角色在 POPOP 上的對應角色（軟刪），本地資料不動
  if (!confirm(`⚠️ 從 POPOP 刪除「${c.name || c.char_id}」？\n僅刪 POPOP 側（軟刪），本地角色資料不受影響，之後可重新匯出。`)) return;
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const rows = await runTask("/api/arca/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: [c.char_id] }),
    });
    const r = (Array.isArray(rows) && rows[0]) || {};
    if ((r.errors || []).length) {
      toast(`刪除失敗：${r.errors.join("; ")}`, "err");
    } else {
      toast(r.deleted ? `已從 POPOP 刪除「${c.name || c.char_id}」` : "該角色未同步過，無需刪除", "ok");
    }
    try {
      await loadCharacters(); // 重新整理「已同步」標籤與卡片按鈕（會重建整張卡片，含本按鈕）
    } catch (e2) {
      toast("列表重新整理失敗，請手動重新整理頁面：" + e2.message, "err");
    }
  } catch (e) {
    toast("刪除失敗：" + e.message, "err");
  } finally {
    // loadCharacters 成功時會重建卡片（此按鈕元素被替換，此處操作是安全的無效操作）；
    // 失敗/異常時舊按鈕仍在 DOM 上，必須在這裡恢復，否則永久卡在禁用的「…」狀態。
    btn.disabled = false;
    btn.textContent = "☁️🗑";
  }
}

$("#btnArcaSyncPosts").addEventListener("click", () => {
  const ids = selectedIgCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  if (!confirm(`把 ${ids.length} 個角色的最近一批 INS 帖子同步到 arca-i18n？未同步過的角色會先建立角色。已同步過的帖子會跳過。`)) return;
  runArcaSync($("#btnArcaSyncPosts"), ids, { syncPosts: true });
});

$("#btnBatchPersona").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  if (!confirm(`重新生成 ${ids.length} 個角色的人設？不改圖、不動外貌/封面/帖子，僅重刷人設 schema。`)) return;
  const btn = $("#btnBatchPersona");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "重生中…";
  try {
    const r = await runTask("/api/characters/regenerate_persona", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids, track: $("#regenTrack").value || null }),
    }, (done, total) => { btn.textContent = `重生中… ${done}/${total}`; });
    const errN = Object.keys(r.errors || {}).length;
    toast(`已重生 ${r.regenerated.length} 個${errN ? `，${errN} 個失敗` : ""}`, errN ? "err" : "ok");
    loadCharacters();
  } catch (e) {
    toast("重生失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

$("#btnBatchOpening").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  if (!confirm(`重寫 ${ids.length} 個角色的開場白？依據其它人設資訊生成新的開場白註釋+訊息，其它欄位不變。`)) return;
  const btn = $("#btnBatchOpening");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "重寫中…";
  try {
    const r = await runTask("/api/characters/regenerate_opening", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids }),
    }, (done, total) => { btn.textContent = `重寫中… ${done}/${total}`; });
    const errN = Object.keys(r.errors || {}).length;
    toast(`已重寫 ${r.regenerated.length} 個開場白${errN ? `，${errN} 個失敗` : ""}`, errN ? "err" : "ok");
    loadCharacters();
  } catch (e) {
    toast("批次重寫開場白失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

async function ensureStyles() {
  if (!STYLES.length) STYLES = await api("/api/styles");
  return STYLES;
}

async function showCharDetail(charId) {
  await ensureStyles();
  const rec = await api("/api/character/" + charId);
  const d = $("#charDetail");
  d.classList.remove("hidden");
  const p = rec.persona || {};
  const isNonhuman = rec.track === "nonhuman";
  // 非人物封面：允許可選畫風。預設給一個"不套畫風（寫實底座）"空值選項並選中，
  // 使用者想套某個畫風時再選——僅影響封面，帖子鏈路不受影響。
  const styleOpts =
    (isNonhuman ? `<option value="" selected>不套畫風（預設·寫實底座）</option>` : "") +
    STYLES.map((s) => `<option value="${s.id}">${s.name}</option>`).join("");

  const fields = [
    ["name", "姓名"], ["profile", "側寫"],
    ["species", "物種"], ["gender", "性別"], ["voice", "音色"],
    ["anonymous_identities", "匿名身份"],
    ["personality", "性格"],
    ["opening", "開場白"],
    ["appearance", "外貌穿搭"],
    ["hometown", "出身地"], ["residence", "居住地"],
    ["social_status", "職業/階級"], ["speech_style", "語言習慣"],
    ["perception", "看世界的角度"],
    ["relationship_with_user", "和使用者的關係"], ["relationship_mode", "社交模式"],
    ["love_style", "表達愛的方式"], ["situational_reactions", "情境反應"],
    ["hidden_side", "反差萌"], ["life_details", "生活習慣"],
    ["likes", "愛好"], ["fears", "討厭的東西"], ["wishlist", "願望清單"],
    ["backstory", "成長經歷"], ["family", "家庭成員"],
    ["social_network", "社交關係"], ["premise", "特殊背景/世界觀"],
  ];
  const tags = Array.isArray(p.tags) ? p.tags.join(" / ") : localized(p.tags);
  let fieldHtml = `<div class="pf"><span class="k">標籤</span><div class="v">${tags}</div></div>`;
  fieldHtml += fields
    .map(([k, label]) => {
      const val = p[k];
      const isEmpty = val == null || val === "" ||
        (Array.isArray(val) && val.length === 0) ||
        (typeof val === "object" && !Array.isArray(val) && Object.keys(val).length === 0);
      if (isEmpty) return "";
      return `<div class="pf"><span class="k">${label}</span><div class="v">${fmtField(val)}</div></div>`;
    })
    .join("");

  const coverImg = rec.cover
    ? `<img loading="lazy" decoding="async" src="${imgUrl(rec.cover.local_path, rec.cover.url, 800)}" />`
    : `<div class="muted">尚未生成封面</div>`;

  d.innerHTML = `
    <div class="detail-grid">
      <div class="cover">
        ${coverImg}
        <label class="field"><span>畫風${isNonhuman ? "（可選）" : ""}</span>
          <select id="detailStyle">${styleOpts}</select></label>
        <label class="field"><span>生成模式</span>
          <select id="detailCoverMode">
            <option value="fill_missing">補缺失+生圖</option>
            <option value="full">全套重跑+生圖</option>
            <option value="image_only">只生圖</option>
          </select></label>
        <button class="primary" id="btnCover">重繪封面圖</button>
        <div id="coverStatus" class="status"></div>
        <label class="field" style="margin-top:8px"><span>手動替換封面${rec.cover && rec.cover.manual ? "（目前：手動上傳）" : ""}</span>
          <input type="file" id="coverReplaceFile" accept="image/png,image/jpeg,image/webp,image/gif" /></label>
        <button class="ghost" id="btnReplaceCover">⬆️ 上傳替換封面（匯出用原圖）</button>
        <div id="coverReplaceStatus" class="status"></div>
        <button class="ghost" id="btnOpening" style="margin-top:8px">💬 單獨重寫開場白</button>
        <div id="openingStatus" class="status"></div>
      </div>
      <div class="persona-fields">
        <h3 style="margin-top:0">${rec.lang ? `<span class="lang-badge ${rec.lang}">${LANG_NAMES_FULL[rec.lang] || rec.lang}</span>` : ""}${esc(localized(p.name))} <span class="muted">${esc(charId)}</span>
          <button class="ghost" id="btnEditPersona" style="float:right">✏️ 編輯人設</button></h3>
        <div id="personaView">${fieldHtml}</div>
        <div id="personaEdit" class="hidden">
          <p class="muted" style="margin:4px 0">直接編輯下面的人設 JSON，儲存後生效。匯出、同步都會使用這份最新內容。</p>
          <textarea id="personaJson" spellcheck="false" style="width:100%;min-height:420px;font-family:ui-monospace,monospace;font-size:12px;line-height:1.5">${escapeHtml(JSON.stringify(p, null, 2))}</textarea>
          <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
            <button class="primary" id="btnSavePersona">儲存人設</button>
            <button class="ghost" id="btnFixPersonaJson">🔧 修復格式</button>
            <button class="ghost" id="btnCancelPersona">取消</button>
            <span id="personaEditStatus" class="status"></span>
          </div>
        </div>
        <details><summary class="muted">檢視完整人設 JSON</summary>
          <pre class="kv">${escapeHtml(JSON.stringify(p, null, 2))}</pre></details>
        ${rec.reasoning ? `<details><summary class="muted">檢視生成推理 reasoning</summary>
          <pre class="kv">${escapeHtml(JSON.stringify(rec.reasoning, null, 2))}</pre></details>` : ""}
        ${rec.identity ? `<details><summary class="muted">檢視外貌 identity</summary>
          <pre class="kv">${escapeHtml(JSON.stringify(rec.identity, null, 2))}</pre></details>` : ""}
        ${rec.cover && rec.cover.spec ? `<details><summary class="muted">檢視封面 variable / scene</summary>
          <pre class="kv">${escapeHtml(JSON.stringify(rec.cover.spec, null, 2))}</pre></details>` : ""}
      </div>
    </div>`;
  d.scrollIntoView({ behavior: "smooth" });

  $("#btnCover").addEventListener("click", async () => {
    const styleId = $("#detailStyle").value;
    const mode = $("#detailCoverMode").value || "fill_missing";
    if (mode === "image_only" && !confirm("只生圖會複用已有 identity + cover_spec，不會補缺失。缺欄位會失敗。繼續？")) return;
    const cs = $("#coverStatus");
    const modeName = { fill_missing: "補缺失+生圖", full: "全套重跑+生圖", image_only: "只生圖" }[mode] || "生成封面";
    cs.innerHTML = `<span class="spinner"></span> ${modeName} 中…（約 60-120s）`;
    $("#btnCover").disabled = true;
    try {
      await runTask("/api/cover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ char_id: charId, style_id: styleId, mode }),
      }, (done, total) => {
        cs.innerHTML = `<span class="spinner"></span> ${modeName} 中… ${done}/${total || 1}`;
      });
      cs.innerHTML = "封面已生成。";
      toast("封面生成成功", "ok");
      showCharDetail(charId);
    } catch (e) {
      cs.innerHTML = "失敗：" + e.message;
      toast("封面失敗", "err");
    } finally {
      $("#btnCover").disabled = false;
    }
  });

  $("#btnReplaceCover").addEventListener("click", async () => {
    const input = $("#coverReplaceFile");
    const file = input.files && input.files[0];
    const cs = $("#coverReplaceStatus");
    if (!file) return toast("請先選擇圖片", "err");
    cs.innerHTML = `<span class="spinner"></span> 上傳中…`;
    $("#btnReplaceCover").disabled = true;
    try {
      const fd = new FormData();
      fd.append("char_id", charId);
      fd.append("file", file);
      const r = await fetch("/api/cover/replace", { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
      cs.innerHTML = "封面已替換（匯出用原圖）。";
      toast("封面替換成功", "ok");
      showCharDetail(charId);
    } catch (e) {
      cs.innerHTML = "失敗：" + e.message;
      toast("封面替換失敗：" + e.message, "err");
    } finally {
      $("#btnReplaceCover").disabled = false;
    }
  });

  $("#btnOpening").addEventListener("click", async () => {
    const os = $("#openingStatus");
    os.innerHTML = `<span class="spinner"></span> 正在依據人設重寫開場白…`;
    $("#btnOpening").disabled = true;
    try {
      await api("/api/opening", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ char_id: charId }),
      });
      os.innerHTML = "開場白已重寫。";
      toast("開場白重寫成功", "ok");
      showCharDetail(charId);
    } catch (e) {
      os.innerHTML = "失敗：" + e.message;
      toast("開場白重寫失敗", "err");
    } finally {
      $("#btnOpening").disabled = false;
    }
  });

  $("#btnEditPersona").addEventListener("click", () => {
    const editing = !$("#personaEdit").classList.contains("hidden");
    $("#personaEdit").classList.toggle("hidden", editing);
    $("#personaView").classList.toggle("hidden", !editing);
    $("#btnEditPersona").textContent = editing ? "✏️ 編輯人設" : "✖ 收起編輯";
  });
  $("#btnCancelPersona").addEventListener("click", () => {
    $("#personaEdit").classList.add("hidden");
    $("#personaView").classList.remove("hidden");
    $("#btnEditPersona").textContent = "✏️ 編輯人設";
    $("#personaJson").value = JSON.stringify(p, null, 2);
    $("#personaEditStatus").innerHTML = "";
  });
  $("#btnFixPersonaJson").addEventListener("click", () => {
    const st = $("#personaEditStatus");
    const ta = $("#personaJson");
    const r = repairJsonText(ta.value);
    if (r.ok) {
      ta.value = JSON.stringify(r.value, null, 2);
      if (r.applied.length) {
        st.innerHTML = "已修復：" + esc(r.applied.join("、")) + "。請確認內容無誤後儲存。";
        toast("JSON 已自動修復", "ok");
      } else {
        st.innerHTML = "格式本來就正確，無需修復。";
        toast("格式正確", "ok");
      }
    } else {
      st.innerHTML = "無法自動修復：" + esc(r.error || "請檢查引號是否成對") +
        "。常見原因：字串值裡直接換行、引號沒成對、或用了全形引號。";
      toast("無法自動修復，請手動檢查", "err");
    }
  });

  $("#btnSavePersona").addEventListener("click", async () => {
    const st = $("#personaEditStatus");
    let parsed;
    try {
      parsed = JSON.parse($("#personaJson").value);
    } catch (err) {
      // 嚴格解析失敗時，自動嘗試修復一次，成功則回填並提示。
      const r = repairJsonText($("#personaJson").value);
      if (r.ok) {
        parsed = r.value;
        $("#personaJson").value = JSON.stringify(r.value, null, 2);
        st.innerHTML = "已自動修復格式（" + esc(r.applied.join("、") || "重新格式化") + "）並儲存。";
        toast("已自動修復 JSON 格式", "ok");
      } else {
        st.innerHTML = "JSON 格式錯誤：" + esc(r.error || err.message) +
          "。可點「🔧 修復格式」嘗試自動修復。";
        return toast("人設 JSON 格式錯誤", "err");
      }
    }
    $("#btnSavePersona").disabled = true;
    st.innerHTML = `<span class="spinner"></span> 儲存中…`;
    try {
      await api("/api/persona", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ char_id: charId, persona: parsed }),
      });
      st.innerHTML = "已儲存。";
      toast("人設已儲存", "ok");
      showCharDetail(charId);
    } catch (e) {
      st.innerHTML = "失敗：" + e.message;
      toast("儲存失敗：" + e.message, "err");
    } finally {
      $("#btnSavePersona").disabled = false;
    }
  });
}

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// 通用 HTML 轉義工具：拼 innerHTML 時包裹任意可能來自 LLM/使用者的文字。
// 相容 null/undefined/非字串輸入，避免呼叫方各自判空。
function esc(s) {
  if (s == null) return "";
  return escapeHtml(String(s));
}

// ========== JSON 自動修復 ==========
// 逐字掃描，追蹤是否在字串內。把字串內的裸換行/Tab/回車轉義成 \n \t \r，
// 這是「Unterminated string」最常見的成因（在字串值裡直接敲了 Enter）。
function jsonEscapeCtrlInStrings(text) {
  let out = "", inStr = false, escaped = false, changed = false;
  for (const ch of text) {
    if (inStr) {
      if (escaped) { out += ch; escaped = false; continue; }
      if (ch === "\\") { out += ch; escaped = true; continue; }
      if (ch === '"') { out += ch; inStr = false; continue; }
      if (ch === "\n") { out += "\\n"; changed = true; continue; }
      if (ch === "\r") { out += "\\r"; changed = true; continue; }
      if (ch === "\t") { out += "\\t"; changed = true; continue; }
      out += ch;
    } else {
      if (ch === '"') inStr = true;
      out += ch;
    }
  }
  return { text: out, changed };
}

// 去掉物件/陣列結尾多餘的逗號（僅在字串外處理）。
function jsonStripTrailingCommas(text) {
  let out = "", inStr = false, escaped = false, changed = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inStr) {
      out += ch;
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') { inStr = true; out += ch; continue; }
    if (ch === ",") {
      let j = i + 1;
      while (j < text.length && /\s/.test(text[j])) j++;
      if (text[j] === "}" || text[j] === "]") { changed = true; continue; }
    }
    out += ch;
  }
  return { text: out, changed };
}

// 把全形/智慧引號、全形標點還原成半形（結構修復的最後手段）。
function jsonNormalizePunct(text) {
  const before = text;
  const out = text
    .replace(/[\u201C\u201D\u2033\uFF02]/g, '"')
    .replace(/[\u2018\u2019\u2032]/g, "'")
    .replace(/\uFF0C/g, ",").replace(/\uFF1A/g, ":")
    .replace(/\uFF1B/g, ";")
    .replace(/\uFF5B/g, "{").replace(/\uFF5D/g, "}")
    .replace(/\uFF3B/g, "[").replace(/\uFF3D/g, "]");
  return { text: out, changed: out !== before };
}

// 分層嘗試修復：由溫和到激進，每步都試著 parse，成功即回傳。
// 回傳 { ok, value, text, applied[] }。
function repairJsonText(raw) {
  const applied = [];
  const tryParse = (s) => { try { return JSON.parse(s); } catch (e) { return undefined; } };
  let v = tryParse(raw);
  if (v !== undefined) return { ok: true, value: v, text: raw, applied };

  let s = raw;
  const ctrl = jsonEscapeCtrlInStrings(s);
  if (ctrl.changed) { s = ctrl.text; applied.push("字串內換行/Tab→轉義"); }
  v = tryParse(s);
  if (v !== undefined) return { ok: true, value: v, text: s, applied };

  const tc = jsonStripTrailingCommas(s);
  if (tc.changed) { s = tc.text; applied.push("移除多餘結尾逗號"); }
  v = tryParse(s);
  if (v !== undefined) return { ok: true, value: v, text: s, applied };

  const punct = jsonNormalizePunct(s);
  if (punct.changed) {
    s = punct.text; applied.push("全形引號/標點→半形");
    const ctrl2 = jsonEscapeCtrlInStrings(s);
    if (ctrl2.changed) s = ctrl2.text;
    const tc2 = jsonStripTrailingCommas(s);
    if (tc2.changed) s = tc2.text;
  }
  v = tryParse(s);
  if (v !== undefined) return { ok: true, value: v, text: s, applied };

  return { ok: false, value: undefined, text: s, applied, error: jsonErrorHint(s) };
}

// 從 JSON.parse 的錯誤訊息推算大概位置，給出「第 N 行」提示。
function jsonErrorHint(s) {
  try { JSON.parse(s); return ""; } catch (e) {
    const m = /position (\d+)/.exec(e.message);
    if (m) {
      const pos = +m[1];
      const line = s.slice(0, pos).split("\n").length;
      return `${e.message}（大約在第 ${line} 行）`;
    }
    return e.message;
  }
}

// ========== POSTS VIEW ==========
let POST_TYPES = [];
let CURRENT_POST_BATCH = null;
let POST_CHARS = [];

async function initPostsView() {
  await ensureStyles();
  const [chars, types] = await Promise.all([
    api("/api/characters"),
    POST_TYPES.length ? Promise.resolve(POST_TYPES) : api("/api/post_types"),
  ]);
  POST_TYPES = types;
  POST_CHARS = chars;
  renderLangFilter("postLangFilter", "post", chars, renderPostCharOptions);
  renderSourceFilter("postSourceFilter", "post", chars, renderPostCharOptions);
  renderPostCharOptions();

  const box = $("#postTypes");
  box.innerHTML = "";
  POST_TYPES.forEach((t) => {
    const el = document.createElement("label");
    el.className = "type-item";
    el.innerHTML = `<input type="checkbox" value="${t.id}" />
      <div><div class="tname">${t.name} <span class="badge">${t.priority || ""}</span></div>
      <div class="tdesc">${t.desc || ""}</div></div>`;
    box.appendChild(el);
  });
}

function renderPostCharOptions() {
  const list = filterBySource(filterByLang(POST_CHARS, "post"), "post");
  $("#postChar").innerHTML = list
    .map((c) => `<option value="${c.char_id}">${c.lang_name ? "[" + esc(c.lang_name) + "] " : ""}${esc(c.name) || esc(c.char_id)}</option>`)
    .join("");
}

$("#btnPosts").addEventListener("click", async () => {
  const charId = $("#postChar").value;
  const typeIds = $$("#postTypes input:checked").map((i) => i.value);
  if (!charId) return toast("請選擇角色", "err");
  if (!typeIds.length) return toast("請勾選至少一個帖子型別", "err");

  const st = $("#postStatus");
  const withImages = $("#withImages").checked;
  st.innerHTML = `<span class="spinner"></span> 正在生成 ${typeIds.length} 類帖子文字${
    withImages ? " + 配圖" : ""
  }…（配圖較慢，請耐心等待）`;
  $("#btnPosts").disabled = true;
  try {
    const r = await runTask("/api/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        char_id: charId,
        post_type_ids: typeIds,
        count_per_type: parseInt($("#countPerType").value) || 2,
        style_id: null,
        with_images: withImages,
        track: $("#postTrack").value || null,
      }),
    });
    st.innerHTML = `已生成 ${r.posts.length} 條帖子。`;
    toast("帖子生成成功", "ok");
    CURRENT_POST_BATCH = {
      char_id: charId,
      batch_id: r.batch_id,
      style_id: r.style_id || null,
    };
    renderPosts(r.posts);
  } catch (e) {
    st.innerHTML = "失敗：" + e.message;
    toast("生成失敗", "err");
  } finally {
    $("#btnPosts").disabled = false;
  }
});

$("#btnPostDeleteChar").addEventListener("click", async () => {
  const sel = $("#postChar");
  const charId = sel.value;
  if (!charId) return toast("請先選擇角色", "err");
  const name = sel.options[sel.selectedIndex]?.textContent?.trim() || charId;
  if (!confirm(`刪除角色「${name}」？連同其封面/帖子/落地頁一併刪除，不可恢復。`)) return;
  const btn = $("#btnPostDeleteChar");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "刪除中…";
  try {
    const r = await runTask("/api/characters/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: [charId] }),
    });
    const delErrN = Object.keys(r.errors || {}).length;
    if (delErrN) {
      toast("刪除失敗(可重試)：" + Object.values(r.errors)[0], "err");
    } else {
      toast(`已刪除角色「${name}」`, "ok");
      POST_CHARS = POST_CHARS.filter((c) => c.char_id !== charId);
      if (CURRENT_POST_BATCH && CURRENT_POST_BATCH.char_id === charId) {
        CURRENT_POST_BATCH = null;
        $("#postResults").innerHTML = "";
        $("#postStatus").innerHTML = "";
      }
      renderLangFilter("postLangFilter", "post", POST_CHARS, renderPostCharOptions);
      renderSourceFilter("postSourceFilter", "post", POST_CHARS, renderPostCharOptions);
      renderPostCharOptions();
    }
  } catch (e) {
    toast("刪除失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

function renderPosts(posts) {
  const box = $("#postResults");
  box.innerHTML = "";
  posts.forEach((p) => {
    const card = document.createElement("div");
    card.className = "post-card";
    let pimg = `<div class="pimg">未生成配圖</div>`;
    if (p.image && p.image.url) {
      pimg = `<img class="pimg" loading="lazy" decoding="async" src="${imgUrl(p.image.local_path, p.image.url, 800)}" />`;
    } else if (p.image && p.image.error) {
      pimg = `<div class="pimg">配圖失敗：${esc(p.image.error)}</div>`;
    }
    const isObj = p.content && typeof p.content === "object";
    const editVal = isObj ? JSON.stringify(p.content, null, 2) : (p.content ?? "");
    card.innerHTML = `
      ${pimg}
      <div class="pbody">
        <div class="ptype">${esc(p.type_name)}</div>
        <div class="post-id-line muted" title="帖子 ID">ID: <code>${esc(p.post_id)}</code></div>
        <div class="content post-content-view" data-post-id="${p.post_id}">${escapeHtml(localized(p.content))}</div>
        <div class="post-content-edit hidden" data-post-id="${p.post_id}">
          <textarea class="post-content-input" spellcheck="false" data-is-obj="${isObj ? 1 : 0}" style="width:100%;min-height:120px;font-family:inherit;font-size:13px;line-height:1.5">${escapeHtml(editVal)}</textarea>
          <div style="margin-top:6px;display:flex;gap:6px">
            <button class="primary save-post-text" data-post-id="${p.post_id}">儲存文字</button>
            <button class="ghost cancel-post-text" data-post-id="${p.post_id}">取消</button>
          </div>
        </div>
        <div class="post-actions">
          <button class="ghost edit-post-text" data-post-id="${p.post_id}">✏️ 編輯文字</button>
          <button class="ghost rerender-post-img" data-post-id="${p.post_id}">重新生成圖片</button>
          <button class="ghost danger delete-post" data-post-id="${p.post_id}">刪除</button>
        </div>
        <div class="kv">
          <details><summary>variable / scene（生圖描述）</summary>
            <pre>${escapeHtml(JSON.stringify({ variable: p.variable, scene: p.scene }, null, 2))}</pre>
          </details>
        </div>
      </div>`;
    box.appendChild(card);
  });
  const togglePostEdit = (pid, on) => {
    document.querySelector(`#postResults .post-content-edit[data-post-id="${pid}"]`)?.classList.toggle("hidden", !on);
    document.querySelector(`#postResults .post-content-view[data-post-id="${pid}"]`)?.classList.toggle("hidden", on);
  };
  $$("#postResults .edit-post-text").forEach((btn) => {
    btn.addEventListener("click", () => togglePostEdit(btn.dataset.postId, true));
  });
  $$("#postResults .cancel-post-text").forEach((btn) => {
    btn.addEventListener("click", () => togglePostEdit(btn.dataset.postId, false));
  });
  $$("#postResults .save-post-text").forEach((btn) => {
    btn.addEventListener("click", () => saveRegularPostText(btn));
  });
  $$("#postResults .rerender-post-img").forEach((btn) => {
    btn.addEventListener("click", () => rerenderRegularPostImage(btn));
  });
  $$("#postResults .delete-post").forEach((btn) => {
    btn.addEventListener("click", () => deleteRegularPost(btn));
  });
}

// 解析編輯框內容：物件型 content 按 JSON 解析，字串型原樣返回
function parseEditedContent(ta) {
  const raw = ta.value;
  if (ta.dataset.isObj === "1") {
    try { return { ok: true, value: JSON.parse(raw) }; }
    catch (e) { return { ok: false, error: e.message }; }
  }
  return { ok: true, value: raw };
}

async function saveRegularPostText(btn) {
  if (!CURRENT_POST_BATCH || !CURRENT_POST_BATCH.batch_id) {
    return toast("缺少當前批次資訊，無法儲存", "err");
  }
  const pid = btn.dataset.postId;
  const ta = document.querySelector(`#postResults .post-content-edit[data-post-id="${pid}"] .post-content-input`);
  const parsed = parseEditedContent(ta);
  if (!parsed.ok) return toast("內容 JSON 格式錯誤：" + parsed.error, "err");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "儲存中…";
  try {
    const r = await api(`/api/posts/${CURRENT_POST_BATCH.char_id}/${CURRENT_POST_BATCH.batch_id}/${pid}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: parsed.value }),
    });
    renderPosts(r.batch.posts);
    toast("文字已儲存", "ok");
  } catch (e) {
    toast("儲存失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function rerenderRegularPostImage(btn) {
  if (!CURRENT_POST_BATCH || !CURRENT_POST_BATCH.batch_id) {
    return toast("缺少當前批次資訊，請重新生成一批帖子後再重繪單圖", "err");
  }
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "重繪中…";
  try {
    const r = await api(`/api/posts/${CURRENT_POST_BATCH.char_id}/${CURRENT_POST_BATCH.batch_id}/${btn.dataset.postId}/image`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style_id: CURRENT_POST_BATCH.style_id }),
    });
    renderPosts(r.batch.posts);
    toast("圖片已重新生成", "ok");
  } catch (e) {
    toast("重繪失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function deleteRegularPost(btn) {
  if (!CURRENT_POST_BATCH || !CURRENT_POST_BATCH.batch_id) {
    return toast("缺少當前批次資訊，無法刪除", "err");
  }
  if (!confirm("刪除這條帖子？對應圖片也會刪除。")) return;
  try {
    const r = await api(`/api/posts/${CURRENT_POST_BATCH.char_id}/${CURRENT_POST_BATCH.batch_id}/${btn.dataset.postId}`, {
      method: "DELETE",
    });
    renderPosts(r.batch.posts);
    toast("帖子已刪除", "ok");
  } catch (e) {
    toast("刪除失敗：" + e.message, "err");
  }
}

// ========== IG POSTS VIEW ==========
let IG_CHARS = [];
let IG_ACTIVE_CHAR = null;
let IG_LOAD_GEN = 0; // loadLatestIg 請求代數，防止慢響應渲染到已切換的角色名下

async function initIgView() {
  await ensureStyles();
  IG_CHARS = await api("/api/characters");
  renderLangFilter("igLangFilter", "ig", IG_CHARS, renderIgCharGrid);
  renderSourceFilter("igSourceFilter", "ig", IG_CHARS, renderIgCharGrid);
  renderIgCharGrid();
  const shown = filterIgChars();
  if (!IG_ACTIVE_CHAR && shown.length) IG_ACTIVE_CHAR = shown[0].char_id;
  if (IG_ACTIVE_CHAR) loadLatestIg(IG_ACTIVE_CHAR);
}

function selectedIgCharIds() {
  return $$("#igCharGrid .ig-csel:checked").map((i) => i.value);
}

function updateIgSelCount() {
  const n = selectedIgCharIds().length;
  $("#igSelCount").textContent = n ? `已選 ${n} 個` : "";
}

function filterIgChars() {
  return filterBySource(filterByLang(IG_CHARS, "ig"), "ig");
}

function renderIgCharGrid() {
  const box = $("#igCharGrid");
  box.innerHTML = "";
  const list = filterIgChars();
  if (!list.length) {
    box.innerHTML = '<p class="muted">沒有符合當前篩選條件的角色。</p>';
    return;
  }
  list.forEach((c) => {
    const card = document.createElement("div");
    card.className = "char-card ig-char-card";
    card.dataset.charId = c.char_id;
    const cover = c.cover_url
      ? `<img class="cover" loading="lazy" decoding="async" src="${imgUrl(null, c.cover_url, 400)}" />`
      : `<div class="cover">無封面</div>`;
    const langTag = c.lang_name
      ? `<span class="lang-badge ${c.lang}">${c.lang_name}</span>`
      : "";
    card.innerHTML = `<label class="char-pick" title="多選生成"><input type="checkbox" class="ig-csel" value="${c.char_id}" /></label>
      <button class="card-arca-del ig-del-char" title="刪除該角色（連同其封面/帖子/落地頁，不可恢復）">🗑</button>
      ${cover}<div class="meta"><div class="name">${langTag}${esc(c.name) || "(未命名)"}</div>
      <div class="tag">點選檢視已生成帖子</div></div>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest(".char-pick")) return;
      if (e.target.closest(".ig-del-char")) return; // 刪除按鈕不觸發檢視
      IG_ACTIVE_CHAR = c.char_id;
      $$("#igCharGrid .ig-char-card").forEach((x) =>
        x.classList.toggle("active", x.dataset.charId === c.char_id)
      );
      loadLatestIg(c.char_id);
    });
    card.querySelector(".ig-csel").addEventListener("change", updateIgSelCount);
    card.querySelector(".ig-del-char").addEventListener("click", () => deleteIgChar(c, card));
    box.appendChild(card);
  });
  $$("#igCharGrid .ig-char-card").forEach((x) =>
    x.classList.toggle("active", x.dataset.charId === IG_ACTIVE_CHAR)
  );
  updateIgSelCount();
}

// INS 頁角色卡右上角「🗑」：徹底刪除該角色（同角色列表頁的刪除，連同封面/帖子/落地頁）。
async function deleteIgChar(c, card) {
  const name = c.name || c.char_id;
  if (!confirm(`刪除角色「${name}」？連同其封面/帖子/落地頁一併刪除，不可恢復。`)) return;
  const btn = card.querySelector(".ig-del-char");
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    const r = (await runTask("/api/characters/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: [c.char_id] }),
    })) || {};
    const delErrN = Object.keys(r.errors || {}).length;
    if (delErrN) {
      toast("刪除失敗(可重試)：" + Object.values(r.errors)[0], "err");
      if (btn) { btn.disabled = false; btn.textContent = "🗑"; }
      return;
    }
    toast(`已刪除角色「${name}」`, "ok");
    IG_CHARS = IG_CHARS.filter((x) => x.char_id !== c.char_id);
    if (IG_ACTIVE_CHAR === c.char_id) {
      IG_ACTIVE_CHAR = null;
      $("#igResults").innerHTML = "";
      $("#igViewingTitle").textContent = "";
    }
    renderLangFilter("igLangFilter", "ig", IG_CHARS, renderIgCharGrid);
    renderSourceFilter("igSourceFilter", "ig", IG_CHARS, renderIgCharGrid);
    renderIgCharGrid();
  } catch (e) {
    toast("刪除失敗：" + e.message, "err");
    if (btn) { btn.disabled = false; btn.textContent = "🗑"; }
  }
}

async function loadLatestIg(charId = IG_ACTIVE_CHAR) {
  IG_ACTIVE_CHAR = charId;
  const myGen = ++IG_LOAD_GEN;
  $("#igResults").innerHTML = "";
  if (!charId) return;
  const c = IG_CHARS.find((x) => x.char_id === charId);
  $("#igViewingTitle").textContent = c ? `正在檢視：${c.lang_name ? "[" + c.lang_name + "] " : ""}${c.name || c.char_id}` : "";
  try {
    const b = await api("/api/ig_posts/" + charId + "/latest");
    if (myGen !== IG_LOAD_GEN || charId !== IG_ACTIVE_CHAR) return; // 過期響應，丟棄
    if (b && b.posts && b.posts.length) {
      $("#igStatus").innerHTML = `已載入上次生成的 ${b.posts.length} 條（${new Date((b.created || 0) * 1000).toLocaleString()}）。重新生成會覆蓋。`;
      renderIgPosts(b.posts, b.persona_read);
    } else {
      $("#igStatus").innerHTML = "";
    }
  } catch (e) {
    if (myGen !== IG_LOAD_GEN || charId !== IG_ACTIVE_CHAR) return; // 過期響應，丟棄
    $("#igStatus").innerHTML = "";
  }
}

$("#btnIgSelAll").addEventListener("click", () => {
  $$("#igCharGrid .ig-csel").forEach((b) => (b.checked = true));
  updateIgSelCount();
});

$("#btnIgSelNone").addEventListener("click", () => {
  $$("#igCharGrid .ig-csel").forEach((b) => (b.checked = false));
  updateIgSelCount();
});

// 上一次 INS 頁刪除失敗的角色 id，供「重試失敗項」按鈕使用
let IG_LAST_DELETE_FAILED = [];

function syncIgRetryDeleteBtn() {
  const btn = $("#btnIgRetryDelete");
  if (!btn) return;
  if (IG_LAST_DELETE_FAILED.length) {
    btn.classList.remove("hidden");
    btn.textContent = `↻ 重試失敗項 (${IG_LAST_DELETE_FAILED.length})`;
  } else {
    btn.classList.add("hidden");
  }
}

// INS 頁批次刪除：勾選多個角色一起徹底刪除（連同封面/帖子/落地頁）。
async function runIgBatchDelete(ids, btn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "刪除中…";
  try {
    const r = (await runTask("/api/characters/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids }),
    }, (done, total) => {
      btn.textContent = `刪除中… ${done}/${total}`;
    })) || {};
    const deleted = Array.isArray(r.deleted) ? r.deleted : [];
    const failed = Object.keys(r.errors || {});
    IG_LAST_DELETE_FAILED = failed;
    toast(`已刪除 ${deleted.length} 個${failed.length ? `，${failed.length} 個失敗(可重試)` : ""}`,
          failed.length ? "err" : "ok");
    if (failed.length) console.warn("[ig-delete] 失敗:", r.errors);
    const delSet = new Set(deleted);
    IG_CHARS = IG_CHARS.filter((x) => !delSet.has(x.char_id));
    if (IG_ACTIVE_CHAR && delSet.has(IG_ACTIVE_CHAR)) {
      IG_ACTIVE_CHAR = null;
      $("#igResults").innerHTML = "";
      $("#igViewingTitle").textContent = "";
    }
    renderLangFilter("igLangFilter", "ig", IG_CHARS, renderIgCharGrid);
    renderSourceFilter("igSourceFilter", "ig", IG_CHARS, renderIgCharGrid);
    renderIgCharGrid();
  } catch (e) {
    toast("刪除失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    syncIgRetryDeleteBtn();
  }
}

$("#btnIgBatchDelete").addEventListener("click", async () => {
  const ids = selectedIgCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  if (!confirm(`刪除 ${ids.length} 個角色？連同其封面/帖子/落地頁一併刪除，不可恢復。`)) return;
  await runIgBatchDelete(ids, $("#btnIgBatchDelete"));
});

$("#btnIgRetryDelete").addEventListener("click", async () => {
  const ids = IG_LAST_DELETE_FAILED.slice();
  if (!ids.length) return;
  if (!confirm(`重試刪除上次失敗的 ${ids.length} 個角色？`)) return;
  await runIgBatchDelete(ids, $("#btnIgRetryDelete"));
});

$("#btnIg").addEventListener("click", async () => {
  const ids = selectedIgCharIds();
  if (!ids.length) return toast("請先勾選角色", "err");
  const st = $("#igStatus");
  const withImages = $("#igWithImages").checked;
  const countRaw = $("#igCount").value.trim();
  const n = countRaw ? parseInt(countRaw) : null;
  const countText = n ? `每個 ${n} 條` : "每個由模型規劃 3~9 條";
  st.innerHTML = `<span class="spinner"></span> 正在為 ${ids.length} 個角色生成 INS 帖子，${countText}${
    withImages ? " + 配圖（較慢）" : ""
  }…`;
  $("#btnIg").disabled = true;
  try {
    const r = await runTask("/api/ig_posts/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        char_ids: ids,
        n,
        style_id: null,
        with_images: withImages,
        track: $("#igTrack").value || null,
      }),
    }, (done, total) => {
      st.innerHTML = `<span class="spinner"></span> 生成中… ${done}/${total} 個角色`;
    });
    const errN = Object.keys(r.errors || {}).length;
    st.innerHTML = `已生成 ${r.generated.length} 個角色的 INS 帖子${errN ? `，${errN} 個失敗` : ""}。點選頭像可檢視各自已儲存的帖子。`;
    toast(`INS 帖子生成完成：${r.generated.length} 個成功${errN ? `，${errN} 個失敗` : ""}`, errN ? "err" : "ok");
    if (r.generated.length) {
      IG_ACTIVE_CHAR = r.generated[0].char_id;
      renderIgCharGrid();
      loadLatestIg(IG_ACTIVE_CHAR);
    }
  } catch (e) {
    st.innerHTML = "失敗：" + e.message;
    toast("生成失敗", "err");
  } finally {
    $("#btnIg").disabled = false;
  }
});

function renderIgPosts(posts, personaRead) {
  const box = $("#igResults");
  box.innerHTML = "";
  if (personaRead) {
    const pr = document.createElement("details");
    pr.className = "ig-reasoning";
    pr.innerHTML = `<summary class="muted">檢視生成推理 reasoning（這個人憑什麼有意思）</summary>
      <pre class="kv">${escapeHtml(JSON.stringify(personaRead, null, 2))}</pre>`;
    box.appendChild(pr);
  }
  posts.forEach((p) => {
    const card = document.createElement("div");
    card.className = "post-card";

    let badge, pimg;
    if (p.format === "text_only") {
      badge = `<span class="badge">純文字 · Threads</span>`;
      pimg = `<div class="pimg">純文字帖（無圖）</div>`;
    } else if (p.image && p.image.url) {
      const PK = {
        screenshot: "截圖",
        graphic: "圖文卡",
        collage: "拼貼",
        photo_dump: "Photo dump",
        journal_overlay: "手寫標註",
        airdrop_card: "AirDrop卡",
        word_cloud: "關鍵詞雲",
        calendar_card: "日曆卡",
        photo: "隨手拍",
      };
      let t;
      if (p.image.type === "selfie") t = "自拍 selfie · 圖生圖";
      else if (p.image.type === "composite") t = "composite · " + (PK[p.image.photo_kind || p.photo_kind] || "拼貼圖生圖");
      else t = "photo · " + (PK[p.image.photo_kind || p.photo_kind] || "文生圖");
      badge = `<span class="badge">${t}</span>`;
      pimg = `<img class="pimg" loading="lazy" decoding="async" src="${imgUrl(p.image.local_path, p.image.url, 800)}" />`;
    } else if (p.image && p.image.error) {
      badge = `<span class="badge">${esc(p.image_type) || ""} 配圖失敗</span>`;
      pimg = `<div class="pimg">配圖失敗：${esc(p.image.error)}</div>`;
    } else {
      badge = `<span class="badge">${esc(p.image_type) || "圖文"}（未生成圖）</span>`;
      pimg = `<div class="pimg">未生成配圖</div>`;
    }

    const typeTag = p.post_type_name
      ? `<span class="ttag ${p.post_type}">${esc(p.post_type_name)}</span>`
      : "";

    const spec = p.selfie
      ? { selfie: p.selfie }
      : p.photo_prompt
      ? { photo_kind: p.photo_kind, photo_schema: p.photo_schema, photo_prompt: p.photo_prompt }
      : {};

    const isObj = p.content && typeof p.content === "object";
    const editVal = isObj ? JSON.stringify(p.content, null, 2) : (p.content ?? "");
    card.innerHTML = `
      ${pimg}
      <div class="pbody">
        <div class="ptype">${typeTag} ${badge}</div>
        <div class="post-id-line muted" title="帖子 ID">ID: <code>${esc(p.post_id)}</code></div>
        <div class="content ig-content-view" data-post-id="${p.post_id}">${escapeHtml(localized(p.content))}</div>
        <div class="ig-content-edit hidden" data-post-id="${p.post_id}">
          <textarea class="ig-content-input" spellcheck="false" data-is-obj="${isObj ? 1 : 0}" style="width:100%;min-height:120px;font-family:inherit;font-size:13px;line-height:1.5">${escapeHtml(editVal)}</textarea>
          <div style="margin-top:6px;display:flex;gap:6px">
            <button class="primary save-ig-text" data-post-id="${p.post_id}">儲存文字</button>
            <button class="ghost cancel-ig-text" data-post-id="${p.post_id}">取消</button>
          </div>
        </div>
        <div class="post-actions">
          <button class="ghost edit-ig-text" data-post-id="${p.post_id}">✏️ 編輯文字</button>
          ${p.format !== "text_only" ? `<button class="ghost rerender-ig-img" data-post-id="${p.post_id}">重新生成圖片</button>` : ""}
          <button class="ghost danger delete-ig-post" data-post-id="${p.post_id}">刪除</button>
        </div>
        <div class="kv">
          <details><summary>生圖描述 / prompt</summary>
            <pre>${escapeHtml(JSON.stringify(spec, null, 2))}</pre>
            ${p.image && p.image.prompt ? `<pre>${escapeHtml(p.image.prompt)}</pre>` : ""}
          </details>
        </div>
      </div>`;
    box.appendChild(card);
  });
  const toggleIgEdit = (pid, on) => {
    document.querySelector(`#igResults .ig-content-edit[data-post-id="${pid}"]`)?.classList.toggle("hidden", !on);
    document.querySelector(`#igResults .ig-content-view[data-post-id="${pid}"]`)?.classList.toggle("hidden", on);
  };
  $$("#igResults .edit-ig-text").forEach((btn) => {
    btn.addEventListener("click", () => toggleIgEdit(btn.dataset.postId, true));
  });
  $$("#igResults .cancel-ig-text").forEach((btn) => {
    btn.addEventListener("click", () => toggleIgEdit(btn.dataset.postId, false));
  });
  $$("#igResults .save-ig-text").forEach((btn) => {
    btn.addEventListener("click", () => saveIgPostText(btn));
  });
  $$("#igResults .rerender-ig-img").forEach((btn) => {
    btn.addEventListener("click", () => rerenderIgPostImage(btn));
  });
  $$("#igResults .delete-ig-post").forEach((btn) => {
    btn.addEventListener("click", () => deleteIgPost(btn));
  });
}

async function saveIgPostText(btn) {
  if (!IG_ACTIVE_CHAR) return toast("請先選擇角色", "err");
  const pid = btn.dataset.postId;
  const ta = document.querySelector(`#igResults .ig-content-edit[data-post-id="${pid}"] .ig-content-input`);
  const parsed = parseEditedContent(ta);
  if (!parsed.ok) return toast("內容 JSON 格式錯誤：" + parsed.error, "err");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "儲存中…";
  try {
    const r = await api(`/api/ig_posts/${IG_ACTIVE_CHAR}/${pid}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: parsed.value }),
    });
    renderIgPosts(r.batch.posts, r.batch.persona_read);
    toast("文字已儲存", "ok");
  } catch (e) {
    toast("儲存失敗：" + e.message, "err");
    if (/not found|沒有/i.test(e.message)) loadLatestIg(IG_ACTIVE_CHAR);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function rerenderIgPostImage(btn) {
  if (!IG_ACTIVE_CHAR) return toast("請先選擇角色", "err");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "重繪中…";
  try {
    const r = await api(`/api/ig_posts/${IG_ACTIVE_CHAR}/${btn.dataset.postId}/image`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style_id: null }),
    });
    renderIgPosts(r.batch.posts, r.batch.persona_read);
    toast("圖片已重新生成", "ok");
  } catch (e) {
    toast("重繪失敗：" + e.message, "err");
    if (/not found|沒有/i.test(e.message)) loadLatestIg(IG_ACTIVE_CHAR);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function deleteIgPost(btn) {
  if (!IG_ACTIVE_CHAR) return toast("請先選擇角色", "err");
  if (!confirm("刪除這條 INS 帖子？對應圖片也會刪除。")) return;
  try {
    const r = await api(`/api/ig_posts/${IG_ACTIVE_CHAR}/${btn.dataset.postId}`, {
      method: "DELETE",
    });
    renderIgPosts(r.batch.posts, r.batch.persona_read);
    toast("帖子已刪除", "ok");
  } catch (e) {
    toast("刪除失敗：" + e.message, "err");
    if (/not found|沒有/i.test(e.message)) loadLatestIg(IG_ACTIVE_CHAR);
  }
}

// ========== CHAT VIEW ==========
let CHAT_CHARS = [];
let CHAT_ACTIVE_CHAR = null;
let CHAT_ACTIVE_REC = null;
let CHAT_SESSION_ID = null;
let CHAT_MESSAGES = [];
let CHAT_DEFAULT_TPL = "";
let CHAT_MODE = "normal";
let CHAT_DEFAULT_TPLS = {};
let CHAT_VIEWING_HISTORY = false;
let CHAT_SELECT_GEN = 0; // selectChatChar 請求代數，用於丟棄過期響應

async function initChatView() {
  CHAT_CHARS = await api("/api/characters");
  renderLangFilter("chatLangFilter", "chat", CHAT_CHARS, renderChatCharGrid);
  renderChatCharGrid();
  const shown = filterChatChars();
  if (!CHAT_ACTIVE_CHAR && shown.length) {
    await selectChatChar(shown[0].char_id);
  } else if (CHAT_ACTIVE_CHAR) {
    await selectChatChar(CHAT_ACTIVE_CHAR, { keepSession: true });
  }
}

function filterChatChars() {
  const q = ($("#chatSearch")?.value || "").trim().toLowerCase();
  return filterByLang(CHAT_CHARS, "chat").filter((c) => {
    if (!q) return true;
    return `${c.name || ""} ${c.char_id || ""}`.toLowerCase().includes(q);
  });
}

function renderChatCharGrid() {
  const box = $("#chatCharGrid");
  if (!box) return;
  box.innerHTML = "";
  const list = filterChatChars();
  if (!list.length) {
    box.innerHTML = '<p class="muted">沒有符合當前條件的角色。</p>';
    return;
  }
  list.forEach((c) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "chat-char-item";
    item.dataset.charId = c.char_id;
    const cover = c.cover_url
      ? `<img loading="lazy" decoding="async" src="${imgUrl(null, c.cover_url, 200)}" />`
      : `<span>${escapeHtml((c.name || "?").slice(0, 1))}</span>`;
    const langTag = c.lang_name
      ? `<span class="lang-badge ${c.lang}">${c.lang_name}</span>`
      : "";
    item.innerHTML = `<div class="chat-char-avatar">${cover}</div>
      <div class="chat-char-meta"><div class="chat-char-name">${langTag}${escapeHtml(c.name || "(未命名)")}</div>
      <div class="chat-char-id">${escapeHtml(c.char_id || "")}</div></div>`;
    item.addEventListener("click", () => selectChatChar(c.char_id));
    box.appendChild(item);
  });
  markActiveChatChar();
}

function markActiveChatChar() {
  $$("#chatCharGrid .chat-char-item").forEach((x) =>
    x.classList.toggle("active", x.dataset.charId === CHAT_ACTIVE_CHAR)
  );
}

async function selectChatChar(charId, opts = {}) {
  if (!charId) return;
  CHAT_ACTIVE_CHAR = charId;
  // 請求代數：每次呼叫自增，響應回來後若代數已過期（被更新的呼叫覆蓋）則丟棄，
  // 防止快速切換角色時舊角色的慢響應覆蓋新角色的狀態。
  const myGen = ++CHAT_SELECT_GEN;
  markActiveChatChar();
  $("#chatEmpty").classList.add("hidden");
  $("#chatPanel").classList.remove("hidden");
  $("#chatMessages").innerHTML = "";
  $("#chatStatus").innerHTML = `<span class="spinner"></span> 正在載入角色…`;
  try {
    const [rec, latest] = await Promise.all([
      api("/api/character/" + charId),
      api("/api/chat/" + charId + "/latest?mode=" + CHAT_MODE),
    ]);
    if (myGen !== CHAT_SELECT_GEN || charId !== CHAT_ACTIVE_CHAR) return; // 過期響應，丟棄
    CHAT_ACTIVE_REC = rec;
    CHAT_DEFAULT_TPL = latest.default_template || CHAT_DEFAULT_TPL || "";
    CHAT_DEFAULT_TPLS = latest.default_templates || CHAT_DEFAULT_TPLS;
    const p = rec.persona || {};
    const summary = p.profile || (p.personality && p.personality.summary) || "";
    $("#chatTitle").innerHTML = `${rec.lang ? `<span class="lang-badge ${rec.lang}">${LANG_NAMES_FULL[rec.lang] || rec.lang}</span>` : ""}${escapeHtml(localized(p.name) || rec.char_id)}`;
    $("#chatSub").textContent = summary ? localized(summary) : rec.char_id;
    renderChatAvatar(rec);

    const session = latest.session;
    if (opts.forceNew) {
      CHAT_SESSION_ID = null;
      setChatTemplate("");
      CHAT_MESSAGES = (latest.opening || []).length
        ? [{ role: "assistant", items: latest.opening, is_opening: true, created: Math.floor(Date.now() / 1000) }]
        : [];
    } else if (opts.keepSession && CHAT_SESSION_ID) {
      // 保持當前會話狀態，僅重新整理角色頭部。
    } else if (session && session.messages && session.messages.length) {
      CHAT_SESSION_ID = session.session_id;
      setChatTemplate(session.prompt_template || "");
      CHAT_MESSAGES = session.messages;
    } else {
      CHAT_SESSION_ID = null;
      setChatTemplate("");
      CHAT_MESSAGES = (latest.opening || []).length
        ? [{ role: "assistant", items: latest.opening, is_opening: true, created: Math.floor(Date.now() / 1000) }]
        : [];
    }
    CHAT_VIEWING_HISTORY = false;
    renderChatMessages();
    $("#chatStatus").innerHTML = CHAT_SESSION_ID ? "已載入最近一次對話。" : "已載入角色開場白，可直接開始聊天。";
  } catch (e) {
    if (myGen !== CHAT_SELECT_GEN || charId !== CHAT_ACTIVE_CHAR) return; // 過期響應，丟棄
    $("#chatStatus").innerHTML = "載入失敗：" + e.message;
    toast("聊天角色載入失敗", "err");
  }
}

function setChatTemplate(tpl) {
  const box = $("#chatPromptTpl");
  if (box) box.value = tpl || "";
  updateChatTplHint();
}

function updateChatTplHint() {
  const box = $("#chatPromptTpl");
  const hint = $("#chatTplHint");
  if (!box || !hint) return;
  const custom = box.value.trim().length > 0;
  hint.textContent = custom
    ? (CHAT_SESSION_ID ? "本會話使用自定義模板" : "將用自定義模板開始新對話")
    : "當前使用預設模板";
}

function renderChatAvatar(rec) {
  const av = $("#chatAvatar");
  const name = localized((rec.persona || {}).name) || rec.char_id || "?";
  const cover = rec.cover && (rec.cover.local_path || rec.cover.url)
    ? imgUrl(rec.cover.local_path, rec.cover.url, 200)
    : null;
  if (cover) {
    av.innerHTML = `<img loading="lazy" decoding="async" src="${cover}" />`;
  } else {
    av.textContent = name.slice(0, 1);
  }
}

function chatContextPayload() {
  return {
    relationship: $("#chatRelationship").value.trim(),
    user_persona: $("#chatUserPersona").value.trim(),
    user_impression: $("#chatUserImpression").value.trim(),
    plot_summary: $("#chatPlotSummary").value.trim(),
    location: $("#chatLocation").value.trim(),
    weather: $("#chatWeather").value.trim(),
    day_summary: $("#chatDaySummary").value.trim(),
    day_schedule: $("#chatDaySchedule").value.trim(),
  };
}

function renderChatMessages() {
  const box = $("#chatMessages");
  box.innerHTML = "";
  if (!CHAT_MESSAGES.length) {
    box.innerHTML = '<div class="chat-placeholder">暫無訊息，發一句開始。</div>';
    return;
  }
  CHAT_MESSAGES.forEach((m) => {
    if (m.role === "user") {
      box.appendChild(renderUserBubble(m.content || ""));
      return;
    }
    const items = Array.isArray(m.items) ? m.items : [];
    if (m.is_opening) {
      const note = document.createElement("div");
      note.className = "chat-note";
      note.textContent = "角色開場白";
      box.appendChild(note);
    }
    items.forEach((it) => box.appendChild(renderAssistantItem(it)));
    if (m.call_log) box.appendChild(renderCallLogRow(m.call_log));
  });
  box.scrollTop = box.scrollHeight;
}

function prettyJsonLog(raw) {
  if (typeof raw !== "string") return JSON.stringify(raw, null, 2);
  try { return JSON.stringify(JSON.parse(raw), null, 2); } catch (e) { return raw; }
}

function renderCallLogRow(log) {
  const sections = [];
  const meta = [log.model && `model: ${log.model}`, log.temperature != null && `temperature: ${log.temperature}`, log.max_tokens != null && `max_tokens: ${log.max_tokens}`].filter(Boolean).join("   ");
  if (meta) sections.push(`<div class="chat-log-meta">${escapeHtml(meta)}</div>`);
  (log.messages || []).forEach((msg) => {
    const label = msg.role === "system" ? "SYSTEM PROMPT" : msg.role === "user" ? "INPUT · user" : "INPUT · assistant";
    sections.push(`<div class="chat-log-block"><div class="chat-log-label">${escapeHtml(label)}</div><pre>${escapeHtml(prettyJsonLog(msg.content))}</pre></div>`);
  });
  sections.push(`<div class="chat-log-block"><div class="chat-log-label">OUTPUT</div><pre>${escapeHtml(prettyJsonLog(log.output))}</pre></div>`);
  const row = document.createElement("div");
  row.className = "chat-row assistant";
  const det = document.createElement("details");
  det.className = "chat-raw-output";
  det.innerHTML = `<summary>模型呼叫日誌</summary><div class="chat-log-body">${sections.join("")}</div>`;
  row.appendChild(det);
  return row;
}

function renderUserBubble(content) {
  const row = document.createElement("div");
  row.className = "chat-row user";
  row.innerHTML = `<div class="chat-bubble user-bubble">${escapeHtml(content)}</div>`;
  return row;
}

function renderAssistantItem(item) {
  const type = item && item.type ? item.type : "text";
  const data = (item && item.data) || {};
  const row = document.createElement("div");
  row.className = "chat-row assistant";
  if (type === "voice") {
    row.innerHTML = `<div class="chat-bubble assistant-bubble voice-bubble"><span class="chat-type-label">VOICE</span>${escapeHtml(data.content || "")}${data.emotion ? `<div class="chat-extra">${escapeHtml(data.emotion)}</div>` : ""}</div>`;
    return row;
  }
  if (type === "sticker") {
    row.innerHTML = `<div class="chat-bubble assistant-bubble sticker-bubble"><span class="chat-type-label">STICKER</span><div>${escapeHtml(data.scene || "sticker")}</div><div class="chat-extra">${escapeHtml(data.emotion || "")}</div></div>`;
    return row;
  }
  if (type === "image") {
    row.innerHTML = `<div class="chat-bubble assistant-bubble image-bubble"><span class="chat-type-label">IMAGE · ${escapeHtml(data.category || "photo")}</span><div>${escapeHtml(data.description || "")}</div></div>`;
    return row;
  }
  if (type === "html_file") {
    const wrap = document.createElement("div");
    wrap.className = "chat-bubble assistant-bubble html-bubble";
    wrap.innerHTML = `<span class="chat-type-label">HTML</span><div class="html-title">${escapeHtml(data.file_name || "공유")}</div><div>${escapeHtml(data.description || "HTML 콘텐츠")}</div><button class="ghost chat-open-html" type="button">預覽 HTML</button>`;
    wrap.querySelector(".chat-open-html").addEventListener("click", () => {
      const w = window.open("", "_blank");
      w.document.open();
      w.document.write(data.html || "");
      w.document.close();
    });
    row.appendChild(wrap);
    return row;
  }
  if (type === "state_update") {
    row.className = "chat-row state";
    const parts = [];
    if (data.status) parts.push(`<span class="chat-state-status">${escapeHtml(data.status)}</span>`);
    if (data.emotion) parts.push(`<span class="chat-state-emotion">${escapeHtml(data.emotion)}</span>`);
    row.innerHTML = `<div class="chat-state">${parts.join("") || escapeHtml("狀態已更新")}</div>`;
    return row;
  }
  if (type === "music") {
    row.innerHTML = `<div class="chat-bubble assistant-bubble music-bubble"><span class="chat-type-label">MUSIC</span>${escapeHtml(data.content || "")}</div>`;
    return row;
  }
  if (type === "match_action") {
    const greeting = data.greeting || data.content || "";
    row.innerHTML = `<div class="chat-bubble assistant-bubble match-bubble"><span class="chat-type-label">加好友</span><div>對方同意後的第一句</div>${greeting ? `<div class="chat-extra">${escapeHtml(greeting)}</div>` : ""}</div>`;
    return row;
  }
  const emotionTag = data.emotion && data.emotion !== "default" ? `<div class="chat-extra">${escapeHtml(data.emotion)}</div>` : "";
  row.innerHTML = `<div class="chat-bubble assistant-bubble">${escapeHtml(data.content || "")}${emotionTag}</div>`;
  return row;
}

// 傳送按鈕與回車共用同一在途標誌，避免併發觸發 /api/chat（會導致會話分叉/訊息丟失）。
let CHAT_SENDING = false;

async function sendChatMessage() {
  if (CHAT_SENDING) return;
  if (!CHAT_ACTIVE_CHAR) return toast("請先選擇角色", "err");
  const input = $("#chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  CHAT_MESSAGES.push({ role: "user", content: text, created: Math.floor(Date.now() / 1000) });
  renderChatMessages();
  CHAT_SENDING = true;
  const btn = $("#btnChatSend");
  btn.disabled = true;
  $("#chatStatus").innerHTML = `<span class="spinner"></span> 角色正在輸入…`;
  try {
    const payload = {
      char_id: CHAT_ACTIVE_CHAR,
      message: text,
      session_id: CHAT_SESSION_ID,
      context: chatContextPayload(),
      mode: CHAT_MODE,
    };
    if (!CHAT_SESSION_ID) {
      const tpl = ($("#chatPromptTpl")?.value || "").trim();
      if (tpl) payload.prompt_template = tpl;
    }
    const r = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    CHAT_SESSION_ID = r.session.session_id;
    CHAT_MESSAGES = r.session.messages || [];
    if (r.session.prompt_template !== undefined) setChatTemplate(r.session.prompt_template || "");
    renderChatMessages();
    $("#chatStatus").innerHTML = "";
  } catch (e) {
    CHAT_MESSAGES.push({
      role: "assistant",
      items: [{ type: "text", data: { content: "傳送失敗：" + e.message } }],
      created: Math.floor(Date.now() / 1000),
    });
    renderChatMessages();
    $("#chatStatus").innerHTML = "失敗：" + e.message;
    toast("聊天失敗", "err");
  } finally {
    CHAT_SENDING = false;
    btn.disabled = false;
    input.focus();
  }
}

$("#chatSearch")?.addEventListener("input", renderChatCharGrid);
$("#chatModeSwitch")?.addEventListener("click", async (e) => {
  const btn = e.target.closest(".chat-mode-btn");
  if (!btn || btn.dataset.mode === CHAT_MODE) return;
  CHAT_MODE = btn.dataset.mode;
  $$("#chatModeSwitch .chat-mode-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === CHAT_MODE));
  CHAT_SESSION_ID = null;
  CHAT_MESSAGES = [];
  if (CHAT_ACTIVE_CHAR) await selectChatChar(CHAT_ACTIVE_CHAR, { forceNew: true });
  toast(CHAT_MODE === "anonymous" ? "已切換到匿名聊天模式" : "已切換到普通聊天模式", "ok");
});
$("#btnChatNew")?.addEventListener("click", async () => {
  CHAT_SESSION_ID = null;
  CHAT_MESSAGES = [];
  if (CHAT_ACTIVE_CHAR) await selectChatChar(CHAT_ACTIVE_CHAR, { forceNew: true });
  toast("已開始新對話", "ok");
});
$("#chatPromptTpl")?.addEventListener("input", updateChatTplHint);
$("#btnChatTplReset")?.addEventListener("click", () => {
  setChatTemplate("");
  toast("已恢復預設模板（新對話生效）", "ok");
});
$("#btnChatHistory")?.addEventListener("click", async () => {
  const box = $("#chatHistoryBox");
  if (!box || !CHAT_ACTIVE_CHAR) return;
  box.hidden = false;
  box.open = true;
  await loadChatHistory();
});

async function loadChatHistory() {
  const list = $("#chatHistoryList");
  if (!list) return;
  list.innerHTML = '<p class="muted">載入中…</p>';
  try {
    const r = await api("/api/chat/" + CHAT_ACTIVE_CHAR + "/sessions?mode=" + CHAT_MODE);
    const sessions = r.sessions || [];
    if (!sessions.length) {
      list.innerHTML = '<p class="muted">暫無歷史對話。</p>';
      return;
    }
    list.innerHTML = "";
    sessions.forEach((s) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "chat-history-item" + (s.session_id === CHAT_SESSION_ID ? " active" : "");
      const when = s.updated ? new Date(s.updated * 1000).toLocaleString() : "";
      const tag = s.has_custom_template ? '<span class="chat-history-tag">自定義</span>' : "";
      item.innerHTML = `<div class="chat-history-top">${when}${tag}<span class="chat-history-count">${s.message_count} 條</span></div>
        <div class="chat-history-preview">${escapeHtml(s.preview || "(無內容)")}</div>`;
      item.addEventListener("click", () => openChatSession(s.session_id));
      list.appendChild(item);
    });
  } catch (e) {
    list.innerHTML = '<p class="muted">載入失敗：' + escapeHtml(e.message) + "</p>";
  }
}

async function openChatSession(sessionId) {
  if (!CHAT_ACTIVE_CHAR || !sessionId) return;
  $("#chatStatus").innerHTML = `<span class="spinner"></span> 載入歷史對話…`;
  try {
    const r = await api("/api/chat/" + CHAT_ACTIVE_CHAR + "/session/" + sessionId);
    const session = r.session;
    CHAT_SESSION_ID = session.session_id;
    CHAT_MESSAGES = session.messages || [];
    setChatTemplate(session.prompt_template || "");
    fillChatContext(session.context || {});
    renderChatMessages();
    await loadChatHistory();
    $("#chatStatus").innerHTML = "已載入該歷史對話，可繼續聊天。";
  } catch (e) {
    $("#chatStatus").innerHTML = "載入失敗：" + e.message;
    toast("歷史對話載入失敗", "err");
  }
}

function fillChatContext(ctx) {
  const map = {
    relationship: "chatRelationship",
    user_persona: "chatUserPersona",
    user_impression: "chatUserImpression",
    plot_summary: "chatPlotSummary",
    location: "chatLocation",
    weather: "chatWeather",
    day_summary: "chatDaySummary",
    day_schedule: "chatDaySchedule",
  };
  Object.entries(map).forEach(([k, id]) => {
    const el = document.getElementById(id);
    if (el) el.value = ctx[k] || "";
  });
}
$("#chatForm")?.addEventListener("submit", (e) => {
  e.preventDefault();
  sendChatMessage();
});
$("#chatInput")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
});

// ========== STYLES EDITOR ==========
async function loadStylesEditor() {
  const s = await api("/api/styles");
  STYLES = s;
  $("#stylesJson").value = JSON.stringify(s, null, 2);
}

$("#btnSaveStyles").addEventListener("click", async () => {
  const st = $("#styleStatus");
  let parsed;
  try {
    parsed = JSON.parse($("#stylesJson").value);
  } catch (e) {
    return toast("JSON 格式錯誤", "err");
  }
  try {
    await api("/api/styles", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed),
    });
    STYLES = parsed;
    refreshStyleSelects();
    st.innerHTML = "已儲存 " + parsed.length + " 個畫風。";
    toast("畫風庫已更新", "ok");
  } catch (e) {
    st.innerHTML = "失敗：" + e.message;
  }
});

// ========== LANDING PAGE ==========
let LANDING_STYLES = [];
let LANDING_VARIANTS = [];
let ldCurrentHtml = "";
let LD_CHARS = [];
let LD_ACTIVE_CHAR = null;
let LD_HTML_OWNER = null; // ldCurrentHtml 當前所屬的角色 id，用於生成前一致性校驗
let LD_LOAD_GEN = 0; // loadLandingHistory 請求代數，防止慢響應渲染到已切換的角色名下

function renderLdCharGrid() {
  const box = $("#ldCharGrid");
  box.innerHTML = "";
  const list = filterBySource(filterByLang(LD_CHARS, "ld"), "ld");
  if (!list.length) {
    box.innerHTML = '<p class="muted">沒有符合當前篩選條件的角色。</p>';
    updateLdSelCount();
    return;
  }
  // 當前編輯目標若不在篩選結果裡，重置為第一個
  if (!list.some((c) => c.char_id === LD_ACTIVE_CHAR)) {
    LD_ACTIVE_CHAR = list[0].char_id;
  }
  list.forEach((c) => {
    const card = document.createElement("div");
    card.className = "char-card ig-char-card";
    card.dataset.charId = c.char_id;
    const cover = c.cover_url
      ? `<img class="cover" loading="lazy" decoding="async" src="${imgUrl(null, c.cover_url, 400)}" />`
      : `<div class="cover">無封面</div>`;
    const langTag = c.lang_name
      ? `<span class="lang-badge ${c.lang}">${c.lang_name}</span>`
      : "";
    card.innerHTML = `<label class="char-pick" title="多選批次生成"><input type="checkbox" class="ld-csel" value="${c.char_id}" /></label>
      ${cover}<div class="meta"><div class="name">${langTag}${esc(c.name) || "(未命名)"}</div>
      <div class="tag">點選載入右側編輯</div></div>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest(".char-pick")) return;
      LD_ACTIVE_CHAR = c.char_id;
      ldCurrentHtml = "";
      LD_HTML_OWNER = null;
      ldRenderPreview();
      $$("#ldCharGrid .ig-char-card").forEach((x) =>
        x.classList.toggle("active", x.dataset.charId === c.char_id)
      );
      loadLandingHistory();
    });
    card.querySelector(".ld-csel").addEventListener("change", updateLdSelCount);
    box.appendChild(card);
  });
  $$("#ldCharGrid .ig-char-card").forEach((x) =>
    x.classList.toggle("active", x.dataset.charId === LD_ACTIVE_CHAR)
  );
  updateLdSelCount();
}

function selectedLdCharIds() {
  return $$("#ldCharGrid .ld-csel:checked").map((i) => i.value);
}
function updateLdSelCount() {
  const n = selectedLdCharIds().length;
  $("#ldSelCount").textContent = n ? `已選 ${n} 個` : "";
}

async function initLandingView() {
  const chars = await api("/api/characters");
  LD_CHARS = chars;
  renderLangFilter("ldLangFilter", "ld", chars, renderLdCharGrid);
  renderSourceFilter("ldSourceFilter", "ld", chars, renderLdCharGrid);
  renderLdCharGrid();
  if (!LANDING_STYLES.length) {
    try { LANDING_STYLES = await api("/api/landing_styles"); } catch (e) { LANDING_STYLES = []; }
  }
  if (!LANDING_VARIANTS.length) {
    try { LANDING_VARIANTS = await api("/api/landing_variants"); } catch (e) { LANDING_VARIANTS = []; }
    ldRenderVariants();
  }
  const chips = $("#ldStyleChips");
  chips.innerHTML = "";
  LANDING_STYLES.forEach((s) => {
    const b = document.createElement("button");
    b.className = "lchip";
    b.textContent = s;
    b.onclick = () => {
      const was = b.classList.contains("on");
      $$(".lchip").forEach((c) => c.classList.remove("on"));
      if (!was) { b.classList.add("on"); $("#ldStyleInput").value = ""; }
    };
    chips.appendChild(b);
  });
  loadLandingHistory();
}

$("#btnLdSelAll").addEventListener("click", () => {
  $$("#ldCharGrid .ld-csel").forEach((b) => (b.checked = true));
  updateLdSelCount();
});
$("#btnLdSelNone").addEventListener("click", () => {
  $$("#ldCharGrid .ld-csel").forEach((b) => (b.checked = false));
  updateLdSelCount();
});

$("#ldStyleInput").addEventListener("input", () => {
  if ($("#ldStyleInput").value.trim()) $$(".lchip").forEach((c) => c.classList.remove("on"));
});

$("#ldVariant").addEventListener("change", ldUpdateVariantDesc);

function ldGetStyle() {
  const on = $(".lchip.on");
  return on ? on.textContent : $("#ldStyleInput").value.trim();
}

function ldRenderVariants() {
  const sel = $("#ldVariant");
  if (!sel) return;
  sel.innerHTML = "";
  LANDING_VARIANTS.forEach((v) => {
    const o = document.createElement("option");
    o.value = v.id;
    o.textContent = v.label || v.id;
    o.dataset.desc = v.desc || "";
    sel.appendChild(o);
  });
  ldUpdateVariantDesc();
}

function ldUpdateVariantDesc() {
  const sel = $("#ldVariant");
  const desc = $("#ldVariantDesc");
  if (!sel || !desc) return;
  const opt = sel.options[sel.selectedIndex];
  desc.textContent = opt ? (opt.dataset.desc || "") : "";
}

function ldGetVariant() {
  const sel = $("#ldVariant");
  return sel && sel.value ? sel.value : null;
}

function ldVariantLabel(id) {
  if (!id) return "";
  const v = LANDING_VARIANTS.find((x) => x.id === id);
  return v ? (v.label || v.id) : "";
}

function ldRenderPreview() {
  $("#ldFrame").srcdoc = ldCurrentHtml || "<!DOCTYPE html><html><body style='margin:0;display:grid;place-items:center;height:100vh;font-family:system-ui;color:#aaa;font-size:14px'>預覽區</body></html>";
  $("#ldEditor").value = ldCurrentHtml;
}

$("#ldEditor").addEventListener("input", () => {
  ldCurrentHtml = $("#ldEditor").value;
  clearTimeout(window._ldTm);
  window._ldTm = setTimeout(() => { $("#ldFrame").srcdoc = ldCurrentHtml; }, 350);
});

$("#ldSeg").addEventListener("click", (e) => {
  const v = e.target.dataset.v;
  if (!v) return;
  $$("#ldSeg button").forEach((x) => x.classList.toggle("on", x.dataset.v === v));
  $("#ldFrame").classList.toggle("hidden", v !== "preview");
  $("#ldEditor").classList.toggle("hidden", v !== "code");
  document.querySelector(".ld-body").classList.toggle("preview-on", v === "preview");
});

$("#btnLandingReset").addEventListener("click", () => {
  ldCurrentHtml = "";
  LD_HTML_OWNER = null;
  ldRenderPreview();
  $("#ldReq").value = "";
  toast("已重置，下次從零生成", "ok");
});

$("#ldSave").addEventListener("click", async () => {
  const charId = LD_ACTIVE_CHAR;
  if (!charId) return toast("請先選擇角色", "err");
  if (!ldCurrentHtml.trim()) return toast("沒有可儲存的內容", "err");
  if (LD_HTML_OWNER && LD_HTML_OWNER !== charId) {
    return toast("當前編輯的落地頁與選中角色不一致，無法儲存", "err");
  }
  const btn = $("#ldSave");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "儲存中…";
  try {
    const r = await api("/api/landing", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_id: charId, html: ldCurrentHtml }),
    });
    ldCurrentHtml = r.html_filled || r.html || ldCurrentHtml;
    LD_HTML_OWNER = charId;
    ldRenderPreview();
    $("#ldStatus").innerHTML = "已儲存當前落地頁。匯出、同步都會使用這份最新內容。";
    toast("落地頁已儲存", "ok");
  } catch (e) {
    $("#ldStatus").innerHTML = "儲存失敗：" + e.message;
    toast("儲存失敗：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

$("#ldCopy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(ldCurrentHtml); toast("已複製 HTML", "ok"); }
  catch (e) { toast("複製失敗", "err"); }
});

$("#ldOpen").addEventListener("click", () => {
  if (!ldCurrentHtml) return toast("還沒有內容", "err");
  const w = window.open("", "_blank");
  w.document.open(); w.document.write(ldCurrentHtml); w.document.close();
});

$("#btnLanding").addEventListener("click", async () => {
  const checked = selectedLdCharIds();
  // 勾選了多個 → 批次從零生成；否則對當前載入的角色單個生成/迭代
  if (checked.length > 1) {
    const st = $("#ldStatus");
    const btn = $("#btnLanding");
    btn.disabled = true;
    const old = btn.textContent;
    st.innerHTML = `<span class="spinner"></span> 正在為 ${checked.length} 個角色批次生成落地頁…`;
    try {
      const r = await runTask("/api/landing/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          char_ids: checked,
          style_text: ldGetStyle() || null,
          variant: ldGetVariant(),
          request: $("#ldReq").value.trim(),
        }),
      }, (done, total) => {
        btn.textContent = `批次生成中… ${done}/${total}`;
      });
      const errN = Object.keys(r.errors || {}).length;
      st.innerHTML = `已為 ${r.generated.length} 個角色生成落地頁${errN ? `，${errN} 個失敗` : ""}。點選卡片可載入檢視/迭代。`;
      toast(`落地頁批次生成完成：${r.generated.length} 個成功${errN ? `，${errN} 個失敗` : ""}`, errN ? "err" : "ok");
      // 載入第一個成功的檢視
      if (r.generated.length) {
        LD_ACTIVE_CHAR = r.generated[0];
        ldCurrentHtml = "";
        LD_HTML_OWNER = null;
        $$("#ldCharGrid .ig-char-card").forEach((x) =>
          x.classList.toggle("active", x.dataset.charId === LD_ACTIVE_CHAR));
        loadLandingHistory();
      }
    } catch (e) {
      st.innerHTML = "失敗：" + e.message;
      toast("批次生成失敗", "err");
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
    return;
  }

  // 單個：用當前載入的角色（或唯一勾選的）
  const charId = checked[0] || LD_ACTIVE_CHAR;
  if (!charId) return toast("請選擇角色", "err");
  // 一致性校驗：只勾選了另一個角色（未點卡片切換 LD_ACTIVE_CHAR）時，
  // ldCurrentHtml 仍屬於之前載入的角色，不能把它當作 charId 的 current_html 傳送迭代，
  // 否則會把 A 的迭代結果覆蓋儲存為 B 的落地頁。這種情況下改為從零生成，並提示使用者。
  const htmlBelongsToCharId = !!ldCurrentHtml.trim() && LD_HTML_OWNER === charId;
  if (ldCurrentHtml.trim() && LD_HTML_OWNER && LD_HTML_OWNER !== charId) {
    toast("勾選的角色與當前載入的落地頁不一致，將從零生成，不會帶入已載入內容", "err");
  }
  const st = $("#ldStatus");
  const isEdit = htmlBelongsToCharId;
  st.innerHTML = `<span class="spinner"></span> 正在${isEdit ? "修改" : "生成"}落地頁…（約 20-60s）`;
  $("#btnLanding").disabled = true;
  try {
    const r = await runTask("/api/landing", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        char_id: charId,
        style_text: ldGetStyle() || null,
        variant: ldGetVariant(),
        request: $("#ldReq").value.trim(),
        current_html: isEdit ? ldCurrentHtml : null,
      }),
    });
    ldCurrentHtml = r.html_filled || r.html || "";
    LD_HTML_OWNER = charId;
    ldRenderPreview();
    st.innerHTML = "已生成。右側可切換程式碼編輯，或在上方追加要求繼續迭代。";
    toast("落地頁生成成功", "ok");
    $("#ldReq").value = "";
    // 僅當生成的角色就是當前載入角色時才過載歷史，
    // 否則會用另一角色的歷史頁覆蓋剛生成的預覽。
    if (charId === LD_ACTIVE_CHAR) loadLandingHistory();
  } catch (e) {
    st.innerHTML = "失敗：" + e.message;
    toast("生成失敗", "err");
  } finally {
    $("#btnLanding").disabled = false;
  }
});

async function loadLandingHistory() {
  const charId = LD_ACTIVE_CHAR;
  const myGen = ++LD_LOAD_GEN;
  const box = $("#ldHistory");
  if (!charId) { box.innerHTML = ""; ldCurrentHtml = ""; LD_HTML_OWNER = null; ldRenderPreview(); return; }
  try {
    const page = await api("/api/landing/" + charId);
    if (myGen !== LD_LOAD_GEN || charId !== LD_ACTIVE_CHAR) return; // 過期響應，丟棄
    if (page && (page.html_filled || page.html)) {
      ldCurrentHtml = page.html_filled || page.html || "";
      LD_HTML_OWNER = charId;
      ldRenderPreview();
      if (page.variant && $("#ldVariant")) {
        const sel = $("#ldVariant");
        if ([].some.call(sel.options, (o) => o.value === page.variant)) {
          sel.value = page.variant;
          ldUpdateVariantDesc();
        }
      }
      const vlabel = ldVariantLabel(page.variant);
      box.innerHTML = `<div class='ld-hist-title'>已載入上次生成（${vlabel ? esc(vlabel) + " · " : ""}${esc(page.style_text) || "無風格"} · ${new Date((page.created || 0) * 1000).toLocaleString()}），重新生成會覆蓋。</div>`;
    } else {
      ldCurrentHtml = "";
      LD_HTML_OWNER = charId;
      ldRenderPreview();
      box.innerHTML = "";
    }
  } catch (e) {
    if (myGen !== LD_LOAD_GEN || charId !== LD_ACTIVE_CHAR) return; // 過期響應，丟棄
    box.innerHTML = "";
  }
}

// 卡片點選切換角色時已處理預覽與歷史載入，無需額外的 select change 監聽。

// ========== 角色庫 MINIMAP（VS Code 風格頁面縮圖） ==========
// 角色庫檢視下滑時，右側浮出整頁縮略預覽；滑塊=當前視口，可拖拽/點選/滾輪快速定位。
(() => {
  const MM_WIDTH = 110; // 與 .minimap 的 CSS 寬度一致
  const box = $("#minimap");
  const content = $("#minimapContent");
  const slider = $("#minimapSlider");
  const section = $("#view-characters");
  const mainEl = $("main");
  if (!box || !section || !mainEl) return;

  let scale = 0.1;
  let dragging = false;

  // 滾動位置 s → 縮圖座標的線性對映引數。
  // 檔案縮略總高超過 minimap 可視高時，縮略內容隨滾動平移（VS Code 的 slider 行為），
  // 此時滑塊螢幕位移斜率 = scale - c。
  function metrics() {
    const docH = document.documentElement.scrollHeight;
    const winH = window.innerHeight;
    const mmH = box.clientHeight;
    const maxS = Math.max(1, docH - winH);
    const c = docH * scale > mmH ? (docH * scale - mmH) / maxS : 0;
    return { docH, winH, mmH, maxS, c, slope: scale - c };
  }

  // 重建縮略 DOM：克隆 main，剝掉所有 id（避免 #charList 等選擇器串擾），同步表單狀態。
  function rebuild() {
    if (!section.classList.contains("active")) { update(); return; }
    const w = mainEl.getBoundingClientRect().width || 1080;
    scale = MM_WIDTH / w;
    const clone = mainEl.cloneNode(true);
    const src = $$("input, textarea, select", mainEl);
    $$("input, textarea, select", clone).forEach((el, i) => {
      if (!src[i]) return;
      if (el.type === "checkbox" || el.type === "radio") el.checked = src[i].checked;
      else el.value = src[i].value;
    });
    clone.querySelectorAll("[id]").forEach((el) => el.removeAttribute("id"));
    clone.style.width = w + "px";
    clone.style.maxWidth = "none";
    clone.style.margin = "0";
    content.replaceChildren(clone);
    update();
  }

  function update() {
    const { docH, winH, mmH, c, slope } = metrics();
    const s = window.scrollY;
    const show = section.classList.contains("active") &&
      docH - winH > 200 && (s > 80 || dragging);
    box.classList.toggle("on", show);
    if (!show) return;
    const ty = mainEl.offsetTop * scale - c * s;
    content.style.transform = `translateY(${ty}px) scale(${scale})`;
    const sliderH = Math.max(winH * scale, 18);
    const y = Math.max(0, Math.min(s * slope, mmH - sliderH));
    slider.style.height = sliderH + "px";
    slider.style.transform = `translateY(${y}px)`;
  }

  function scrollDoc(s) {
    window.scrollTo({ top: Math.max(0, Math.min(s, metrics().maxS)) });
  }

  let dragStartY = 0, dragStartScroll = 0, dragSlope = 0.1;

  box.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const m = metrics();
    if (e.target !== slider) {
      // 點選空白軌道：滑塊中心跳到點選處（VS Code 行為），隨後可繼續拖拽
      const sliderH = Math.max(m.winH * scale, 18);
      const targetY = e.clientY - box.getBoundingClientRect().top - sliderH / 2;
      scrollDoc(targetY / m.slope);
    }
    dragging = true;
    box.classList.add("dragging");
    dragStartY = e.clientY;
    dragStartScroll = window.scrollY;
    dragSlope = m.slope;
    try { box.setPointerCapture(e.pointerId); } catch (_) {}
  });
  box.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    scrollDoc(dragStartScroll + (e.clientY - dragStartY) / dragSlope);
  });
  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    box.classList.remove("dragging");
    update();
  };
  box.addEventListener("pointerup", endDrag);
  box.addEventListener("pointercancel", endDrag);
  box.addEventListener("wheel", (e) => {
    e.preventDefault();
    window.scrollBy({ top: e.deltaY });
  }, { passive: false });

  let timer = null;
  const scheduleRebuild = () => {
    clearTimeout(timer);
    timer = setTimeout(rebuild, 120);
  };
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", scheduleRebuild);
  // 檢視切換、列表/詳情重渲染、封面圖載入都會觸發這裡，防抖後整體重建
  new MutationObserver(scheduleRebuild).observe(mainEl, {
    subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ["class", "style", "src"],
  });
  mainEl.addEventListener("change", scheduleRebuild, true); // 勾選狀態同步進縮圖
})();
