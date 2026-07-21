"""Landing-page (角色主頁/展示頁) generation.

Reuses the production pipeline's character records (persona authored natively in
one language + a redrawn cover) and turns them into a single-screen HTML
showcase page, driven by the same system-prompt + style-map approach as the
standalone OC-studio tool.

The heavy prompt constants (SP_TEMPLATE, STYLE_MAP, DEFAULT_DESIGN_DIRECTIVE,
FALLBACK) are extracted verbatim from the reference tool into
data/landing_prompts.json so they stay in sync and don't get truncated.
"""
import json
import re
from pathlib import Path

from . import config

_PROMPTS_FILE = Path(__file__).resolve().parent / "data" / "landing_prompts.json"
_PROMPTS = json.loads(_PROMPTS_FILE.read_text(encoding="utf-8"))

_PROMPT_PACKS: dict[str, dict] = _PROMPTS.get("PROMPT_PACKS") or {
    "zh-CN": {
        "SP_TEMPLATE": _PROMPTS.get("SP_TEMPLATE", ""),
        "STYLE_MAP": _PROMPTS.get("STYLE_MAP", {}),
        "DEFAULT_DESIGN_DIRECTIVE": _PROMPTS.get("DEFAULT_DESIGN_DIRECTIVE", ""),
        "FALLBACK": _PROMPTS.get("FALLBACK", ""),
    }
}

# 可切換的落地頁 prompt 變體：default = 原長圖敘事頁；其餘變體（如
# interactive 互動卡片版）自帶完整 SYSTEM_PROMPT，輸出整份 HTML 檔案。
_VARIANTS: dict[str, dict] = _PROMPTS.get("PROMPT_VARIANTS") or {}
_DEFAULT_VARIANT = "default"


def _variant(variant: str | None) -> dict:
    return _VARIANTS.get(variant or "") or _VARIANTS.get(_DEFAULT_VARIANT) or {}


def landing_variants() -> list[dict]:
    """落地頁 prompt 變體清單，供前端做選項切換。"""
    out = []
    for vid, v in _VARIANTS.items():
        out.append({
            "id": vid,
            "label": v.get("label") or vid,
            "desc": v.get("desc") or "",
        })
    return out


_LANG_TO_PROMPT_LANG = {
    "zh": "zh-CN",
    "zh-CN": "zh-CN",
    "zh-TW": "zh-TW",
    "ja": "ja",
    "ko": "ko",
    "en": "en",
}
SP_TEMPLATE: str = _PROMPT_PACKS["zh-CN"].get("SP_TEMPLATE", "")
STYLE_MAP: dict = _PROMPT_PACKS["zh-CN"].get("STYLE_MAP", {})
DEFAULT_DESIGN_DIRECTIVE: str = _PROMPT_PACKS["zh-CN"].get(
    "DEFAULT_DESIGN_DIRECTIVE", ""
)
FALLBACK: str = _PROMPT_PACKS["zh-CN"].get("FALLBACK", "")


def _prompt_lang(lang: str | None) -> str:
    code = _LANG_TO_PROMPT_LANG.get(lang or "") or "zh-CN"
    return code if code in _PROMPT_PACKS else "zh-CN"


def _prompt_pack(lang: str | None) -> dict:
    return _PROMPT_PACKS.get(_prompt_lang(lang)) or _PROMPT_PACKS["zh-CN"]


def landing_styles(lang: str | None = None) -> list[str]:
    """Preset landing-page style names (free text also accepted).

    The UI uses the zh-CN keys as stable cross-language preset IDs. When a
    character is generated in another language, the same preset index is mapped
    to that language's prompt pack.
    """
    if not lang:
        return list(STYLE_MAP.keys())
    return list((_prompt_pack(lang).get("STYLE_MAP") or STYLE_MAP).keys())


def _style_content(style_text: str | None, pack: dict) -> str:
    style = (style_text or "").strip()
    style_map = pack.get("STYLE_MAP") or {}
    if style in style_map:
        return style_map[style]

    # Preserve existing front-end chips: zh-CN preset names are treated as
    # stable IDs and mapped by position into the active language pack.
    default_keys = list(STYLE_MAP.keys())
    if style in STYLE_MAP:
        idx = default_keys.index(style)
        current_values = list(style_map.values())
        if idx < len(current_values):
            return current_values[idx]
        return STYLE_MAP[style]

    # Also support localized style names from older pages or direct API calls.
    for other_pack in _PROMPT_PACKS.values():
        other_map = other_pack.get("STYLE_MAP") or {}
        other_keys = list(other_map.keys())
        if style in other_map:
            idx = other_keys.index(style)
            current_values = list(style_map.values())
            if idx < len(current_values):
                return current_values[idx]
            return other_map[style]

    return style or pack.get("FALLBACK") or FALLBACK


def build_system_prompt(
    style_text: str | None,
    lang: str | None = "zh",
    locale: str | None = None,
    variant: str | None = None,
) -> str:
    """Inject style into the language-specific system prompt.

    非預設變體（如互動卡片版）自帶完整、自包含的 SYSTEM_PROMPT，直接返回，
    不注入 style（其母版已內建全套約束）。"""
    v = _variant(variant)
    if v.get("SYSTEM_PROMPT"):
        return v["SYSTEM_PROMPT"]

    pack = _prompt_pack(lang)
    template = pack.get("SP_TEMPLATE") or SP_TEMPLATE
    return template.replace("{{style}}", _style_content(style_text, pack))


def _default_design_directive(lang: str | None) -> str:
    return _prompt_pack(lang).get("DEFAULT_DESIGN_DIRECTIVE") or DEFAULT_DESIGN_DIRECTIVE


# --------------------------------------------------------------------------
# Persona record -> flat "profile" text the system prompt expects
# --------------------------------------------------------------------------
_MSG_I18N = {
    "zh": {
        "unnamed": "(未命名角色)",
        "char_info": "# 角色的資訊：",
        "opening_title": "# TA 的開場白與關係鉤子（用作頁面「勾你來聊天」的素材，提煉成 TA 向你自我呈現/邀請的語氣，不要原樣照搬整段）：",
        "opening_note": "開場情境 note",
        "opening_msgs": "TA 主動對你說的第一句話們",
        "cover_yes": 'cover: 角色有封面圖（渲染器會自動注入到 class="oc-cover" 的元素中，你只需留好槽位，src 留空）',
        "cover_no": "cover: 無封面圖（請用 CSS 漸變/紋理生成抽象視覺佔位）",
        "page_lang": "# 頁面文案語言：請用 {name} 撰寫頁面上所有可見文案。{directive}",
        "style_prefix": "風格：",
        "current_html": "\n\n----\n當前 HTML（在此基礎上修改）：\n",
        "current_html_long": "\n\n----\n當前頁面已生成（程式碼較長不重複附上）。請在現有結構基礎上修改，保持整體風格一致。",
        "request": "# 使用者要求：",
        "default_request": "請根據角色資訊生成主頁",
        "output_lang": "\n\n⚠️ 頁面上所有可見文字（標題、正文、標籤、裝飾文案等）一律使用簡體中文。",
        "brand_rule": "⚠️ 品牌規則：角色發帖/聊天所在的平臺一律稱「Popop」，頁面文案不得出現 Instagram/ins/Threads/小紅書/推特 等真實社交平臺名。",
        "design_keywords": r"互動|互動|滑動|佈局|美觀|元件|模組|元件",
    },
    "ja": {
        "unnamed": "(名前未設定)",
        "char_info": "# キャラクター情報：",
        "opening_title": "# 最初のセリフと関係性のフック（ページで『話してみたい』と思わせる素材。全文をそのまま寫さず、自己提示／招待の口調に要約）：",
        "opening_note": "匯入シチュエーション note",
        "opening_msgs": "キャラクターから最初に送られる言葉",
        "cover_yes": 'cover: カバー畫像あり（レンダラーが class="oc-cover" の要素に自動注入するので、src は空のままスロットだけ用意する）',
        "cover_no": "cover: カバー畫像なし（CSS グラデーション／テクスチャで抽象的なビジュアルを作る）",
        "page_lang": "# ページ文言の言語：ページ上の可視テキストはすべて {name} で書くこと。{directive}",
        "style_prefix": "スタイル：",
        "current_html": "\n\n----\n現在の HTML（これをベースに修正）：\n",
        "current_html_long": "\n\n----\nページは生成済み（コードが長いため再添付しません）。既存の構造をベースに修正し、全體のスタイルを一貫させてください。",
        "request": "# ユーザーの要望：",
        "default_request": "キャラクター情報をもとにホームページを生成してください",
        "output_lang": "\n\n⚠️ ページ上のすべての可視テキスト（見出し・本文・ラベル・裝飾コピーなど）は必ず日本語で書いてください。",
        "brand_rule": "⚠️ ブランド規則：キャラクターが投稿・チャットするプラットフォームは必ず「Popop」と呼ぶこと。Instagram/インスタ/Threads/X など実在のSNS名をページ文言に出さない。",
        "design_keywords": r"インタラクション|操作|スライド|レイアウト|デザイン|コンポーネント|モジュール",
    },
    "ko": {
        "unnamed": "(이름 없는 캐릭터)",
        "char_info": "# 캐릭터 정보:",
        "opening_title": "# 첫 대사와 관계 훅(페이지에서 ‘말 걸고 싶게’ 만드는 소재. 원문을 그대로 복붙하지 말고, 캐릭터가 자신을 드러내거나 초대하는 말투로 추출):",
        "opening_note": "오프닝 상황 note",
        "opening_msgs": "캐릭터가 먼저 보내는 첫마디들",
        "cover_yes": 'cover: 커버 이미지 있음(렌더러가 class="oc-cover" 요소에 자동 주입하므로, src는 비워 둔 슬롯만 준비)',
        "cover_no": "cover: 커버 이미지 없음(CSS 그라데이션/텍스처로 추상 비주얼 플레이스홀더 생성)",
        "page_lang": "# 페이지 문구 언어: 페이지의 모든 가시 텍스트는 {name}로 작성하세요. {directive}",
        "style_prefix": "스타일: ",
        "current_html": "\n\n----\n현재 HTML(이를 기반으로 수정):\n",
        "current_html_long": "\n\n----\n현재 페이지가 이미 생성됨(코드가 길어 반복 첨부 안 함). 기존 구조를 기반으로 수정하고 전체 스타일을 일관되게 유지하세요.",
        "request": "# 사용자 요청:",
        "default_request": "캐릭터 정보를 바탕으로 메인 페이지를 생성해 주세요",
        "output_lang": "\n\n⚠️ 페이지의 모든 가시 텍스트(제목, 본문, 라벨, 장식 문구 등)는 전부 한국어로 작성하세요.",
        "brand_rule": "⚠️ 브랜드 규칙: 캐릭터가 글 올리고 대화하는 플랫폼은 반드시 「Popop」이라 부른다. Instagram/인스타/Threads/스레드/X 같은 실제 SNS명을 페이지 문구에 쓰지 않는다.",
        "design_keywords": r"인터랙션|상호작용|슬라이드|레이아웃|디자인|컴포넌트|모듈",
    },
    "en": {
        "unnamed": "(Unnamed character)",
        "char_info": "# Character info:",
        "opening_title": "# Opening line and relationship hook (use as material to make the page invite a chat; distill the voice, do not copy the whole passage verbatim):",
        "opening_note": "opening situation note",
        "opening_msgs": "first lines the character sends first",
        "cover_yes": 'cover: the character has a cover image (the renderer injects it into class="oc-cover" automatically; just leave an empty slot/src)',
        "cover_no": "cover: no cover image (use a CSS gradient/texture for an abstract visual placeholder)",
        "page_lang": "# Page copy language: write every visible text on the page in {name}. {directive}",
        "style_prefix": "Style: ",
        "current_html": "\n\n----\nCurrent HTML (modify on top of this):\n",
        "current_html_long": "\n\n----\nThe page has already been generated (code is long, not re-attached). Please modify on top of the existing structure and keep the overall style consistent.",
        "request": "# User request:",
        "default_request": "Please generate a homepage based on the character info",
        "output_lang": "\n\n⚠️ All visible text on the page (titles, body, labels, decorative copy, etc.) must be written in English.",
        "brand_rule": "⚠️ Brand rule: the platform where the character posts and chats is always called \"Popop\". Never use real social-network names (Instagram/Threads/X/TikTok) in the page copy.",
        "design_keywords": r"interactive|interaction|slide|layout|aesthetic|component|module",
    },
}


def _msg(lang: str | None) -> dict:
    return _MSG_I18N.get(lang or "") or _MSG_I18N["zh"]


# 新舊 schema 鍵並存（同一角色只帶其中一套）：identity/dislikes/worldview/
# online_chat_style/behavior_patterns/inner_structure/value 為新鍵，其餘舊鍵相容存量。
_FIELD_LABELS = {
    "profile": "profile",
    "tags": "tags",
    "species": "species",
    "gender": "gender",
    "value": "value",
    "personality": "personality",
    "inner_structure": "inner_structure",
    "hometown": "hometown",
    "residence": "residence",
    "identity": "identity",
    "social_status": "social_status",
    "speech_style": "speech_style",
    "online_chat_style": "online_chat_style",
    "relationship_with_user": "relationship_with_user",
    "relationship_mode": "relationship_mode",
    "love_style": "love_style",
    "behavior_patterns": "behavior_patterns",
    "situational_reactions": "situational_reactions",
    "hidden_side": "hidden_side",
    "life_details": "life_details",
    "likes": "likes",
    "dislikes": "dislikes",
    "fears": "fears",
    "wishlist": "wishlist",
    "backstory": "backstory",
    "family": "family",
    "social_network": "social_network",
    "worldview": "worldview",
    "premise": "premise",
}


def _stringify(value) -> str:
    """Flatten persona field values (str / list / dict) into readable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for it in value:
            if isinstance(it, str):
                parts.append(it.strip())
            elif isinstance(it, dict):
                # backstory {stage,detail} / 舊 social {name,relation,info,dynamic}
                # / 新 social {name,relationship,description}
                if it.get("stage") or it.get("detail"):
                    head = it.get("stage", "")
                    parts.append(f"{head}：{it.get('detail', '')}".strip("："))
                elif it.get("content") and not (it.get("relation") or it.get("relationship")):
                    parts.append(it.get("content", ""))
                else:
                    head = " · ".join(
                        x for x in (it.get("name"), it.get("relation"),
                                    it.get("relationship")) if x
                    )
                    tail = "；".join(
                        x for x in (it.get("info"), it.get("dynamic"),
                                    it.get("description")) if x
                    )
                    parts.append(f"{head}：{tail}" if tail else head)
            else:
                parts.append(str(it))
        return "；".join(p for p in parts if p)
    if isinstance(value, dict):
        rows = []
        for k, v in value.items():
            sv = _stringify(v)
            if sv:
                rows.append(f"{k}: {sv}")
        return " / ".join(rows)
    return str(value)


def persona_to_profile_text(persona: dict) -> str:
    """Render a persona record into the multi-line `key: value` block the
    landing-page system prompt is designed to read (mirrors the reference tool's
    preset `profile` strings)."""
    lines = []
    for key in _FIELD_LABELS:
        if key == "profile":
            continue  # handled by caller as the lead block
        val = _stringify(persona.get(key))
        if val:
            lines.append(f"{key}: {val}")
    return "\n".join(lines)


def _nonempty(value) -> bool:
    return value not in (None, "", [], {})


def _moment_value(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def moments_to_profile_text(moments: list[dict] | None) -> str:
    rows = []
    for idx, post in enumerate(moments or [], start=1):
        lines = [f"moment {idx}:"]

        def add(label: str, value) -> None:
            if _nonempty(value):
                lines.append(f"  {label}: {_moment_value(value)}")

        add("content", post.get("content"))
        add("post_type", post.get("post_type"))
        add("format", post.get("format"))
        add("image_type", post.get("image_type"))
        add("photo_kind", post.get("photo_kind"))

        photo_schema = post.get("photo_schema") or {}
        for key in (
            "source_materials", "layout", "text_overlay", "decorations",
            "color_tone", "camera_logic", "feed_thumbnail_goal",
        ):
            val = photo_schema.get(key)
            if key == "text_overlay" and val == "none":
                val = ""
            add(f"photo_schema.{key}", val)

        selfie = post.get("selfie") or {}
        selfie_fields = {
            "selfie.variable.expression": ((selfie.get("variable") or {}).get("expression")),
            "selfie.variable.outfit": ((selfie.get("variable") or {}).get("outfit")),
            "selfie.variable.pose_gesture": ((selfie.get("variable") or {}).get("pose_gesture")),
            "selfie.shooting.capture_mode": ((selfie.get("shooting") or {}).get("capture_mode")),
            "selfie.shooting.filter": ((selfie.get("shooting") or {}).get("filter")),
            "selfie.shooting.shot_size": ((selfie.get("shooting") or {}).get("shot_size")),
            "selfie.shooting.angle": ((selfie.get("shooting") or {}).get("angle")),
            "selfie.shooting.framing": ((selfie.get("shooting") or {}).get("framing")),
            "selfie.scene.activity": ((selfie.get("scene") or {}).get("activity")),
            "selfie.scene.location": ((selfie.get("scene") or {}).get("location")),
            "selfie.scene.time_of_day": ((selfie.get("scene") or {}).get("time_of_day")),
        }
        for label, value in selfie_fields.items():
            add(label, value)

        add("photo_prompt", post.get("photo_prompt"))
        rows.append("\n".join(lines))
    return "\n\n".join(rows)


def _opening_text(persona: dict, lang: str | None = "zh") -> str:
    """Render opening (note + first messages) as a localized chat hook."""
    op = persona.get("opening")
    if not isinstance(op, dict):
        return ""
    labels = _msg(lang)
    lines = []
    note = _stringify(op.get("note"))
    if note:
        lines.append(f"{labels['opening_note']}: {note}")
    msgs = op.get("messages")
    if isinstance(msgs, list):
        texts = []
        for m in msgs:
            if isinstance(m, dict):
                data = m.get("data") or {}
                c = data.get("content") if isinstance(data, dict) else None
                c = c or m.get("content")
                if isinstance(c, str) and c.strip():
                    texts.append(c.strip())
            elif isinstance(m, str) and m.strip():
                texts.append(m.strip())
        if texts:
            lines.append(f"{labels['opening_msgs']}: " + " / ".join(texts[:6]))
    return "\n".join(lines)


def _cover_note_for_variant(v: dict, has_cover: bool, labels: dict) -> str:
    """按變體的封面承載方式給出封面說明。

    - cover_placeholder（如互動版 __IMG_BASE64__）：只作有/無宣告，勿改佔位符。
    - cover_slot（如 narrative_pro 用 oc-cover）：沿用通用槽位說明，渲染器注入。
    """
    if v.get("cover_placeholder"):
        ph = v["cover_placeholder"]
        return (f"cover: 有封面圖（原樣保留母版裡的封面佔位符 {ph}，"
                f"後處理會填成公網圖片 URL，勿自行生成 base64）"
                if has_cover else f"cover: 無封面圖（按母版佔位符 {ph} 處理）")
    # 預設走 oc-cover 槽位說明（與預設變體一致）。
    return labels["cover_yes"] if has_cover else labels["cover_no"]


def _build_self_contained_message(
    persona: dict, lang: str, has_cover: bool, labels: dict,
    name: str, profile: str, detail: str,
    request: str, style_text: str | None, current_html: str | None,
    variant_cfg: dict,
) -> str:
    """自包含變體的使用者訊息：輸入只有角色 json + 封面圖，
    不拼接 moments/帖子內容，也不追加品牌/語言/設計指令等額外內容。"""
    info = labels["char_info"] + "\n" + name + "\n"
    info += f"lang: {lang}\n"
    if profile:
        info += "profile: " + profile + "\n"
    if detail:
        info += detail + "\n"
    opening = _opening_text(persona, lang)
    if opening:
        info += "\n" + labels["opening_title"] + "\n" + opening + "\n"
    info += _cover_note_for_variant(variant_cfg, has_cover, labels) + "\n"
    parts = [info]

    command = (request or "").strip()
    if style_text:
        command = (
            command + "\n" if command else ""
        ) + labels["style_prefix"] + style_text
    if current_html and current_html.strip():
        html = current_html.strip()
        command += (labels["current_html"] + html
                    if len(html) < 6000 else labels["current_html_long"])
    if command:
        parts.append(labels["request"] + command)
    return "\n\n".join(parts)


def build_user_message(persona: dict, lang: str, has_cover: bool,
                       request: str = "", style_text: str | None = None,
                       current_html: str | None = None,
                       moments: list[dict] | None = None,
                       variant: str | None = None) -> str:
    """Assemble the structured user turn (character info + directive + request)."""
    labels = _msg(lang)
    name = _stringify(persona.get("name")) or labels["unnamed"]
    profile = _stringify(persona.get("profile"))
    detail = persona_to_profile_text(persona)

    vcfg = _variant(variant)
    if vcfg.get("SYSTEM_PROMPT"):
        return _build_self_contained_message(
            persona, lang, has_cover, labels, name, profile, detail,
            request, style_text, current_html, vcfg,
        )

    parts = []
    info = labels["char_info"] + "\n" + name + "\n"
    if profile:
        info += "profile: " + profile + "\n"
    if detail:
        info += detail + "\n"
    opening = _opening_text(persona, lang)
    if opening:
        info += "\n" + labels["opening_title"] + "\n" + opening + "\n"
    info += (labels["cover_yes"] if has_cover else labels["cover_no"]) + "\n"
    parts.append(info)

    moment_text = moments_to_profile_text(moments)
    if moment_text:
        parts.append(
            "# 角色最近動態 moments（請優先按這些真實資料展示，不要另編空泛動態）：\n"
            + moment_text
        )
        parts.append(
            "# moments 展示規則：如果頁面包含 Stories / 限時動態 / recent moments 模組，必須展示每條動態的非空結構化欄位；"
            "不要只顯示 photo_kind + color_tone。photo/composite 至少展示素材、版式、畫面文字、裝飾、色調、手機來源、縮圖重點中有值的欄位；"
            "selfie 至少展示拍攝方式、鏡頭/裁切、角度、濾鏡質感、地點、動作、穿搭、表情中有值的欄位。"
            "空欄位不要渲染，不要寫 null/none/空物件。"
        )

    parts.append(labels["page_lang"].format(
        name=config.lang_name(lang), directive=config.lang_directive(lang)
    ))
    if labels.get("brand_rule"):
        parts.append(labels["brand_rule"])

    command = (request or "").strip()
    if style_text:
        command = (
            command + "\n" if command else ""
        ) + labels["style_prefix"] + style_text
    if current_html and current_html.strip():
        html = current_html.strip()
        if len(html) < 6000:
            command += labels["current_html"] + html
        else:
            command += labels["current_html_long"]

    if not current_html and not re.search(labels["design_keywords"], command, flags=re.I):
        default_directive = _default_design_directive(lang)
        command = (
            default_directive + "\n\n" + command
            if command else default_directive
        )

    command += labels["output_lang"]
    parts.append(labels["request"] + (command or labels["default_request"]))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Output cleanup + cover injection
# --------------------------------------------------------------------------
def clean_html(out: str) -> str:
    """Strip markdown fences / leading prose from the model output."""
    s = out.strip()
    s = re.sub(r"^```(?:html)?", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"```$", "", s).strip()
    m = re.search(r"<\w", s)
    if m and m.start() > 0:
        # keep from first tag if there's leading prose, but only if it looks safe
        head = s[: m.start()]
        if "<" not in head:
            s = s[m.start():]
    return s


# 落地頁裡如果漏出佔位符 {user}/{{user}}（來自 persona 欄位或 prompt 模板），
# 按頁面語言替換成對應的第二人稱「你」。
_USER_YOU = {
    "zh": "你",
    "zh-CN": "你",
    "zh-TW": "你",
    "zh-Hant": "你",
    "ja": "あなた",
    "ko": "당신",
    "en": "you",
}
# 相容大小寫 / 花括號數量 / 前後空格：{user}、{{user}}、{ User } 等一併命中。
_USER_TOKEN_RE = re.compile(r"\{{1,2}\s*user\s*\}{1,2}", re.IGNORECASE)


def replace_user_placeholder(html: str, lang: str | None) -> str:
    """把落地頁文案裡殘留的 {user}/{{user}} 佔位符換成本語種的「你」。"""
    if not html or "user" not in html.lower():
        return html
    you = _USER_YOU.get(lang or "") or _USER_YOU["zh"]
    return _USER_TOKEN_RE.sub(you, html)


def _set_img_src(tag: str, url: str) -> str:
    """Set an <img> tag's src to url. Handles three cases:
    already has a non-empty src (leave it), has an empty src="" (fill it),
    or has no src at all (append one)."""
    if re.search(r'\bsrc\s*=\s*["\'][^"\']+["\']', tag):
        return tag  # already points somewhere real
    empty = re.search(r'\bsrc\s*=\s*["\']["\']', tag)
    if empty:
        return tag[:empty.start()] + f'src="{url}"' + tag[empty.end():]
    return tag[:-1] + f' src="{url}">'


def inject_cover(html: str, cover_url: str | None) -> str:
    """Best-effort inject the cover image into oc-cover / oc-img-1 slots so a
    saved/standalone page (and the live preview) shows the portrait.

    Handles both template forms the model emits:
      1. <img class="oc-cover"> with empty src  -> fill src
      2. <div class="oc-cover"></div> shown via CSS background -> fill
         background-image (this is the common case and was previously missed).
    """
    if not cover_url:
        return html
    url = cover_url

    # 互動卡片版母版用佔位符承載封面（<img src>）。新版佔位符是 __IMG_URL__
    # （填公網/相對 URL，不內聯 base64）；老頁面裡仍是 __IMG_BASE64__，一併相容。
    for placeholder in ("__IMG_URL__", "__IMG_BASE64__"):
        if placeholder in html:
            html = html.replace(placeholder, url)

    def _img_src(m):
        return _set_img_src(m.group(0), url)

    html = re.sub(
        r'<img\b[^>]*\bclass=["\'][^"\']*oc-(?:cover|img-1)(?![\w-])[^"\']*["\'][^>]*>',
        _img_src,
        html,
    )

    def _div_bg(m):
        tag = m.group(0)
        if "background-image" in tag.lower():
            return tag
        style = f"background-image:url('{url}');"
        sm = re.search(r'style\s*=\s*"([^"]*)"', tag)
        if sm:
            return tag[:sm.start(1)] + sm.group(1) + ";" + style + tag[sm.end(1):]
        return tag[:-1] + f' style="{style}">'

    html = re.sub(
        r'<div\b[^>]*\bclass=["\'][^"\']*oc-(?:cover|img-1)(?![\w-])[^"\']*["\'][^>]*>',
        _div_bg,
        html,
    )
    return html


def inject_post_images(html: str, post_urls: list[str] | None) -> str:
    """Fill oc-post-N slots (1-based) with each post image URL.

    Mirrors inject_cover: handles both <img class="oc-post-N"> (fill src) and
    <div class="oc-post-N"> shown via CSS background-image. URLs may be public
    /img/ paths (live preview) or data URIs (standalone export).
    """
    if not post_urls:
        return html

    for idx, url in enumerate(post_urls, start=1):
        if not url:
            continue
        cls = f"oc-post-{idx}"

        def _img_src(m):
            return _set_img_src(m.group(0), url)

        html = re.sub(
            rf'<img\b[^>]*\bclass=["\'][^"\']*{cls}(?![\w-])[^"\']*["\'][^>]*>',
            _img_src, html,
        )

        def _div_bg(m):
            tag = m.group(0)
            if "background-image" in tag.lower():
                return tag
            style = f"background-image:url('{url}');"
            sm = re.search(r'style\s*=\s*"([^"]*)"', tag)
            if sm:
                return tag[:sm.start(1)] + sm.group(1) + ";" + style + tag[sm.end(1):]
            return tag[:-1] + f' style="{style}">'

        html = re.sub(
            rf'<div\b[^>]*\bclass=["\'][^"\']*{cls}(?![\w-])[^"\']*["\'][^>]*>',
            _div_bg, html,
        )
    return html


# 站內相對資源字首（img/上傳/縮圖），改寫成絕對 URL 時匹配這些根路徑。
_REL_ASSET_PREFIXES = ("/img/", "/upload/", "/thumbs/")


def absolutize_urls(html: str, base_url: str | None) -> str:
    """把落地頁裡 src/url() 引用的站內相對資源改寫成帶域名的絕對 URL。

    只改以 /img/、/upload/、/thumbs/ 開頭的相對路徑（站內靜態資源），
    形如 src="/img/x.png" / url('/img/x.png') → base_url + 路徑。
    已是 http(s)/data: 的絕對地址、以及其它相對路徑一律不動。base_url
    為空時原樣返回（保持相對路徑的歷史行為）。"""
    if not base_url or not html:
        return html
    base = base_url.rstrip("/")

    def _abs(m: "re.Match") -> str:
        quote, path = m.group("q"), m.group("p")
        return f'{m.group("attr")}={quote}{base}{path}{quote}'

    prefix_alt = "|".join(re.escape(p) for p in _REL_ASSET_PREFIXES)
    # src="/img/..." 或 href="/img/..."（引號內以受支援字首開頭的相對路徑）
    html = re.sub(
        rf'(?P<attr>\b(?:src|href))\s*=\s*(?P<q>["\'])(?P<p>(?:{prefix_alt})[^"\']*)(?P=q)',
        _abs, html,
    )

    def _abs_css(m: "re.Match") -> str:
        quote, path = m.group("q") or "", m.group("p")
        return f'url({quote}{base}{path}{quote})'

    # CSS url(/img/...) —— 引號可有可無
    html = re.sub(
        rf'url\(\s*(?P<q>["\']?)(?P<p>(?:{prefix_alt})[^"\')]*)(?P=q)\s*\)',
        _abs_css, html,
    )
    return html
