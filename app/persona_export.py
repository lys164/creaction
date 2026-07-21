"""persona 舊 schema → 交付新 schema 的純轉換（無副作用、無 IO，便於單測）。

存量資料與生成/聊天/落地頁鏈路保持舊 schema 不動，只在兩條出口處轉換：
  1. /api/characters/export 的 character.json（to_export_schema）
  2. arca 建角色的 disposition 拼接（build_personality_text，由 arca_mapping 呼叫）

變換內容：
  - personality 因果鏈 dict → 按角色語言+性別拼成一段自然語言（healing 有意丟棄；
    desire_inner 屬裡層，不進 personality，改併入 inner_structure）
  - inner_structure = perception + hidden_side（+ personality.desire_inner 一句）
  - social_network = family + social_network（同構列表直接相接）
  - 改名 social_status→identity / situational_reactions→behavior_patterns /
    fears→dislike / premise→worldview

注意：新 schema 生成端 personality 本就是因果鏈 dict（含 imprint/desire_inner
等子欄位），本轉換對存量舊 dict 與新 dict 一視同仁。
"""
from __future__ import annotations

from .arca_mapping import normalize_gender

_RENAMES = {
    "social_status": "identity",
    "situational_reactions": "behavior_patterns",
    "fears": "dislikes",
    "premise": "worldview",
}

# 句末標點：拼接時先剝掉再按語言補，避免出現「。，」這類接縫
_END_PUNCT = "。．.!！?？;；"

# 每語言的連線詞 + 代詞（female / male / other）。
# 拼接模板（以 zh 女性為例）：
#   {summary}{decisive_event}結果，{response}因此她{cost}
#   她表面上想要{desire_outer}，實際上想要{desire_inner}。她的底線是{desire_bottom_line}。
# ko/en 的 desire 段不把代詞粘進從句（韓語助詞/英語句式對任意插值不穩），改用
# 「겉으로 원하는 것은…」「What she appears to want:」這類對句式免疫的寫法。
_L10N = {
    "zh": {
        "sep": "",
        "end": "。",
        "pron": {"female": "她", "male": "他", "other": "TA"},
        "response": "結果，{v}",
        "cost": "因此{p}{v}",
        "desire_pair": "{p}表面上想要{o}，實際上想要{i}",
        "desire_outer": "{p}表面上想要{o}",
        "desire_inner": "{p}真正想要的是{i}",
        "bottom_line": "{p}的底線是{v}",
    },
    "ja": {
        "sep": "",
        "end": "。",
        "pron": {"female": "彼女", "male": "彼", "other": "この人"},
        "response": "その結果、{v}",
        "cost": "そのため、{p}は{v}",
        # 「表向きは/本當は」是副詞式接法：desire 值無論以「〜たい/ほしい」
        # 「〜こと」還是名詞結尾都成立（存量 ja 資料 35% 以たい/ほしい結尾，
        # 「望んでいるのは…たい」這類系詞式接法會前後不搭）
        "desire_pair": "表向きは、{o}。だが本當は、{i}",
        "desire_outer": "表向きは、{o}",
        "desire_inner": "本當は、{i}",
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
# en 的所有格另配（she→Her 不是規則變換）
_EN_POSS = {"female": "Her", "male": "His", "other": "Their"}

# cost 值偶見自帶主語開頭（如「彼女は…」），此時連線詞不再前置代詞，
# 避免拼出「そのため、彼女は彼女は…」。ko 不用裸「그」防誤傷「그래서/그런」。
_SUBJ_PREFIXES = {
    "zh": ("她", "他", "TA"),
    "ja": ("彼",),  # 「彼」同時覆蓋「彼女」
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
    """persona 值拍平成文字：字串原樣，列表換行相接，其餘轉 str。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(p for p in (_text(v) for v in value) if p)
    return str(value).strip()


def _bare(text: str) -> str:
    return text.rstrip(_END_PUNCT + " ")


def build_personality_text(pers, lang: str, gender: str,
                           include_desire_inner: bool = True) -> str:
    """personality dict → 單段自然語言。healing 有意丟棄。

    只有 summary 的角色（PERSONALITY_RULES 允許的常見形態）原樣輸出 summary；
    因果鏈欄位缺哪個就跳過哪段，不硬湊連線詞。
    include_desire_inner=False 時把 desire_inner 排除在外（交付 schema 改把它
    併入 inner_structure，見 to_export_schema）；arca disposition 全部拼進一段
    文字，維持預設 True 即可，不丟資訊。
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
         ("summary", "decisive_event", "imprint", "response", "cost",
          "desire_outer", "desire_inner", "desire_bottom_line")}
    di = v["desire_inner"] if include_desire_inner else ""
    chunks: list[str] = []
    if v["summary"]:
        chunks.append(_end(v["summary"]))
    if v["decisive_event"]:
        chunks.append(_end(v["decisive_event"]))
    if v["imprint"]:
        chunks.append(_end(v["imprint"]))
    if v["response"]:
        chunks.append(_end(t["response"].format(v=_bare(v["response"]), p=p)))
    if v["cost"]:
        cost_tpl = (_COST_NO_PRON[lang_key]
                    if v["cost"].startswith(_SUBJ_PREFIXES[lang_key])
                    else t["cost"])
        chunks.append(_end(cost_tpl.format(v=_bare(v["cost"]), p=p)))
    if v["desire_outer"] and di:
        chunks.append(_end(t["desire_pair"].format(
            o=_bare(v["desire_outer"]), i=_bare(di), p=p)))
    elif v["desire_outer"]:
        chunks.append(_end(t["desire_outer"].format(o=_bare(v["desire_outer"]), p=p)))
    elif di:
        chunks.append(_end(t["desire_inner"].format(i=_bare(di), p=p)))
    if v["desire_bottom_line"]:
        chunks.append(_end(t["bottom_line"].format(
            v=_bare(v["desire_bottom_line"]), p=p, P=P)))
    return t["sep"].join(chunks)


def desire_inner_sentence(pers, lang: str, gender: str) -> str:
    """把 personality.desire_inner 格式化成一句獨立自然語言，供併入 inner_structure。"""
    if not isinstance(pers, dict):
        return ""
    di = _text(pers.get("desire_inner"))
    if not di:
        return ""
    lang_key = (lang or "").lower()[:2]
    if lang_key not in _L10N:
        lang_key = "en"
    t = _L10N[lang_key]
    p = t["pron"].get(gender, t["pron"]["other"])
    text = t["desire_inner"].format(i=_bare(di), p=p).rstrip()
    return text if text[-1:] in _END_PUNCT else text + t["end"]


def _merge_inner(perception, hidden_side) -> str:
    return "\n".join(p for p in (_text(perception), _text(hidden_side)) if p)


def _social_item(item):
    """舊條目 {name,relation,info,dynamic} → 新條目 {name,relationship,description}；
    已是新結構（或非 dict）的原樣返回。"""
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
    """family + social_network 相接，條目統一成新結構 {name,relationship,description}；
    混入字串時歸一成列表元素。"""
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
    """舊 schema persona → 交付 schema（新 dict，不改原物件）。

    欄位順序儘量保持原樣：合併欄位落在首個來源欄位的位置上。
    ``source`` 是本系統的追溯資料，不屬於交付 schema，絕不導出；
    ``style`` 則只保留角色生產時明確選擇的業務值。
    """
    persona = persona or {}
    gender = normalize_gender(_text(persona.get("gender")))
    inner = _merge_inner(persona.get("perception"), persona.get("hidden_side"))
    social = _merge_social(persona.get("family"), persona.get("social_network"))
    # desire_inner（心裡真正要的）屬裡層，從 personality 因果鏈移出、併入 inner_structure。
    di_sentence = desire_inner_sentence(persona.get("personality"), lang, gender)
    out: dict = {}
    for k, val in persona.items():
        if k == "personality":
            text = build_personality_text(val, lang, gender,
                                          include_desire_inner=False)
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
    if di_sentence:
        merged_inner = "\n".join(p for p in (_text(out.get("inner_structure")),
                                             di_sentence) if p)
        out["inner_structure"] = merged_inner
    return out
