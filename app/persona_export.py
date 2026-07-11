"""persona 旧 schema → 交付新 schema 的纯转换（无副作用、无 IO，便于单测）。

存量数据与生成/聊天/落地页链路保持旧 schema 不动，只在两条出口处转换：
  1. /api/characters/export 的 character.json（to_export_schema）
  2. arca 建角色的 disposition 拼接（build_personality_text，由 arca_mapping 调用）

变换内容：
  - personality dict → 按角色语言+性别拼成一段自然语言（healing 有意丢弃）
  - inner_structure = perception + hidden_side
  - social_network = family + social_network（同构列表直接相接）
  - 改名 social_status→identity / situational_reactions→behavior_patterns /
    fears→dislike / premise→worldview
"""
from __future__ import annotations

from .arca_mapping import normalize_gender

_RENAMES = {
    "social_status": "identity",
    "situational_reactions": "behavior_patterns",
    "fears": "dislikes",
    "premise": "worldview",
}

# 句末标点：拼接时先剥掉再按语言补，避免出现「。，」这类接缝
_END_PUNCT = "。．.!！?？;；"

# 每语言的连接词 + 代词（female / male / other）。
# 拼接模板（以 zh 女性为例）：
#   {summary}{decisive_event}结果，{response}因此她{cost}
#   她表面上想要{desire_outer}，实际上想要{desire_inner}。她的底线是{desire_bottom_line}。
# ko/en 的 desire 段不把代词粘进从句（韩语助词/英语句式对任意插值不稳），改用
# 「겉으로 원하는 것은…」「What she appears to want:」这类对句式免疫的写法。
_L10N = {
    "zh": {
        "sep": "",
        "end": "。",
        "pron": {"female": "她", "male": "他", "other": "TA"},
        "response": "结果，{v}",
        "cost": "因此{p}{v}",
        "desire_pair": "{p}表面上想要{o}，实际上想要{i}",
        "desire_outer": "{p}表面上想要{o}",
        "desire_inner": "{p}真正想要的是{i}",
        "bottom_line": "{p}的底线是{v}",
    },
    "ja": {
        "sep": "",
        "end": "。",
        "pron": {"female": "彼女", "male": "彼", "other": "この人"},
        "response": "その結果、{v}",
        "cost": "そのため、{p}は{v}",
        # 「表向きは/本当は」是副词式接法：desire 值无论以「〜たい/ほしい」
        # 「〜こと」还是名词结尾都成立（存量 ja 数据 35% 以たい/ほしい结尾，
        # 「望んでいるのは…たい」这类系词式接法会前后不搭）
        "desire_pair": "表向きは、{o}。だが本当は、{i}",
        "desire_outer": "表向きは、{o}",
        "desire_inner": "本当は、{i}",
        "bottom_line": "{p}の譲れない一線：{v}",
    },
    "ko": {
        "sep": " ",
        "end": ".",
        "pron": {"female": "그녀", "male": "그", "other": "이 사람"},
        "response": "그 결과, {v}",
        "cost": "그래서 {p}는 {v}",
        "desire_pair": "겉으로 원하는 것은 {o}. 하지만 진짜 원하는 것은 {i}",
        "desire_outer": "겉으로 원하는 것은 {o}",
        "desire_inner": "진짜 원하는 것은 {i}",
        "bottom_line": "{p}의 마지노선: {v}",
    },
    "en": {
        "sep": " ",
        "end": ".",
        "pron": {"female": "she", "male": "he", "other": "they"},
        "response": "As a result: {v}",
        "cost": "The cost: {v}",
        "desire_pair": "What {p} appears to want: {o}. What {p} truly wants: {i}",
        "desire_outer": "What {p} appears to want: {o}",
        "desire_inner": "What {p} truly wants: {i}",
        "bottom_line": "{P} bottom line: {v}",
    },
}
# en 的所有格另配（she→Her 不是规则变换）
_EN_POSS = {"female": "Her", "male": "His", "other": "Their"}

# cost 值偶见自带主语开头（如「彼女は…」），此时连接词不再前置代词，
# 避免拼出「そのため、彼女は彼女は…」。ko 不用裸「그」防误伤「그래서/그런」。
_SUBJ_PREFIXES = {
    "zh": ("她", "他", "TA"),
    "ja": ("彼",),  # 「彼」同时覆盖「彼女」
    "ko": ("그녀", "그는", "그가", "그 사람"),
    "en": ("she ", "he ", "they ", "She ", "He ", "They "),
}
_COST_NO_PRON = {
    "zh": "因此{v}",
    "ja": "そのため、{v}",
    "ko": "그래서 {v}",
    "en": "The cost: {v}",
}


def _text(value) -> str:
    """persona 值拍平成文本：字符串原样，列表换行相接，其余转 str。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(p for p in (_text(v) for v in value) if p)
    return str(value).strip()


def _bare(text: str) -> str:
    return text.rstrip(_END_PUNCT + " ")


def build_personality_text(pers, lang: str, gender: str) -> str:
    """personality dict → 单段自然语言。healing 有意丢弃。

    只有 summary 的角色（PERSONALITY_RULES 允许的常见形态）原样输出 summary；
    因果链字段缺哪个就跳过哪段，不硬凑连接词。
    """
    if isinstance(pers, str):
        return pers.strip()
    if not isinstance(pers, dict):
        return ""
    lang_key = (lang or "").lower()[:2]
    if lang_key not in _L10N:
        lang_key = "en"
    t = _L10N[lang_key]
    p = t["pron"].get(gender, t["pron"]["other"])
    P = _EN_POSS.get(gender, _EN_POSS["other"]) if lang_key == "en" else p

    def _end(text: str) -> str:
        text = text.rstrip()
        return text if text[-1:] in _END_PUNCT else text + t["end"]

    v = {k: _text(pers.get(k)) for k in
         ("summary", "decisive_event", "response", "cost",
          "desire_outer", "desire_inner", "desire_bottom_line")}
    chunks: list[str] = []
    if v["summary"]:
        chunks.append(_end(v["summary"]))
    if v["decisive_event"]:
        chunks.append(_end(v["decisive_event"]))
    if v["response"]:
        chunks.append(_end(t["response"].format(v=_bare(v["response"]), p=p)))
    if v["cost"]:
        cost_tpl = (_COST_NO_PRON[lang_key]
                    if v["cost"].startswith(_SUBJ_PREFIXES[lang_key])
                    else t["cost"])
        chunks.append(_end(cost_tpl.format(v=_bare(v["cost"]), p=p)))
    if v["desire_outer"] and v["desire_inner"]:
        chunks.append(_end(t["desire_pair"].format(
            o=_bare(v["desire_outer"]), i=_bare(v["desire_inner"]), p=p)))
    elif v["desire_outer"]:
        chunks.append(_end(t["desire_outer"].format(o=_bare(v["desire_outer"]), p=p)))
    elif v["desire_inner"]:
        chunks.append(_end(t["desire_inner"].format(i=_bare(v["desire_inner"]), p=p)))
    if v["desire_bottom_line"]:
        chunks.append(_end(t["bottom_line"].format(
            v=_bare(v["desire_bottom_line"]), p=p, P=P)))
    return t["sep"].join(chunks)


def _merge_inner(perception, hidden_side) -> str:
    return "\n".join(p for p in (_text(perception), _text(hidden_side)) if p)


def _social_item(item):
    """旧条目 {name,relation,info,dynamic} → 新条目 {name,relationship,description}；
    已是新结构（或非 dict）的原样返回。"""
    if not isinstance(item, dict) or "relationship" in item or "description" in item:
        return item
    desc = "；".join(p for p in (_text(item.get("info")), _text(item.get("dynamic"))) if p)
    out = {}
    if _text(item.get("name")):
        out["name"] = item["name"]
    if _text(item.get("relation")):
        out["relationship"] = item["relation"]
    if desc:
        out["description"] = desc
    return out or item


def _merge_social(family, social_network):
    """family + social_network 相接，条目统一成新结构 {name,relationship,description}；
    混入字符串时归一成列表元素。"""
    merged: list = []
    for value in (family, social_network):
        if not value:
            continue
        if isinstance(value, list):
            merged.extend(_social_item(it) for it in value)
        else:
            merged.append(value)
    return merged


def to_export_schema(persona: dict, lang: str) -> dict:
    """旧 schema persona → 交付 schema（新 dict，不改原对象）。

    字段顺序尽量保持原样：合并字段落在首个来源字段的位置上。
    """
    persona = persona or {}
    gender = normalize_gender(_text(persona.get("gender")))
    inner = _merge_inner(persona.get("perception"), persona.get("hidden_side"))
    social = _merge_social(persona.get("family"), persona.get("social_network"))
    out: dict = {}
    for k, val in persona.items():
        if k == "personality":
            text = build_personality_text(val, lang, gender)
            if text:
                out["personality"] = text
        elif k in ("perception", "hidden_side"):
            if inner and "inner_structure" not in out:
                out["inner_structure"] = inner
        elif k in ("family", "social_network"):
            if social and "social_network" not in out:
                out["social_network"] = social
        elif k in _RENAMES:
            out[_RENAMES[k]] = val
        else:
            out[k] = val
    return out
