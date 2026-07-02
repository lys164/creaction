"""本地 record → arca 请求体的纯映射（无副作用、无 IO，便于单测）。"""
import json

_MALE = ("男", "male", "남")
_FEMALE = ("女", "female", "여")

# 已被直接映射的 persona 键，不再重复塞进 customized_settings
_MAPPED_KEYS = {"name", "profile", "gender", "species", "personality", "opening",
                "voice", "tags", "visibility", "anonymous_identities"}

# persona 英文 schema 键 → 平台 setting_options 的 tag_key（简体中文，跨语言稳定主键）。
# customized_settings 的最终 key 会再经 page_config 换成角色语言的 tag_name。
_SETTING_KEY_MAP = {
    "hometown": "出生地",
    "residence": "居住地",
    "social_status": "职业",
    "appearance": "外貌",
    "speech_style": "语言习惯",
    "relationship_mode": "社交模式",
    "love_style": "表达爱的方式",
    "life_details": "生活习惯",
    "likes": "爱好",
    "fears": "讨厌的东西",
    "backstory": "成长经历",
    "family": "家庭成员",
    "social_network": "社交关系",
    "premise": "特殊背景/世界观",
    "wishlist": "愿望清单",
}


def _tag_lookup(items: list | None) -> dict:
    """由 page_config 的枚举列表建反查表：tag_name→tag_key 且 tag_key→tag_key。"""
    lookup: dict[str, str] = {}
    for t in items or []:
        key = (t or {}).get("tag_key") or ""
        name = (t or {}).get("tag_name") or ""
        if key:
            lookup[key] = key
            if name:
                lookup[name] = key
    return lookup


def normalize_gender(value: str) -> str:
    v = (value or "").strip().lower()
    if any(t in v for t in (t.lower() for t in _FEMALE)):
        return "female"
    if any(t in v for t in (t.lower() for t in _MALE)):
        return "male"
    return "other"


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _naturalize(value) -> str:
    """把任意 persona 值拼成自然语言文本（customized_settings 的 value 不允许 JSON）。

    - 字符串原样；数组每项一行；对象拼成「key: value」用「；」相连（嵌套递归）。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [p for p in (_naturalize(v) for v in value) if p]
        return "\n".join(parts)
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            nv = _naturalize(v)
            if nv:
                parts.append(f"{k}: {nv}")
        return "；".join(parts)
    return "" if value is None else str(value)


def _opening_prologue_item(msg) -> dict | None:
    """把一条 opening message 归一为 arca OpeningPrologueData。

    creaction 的 opening.messages 元素形如 {"type":"text|voice","data":{"content":"..."}}，
    偶尔是纯字符串。取真实文案（而非序列化整个对象），voice → output_type=tts。
    """
    if isinstance(msg, str):
        text = msg.strip()
        return {"text": text, "output_type": "text"} if text else None
    if isinstance(msg, dict):
        data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
        content = data.get("content") or msg.get("content") or ""
        text = content.strip() if isinstance(content, str) else _stringify(content)
        if not text:
            return None
        output_type = "tts" if msg.get("type") == "voice" else "text"
        return {"text": text, "output_type": output_type}
    return None


def persona_to_character_form(persona: dict,
                              images: list[dict] | None = None,
                              landing_url: str | None = None,
                              page_config: dict | None = None) -> dict:
    """persona → CharacterCreateForm。

    page_config 为 GET /character/page_config 的 data（可选）：用于把 tags/species
    归一成平台 tag_key（跨语言主键），并把 customized_settings 的 key 换成
    平台 setting_options 在该语言下的 tag_name。传 None 则跳过对齐、原样透传。
    """
    persona = persona or {}
    page_config = page_config or {}
    # 表单字段的取值一律用 page_config 的 tag_key（平台主键）；
    # 未传 page_config（降级模式）时宽松透传，不误伤。
    strict = bool(page_config)
    tag_lookup = _tag_lookup(page_config.get("character_tags"))
    species_lookup = _tag_lookup(page_config.get("species"))

    form: dict = {}
    if persona.get("name"):
        form["name"] = persona["name"]
    if persona.get("profile"):
        form["profile"] = persona["profile"]
    if persona.get("gender"):
        form["gender"] = normalize_gender(persona["gender"])
    if persona.get("species"):
        sp = _naturalize(persona["species"])
        if strict:
            # 严格模式：species 必须是平台 tag_key；非枚举物种归「其他」
            # （物种细节在 appearance/正文里，不丢信息）
            form["species"] = species_lookup.get(
                sp, "其他" if "其他" in species_lookup.values() else sp)
        else:
            form["species"] = sp
    pers = persona.get("personality")
    if isinstance(pers, dict):
        # personality 的全部 value 依序拼接进 disposition（summary/decisive_event/…）
        parts = [_naturalize(v) for v in pers.values()
                 if v not in (None, "", [], {})]
        if parts:
            form["disposition"] = "\n".join(p for p in parts if p)
    elif isinstance(pers, str) and pers:
        form["disposition"] = pers
    # arca 建角色硬校验 voice_id 非空(「请选择音色」)；persona.voice 与 arca 音色表同源
    if persona.get("voice"):
        form["voice_id"] = str(persona["voice"])
    # tags 是 CharacterCreateForm 的一等字段([]string)。取值一律用平台 tag_key；
    # 严格模式下平台词表没有的词直接丢弃（平台本就不识别），降级模式透传。
    tags = persona.get("tags")
    if isinstance(tags, str) and tags:
        tags = [tags]
    if isinstance(tags, list):
        normalized = []
        for t in tags:
            if not t:
                continue
            t = str(t)
            key = tag_lookup.get(t) if strict else tag_lookup.get(t, t)
            if key and key not in normalized:
                normalized.append(key)
        if normalized:
            form["tags"] = normalized
    # visibility 一等字段，枚举 public|private（非法值会被 go-zero 拒 400，只透传合法值）
    visibility = (persona.get("visibility") or "").strip().lower()
    if visibility in ("public", "private"):
        form["visibility"] = visibility
    # persona.anonymous_identities → 表单 anonymous_tags（匿名身份标签）
    anon = persona.get("anonymous_identities")
    if isinstance(anon, str) and anon:
        anon = [anon]
    if isinstance(anon, list):
        anon = [str(a) for a in anon if a]
        if anon:
            form["anonymous_tags"] = anon

    opening = persona.get("opening") or {}
    msgs = opening.get("messages") if isinstance(opening, dict) else None
    if msgs:
        prologue = [it for it in (_opening_prologue_item(m) for m in msgs) if it]
        if prologue:
            form["opening_prologue"] = prologue

    # 兜底：其余字段（appearance/backstory/family/...）进 customized_settings。
    # 新 IDL 为 []GeneralTagInfo（{tag_key, tag_name, tag_icon, tag_value, index}），
    # 后端只消费 tag_key+tag_value，且 tag_key 会被 characterPageSettingKeySet
    # 强校验——非平台设定项的 key 会导致整个 create 被拒，因此平台没有对应
    # setting 的 persona 字段只能丢弃（不能像旧 map 一样保留原英文键）。
    # value 一律拼成自然语言文本（不允许 JSON 串）；personality 已拼进 disposition。
    setting_names = {t.get("tag_key"): (t.get("tag_name") or t.get("tag_key"))
                     for t in page_config.get("setting_options") or []
                     if t.get("tag_key")}
    settings: list[dict] = []
    for k, v in persona.items():
        if k in _MAPPED_KEYS or v in (None, "", [], {}):
            continue
        tag_key = _SETTING_KEY_MAP.get(k)
        if not tag_key:
            continue  # 平台无对应设定项，带上会被后端整单拒绝
        if setting_names and tag_key not in setting_names:
            continue  # page_config 可用时二次校验，防静态表与平台漂移
        text = _naturalize(v)
        if not text:
            continue
        settings.append({
            "tag_key": tag_key,
            "tag_name": setting_names.get(tag_key, tag_key),
            "tag_icon": "",
            "tag_value": text,
            "index": len(settings),
        })
    if settings:
        form["customized_settings"] = settings

    if images:
        form["images"] = images
    if landing_url:
        form["landing_page_url"] = landing_url
    return form
