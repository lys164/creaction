"""Landing-page (角色主页/展示页) generation.

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

_LANG_TO_PROMPT_LANG = {
    "zh": "zh-CN",
    "zh-CN": "zh-CN",
    "zh-TW": "zh-TW",
    "ja": "ja",
    "ko": "ko",
    "en": "en",
}
_DEFAULT_LOCALE_BY_LANG = {
    "zh": "CN",
    "zh-CN": "CN",
    "zh-TW": "TW",
    "ja": "JP",
    "ko": "KR",
    "en": "US",
}
_LOCALE_FIELDS = (
    "im", "social", "id_docs", "photo", "fonts",
    "idol", "money", "aesthetic", "manners",
)

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


def _locale_code(lang: str | None, locale: str | None = None) -> str:
    locale_map = _PROMPTS.get("LOCALE_MAP") or {}
    code = (locale or "").strip().upper()
    if code in locale_map:
        return code
    return _DEFAULT_LOCALE_BY_LANG.get(lang or "", "CN")


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


def _locale_directive(lang: str | None, locale: str | None = None) -> str:
    locale_map = _PROMPTS.get("LOCALE_MAP") or {}
    locale_dirs = _PROMPTS.get("LOCALE_DIR_I18N") or {}
    if not locale_map or not locale_dirs:
        return ""

    prompt_lang = _prompt_lang(lang)
    loc = locale_map.get(_locale_code(lang, locale)) or locale_map.get("CN")
    directive = locale_dirs.get(prompt_lang) or locale_dirs.get("zh-CN")
    if not loc or not directive:
        return ""

    sep = directive.get("sep", ": ")
    out = [
        "---",
        "",
        directive.get("title", "## Cultural localization"),
        "",
        directive.get("intro", ""),
        "",
        f"**{directive.get('targetLabel', 'Target region')}{sep}"
        f"{loc.get('label') or loc.get('name') or ''}**",
    ]
    bullets = directive.get("bullets") or []
    values = [loc.get(field, "") for field in _LOCALE_FIELDS]
    for idx, label in enumerate(bullets):
        if idx < len(values) and values[idx]:
            out.append(f"- {label}{sep}{values[idx]}")
    if directive.get("eg"):
        out.extend(["", directive["eg"]])
    return "\n\n" + "\n".join(out)


def build_system_prompt(
    style_text: str | None,
    lang: str | None = "zh",
    locale: str | None = None,
) -> str:
    """Inject style + language/locale prompt pack into the system prompt."""
    pack = _prompt_pack(lang)
    template = pack.get("SP_TEMPLATE") or SP_TEMPLATE
    return template.replace("{{style}}", _style_content(style_text, pack)) + _locale_directive(
        lang, locale
    )


def _default_design_directive(lang: str | None) -> str:
    return _prompt_pack(lang).get("DEFAULT_DESIGN_DIRECTIVE") or DEFAULT_DESIGN_DIRECTIVE


# --------------------------------------------------------------------------
# Persona record -> flat "profile" text the system prompt expects
# --------------------------------------------------------------------------
_MSG_I18N = {
    "zh": {
        "unnamed": "(未命名角色)",
        "char_info": "# 角色的信息：",
        "opening_title": "# TA 的开场白与关系钩子（用作页面「勾你来聊天」的素材，提炼成 TA 向你自我呈现/邀请的语气，不要原样照搬整段）：",
        "opening_note": "开场情境 note",
        "opening_msgs": "TA 主动对你说的第一句话们",
        "cover_yes": 'cover: 角色有封面图（渲染器会自动注入到 class="oc-cover" 的元素中，你只需留好槽位，src 留空）',
        "cover_no": "cover: 无封面图（请用 CSS 渐变/纹理生成抽象视觉占位）",
        "page_lang": "# 页面文案语言：请用 {name} 撰写页面上所有可见文案。{directive}",
        "style_prefix": "风格：",
        "current_html": "\n\n----\n当前 HTML（在此基础上修改）：\n",
        "current_html_long": "\n\n----\n当前页面已生成（代码较长不重复附上）。请在现有结构基础上修改，保持整体风格一致。",
        "request": "# 用户要求：",
        "default_request": "请根据角色信息生成主页",
        "output_lang": "\n\n⚠️ 页面上所有可见文字（标题、正文、标签、装饰文案等）一律使用简体中文。",
        "brand_rule": "⚠️ 品牌规则：角色发帖/聊天所在的平台一律称「Popop」，页面文案不得出现 Instagram/ins/Threads/小红书/推特 等真实社交平台名。",
        "design_keywords": r"交互|互动|滑动|布局|美观|组件|模块|元件",
    },
    "ja": {
        "unnamed": "(名前未設定)",
        "char_info": "# キャラクター情報：",
        "opening_title": "# 最初のセリフと関係性のフック（ページで『話してみたい』と思わせる素材。全文をそのまま写さず、自己提示／招待の口調に要約）：",
        "opening_note": "導入シチュエーション note",
        "opening_msgs": "キャラクターから最初に送られる言葉",
        "cover_yes": 'cover: カバー画像あり（レンダラーが class="oc-cover" の要素に自動注入するので、src は空のままスロットだけ用意する）',
        "cover_no": "cover: カバー画像なし（CSS グラデーション／テクスチャで抽象的なビジュアルを作る）",
        "page_lang": "# ページ文言の言語：ページ上の可視テキストはすべて {name} で書くこと。{directive}",
        "style_prefix": "スタイル：",
        "current_html": "\n\n----\n現在の HTML（これをベースに修正）：\n",
        "current_html_long": "\n\n----\nページは生成済み（コードが長いため再添付しません）。既存の構造をベースに修正し、全体のスタイルを一貫させてください。",
        "request": "# ユーザーの要望：",
        "default_request": "キャラクター情報をもとにホームページを生成してください",
        "output_lang": "\n\n⚠️ ページ上のすべての可視テキスト（見出し・本文・ラベル・装飾コピーなど）は必ず日本語で書いてください。",
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


_FIELD_LABELS = {
    "profile": "profile",
    "tags": "tags",
    "species": "species",
    "gender": "gender",
    "personality": "personality",
    "hometown": "hometown",
    "residence": "residence",
    "social_status": "social_status",
    "speech_style": "speech_style",
    "relationship_with_user": "relationship_with_user",
    "relationship_mode": "relationship_mode",
    "love_style": "love_style",
    "situational_reactions": "situational_reactions",
    "hidden_side": "hidden_side",
    "life_details": "life_details",
    "likes": "likes",
    "fears": "fears",
    "wishlist": "wishlist",
    "backstory": "backstory",
    "family": "family",
    "social_network": "social_network",
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
                # backstory {stage,detail} / family|social {name,relation,info,dynamic}
                if it.get("stage") or it.get("detail"):
                    head = it.get("stage", "")
                    parts.append(f"{head}：{it.get('detail', '')}".strip("："))
                elif it.get("content") and not it.get("relation"):
                    parts.append(it.get("content", ""))
                else:
                    head = " · ".join(
                        x for x in (it.get("name"), it.get("relation")) if x
                    )
                    tail = "；".join(
                        x for x in (it.get("info"), it.get("dynamic")) if x
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


def build_user_message(persona: dict, lang: str, has_cover: bool,
                       request: str = "", style_text: str | None = None,
                       current_html: str | None = None,
                       moments: list[dict] | None = None) -> str:
    """Assemble the structured user turn (character info + directive + request)."""
    labels = _msg(lang)
    name = _stringify(persona.get("name")) or labels["unnamed"]
    profile = _stringify(persona.get("profile"))
    detail = persona_to_profile_text(persona)

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
            "# 角色最近动态 moments（请优先按这些真实数据展示，不要另编空泛动态）：\n"
            + moment_text
        )
        parts.append(
            "# moments 展示规则：如果页面包含 Stories / 限时动态 / recent moments 模块，必须展示每条动态的非空结构化字段；"
            "不要只显示 photo_kind + color_tone。photo/composite 至少展示素材、版式、画面文字、装饰、色调、手机来源、缩略图重点中有值的字段；"
            "selfie 至少展示拍摄方式、镜头/裁切、角度、滤镜质感、地点、动作、穿搭、表情中有值的字段。"
            "空字段不要渲染，不要写 null/none/空对象。"
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
