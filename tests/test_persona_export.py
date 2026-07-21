from app.persona_export import build_personality_text, to_export_schema


def test_personality_zh_female_full_chain():
    pers = {
        "summary": "表面冷靜，內裡怕失去。",
        "decisive_event": "15歲那年被獨自留在空房子裡。",
        "imprint": "從此認定親近的人都會走。",
        "response": "變成不需要任何人的完美成年人。",
        "cost": "失去了建立親密關係的能力。",
        "desire_outer": "互不打擾的清淨生活。",
        "desire_inner": "一個不會離開的人。",
        "desire_bottom_line": "絕不容忍不告而別。",
        "healing": "有意丟棄的欄位。",
    }
    text = build_personality_text(pers, "zh", "female")
    assert text == (
        "表面冷靜，內裡怕失去。15歲那年被獨自留在空房子裡。從此認定親近的人都會走。"
        "結果，變成不需要任何人的完美成年人。因此她失去了建立親密關係的能力。"
        "她表面上想要互不打擾的清淨生活，實際上想要一個不會離開的人。"
        "她的底線是絕不容忍不告而別。")
    assert "有意丟棄" not in text
    # include_desire_inner=False：desire_inner 不出現，desire_outer 獨立成句
    text2 = build_personality_text(pers, "zh", "female", include_desire_inner=False)
    assert "一個不會離開的人" not in text2
    assert "她表面上想要互不打擾的清淨生活。" in text2
    assert "從此認定親近的人都會走。" in text2


def test_personality_male_pronoun_and_langs():
    pers = {
        "summary": "S.",
        "cost": "C.",
        "desire_outer": "O.",
        "desire_inner": "I.",
        "desire_bottom_line": "B.",
    }
    zh = build_personality_text(pers, "zh", "male")
    assert "因此他C" in zh and "他表面上想要O" in zh and "他的底線是B" in zh
    ko = build_personality_text(pers, "ko", "male")
    assert "그래서 그는 C." in ko
    assert "겉으로 원하는 것은 O. 하지만 진짜 원하는 것은 I." in ko
    assert "그의 마지노선: B." in ko
    ja = build_personality_text(pers, "ja", "female")
    assert "そのため、彼女はC。" in ja and "彼の" not in ja
    assert "表向きは、O。だが本當は、I。" in ja
    en = build_personality_text(pers, "en", "male")
    assert "What he appears to want: O." in en
    assert "His bottom line: B." in en
    # 未知語種回退 en；未知性別回退中性代詞
    other = build_personality_text(pers, "fr", "other")
    assert "What they appears to want" in other or "What they" in other
    assert "Their bottom line: B." in other


def test_cost_with_own_subject_skips_pronoun_prefix():
    # cost 值自帶主語時不再前置代詞，避免「そのため、彼女は彼女は…」
    ja = build_personality_text(
        {"cost": "彼女は距離を置くようになった。"}, "ja", "female")
    assert ja == "そのため、彼女は距離を置くようになった。"
    zh = build_personality_text({"cost": "他不再信任任何人。"}, "zh", "male")
    assert zh == "因此他不再信任任何人。"
    # 韓語不用裸「그」判定，「그런」開頭的正常謂句不受影響
    ko = build_personality_text({"cost": "그런 방식 탓에 지쳐 간다."}, "ko", "male")
    assert ko == "그래서 그는 그런 방식 탓에 지쳐 간다."


def test_personality_summary_only_and_string_passthrough():
    assert build_personality_text({"summary": "平和的人"}, "zh", "female") == "平和的人。"
    assert build_personality_text("沉穩內斂", "ko", "male") == "沉穩內斂"
    assert build_personality_text({}, "zh", "other") == ""
    assert build_personality_text(None, "zh", "other") == ""


def test_export_schema_renames_and_removals():
    persona = {
        "name": "A",
        "gender": "女",
        "social_status": "咖啡師",
        "situational_reactions": "生氣時打字變短",
        "fears": "敷衍的回應",
        "premise": "近未來城市",
        "likes": "手衝咖啡",
    }
    out = to_export_schema(persona, "zh")
    assert out["identity"] == "咖啡師"
    assert out["behavior_patterns"] == "生氣時打字變短"
    assert out["dislikes"] == "敷衍的回應"
    assert out["worldview"] == "近未來城市"
    assert out["likes"] == "手衝咖啡"
    for old in ("social_status", "situational_reactions", "fears", "premise"):
        assert old not in out
    # 原物件不被修改
    assert persona["social_status"] == "咖啡師"


def test_export_schema_merges_inner_structure_and_social_network():
    persona = {
        "name": "A",
        "perception": "世界對她來說是一間自習室",
        "hidden_side": "深夜會寫沒人看的詩",
        "family": [{"name": "哥哥", "relation": "兄", "info": "在外地", "dynamic": "偶爾通話"}],
        "social_network": [{"name": "店長", "relation": "上司", "info": "話少", "dynamic": "互相信任"}],
    }
    out = to_export_schema(persona, "zh")
    assert out["inner_structure"] == "世界對她來說是一間自習室\n深夜會寫沒人看的詩"
    assert [x["name"] for x in out["social_network"]] == ["哥哥", "店長"]
    # 舊條目鍵歸一成新結構 {name, relationship, description}
    assert out["social_network"][0]["relationship"] == "兄"
    assert out["social_network"][0]["description"] == "在外地；偶爾通話"
    assert "relation" not in out["social_network"][0]
    for old in ("perception", "hidden_side", "family"):
        assert old not in out
    # perception 常見為 null：只剩 hidden_side 也能落 inner_structure
    out2 = to_export_schema({"perception": None, "hidden_side": "藏著軟肋"}, "zh")
    assert out2["inner_structure"] == "藏著軟肋"
    # 兩個來源都空則不產出合併欄位
    out3 = to_export_schema({"perception": None, "family": []}, "zh")
    assert "inner_structure" not in out3 and "social_network" not in out3


def test_export_schema_new_schema_personality_chain_and_inner_merge():
    # 生成端 personality 為因果鏈 dict：匯出時拼成散文，desire_inner 併入 inner_structure
    persona = {
        "name": "B",
        "value": "24歲/165cm/INFP",
        "gender": "女",
        "personality": {
            "summary": "看似無欲無求。",
            "desire_outer": "一個人清靜。",
            "desire_inner": "有人記得她。",
        },
        "inner_structure": "世界對她是一場考試。",
        "identity": "便利店夜班店員",
    }
    out = to_export_schema(persona, "zh")
    # personality 散文不含 desire_inner
    assert "有人記得她" not in out["personality"]
    assert "她表面上想要一個人清靜" in out["personality"]
    # desire_inner 併入 inner_structure（追加在原 inner_structure 之後）
    assert out["inner_structure"].startswith("世界對她是一場考試。")
    assert "她真正想要的是有人記得她。" in out["inner_structure"]
    # 其餘欄位原樣直通
    assert out["value"] == "24歲/165cm/INFP"
    assert out["identity"] == "便利店夜班店員"


def test_export_schema_desire_inner_creates_inner_structure_when_absent():
    # 沒有 inner_structure 欄位時，desire_inner 自己撐起 inner_structure
    persona = {
        "gender": "男",
        "personality": {"summary": "話少。", "desire_inner": "被看見。"},
    }
    out = to_export_schema(persona, "zh")
    assert out["inner_structure"] == "他真正想要的是被看見。"
    assert "被看見" not in out["personality"]


def test_export_schema_keeps_explicit_style_without_exporting_source():
    persona = {"name": "A", "likes": "咖啡"}
    assert "style" not in to_export_schema(persona, "zh")
    chosen = {**persona, "style": "fantasy"}
    assert to_export_schema(chosen, "zh")["style"] == "fantasy"
    # 原物件不被汙染
    assert "style" not in persona


def test_export_schema_personality_uses_record_lang_and_gender():
    persona = {
        "gender": "남성",
        "personality": {"summary": "무기력해 보이는 완벽주의자.",
                        "desire_bottom_line": "내 사람은 지킨다."},
    }
    out = to_export_schema(persona, "ko")
    assert out["personality"] == (
        "무기력해 보이는 완벽주의자. 그의 마지노선: 내 사람은 지킨다.")
