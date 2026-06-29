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

SP_TEMPLATE: str = _PROMPTS["SP_TEMPLATE"]
STYLE_MAP: dict = _PROMPTS["STYLE_MAP"]
DEFAULT_DESIGN_DIRECTIVE: str = _PROMPTS["DEFAULT_DESIGN_DIRECTIVE"]
FALLBACK: str = _PROMPTS["FALLBACK"]


def landing_styles() -> list[str]:
    """Preset landing-page style names (free text also accepted)."""
    return list(STYLE_MAP.keys())


def build_system_prompt(style_text: str | None) -> str:
    """Inject the chosen style (preset description or free text) into the template."""
    style = (style_text or "").strip()
    style_content = STYLE_MAP.get(style) or style or FALLBACK
    return SP_TEMPLATE.replace("{{style}}", style_content)


# --------------------------------------------------------------------------
# Persona record -> flat "profile" text the system prompt expects
# --------------------------------------------------------------------------
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


def _opening_text(persona: dict) -> str:
    """Render the persona's opening (note + first messages) as a chat hook so the
    landing page can speak in the character's own voice and invite a conversation."""
    op = persona.get("opening")
    if not isinstance(op, dict):
        return ""
    lines = []
    note = _stringify(op.get("note"))
    if note:
        lines.append(f"开场情境 note: {note}")
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
            lines.append("TA 主动对你说的第一句话们: " + " / ".join(texts[:6]))
    return "\n".join(lines)


def build_user_message(persona: dict, lang: str, has_cover: bool,
                       request: str = "", style_text: str | None = None,
                       current_html: str | None = None,
                       moments: list[dict] | None = None,
                       post_images: list[dict] | None = None) -> str:
    """Assemble the structured user turn (character info + directive + request)."""
    name = _stringify(persona.get("name")) or "(未命名角色)"
    profile = _stringify(persona.get("profile"))
    detail = persona_to_profile_text(persona)

    parts = []
    info = "# 角色的信息：\n" + name + "\n"
    if profile:
        info += "profile: " + profile + "\n"
    if detail:
        info += detail + "\n"
    opening = _opening_text(persona)
    if opening:
        info += (
            "\n# TA 的开场白与关系钩子（用作页面「勾你来聊天」的素材，"
            "提炼成 TA 向你自我呈现/邀请的语气，不要原样照搬整段）：\n" + opening + "\n"
        )
    if has_cover:
        info += (
            "cover: 角色有封面图（渲染器会自动注入到 class=\"oc-cover\" 的元素中，"
            "你只需留好槽位，src 留空）\n"
        )
    else:
        info += "cover: 无封面图（请用 CSS 渐变/纹理生成抽象视觉占位）\n"
    if post_images:
        n = len(post_images)
        lines = [
            f"\n# TA 的 {n} 张真实帖子照片（已作为图片发给你，可见）：这些是 TA 社交动态里"
            f"真实拍的照片，**强烈建议**做成相册 / 九宫格 / 动态流 / 拼贴等模块真实展示出来，"
            f"让页面更有「TA 的生活」实感。",
            "槽位规则：第 i 张帖子图请放进 class=\"oc-post-i\" 的元素（i 从 1 开始），"
            "用法同封面——`<img class=\"oc-post-1\" src=\"\" alt=\"...\">` 留空 src，"
            "或 `<div class=\"oc-post-1\" style=\"background-size:cover;background-position:center;"
            "width:...;height:...\"></div>`，渲染器会自动注入真实图片。容器务必设明确宽高。",
            "各帖子图对应的动态内容（供你判断如何编排，不必原样写进页面）：",
        ]
        for i, pi in enumerate(post_images, start=1):
            cap = (pi.get("caption") or "").strip().replace("\n", " ")
            lines.append(f"  oc-post-{i}: {cap[:60]}" if cap else f"  oc-post-{i}: （无文字）")
        info += "\n".join(lines) + "\n"
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

    # locale hint so on-page copy reads native to the character's language
    parts.append(
        f"# 页面文案语言：请用 {config.lang_name(lang)} 撰写页面上所有可见文案。"
        f"{config.lang_directive(lang)}"
    )

    command = (request or "").strip()
    if style_text:
        command = (command + "\n" if command else "") + "风格：" + style_text
    if current_html and current_html.strip():
        html = current_html.strip()
        if len(html) < 6000:
            command += "\n\n----\n当前 HTML（在此基础上修改）：\n" + html
        else:
            command += (
                "\n\n----\n当前页面已生成（代码较长不重复附上）。"
                "请在现有结构基础上修改，保持整体风格一致。"
            )

    # first-pass auto design directive (only when user didn't already steer)
    if not current_html:
        if not re.search(r"交互|互动|滑动|布局|美观|组件", command):
            command = (
                DEFAULT_DESIGN_DIRECTIVE + "\n\n" + command
                if command else DEFAULT_DESIGN_DIRECTIVE
            )

    parts.append("# 用户要求：" + (command or "请根据角色信息生成主页"))
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
        tag = m.group(0)
        if re.search(r'\bsrc\s*=\s*["\'][^"\']+["\']', tag):
            return tag
        return tag[:-1] + f' src="{url}">'

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
            tag = m.group(0)
            if re.search(r'\bsrc\s*=\s*["\'][^"\']+["\']', tag):
                return tag
            return tag[:-1] + f' src="{url}">'

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
