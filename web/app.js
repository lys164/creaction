const LANGS = ["zh", "ja", "ko", "en"];
const LANG_NAMES = { zh: "中", ja: "日", ko: "韩", en: "EN" };
const LANG_NAMES_FULL = { zh: "简体中文", ja: "日本語", ko: "한국어", en: "English" };

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

// 轮询后台任务直到完成。onProgress(done,total) 可选。返回任务 result。
async function pollTask(taskId, onProgress) {
  let netRetries = 0;
  while (true) {
    await new Promise((r) => setTimeout(r, 2000));
    let t;
    try {
      t = await api("/api/tasks/" + taskId);
    } catch (e) {
      if (/\b404\b|not found/i.test(e.message)) {
        throw new Error("任务已失效（可能服务已重启），请重试");
      }
      if (++netRetries > 5) throw new Error("网络异常，任务轮询中断：" + e.message);
      continue;
    }
    netRetries = 0;
    if (onProgress) onProgress(t.done_count || 0, t.total || 0);
    if (t.status === "done") return t.result;
    if (t.status === "error") throw new Error(t.error || "任务失败");
  }
}

// 提交一个返回 {task_id} 的接口并轮询到完成。
async function runTask(path, opts, onProgress) {
  const r = await api(path, opts);
  if (!r || !r.task_id) return r; // 兼容仍同步返回的接口
  return pollTask(r.task_id, onProgress);
}

// 角色语种筛选：各视图独立保存当前选中语种（""=全部）。
const LANG_FILTER = { char: "", ig: "", post: "", ld: "" };
const LANG_ORDER = ["zh", "ja", "ko", "en"];

// 渲染语种筛选条。containerId 对应 HTML 里的 .lang-filter，chars 为完整角色列表，
// onChange 在用户切换语种时回调（用于重渲染对应列表）。
function renderLangFilter(containerId, key, chars, onChange) {
  const box = document.getElementById(containerId);
  if (!box) return;
  const present = LANG_ORDER.filter((lg) => chars.some((c) => c.lang === lg));
  // 只剩一种或没有语种时不显示筛选条
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

// 按当前筛选语种过滤角色列表。
function filterByLang(chars, key) {
  const lg = LANG_FILTER[key];
  return lg ? chars.filter((c) => c.lang === lg) : chars;
}

function imgUrl(local_path, url) {
  if (local_path) {
    const name = local_path.split("/").pop();
    return "/img/" + name;
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
  summary: "概述", decisive_event: "关键经历",
  response: "性格底色", cost: "另一面/盲区", desire_outer: "声称要的",
  desire_inner: "真正要的", desire_bottom_line: "底线", healing: "治愈条件",
  note: "注释", messages: "开场白",
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
    e.preventDefault();
    const v = s.dataset.view;
    $$(".step").forEach((x) => x.classList.toggle("active", x === s));
    $$(".view").forEach((x) => x.classList.toggle("active", x.id === "view-" + v));
    if (v === "upload") initCreateCoverStyle();
    if (v === "characters") loadCharacters();
    if (v === "posts") initPostsView();
    if (v === "igposts") initIgView();
    if (v === "landing") initLandingView();
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

// 直接粘贴图片（Ctrl/Cmd+V）：把剪贴板里的图片当作上传文件，不落本地磁盘。
// 仅在「上传」视图激活时响应，且避免干扰在输入框里粘贴文字。
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
  if (!files.length) return;        // 没有图片就放行（比如在输入框粘贴文字）
  if (isTextInput && !files.length) return;
  e.preventDefault();
  addFiles(files);
  toast(`已粘贴 ${files.length} 张图片`, "ok");
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
  sel.innerHTML = STYLES.map((s) => `<option value="${s.id}">${s.name}</option>`).join("");
  if (prev && STYLES.some((s) => s.id === prev)) sel.value = prev;
}
initCreateCoverStyle();

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
function renderThumbs() {
  const box = $("#thumbs");
  box.innerHTML = "";
  pendingFiles.forEach((f) => {
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    box.appendChild(img);
  });
  pendingJson.forEach((f) => {
    const chip = document.createElement("span");
    chip.className = "json-chip";
    chip.textContent = "📄 " + (f.name || "characters.json");
    box.appendChild(chip);
  });
  // JSON 导入时显示"下载源图"开关
  const row = $("#dlImageRow");
  if (row) row.style.display = pendingJson.length ? "" : "none";
}

$("#btnPersona").addEventListener("click", async () => {
  const langs = $$("#langPick input:checked").map((i) => i.value);
  if (!langs.length) return toast("请至少选择一种语言", "err");
  const hintText = $("#userHint").value.trim();
  if (!pendingFiles.length && !pendingJson.length && !hintText)
    return toast("请上传图片 / 角色 JSON，或在补充要求里填写文字", "err");

  const btn = $("#btnPersona");
  btn.disabled = true;
  const st = $("#uploadStatus");
  const withCover = $("#withCoverOnCreate").checked;

  try {
    // JSON 导入分支：把已有角色 JSON 扩写成 POPOP 人设
    if (pendingJson.length) {
      st.innerHTML = `<span class="spinner"></span> 正在解析 JSON，并为 ${langs.length} 种语言各自扩写人设${
        withCover ? " + 封面图" : ""
      }…（条数多时较慢）`;
      const fd = new FormData();
      pendingJson.forEach((f) => fd.append("files", f));
      fd.append("user_hint", $("#userHint").value);
      fd.append("langs", langs.join(","));
      fd.append("download_image", $("#downloadImage").checked);
      fd.append("with_cover", withCover);
      fd.append("cover_style_id", withCover ? $("#createCoverStyle").value : "");
      const r = await runTask("/api/personas/import_json", { method: "POST", body: fd }, (done, total) => {
        st.innerHTML = `<span class="spinner"></span> 导入中… ${done}/${total} 个角色`;
      });
      const errN = Object.keys(r.cover_errors || {}).length;
      const failN = Object.keys(r.errors || {}).length;
      st.innerHTML = `已导入 ${r.count} 个角色（按语言拆分）${
        failN ? `，扩写失败 ${failN} 个` : ""
      }${withCover ? `，封面失败 ${errN} 个` : ""}。前往「② 角色」查看。`;
      toast(`导入成功${failN || errN ? "（部分失败）" : ""}`, failN || errN ? "err" : "ok");
      pendingJson = [];
      renderThumbs();
      return;
    }

    // 图片分支（也兼容纯文字：无图时按补充要求生成）
    const textOnly = !pendingFiles.length;
    st.innerHTML = `<span class="spinner"></span> ${textOnly ? "正在按文字" : "正在上传，并"}为 ${langs.length} 种语言各自生成本土化人设${
      withCover ? " + 封面图" : ""
    }…（封面图会额外耗时）`;
    const fd = new FormData();
    pendingFiles.forEach((f) => fd.append("files", f));
    fd.append("user_hint", $("#userHint").value);
    fd.append("one_per_image", $("#onePerImage").checked);
    fd.append("langs", langs.join(","));
    fd.append("with_cover", withCover);
    fd.append("cover_style_id", withCover ? $("#createCoverStyle").value : "");
    const r = await runTask("/api/personas", { method: "POST", body: fd }, (done, total) => {
      st.innerHTML = `<span class="spinner"></span> 生成中… ${done}/${total} 组`;
    });
    const errN = Object.keys(r.cover_errors || {}).length;
    st.innerHTML = `已生成 ${r.count} 个角色（按语言拆分）${
      withCover ? `，封面失败 ${errN} 个` : ""
    }。前往「② 角色」查看。`;
    toast(`人设生成成功${errN ? `，${errN} 个封面失败` : ""}`, errN ? "err" : "ok");
    pendingFiles = [];
    renderThumbs();
  } catch (e) {
    st.innerHTML = "失败：" + e.message;
    toast("生成失败", "err");
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
  renderCharList();
}

function renderCharList() {
  const list = filterByLang(CHAR_LIST, "char");
  const box = $("#charList");
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<p class="muted">没有符合当前语种的角色。</p>';
    updateSelCount();
    return;
  }
  list.forEach((c) => {
    const card = document.createElement("div");
    card.className = "char-card";
    const cover = c.cover_url
      ? `<img class="cover" src="${imgUrl(null, c.cover_url)}" />`
      : `<div class="cover">无封面</div>`;
    const langTag = c.lang_name
      ? `<span class="lang-badge ${c.lang}">${c.lang_name}</span>`
      : "";
    const exportTag = c.exported
      ? `<span class="export-badge done">已导出</span>`
      : `<span class="export-badge todo">未导出</span>`;
    card.innerHTML = `<label class="char-pick" title="多选"><input type="checkbox" class="csel" value="${c.char_id}" /></label>
      ${cover}<div class="meta"><div class="name">${langTag}${
      c.name || "(未命名)"
    }</div><div class="tag">${c.has_identity ? "已生成外貌DNA" : "未生成外貌"}${exportTag}</div></div>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest(".char-pick")) return; // 勾选不打开详情
      showCharDetail(c.char_id);
    });
    card.querySelector(".csel").addEventListener("change", updateSelCount);
    box.appendChild(card);
  });
  updateSelCount();
}

function selectedCharIds() {
  return $$("#charList .csel:checked").map((i) => i.value);
}
function updateSelCount() {
  const n = selectedCharIds().length;
  $("#selCount").textContent = n ? `已选 ${n} 个` : "";
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
  if (!ids.length) return toast("请先勾选角色", "err");
  const styleId = $("#batchStyle").value;
  if (!styleId) return toast("请选择封面画风", "err");
  const mode = $("#batchCoverMode").value || "fill_missing";
  if (mode === "image_only" && !confirm("只生图会复用已有 identity + cover_spec，不会补缺失。缺字段的角色会失败。继续？")) return;
  const btn = $("#btnBatchCover");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "生成封面中…";
  const modeName = { fill_missing: "补缺失+生图", full: "全套重跑+生图", image_only: "只生图" }[mode] || "生成封面";
  toast(`正在为 ${ids.length} 个角色生成封面（${modeName}）…`);
  try {
    const r = await runTask("/api/characters/batch_cover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids, style_id: styleId, mode }),
    }, (done, total) => {
      btn.textContent = `生成封面中… ${done}/${total}`;
    });
    const errN = Object.keys(r.errors || {}).length;
    toast(`已生成 ${r.covered.length} 个封面${errN ? `，${errN} 个失败` : ""}`, errN ? "err" : "ok");
    loadCharacters();
  } catch (e) {
    toast("批量生成封面失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

$("#btnBatchDelete").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("请先勾选角色", "err");
  if (!confirm(`删除 ${ids.length} 个角色？连同其封面/帖子/落地页一并删除，不可恢复。`)) return;
  try {
    const r = await api("/api/characters/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids }),
    });
    toast(`已删除 ${r.deleted.length} 个`, "ok");
    $("#charDetail").classList.add("hidden");
    loadCharacters();
  } catch (e) {
    toast("删除失败：" + e.message, "err");
  }
});

$("#btnBatchExport").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("请先勾选角色", "err");
  const btn = $("#btnBatchExport");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "导出中…";
  try {
    const res = await fetch("/api/characters/export", {
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
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    const fname = m ? m[1] : "characters_export.zip";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast(`已导出 ${ids.length} 个角色`, "ok");
    loadCharacters();
  } catch (e) {
    toast("导出失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

$("#btnBatchPersona").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("请先勾选角色", "err");
  if (!confirm(`重新生成 ${ids.length} 个角色的人设？不改图、不动外貌/封面/帖子，仅重刷人设 schema。`)) return;
  const btn = $("#btnBatchPersona");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "重生中…";
  try {
    const r = await api("/api/characters/regenerate_persona", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids }),
    });
    const errN = Object.keys(r.errors || {}).length;
    toast(`已重生 ${r.regenerated.length} 个${errN ? `，${errN} 个失败` : ""}`, errN ? "err" : "ok");
    loadCharacters();
  } catch (e) {
    toast("重生失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

$("#btnBatchOpening").addEventListener("click", async () => {
  const ids = selectedCharIds();
  if (!ids.length) return toast("请先勾选角色", "err");
  if (!confirm(`重写 ${ids.length} 个角色的开场白？依据其它人设信息生成新的开场白注释+消息，其它字段不变。`)) return;
  const btn = $("#btnBatchOpening");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "重写中…";
  try {
    const r = await api("/api/characters/regenerate_opening", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ char_ids: ids }),
    });
    const errN = Object.keys(r.errors || {}).length;
    toast(`已重写 ${r.regenerated.length} 个开场白${errN ? `，${errN} 个失败` : ""}`, errN ? "err" : "ok");
    loadCharacters();
  } catch (e) {
    toast("批量重写开场白失败：" + e.message, "err");
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
  const styleOpts = STYLES.map(
    (s) => `<option value="${s.id}">${s.name}</option>`
  ).join("");

  const fields = [
    ["name", "姓名"], ["profile", "侧写"],
    ["species", "物种"], ["gender", "性别"], ["voice", "音色"],
    ["anonymous_identities", "匿名身份"],
    ["personality", "性格"],
    ["opening", "开场白"],
    ["appearance", "外貌穿搭"],
    ["hometown", "出身地"], ["residence", "居住地"],
    ["social_status", "职业/阶级"], ["speech_style", "语言习惯"],
    ["relationship_with_user", "和用户的关系"], ["relationship_mode", "社交模式"],
    ["love_style", "表达爱的方式"], ["situational_reactions", "情境反应"],
    ["hidden_side", "反差萌"], ["life_details", "生活习惯"],
    ["likes", "爱好"], ["fears", "讨厌的东西"], ["wishlist", "愿望清单"],
    ["backstory", "成长经历"], ["family", "家庭成员"],
    ["social_network", "社交关系"], ["premise", "特殊背景/世界观"],
  ];
  const tags = Array.isArray(p.tags) ? p.tags.join(" / ") : localized(p.tags);
  let fieldHtml = `<div class="pf"><span class="k">标签</span><div class="v">${tags}</div></div>`;
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
    ? `<img src="${imgUrl(rec.cover.local_path, rec.cover.url)}" />`
    : `<div class="muted">尚未生成封面</div>`;

  d.innerHTML = `
    <div class="detail-grid">
      <div class="cover">
        ${coverImg}
        <label class="field"><span>画风</span>
          <select id="detailStyle">${styleOpts}</select></label>
        <label class="field"><span>生成模式</span>
          <select id="detailCoverMode">
            <option value="fill_missing">补缺失+生图</option>
            <option value="full">全套重跑+生图</option>
            <option value="image_only">只生图</option>
          </select></label>
        <button class="primary" id="btnCover">重绘封面图</button>
        <div id="coverStatus" class="status"></div>
        <button class="ghost" id="btnOpening" style="margin-top:8px">💬 单独重写开场白</button>
        <div id="openingStatus" class="status"></div>
      </div>
      <div class="persona-fields">
        <h3 style="margin-top:0">${rec.lang ? `<span class="lang-badge ${rec.lang}">${LANG_NAMES_FULL[rec.lang] || rec.lang}</span>` : ""}${localized(p.name)} <span class="muted">${charId}</span></h3>
        ${fieldHtml}
        <details><summary class="muted">查看完整人设 JSON</summary>
          <pre class="kv">${escapeHtml(JSON.stringify(p, null, 2))}</pre></details>
        ${rec.identity ? `<details><summary class="muted">查看外貌 identity</summary>
          <pre class="kv">${escapeHtml(JSON.stringify(rec.identity, null, 2))}</pre></details>` : ""}
        ${rec.cover && rec.cover.spec ? `<details><summary class="muted">查看封面 variable / scene</summary>
          <pre class="kv">${escapeHtml(JSON.stringify(rec.cover.spec, null, 2))}</pre></details>` : ""}
      </div>
    </div>`;
  d.scrollIntoView({ behavior: "smooth" });

  $("#btnCover").addEventListener("click", async () => {
    const styleId = $("#detailStyle").value;
    const mode = $("#detailCoverMode").value || "fill_missing";
    if (mode === "image_only" && !confirm("只生图会复用已有 identity + cover_spec，不会补缺失。缺字段会失败。继续？")) return;
    const cs = $("#coverStatus");
    const modeName = { fill_missing: "补缺失+生图", full: "全套重跑+生图", image_only: "只生图" }[mode] || "生成封面";
    cs.innerHTML = `<span class="spinner"></span> ${modeName} 中…（约 60-120s）`;
    $("#btnCover").disabled = true;
    try {
      await api("/api/cover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ char_id: charId, style_id: styleId, mode }),
      });
      cs.innerHTML = "封面已生成。";
      toast("封面生成成功", "ok");
      showCharDetail(charId);
    } catch (e) {
      cs.innerHTML = "失败：" + e.message;
      toast("封面失败", "err");
    } finally {
      $("#btnCover").disabled = false;
    }
  });

  $("#btnOpening").addEventListener("click", async () => {
    const os = $("#openingStatus");
    os.innerHTML = `<span class="spinner"></span> 正在依据人设重写开场白…`;
    $("#btnOpening").disabled = true;
    try {
      await api("/api/opening", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ char_id: charId }),
      });
      os.innerHTML = "开场白已重写。";
      toast("开场白重写成功", "ok");
      showCharDetail(charId);
    } catch (e) {
      os.innerHTML = "失败：" + e.message;
      toast("开场白重写失败", "err");
    } finally {
      $("#btnOpening").disabled = false;
    }
  });
}

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
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
  const list = filterByLang(POST_CHARS, "post");
  $("#postChar").innerHTML = list
    .map((c) => `<option value="${c.char_id}">${c.lang_name ? "[" + c.lang_name + "] " : ""}${c.name || c.char_id}</option>`)
    .join("");
}

$("#btnPosts").addEventListener("click", async () => {
  const charId = $("#postChar").value;
  const typeIds = $$("#postTypes input:checked").map((i) => i.value);
  if (!charId) return toast("请选择角色", "err");
  if (!typeIds.length) return toast("请勾选至少一个帖子类型", "err");

  const st = $("#postStatus");
  const withImages = $("#withImages").checked;
  st.innerHTML = `<span class="spinner"></span> 正在生成 ${typeIds.length} 类帖子文本${
    withImages ? " + 配图" : ""
  }…（配图较慢，请耐心等待）`;
  $("#btnPosts").disabled = true;
  try {
    const r = await api("/api/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        char_id: charId,
        post_type_ids: typeIds,
        count_per_type: parseInt($("#countPerType").value) || 2,
        style_id: null,
        with_images: withImages,
      }),
    });
    st.innerHTML = `已生成 ${r.posts.length} 条帖子。`;
    toast("帖子生成成功", "ok");
    CURRENT_POST_BATCH = {
      char_id: charId,
      batch_id: r.batch_id,
      style_id: r.style_id || null,
    };
    renderPosts(r.posts);
  } catch (e) {
    st.innerHTML = "失败：" + e.message;
    toast("生成失败", "err");
  } finally {
    $("#btnPosts").disabled = false;
  }
});

function renderPosts(posts) {
  const box = $("#postResults");
  box.innerHTML = "";
  posts.forEach((p) => {
    const card = document.createElement("div");
    card.className = "post-card";
    let pimg = `<div class="pimg">未生成配图</div>`;
    if (p.image && p.image.url) {
      pimg = `<img class="pimg" src="${imgUrl(p.image.local_path, p.image.url)}" />`;
    } else if (p.image && p.image.error) {
      pimg = `<div class="pimg">配图失败：${p.image.error}</div>`;
    }
    card.innerHTML = `
      ${pimg}
      <div class="pbody">
        <div class="ptype">${p.type_name}</div>
        <div class="content">${escapeHtml(localized(p.content))}</div>
        <div class="post-actions">
          <button class="ghost rerender-post-img" data-post-id="${p.post_id}">重新生成图片</button>
          <button class="ghost danger delete-post" data-post-id="${p.post_id}">删除</button>
        </div>
        <div class="kv">
          <details><summary>variable / scene（生图描述）</summary>
            <pre>${escapeHtml(JSON.stringify({ variable: p.variable, scene: p.scene }, null, 2))}</pre>
          </details>
        </div>
      </div>`;
    box.appendChild(card);
  });
  $$("#postResults .rerender-post-img").forEach((btn) => {
    btn.addEventListener("click", () => rerenderRegularPostImage(btn));
  });
  $$("#postResults .delete-post").forEach((btn) => {
    btn.addEventListener("click", () => deleteRegularPost(btn));
  });
}

async function rerenderRegularPostImage(btn) {
  if (!CURRENT_POST_BATCH || !CURRENT_POST_BATCH.batch_id) {
    return toast("缺少当前批次信息，请重新生成一批帖子后再重绘单图", "err");
  }
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "重绘中…";
  try {
    const r = await api(`/api/posts/${CURRENT_POST_BATCH.char_id}/${CURRENT_POST_BATCH.batch_id}/${btn.dataset.postId}/image`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style_id: CURRENT_POST_BATCH.style_id }),
    });
    renderPosts(r.batch.posts);
    toast("图片已重新生成", "ok");
  } catch (e) {
    toast("重绘失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function deleteRegularPost(btn) {
  if (!CURRENT_POST_BATCH || !CURRENT_POST_BATCH.batch_id) {
    return toast("缺少当前批次信息，无法删除", "err");
  }
  if (!confirm("删除这条帖子？对应图片也会删除。")) return;
  try {
    const r = await api(`/api/posts/${CURRENT_POST_BATCH.char_id}/${CURRENT_POST_BATCH.batch_id}/${btn.dataset.postId}`, {
      method: "DELETE",
    });
    renderPosts(r.batch.posts);
    toast("帖子已删除", "ok");
  } catch (e) {
    toast("删除失败：" + e.message, "err");
  }
}

// ========== IG POSTS VIEW ==========
let IG_CHARS = [];
let IG_ACTIVE_CHAR = null;

async function initIgView() {
  await ensureStyles();
  IG_CHARS = await api("/api/characters");
  renderLangFilter("igLangFilter", "ig", IG_CHARS, renderIgCharGrid);
  renderIgCharGrid();
  const shown = filterByLang(IG_CHARS, "ig");
  if (!IG_ACTIVE_CHAR && shown.length) IG_ACTIVE_CHAR = shown[0].char_id;
  if (IG_ACTIVE_CHAR) loadLatestIg(IG_ACTIVE_CHAR);
}

function selectedIgCharIds() {
  return $$("#igCharGrid .ig-csel:checked").map((i) => i.value);
}

function updateIgSelCount() {
  const n = selectedIgCharIds().length;
  $("#igSelCount").textContent = n ? `已选 ${n} 个` : "";
}

function renderIgCharGrid() {
  const box = $("#igCharGrid");
  box.innerHTML = "";
  const list = filterByLang(IG_CHARS, "ig");
  if (!list.length) {
    box.innerHTML = '<p class="muted">没有符合当前语种的角色。</p>';
    return;
  }
  list.forEach((c) => {
    const card = document.createElement("div");
    card.className = "char-card ig-char-card";
    card.dataset.charId = c.char_id;
    const cover = c.cover_url
      ? `<img class="cover" src="${imgUrl(null, c.cover_url)}" />`
      : `<div class="cover">无封面</div>`;
    const langTag = c.lang_name
      ? `<span class="lang-badge ${c.lang}">${c.lang_name}</span>`
      : "";
    card.innerHTML = `<label class="char-pick" title="多选生成"><input type="checkbox" class="ig-csel" value="${c.char_id}" /></label>
      ${cover}<div class="meta"><div class="name">${langTag}${c.name || "(未命名)"}</div>
      <div class="tag">点击查看已生成帖子</div></div>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest(".char-pick")) return;
      IG_ACTIVE_CHAR = c.char_id;
      $$("#igCharGrid .ig-char-card").forEach((x) =>
        x.classList.toggle("active", x.dataset.charId === c.char_id)
      );
      loadLatestIg(c.char_id);
    });
    card.querySelector(".ig-csel").addEventListener("change", updateIgSelCount);
    box.appendChild(card);
  });
  $$("#igCharGrid .ig-char-card").forEach((x) =>
    x.classList.toggle("active", x.dataset.charId === IG_ACTIVE_CHAR)
  );
  updateIgSelCount();
}

async function loadLatestIg(charId = IG_ACTIVE_CHAR) {
  IG_ACTIVE_CHAR = charId;
  $("#igResults").innerHTML = "";
  if (!charId) return;
  const c = IG_CHARS.find((x) => x.char_id === charId);
  $("#igViewingTitle").textContent = c ? `正在查看：${c.lang_name ? "[" + c.lang_name + "] " : ""}${c.name || c.char_id}` : "";
  try {
    const b = await api("/api/ig_posts/" + charId + "/latest");
    if (b && b.posts && b.posts.length) {
      $("#igStatus").innerHTML = `已加载上次生成的 ${b.posts.length} 条（${new Date((b.created || 0) * 1000).toLocaleString()}）。重新生成会覆盖。`;
      renderIgPosts(b.posts);
    } else {
      $("#igStatus").innerHTML = "";
    }
  } catch (e) { $("#igStatus").innerHTML = ""; }
}

$("#btnIgSelAll").addEventListener("click", () => {
  $$("#igCharGrid .ig-csel").forEach((b) => (b.checked = true));
  updateIgSelCount();
});

$("#btnIgSelNone").addEventListener("click", () => {
  $$("#igCharGrid .ig-csel").forEach((b) => (b.checked = false));
  updateIgSelCount();
});

$("#btnIg").addEventListener("click", async () => {
  const ids = selectedIgCharIds();
  if (!ids.length) return toast("请先勾选角色", "err");
  const st = $("#igStatus");
  const withImages = $("#igWithImages").checked;
  const countRaw = $("#igCount").value.trim();
  const n = countRaw ? parseInt(countRaw) : null;
  const countText = n ? `每个 ${n} 条` : "每个由模型规划 3~9 条";
  st.innerHTML = `<span class="spinner"></span> 正在为 ${ids.length} 个角色生成 INS 帖子，${countText}${
    withImages ? " + 配图（较慢）" : ""
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
      }),
    }, (done, total) => {
      st.innerHTML = `<span class="spinner"></span> 生成中… ${done}/${total} 个角色`;
    });
    const errN = Object.keys(r.errors || {}).length;
    st.innerHTML = `已生成 ${r.generated.length} 个角色的 INS 帖子${errN ? `，${errN} 个失败` : ""}。点击头像可查看各自已保存的帖子。`;
    toast(`INS 帖子生成完成：${r.generated.length} 个成功${errN ? `，${errN} 个失败` : ""}`, errN ? "err" : "ok");
    if (r.generated.length) {
      IG_ACTIVE_CHAR = r.generated[0].char_id;
      renderIgCharGrid();
      loadLatestIg(IG_ACTIVE_CHAR);
    }
  } catch (e) {
    st.innerHTML = "失败：" + e.message;
    toast("生成失败", "err");
  } finally {
    $("#btnIg").disabled = false;
  }
});

function renderIgPosts(posts) {
  const box = $("#igResults");
  box.innerHTML = "";
  posts.forEach((p) => {
    const card = document.createElement("div");
    card.className = "post-card";

    let badge, pimg;
    if (p.format === "text_only") {
      badge = `<span class="badge">纯文本 · Threads</span>`;
      pimg = `<div class="pimg">纯文本帖（无图）</div>`;
    } else if (p.image && p.image.url) {
      const PK = {
        screenshot: "截图",
        graphic: "图文卡",
        collage: "拼贴",
        photo_dump: "Photo dump",
        journal_overlay: "手写标注",
        airdrop_card: "AirDrop卡",
        word_cloud: "关键词云",
        calendar_card: "日历卡",
        photo: "随手拍",
      };
      let t;
      if (p.image.type === "selfie") t = "自拍 selfie · 图生图";
      else if (p.image.type === "composite") t = "composite · " + (PK[p.image.photo_kind || p.photo_kind] || "拼贴图生图");
      else t = "photo · " + (PK[p.image.photo_kind || p.photo_kind] || "文生图");
      badge = `<span class="badge">${t}</span>`;
      pimg = `<img class="pimg" src="${imgUrl(p.image.local_path, p.image.url)}" />`;
    } else if (p.image && p.image.error) {
      badge = `<span class="badge">${p.image_type || ""} 配图失败</span>`;
      pimg = `<div class="pimg">配图失败：${p.image.error}</div>`;
    } else {
      badge = `<span class="badge">${p.image_type || "图文"}（未生成图）</span>`;
      pimg = `<div class="pimg">未生成配图</div>`;
    }

    const typeTag = p.post_type_name
      ? `<span class="ttag ${p.post_type}">${p.post_type_name}</span>`
      : "";

    const spec = p.selfie
      ? { selfie: p.selfie }
      : p.photo_prompt
      ? { photo_kind: p.photo_kind, photo_schema: p.photo_schema, photo_prompt: p.photo_prompt }
      : {};

    card.innerHTML = `
      ${pimg}
      <div class="pbody">
        <div class="ptype">${typeTag} ${badge}</div>
        <div class="content">${escapeHtml(localized(p.content))}</div>
        <div class="post-actions">
          ${p.format !== "text_only" ? `<button class="ghost rerender-ig-img" data-post-id="${p.post_id}">重新生成图片</button>` : ""}
          <button class="ghost danger delete-ig-post" data-post-id="${p.post_id}">删除</button>
        </div>
        <div class="kv">
          <details><summary>生图描述 / prompt</summary>
            <pre>${escapeHtml(JSON.stringify(spec, null, 2))}</pre>
            ${p.image && p.image.prompt ? `<pre>${escapeHtml(p.image.prompt)}</pre>` : ""}
          </details>
        </div>
      </div>`;
    box.appendChild(card);
  });
  $$("#igResults .rerender-ig-img").forEach((btn) => {
    btn.addEventListener("click", () => rerenderIgPostImage(btn));
  });
  $$("#igResults .delete-ig-post").forEach((btn) => {
    btn.addEventListener("click", () => deleteIgPost(btn));
  });
}

async function rerenderIgPostImage(btn) {
  if (!IG_ACTIVE_CHAR) return toast("请先选择角色", "err");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "重绘中…";
  try {
    const r = await api(`/api/ig_posts/${IG_ACTIVE_CHAR}/${btn.dataset.postId}/image`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style_id: null }),
    });
    renderIgPosts(r.batch.posts);
    toast("图片已重新生成", "ok");
  } catch (e) {
    toast("重绘失败：" + e.message, "err");
    if (/not found|没有/i.test(e.message)) loadLatestIg(IG_ACTIVE_CHAR);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function deleteIgPost(btn) {
  if (!IG_ACTIVE_CHAR) return toast("请先选择角色", "err");
  if (!confirm("删除这条 INS 帖子？对应图片也会删除。")) return;
  try {
    const r = await api(`/api/ig_posts/${IG_ACTIVE_CHAR}/${btn.dataset.postId}`, {
      method: "DELETE",
    });
    renderIgPosts(r.batch.posts);
    toast("帖子已删除", "ok");
  } catch (e) {
    toast("删除失败：" + e.message, "err");
    if (/not found|没有/i.test(e.message)) loadLatestIg(IG_ACTIVE_CHAR);
  }
}

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
    return toast("JSON 格式错误", "err");
  }
  try {
    await api("/api/styles", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed),
    });
    STYLES = parsed;
    refreshStyleSelects();
    st.innerHTML = "已保存 " + parsed.length + " 个画风。";
    toast("画风库已更新", "ok");
  } catch (e) {
    st.innerHTML = "失败：" + e.message;
  }
});

// ========== LANDING PAGE ==========
let LANDING_STYLES = [];
let ldCurrentHtml = "";
let LD_CHARS = [];
let LD_ACTIVE_CHAR = null;

function renderLdCharGrid() {
  const box = $("#ldCharGrid");
  box.innerHTML = "";
  const list = filterByLang(LD_CHARS, "ld");
  if (!list.length) {
    box.innerHTML = '<p class="muted">没有符合当前语种的角色。</p>';
    updateLdSelCount();
    return;
  }
  // 当前编辑目标若不在筛选结果里，重置为第一个
  if (!list.some((c) => c.char_id === LD_ACTIVE_CHAR)) {
    LD_ACTIVE_CHAR = list[0].char_id;
  }
  list.forEach((c) => {
    const card = document.createElement("div");
    card.className = "char-card ig-char-card";
    card.dataset.charId = c.char_id;
    const cover = c.cover_url
      ? `<img class="cover" src="${imgUrl(null, c.cover_url)}" />`
      : `<div class="cover">无封面</div>`;
    const langTag = c.lang_name
      ? `<span class="lang-badge ${c.lang}">${c.lang_name}</span>`
      : "";
    card.innerHTML = `<label class="char-pick" title="多选批量生成"><input type="checkbox" class="ld-csel" value="${c.char_id}" /></label>
      ${cover}<div class="meta"><div class="name">${langTag}${c.name || "(未命名)"}</div>
      <div class="tag">点击载入右侧编辑</div></div>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest(".char-pick")) return;
      LD_ACTIVE_CHAR = c.char_id;
      ldCurrentHtml = "";
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
  $("#ldSelCount").textContent = n ? `已选 ${n} 个` : "";
}

async function initLandingView() {
  const chars = await api("/api/characters");
  LD_CHARS = chars;
  renderLangFilter("ldLangFilter", "ld", chars, renderLdCharGrid);
  renderLdCharGrid();
  if (!LANDING_STYLES.length) {
    try { LANDING_STYLES = await api("/api/landing_styles"); } catch (e) { LANDING_STYLES = []; }
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

function ldGetStyle() {
  const on = $(".lchip.on");
  return on ? on.textContent : $("#ldStyleInput").value.trim();
}

function ldRenderPreview() {
  $("#ldFrame").srcdoc = ldCurrentHtml || "<!DOCTYPE html><html><body style='margin:0;display:grid;place-items:center;height:100vh;font-family:system-ui;color:#aaa;font-size:14px'>预览区</body></html>";
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
  ldRenderPreview();
  $("#ldReq").value = "";
  toast("已重置，下次从零生成", "ok");
});

$("#ldCopy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(ldCurrentHtml); toast("已复制 HTML", "ok"); }
  catch (e) { toast("复制失败", "err"); }
});

$("#ldOpen").addEventListener("click", () => {
  if (!ldCurrentHtml) return toast("还没有内容", "err");
  const w = window.open("", "_blank");
  w.document.open(); w.document.write(ldCurrentHtml); w.document.close();
});

$("#btnLanding").addEventListener("click", async () => {
  const checked = selectedLdCharIds();
  // 勾选了多个 → 批量从零生成；否则对当前载入的角色单个生成/迭代
  if (checked.length > 1) {
    const st = $("#ldStatus");
    const btn = $("#btnLanding");
    btn.disabled = true;
    const old = btn.textContent;
    st.innerHTML = `<span class="spinner"></span> 正在为 ${checked.length} 个角色批量生成落地页…`;
    try {
      const r = await runTask("/api/landing/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          char_ids: checked,
          style_text: ldGetStyle() || null,
          request: $("#ldReq").value.trim(),
        }),
      }, (done, total) => {
        btn.textContent = `批量生成中… ${done}/${total}`;
      });
      const errN = Object.keys(r.errors || {}).length;
      st.innerHTML = `已为 ${r.generated.length} 个角色生成落地页${errN ? `，${errN} 个失败` : ""}。点击卡片可载入查看/迭代。`;
      toast(`落地页批量生成完成：${r.generated.length} 个成功${errN ? `，${errN} 个失败` : ""}`, errN ? "err" : "ok");
      // 载入第一个成功的查看
      if (r.generated.length) {
        LD_ACTIVE_CHAR = r.generated[0];
        ldCurrentHtml = "";
        $$("#ldCharGrid .ig-char-card").forEach((x) =>
          x.classList.toggle("active", x.dataset.charId === LD_ACTIVE_CHAR));
        loadLandingHistory();
      }
    } catch (e) {
      st.innerHTML = "失败：" + e.message;
      toast("批量生成失败", "err");
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
    return;
  }

  // 单个：用当前载入的角色（或唯一勾选的）
  const charId = checked[0] || LD_ACTIVE_CHAR;
  if (!charId) return toast("请选择角色", "err");
  const st = $("#ldStatus");
  const isEdit = !!ldCurrentHtml.trim();
  st.innerHTML = `<span class="spinner"></span> 正在${isEdit ? "修改" : "生成"}落地页…（约 20-60s）`;
  $("#btnLanding").disabled = true;
  try {
    const r = await runTask("/api/landing", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        char_id: charId,
        style_text: ldGetStyle() || null,
        request: $("#ldReq").value.trim(),
        current_html: isEdit ? ldCurrentHtml : null,
      }),
    });
    ldCurrentHtml = r.html_filled || r.html || "";
    ldRenderPreview();
    st.innerHTML = "已生成。右侧可切换代码编辑，或在上方追加要求继续迭代。";
    toast("落地页生成成功", "ok");
    $("#ldReq").value = "";
    loadLandingHistory();
  } catch (e) {
    st.innerHTML = "失败：" + e.message;
    toast("生成失败", "err");
  } finally {
    $("#btnLanding").disabled = false;
  }
});

async function loadLandingHistory() {
  const charId = LD_ACTIVE_CHAR;
  const box = $("#ldHistory");
  if (!charId) { box.innerHTML = ""; ldCurrentHtml = ""; ldRenderPreview(); return; }
  try {
    const page = await api("/api/landing/" + charId);
    if (page && (page.html_filled || page.html)) {
      ldCurrentHtml = page.html_filled || page.html || "";
      ldRenderPreview();
      box.innerHTML = `<div class='ld-hist-title'>已加载上次生成（${page.style_text || "无风格"} · ${new Date((page.created || 0) * 1000).toLocaleString()}），重新生成会覆盖。</div>`;
    } else {
      ldCurrentHtml = "";
      ldRenderPreview();
      box.innerHTML = "";
    }
  } catch (e) { box.innerHTML = ""; }
}

// 卡片点击切换角色时已处理预览与历史加载，无需额外的 select change 监听。
