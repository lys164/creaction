from app.persona_export import build_personality_text, to_export_schema


def test_personality_zh_female_full_chain():
    pers = {
        "summary": "表面冷静，内里怕失去。",
        "decisive_event": "15岁那年被独自留在空房子里。",
        "response": "变成不需要任何人的完美成年人。",
        "cost": "失去了建立亲密关系的能力。",
        "desire_outer": "互不打扰的清净生活。",
        "desire_inner": "一个不会离开的人。",
        "desire_bottom_line": "绝不容忍不告而别。",
        "healing": "有意丢弃的字段。",
    }
    text = build_personality_text(pers, "zh", "female")
    assert text == (
        "表面冷静，内里怕失去。15岁那年被独自留在空房子里。"
        "结果，变成不需要任何人的完美成年人。因此她失去了建立亲密关系的能力。"
        "她表面上想要互不打扰的清净生活，实际上想要一个不会离开的人。"
        "她的底线是绝不容忍不告而别。")
    assert "有意丢弃" not in text


def test_personality_male_pronoun_and_langs():
    pers = {
        "summary": "S.",
        "cost": "C.",
        "desire_outer": "O.",
        "desire_inner": "I.",
        "desire_bottom_line": "B.",
    }
    zh = build_personality_text(pers, "zh", "male")
    assert "因此他C" in zh and "他表面上想要O" in zh and "他的底线是B" in zh
    ko = build_personality_text(pers, "ko", "male")
    assert "그래서 그는 C." in ko
    assert "겉으로 원하는 것은 O. 하지만 진짜 원하는 것은 I." in ko
    assert "그의 마지노선: B." in ko
    ja = build_personality_text(pers, "ja", "female")
    assert "そのため、彼女はC。" in ja and "彼の" not in ja
    assert "表向きは、O。だが本当は、I。" in ja
    en = build_personality_text(pers, "en", "male")
    assert "What he appears to want: O." in en
    assert "His bottom line: B." in en
    # 未知语种回退 en；未知性别回退中性代词
    other = build_personality_text(pers, "fr", "other")
    assert "What they appears to want" in other or "What they" in other
    assert "Their bottom line: B." in other


def test_cost_with_own_subject_skips_pronoun_prefix():
    # cost 值自带主语时不再前置代词，避免「そのため、彼女は彼女は…」
    ja = build_personality_text(
        {"cost": "彼女は距離を置くようになった。"}, "ja", "female")
    assert ja == "そのため、彼女は距離を置くようになった。"
    zh = build_personality_text({"cost": "他不再信任任何人。"}, "zh", "male")
    assert zh == "因此他不再信任任何人。"
    # 韩语不用裸「그」判定，「그런」开头的正常谓句不受影响
    ko = build_personality_text({"cost": "그런 방식 탓에 지쳐 간다."}, "ko", "male")
    assert ko == "그래서 그는 그런 방식 탓에 지쳐 간다."


def test_personality_summary_only_and_string_passthrough():
    assert build_personality_text({"summary": "平和的人"}, "zh", "female") == "平和的人。"
    assert build_personality_text("沉稳内敛", "ko", "male") == "沉稳内敛"
    assert build_personality_text({}, "zh", "other") == ""
    assert build_personality_text(None, "zh", "other") == ""


def test_export_schema_renames_and_removals():
    persona = {
        "name": "A",
        "gender": "女",
        "social_status": "咖啡师",
        "situational_reactions": "生气时打字变短",
        "fears": "敷衍的回应",
        "premise": "近未来城市",
        "likes": "手冲咖啡",
    }
    out = to_export_schema(persona, "zh")
    assert out["identity"] == "咖啡师"
    assert out["behavior_patterns"] == "生气时打字变短"
    assert out["dislikes"] == "敷衍的回应"
    assert out["worldview"] == "近未来城市"
    assert out["likes"] == "手冲咖啡"
    for old in ("social_status", "situational_reactions", "fears", "premise"):
        assert old not in out
    # 原对象不被修改
    assert persona["social_status"] == "咖啡师"


def test_export_schema_merges_inner_structure_and_social_network():
    persona = {
        "name": "A",
        "perception": "世界对她来说是一间自习室",
        "hidden_side": "深夜会写没人看的诗",
        "family": [{"name": "哥哥", "relation": "兄", "info": "在外地", "dynamic": "偶尔通话"}],
        "social_network": [{"name": "店长", "relation": "上司", "info": "话少", "dynamic": "互相信任"}],
    }
    out = to_export_schema(persona, "zh")
    assert out["inner_structure"] == "世界对她来说是一间自习室\n深夜会写没人看的诗"
    assert [x["name"] for x in out["social_network"]] == ["哥哥", "店长"]
    # 旧条目键归一成新结构 {name, relationship, description}
    assert out["social_network"][0]["relationship"] == "兄"
    assert out["social_network"][0]["description"] == "在外地；偶尔通话"
    assert "relation" not in out["social_network"][0]
    for old in ("perception", "hidden_side", "family"):
        assert old not in out
    # perception 常见为 null：只剩 hidden_side 也能落 inner_structure
    out2 = to_export_schema({"perception": None, "hidden_side": "藏着软肋"}, "zh")
    assert out2["inner_structure"] == "藏着软肋"
    # 两个来源都空则不产出合并字段
    out3 = to_export_schema({"perception": None, "family": []}, "zh")
    assert "inner_structure" not in out3 and "social_network" not in out3


def test_export_schema_passes_new_schema_records_through():
    # 生成端已切新 schema：新记录过导出转换应原样直通（幂等）
    persona = {
        "name": "B",
        "value": "24岁/165cm/INFP",
        "gender": "女",
        "personality": "【标签】具体行为模式……",
        "inner_structure": "价值观、恐惧、隐藏面……",
        "identity": "便利店夜班店员",
        "online_chat_style": "短句连发，几乎不用句号",
        "behavior_patterns": "生气时先沉默再讲道理",
        "dislikes": ["被催"],
        "worldview": "",
        "social_network": [{"name": "店长", "relationship": "上司", "description": "话少但可靠"}],
    }
    out = to_export_schema(persona, "zh")
    assert {k: v for k, v in out.items() if v} == {k: v for k, v in persona.items() if v}


def test_export_schema_personality_uses_record_lang_and_gender():
    persona = {
        "gender": "남성",
        "personality": {"summary": "무기력해 보이는 완벽주의자.",
                        "desire_bottom_line": "내 사람은 지킨다."},
    }
    out = to_export_schema(persona, "ko")
    assert out["personality"] == (
        "무기력해 보이는 완벽주의자. 그의 마지노선: 내 사람은 지킨다.")
