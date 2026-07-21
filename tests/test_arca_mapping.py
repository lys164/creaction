from app.arca_mapping import normalize_gender, persona_to_character_form


def cs_get(form, tag_key):
    """從新版 customized_settings（[]GeneralTagInfo）取 tag_value。"""
    for item in form.get("customized_settings", []):
        if item["tag_key"] == tag_key:
            return item["tag_value"]
    return None


def cs_keys(form):
    return [i["tag_key"] for i in form.get("customized_settings", [])]


def test_opening_prologue_extracts_content_from_real_message_shape():
    # opening.messages 的真實形態是 {type, data:{content}} 物件（非純字串）
    persona = {
        "name": "A",
        "opening": {
            "note": "",
            "messages": [
                {"type": "text", "data": {"content": "最近好嗎？"}},
                {"type": "voice", "data": {"content": "想你了", "emotion": "soft"}},
                {"type": "text", "data": {"content": ""}},  # 空內容應跳過
            ],
        },
    }
    form = persona_to_character_form(persona)
    assert form["opening_prologue"] == [
        {"text": "最近好嗎？", "output_type": "text"},
        {"text": "想你了", "output_type": "tts"},  # voice → tts
    ]
    # 不能把整個 message 物件序列化成 text
    assert all("data" not in item["text"] for item in form["opening_prologue"])


_PAGE_CONFIG_JA = {
    "character_tags": [
        {"tag_key": "反差萌", "tag_name": "ギャップ萌え"},
        {"tag_key": "非人類", "tag_name": "非人類"},
        {"tag_key": "溫柔", "tag_name": "優しい"},
    ],
    "species": [
        {"tag_key": "人類", "tag_name": "人間"},
        {"tag_key": "動物", "tag_name": "動物"},
    ],
    "setting_options": [
        {"tag_key": "出生地", "tag_name": "出身地"},
        {"tag_key": "愛好", "tag_name": "興味"},
    ],
}


def test_tags_species_normalized_to_platform_tag_key():
    persona = {"name": "A", "tags": ["ギャップ萌え", "非人類", "ドラ焼き好き"],
               "species": "人間"}
    form = persona_to_character_form(persona, page_config=_PAGE_CONFIG_JA)
    # 本地化詞→tag_key；嚴格模式下平臺沒有的詞直接丟棄
    assert form["tags"] == ["反差萌", "非人類"]
    assert form["species"] == "人類"
    # 非列舉物種歸「其他」(_PAGE_CONFIG_JA 無「其他」→保留原詞兜底)
    form2 = persona_to_character_form({"name": "A", "species": "ネコ型ロボット"},
                                      page_config=_PAGE_CONFIG_JA)
    assert form2["species"] == "ネコ型ロボット"
    # 詞表含「其他」時歸「其他」
    pc = {"species": _PAGE_CONFIG_JA["species"] + [{"tag_key": "其他", "tag_name": "他の"}]}
    form3 = persona_to_character_form({"name": "A", "species": "ネコ型ロボット"},
                                      page_config=pc)
    assert form3["species"] == "其他"
    # 全部 tags 都不在詞表 → 不帶 tags 欄位
    form4 = persona_to_character_form({"name": "A", "tags": ["自由詞"]},
                                      page_config=_PAGE_CONFIG_JA)
    assert "tags" not in form4
    # 降級模式（無 page_config）寬鬆透傳
    form5 = persona_to_character_form({"name": "A", "tags": ["自由詞"], "species": "人間"})
    assert form5["tags"] == ["自由詞"] and form5["species"] == "人間"


def test_customized_settings_keys_use_platform_tag_key():
    persona = {"name": "A", "hometown": "東京", "likes": "銅鑼燒", "hidden_side": "怕鼠"}
    # tag_key 一律用平臺主鍵（簡中）；後端強校驗 tag_key，
    # 平臺無對應設定項的欄位(hidden_side)必須丟棄，否則整個 create 被拒
    for kwargs in ({"page_config": _PAGE_CONFIG_JA}, {}):
        form = persona_to_character_form(persona, **kwargs)
        assert cs_get(form, "出生地") == "東京"
        assert "hidden_side" not in cs_keys(form)
    # tag_name 用 page_config 的本地化名；無 page_config 回退 tag_key
    form_ja = persona_to_character_form(persona, page_config=_PAGE_CONFIG_JA)
    by_key = {i["tag_key"]: i for i in form_ja["customized_settings"]}
    assert by_key["出生地"]["tag_name"] == "出身地"
    assert by_key["愛好"]["tag_name"] == "興味"
    # _PAGE_CONFIG_JA 的 setting_options 不含「愛好」以外未列項 → 二次校驗會過濾，
    # 無 page_config 時靜態表放行
    form_plain = persona_to_character_form(persona)
    assert cs_get(form_plain, "愛好") == "銅鑼燒"


def test_customized_settings_values_are_natural_language_not_json():
    persona = {
        "name": "A",
        "life_details": ["睡前必須喝熱牛奶", "手機相簿全是流浪貓"],
        "family": [
            {"name": "田中太郎", "relation": "父", "info": "嚴厲的上班族", "dynamic": "沉默的關心"},
        ],
        "wishlist": {"目標": "開一家小店", "隱藏": "被人需要"},
    }
    form = persona_to_character_form(persona)
    # 陣列 → 每項一行
    assert cs_get(form, "生活習慣") == "睡前必須喝熱牛奶\n手機相簿全是流浪貓"
    # 物件陣列 → 每項「key: value；…」一行
    assert cs_get(form, "家庭成員") == "name: 田中太郎；relation: 父；info: 嚴厲的上班族；dynamic: 沉默的關心"
    assert cs_get(form, "願望清單") == "目標: 開一家小店；隱藏: 被人需要"
    # 任何 value 都不允許出現 JSON 語法符號
    for item in form["customized_settings"]:
        v = item["tag_value"]
        assert '{"' not in v and '["' not in v and '\\u' not in v


def test_visibility_and_anonymous_identities_are_form_fields():
    persona = {
        "name": "A",
        "visibility": "public",
        "anonymous_identities": ["22世紀のロボ", "世話焼きネコ"],
    }
    form = persona_to_character_form(persona)
    assert form["visibility"] == "public"
    assert form["anonymous_tags"] == ["22世紀のロボ", "世話焼きネコ"]
    cs = form.get("customized_settings", {})
    assert "visibility" not in cs and "anonymous_identities" not in cs
    # 非法 visibility 不透傳（go-zero options 列舉會拒 400）
    form2 = persona_to_character_form({"name": "A", "visibility": "friends_only"})
    assert "visibility" not in form2


def test_disposition_builds_natural_language_by_lang_and_gender():
    persona = {
        "name": "A",
        "gender": "女",
        "personality": {
            "summary": "外柔內剛。",
            "decisive_event": "少年時的一場離別。",
            "response": "學會了先照顧別人。",
            "cost": "很少表達自己的需要。",
            "desire_outer": "安穩的生活。",
            "desire_inner": "被無條件接納。",
            "desire_bottom_line": "絕不背叛信任她的人。",
            "healing": "被人堅定選擇。",  # healing 有意丟棄，不進 disposition
        },
    }
    form = persona_to_character_form(persona, lang="zh")
    assert form["disposition"] == (
        "外柔內剛。少年時的一場離別。結果，學會了先照顧別人。因此她很少表達自己的需要。"
        "她表面上想要安穩的生活，實際上想要被無條件接納。她的底線是絕不背叛信任她的人。")
    assert "被人堅定選擇" not in form["disposition"]
    # 缺鏈條欄位時逐段跳過，不硬湊連線詞
    form2 = persona_to_character_form(
        {"name": "A", "personality": {"summary": "外柔內剛", "response": ""}})
    assert form2["disposition"] == "外柔內剛。"


def test_tags_map_to_form_field_not_customized_settings():
    persona = {"name": "A", "tags": ["非人類", "能幹", "溫柔"], "likes": "蛋糕"}
    form = persona_to_character_form(persona)
    assert form["tags"] == ["非人類", "能幹", "溫柔"]
    assert "tags" not in cs_keys(form)
    # 其它有平臺對應位的欄位仍走兜底
    assert cs_get(form, "愛好") == "蛋糕"


def test_normalize_gender_all_langs():
    assert normalize_gender("男") == "male"
    assert normalize_gender("男性") == "male"
    assert normalize_gender("남성") == "male"
    assert normalize_gender("Male") == "male"
    assert normalize_gender("女") == "female"
    assert normalize_gender("여성") == "female"
    assert normalize_gender("その他") == "other"
    assert normalize_gender("") == "other"


def test_persona_to_form_core_fields():
    persona = {
        "name": "小櫻",
        "profile": "溫柔堅定的魔法少女",
        "gender": "女",
        "species": "人類",
        "personality": {"summary": "外柔內剛"},
        "opening": {"note": "hi", "messages": ["最近好嗎？", "在忙什麼"]},
        "backstory": "一段往事",
        "likes": "草莓蛋糕",
    }
    form = persona_to_character_form(persona)
    assert form["name"] == "小櫻"
    assert form["profile"] == "溫柔堅定的魔法少女"
    assert form["gender"] == "female"
    assert form["species"] == "人類"
    assert form["disposition"] == "外柔內剛。"
    # opening 兩句 → opening_prologue，output_type=text
    assert form["opening_prologue"] == [
        {"text": "最近好嗎？", "output_type": "text"},
        {"text": "在忙什麼", "output_type": "text"},
    ]
    # 未直接對映的欄位進 customized_settings（新 IDL: []GeneralTagInfo）
    assert cs_get(form, "成長經歷") == "一段往事"
    assert cs_get(form, "愛好") == "草莓蛋糕"
    # 一等欄位不重複出現；每個元素帶齊 GeneralTagInfo 必需鍵
    assert not set(cs_keys(form)) & {"name", "opening", "personality"}
    for item in form["customized_settings"]:
        assert set(item) == {"tag_key", "tag_name", "tag_icon", "tag_value", "index"}


def test_persona_to_form_personality_as_string():
    # 字串 personality → disposition；personality 不再進 customized_settings
    persona = {
        "name": "阿朗",
        "personality": "沉穩內斂",
    }
    form = persona_to_character_form(persona)
    assert form["disposition"] == "沉穩內斂"
    assert "personality" not in form.get("customized_settings", {})

    # 空 dict personality → 同樣不出現
    persona_empty = {
        "name": "測試",
        "personality": {},
    }
    form_empty = persona_to_character_form(persona_empty)
    assert "personality" not in form_empty.get("customized_settings", {})


def test_persona_to_form_images_and_landing():
    img = {"image_type": "aigc", "media": {"bucket_name": "b", "object_key": "k", "object_type": "image"}, "is_main_pic": True}
    form = persona_to_character_form({"name": "A"}, images=[img], landing_url="https://cdn/x.html")
    assert form["images"] == [img]
    assert form["landing_page_url"] == "https://cdn/x.html"
