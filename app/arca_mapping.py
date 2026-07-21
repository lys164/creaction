"""本地 record → arca 請求體的純對映（無副作用、無 IO，便於單測）。"""
import json

_MALE = ("男", "male", "남")
_FEMALE = ("女", "female", "여")

# 已被直接對映的 persona 鍵，不再重複塞進 customized_settings
# （inner_structure 併入 disposition，見 persona_to_character_form）
_MAPPED_KEYS = {"name", "profile", "gender", "species", "personality", "opening",
                "voice", "tags", "visibility", "anonymous_identities",
                "inner_structure"}

# persona 英文 schema 鍵 → 平臺 setting_options 的 tag_key（簡體中文，跨語言穩定主鍵）。
# customized_settings 的最終 key 會再經 page_config 換成角色語言的 tag_name。
# 新舊 schema 鍵並存：identity/dislikes/worldview 是新鍵，social_status/fears/premise/
# family 相容存量舊資料；同一角色只會帶其中一套，不會撞 tag_key。
_SETTING_KEY_MAP = {
    "hometown": "出生地",
    "residence": "居住地",
    "identity": "職業",
    "social_status": "職業",
    "appearance": "外貌",
    "speech_style": "語言習慣",
    "relationship_mode": "社交模式",
    "love_style": "表達愛的方式",
    "life_details": "生活習慣",
    "likes": "愛好",
    "dislikes": "討厭的東西",
    "fears": "討厭的東西",
    "backstory": "成長經歷",
    "family": "家庭成員",
    "social_network": "社交關係",
    "worldview": "特殊背景/世界觀",
    "premise": "特殊背景/世界觀",
    "wishlist": "願望清單",
}


def _tag_lookup(items: list | None) -> dict:
    """由 page_config 的列舉列表建反查表：tag_name→tag_key 且 tag_key→tag_key。"""
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
    """把任意 persona 值拼成自然語言文字（customized_settings 的 value 不允許 JSON）。

    - 字串原樣；陣列每項一行；物件拼成「key: value」用「；」相連（巢狀遞迴）。
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
    """把一條 opening message 歸一為 arca OpeningPrologueData。

    creaction 的 opening.messages 元素形如 {"type":"text|voice","data":{"content":"..."}}，
    偶爾是純字串。取真實文案（而非序列化整個物件），voice → output_type=tts。
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
                              page_config: dict | None = None,
                              lang: str | None = None) -> dict:
    """persona → CharacterCreateForm。

    page_config 為 GET /character/page_config 的 data（可選）：用於把 tags/species
    歸一成平臺 tag_key（跨語言主鍵），並把 customized_settings 的 key 換成
    平臺 setting_options 在該語言下的 tag_name。傳 None 則跳過對齊、原樣透傳。
    lang 為角色語言（zh/ja/ko/en），決定 disposition 拼接用哪套連線詞；預設 zh。
    """
    persona = persona or {}
    page_config = page_config or {}
    # 表單欄位的取值一律用 page_config 的 tag_key（平臺主鍵）；
    # 未傳 page_config（降級模式）時寬鬆透傳，不誤傷。
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
            # 嚴格模式：species 必須是平臺 tag_key；非列舉物種歸「其他」
            # （物種細節在 appearance/正文裡，不丟資訊）
            form["species"] = species_lookup.get(
                sp, "其他" if "其他" in species_lookup.values() else sp)
        else:
            form["species"] = sp
    pers = persona.get("personality")
    if isinstance(pers, dict):
        # 舊 schema：personality 按角色語言+性別拼成自然語言段落（healing 有意丟棄）。
        # 函式內延遲 import：persona_export 頂層引用本模組的 normalize_gender。
        from .persona_export import build_personality_text
        text = build_personality_text(
            pers, lang or "zh", normalize_gender(persona.get("gender") or ""))
        if text:
            form["disposition"] = text
    elif isinstance(pers, str) and pers:
        form["disposition"] = pers
    # 新 schema 的內在結構併入 disposition：舊 schema 拼出的 disposition 本就含
    # 慾望/底線這層內在資訊，新 schema 靠 inner_structure 補齊等價資訊量。
    inner = persona.get("inner_structure")
    if isinstance(inner, str) and inner.strip():
        form["disposition"] = (form.get("disposition", "") + "\n" + inner.strip()).strip()
    # arca 建角色硬校驗 voice_id 非空(「請選擇音色」)；persona.voice 與 arca 音色表同源
    if persona.get("voice"):
        form["voice_id"] = str(persona["voice"])
    # tags 是 CharacterCreateForm 的一等欄位([]string)。取值一律用平臺 tag_key；
    # 嚴格模式下平臺詞表沒有的詞直接丟棄（平臺本就不識別），降級模式透傳。
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
    # visibility 一等欄位，列舉 public|private（非法值會被 go-zero 拒 400，只透傳合法值）
    visibility = (persona.get("visibility") or "").strip().lower()
    if visibility in ("public", "private"):
        form["visibility"] = visibility
    # persona.anonymous_identities → 表單 anonymous_tags（匿名身份標籤）
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

    # 兜底：其餘欄位（appearance/backstory/family/...）進 customized_settings。
    # 新 IDL 為 []GeneralTagInfo（{tag_key, tag_name, tag_icon, tag_value, index}），
    # 後端只消費 tag_key+tag_value，且 tag_key 會被 characterPageSettingKeySet
    # 強校驗——非平臺設定項的 key 會導致整個 create 被拒，因此平臺沒有對應
    # setting 的 persona 欄位只能丟棄（不能像舊 map 一樣保留原英文鍵）。
    # value 一律拼成自然語言文字（不允許 JSON 串）；personality 已拼進 disposition。
    setting_names = {t.get("tag_key"): (t.get("tag_name") or t.get("tag_key"))
                     for t in page_config.get("setting_options") or []
                     if t.get("tag_key")}
    settings: list[dict] = []
    for k, v in persona.items():
        if k in _MAPPED_KEYS or v in (None, "", [], {}):
            continue
        tag_key = _SETTING_KEY_MAP.get(k)
        if not tag_key:
            continue  # 平臺無對應設定項，帶上會被後端整單拒絕
        if setting_names and tag_key not in setting_names:
            continue  # page_config 可用時二次校驗，防靜態表與平臺漂移
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
