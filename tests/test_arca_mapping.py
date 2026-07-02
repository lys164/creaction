from app.arca_mapping import normalize_gender, persona_to_character_form


def cs_get(form, tag_key):
    """从新版 customized_settings（[]GeneralTagInfo）取 tag_value。"""
    for item in form.get("customized_settings", []):
        if item["tag_key"] == tag_key:
            return item["tag_value"]
    return None


def cs_keys(form):
    return [i["tag_key"] for i in form.get("customized_settings", [])]


def test_opening_prologue_extracts_content_from_real_message_shape():
    # opening.messages 的真实形态是 {type, data:{content}} 对象（非纯字符串）
    persona = {
        "name": "A",
        "opening": {
            "note": "",
            "messages": [
                {"type": "text", "data": {"content": "最近好吗？"}},
                {"type": "voice", "data": {"content": "想你了", "emotion": "soft"}},
                {"type": "text", "data": {"content": ""}},  # 空内容应跳过
            ],
        },
    }
    form = persona_to_character_form(persona)
    assert form["opening_prologue"] == [
        {"text": "最近好吗？", "output_type": "text"},
        {"text": "想你了", "output_type": "tts"},  # voice → tts
    ]
    # 不能把整个 message 对象序列化成 text
    assert all("data" not in item["text"] for item in form["opening_prologue"])


_PAGE_CONFIG_JA = {
    "character_tags": [
        {"tag_key": "反差萌", "tag_name": "ギャップ萌え"},
        {"tag_key": "非人类", "tag_name": "非人類"},
        {"tag_key": "温柔", "tag_name": "優しい"},
    ],
    "species": [
        {"tag_key": "人类", "tag_name": "人間"},
        {"tag_key": "动物", "tag_name": "動物"},
    ],
    "setting_options": [
        {"tag_key": "出生地", "tag_name": "出身地"},
        {"tag_key": "爱好", "tag_name": "興味"},
    ],
}


def test_tags_species_normalized_to_platform_tag_key():
    persona = {"name": "A", "tags": ["ギャップ萌え", "非人類", "ドラ焼き好き"],
               "species": "人間"}
    form = persona_to_character_form(persona, page_config=_PAGE_CONFIG_JA)
    # 本地化词→tag_key；严格模式下平台没有的词直接丢弃
    assert form["tags"] == ["反差萌", "非人类"]
    assert form["species"] == "人类"
    # 非枚举物种归「其他」(_PAGE_CONFIG_JA 无「其他」→保留原词兜底)
    form2 = persona_to_character_form({"name": "A", "species": "ネコ型ロボット"},
                                      page_config=_PAGE_CONFIG_JA)
    assert form2["species"] == "ネコ型ロボット"
    # 词表含「其他」时归「其他」
    pc = {"species": _PAGE_CONFIG_JA["species"] + [{"tag_key": "其他", "tag_name": "他の"}]}
    form3 = persona_to_character_form({"name": "A", "species": "ネコ型ロボット"},
                                      page_config=pc)
    assert form3["species"] == "其他"
    # 全部 tags 都不在词表 → 不带 tags 字段
    form4 = persona_to_character_form({"name": "A", "tags": ["自由词"]},
                                      page_config=_PAGE_CONFIG_JA)
    assert "tags" not in form4
    # 降级模式（无 page_config）宽松透传
    form5 = persona_to_character_form({"name": "A", "tags": ["自由词"], "species": "人間"})
    assert form5["tags"] == ["自由词"] and form5["species"] == "人間"


def test_customized_settings_keys_use_platform_tag_key():
    persona = {"name": "A", "hometown": "东京", "likes": "铜锣烧", "hidden_side": "怕鼠"}
    # tag_key 一律用平台主键（简中）；后端强校验 tag_key，
    # 平台无对应设定项的字段(hidden_side)必须丢弃，否则整个 create 被拒
    for kwargs in ({"page_config": _PAGE_CONFIG_JA}, {}):
        form = persona_to_character_form(persona, **kwargs)
        assert cs_get(form, "出生地") == "东京"
        assert "hidden_side" not in cs_keys(form)
    # tag_name 用 page_config 的本地化名；无 page_config 回退 tag_key
    form_ja = persona_to_character_form(persona, page_config=_PAGE_CONFIG_JA)
    by_key = {i["tag_key"]: i for i in form_ja["customized_settings"]}
    assert by_key["出生地"]["tag_name"] == "出身地"
    assert by_key["爱好"]["tag_name"] == "興味"
    # _PAGE_CONFIG_JA 的 setting_options 不含「爱好」以外未列项 → 二次校验会过滤，
    # 无 page_config 时静态表放行
    form_plain = persona_to_character_form(persona)
    assert cs_get(form_plain, "爱好") == "铜锣烧"


def test_customized_settings_values_are_natural_language_not_json():
    persona = {
        "name": "A",
        "life_details": ["睡前必须喝热牛奶", "手机相册全是流浪猫"],
        "family": [
            {"name": "田中太郎", "relation": "父", "info": "严厉的上班族", "dynamic": "沉默的关心"},
        ],
        "wishlist": {"目标": "开一家小店", "隐藏": "被人需要"},
    }
    form = persona_to_character_form(persona)
    # 数组 → 每项一行
    assert cs_get(form, "生活习惯") == "睡前必须喝热牛奶\n手机相册全是流浪猫"
    # 对象数组 → 每项「key: value；…」一行
    assert cs_get(form, "家庭成员") == "name: 田中太郎；relation: 父；info: 严厉的上班族；dynamic: 沉默的关心"
    assert cs_get(form, "愿望清单") == "目标: 开一家小店；隐藏: 被人需要"
    # 任何 value 都不允许出现 JSON 语法符号
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
    # 非法 visibility 不透传（go-zero options 枚举会拒 400）
    form2 = persona_to_character_form({"name": "A", "visibility": "friends_only"})
    assert "visibility" not in form2


def test_disposition_joins_all_personality_values():
    persona = {
        "name": "A",
        "personality": {
            "summary": "外柔内刚",
            "decisive_event": "少年时的一场离别",
            "response": "",  # 空值跳过
            "desire_inner": "渴望被无条件接纳",
        },
    }
    form = persona_to_character_form(persona)
    assert form["disposition"] == "外柔内刚\n少年时的一场离别\n渴望被无条件接纳"


def test_tags_map_to_form_field_not_customized_settings():
    persona = {"name": "A", "tags": ["非人类", "能干", "温柔"], "likes": "蛋糕"}
    form = persona_to_character_form(persona)
    assert form["tags"] == ["非人类", "能干", "温柔"]
    assert "tags" not in cs_keys(form)
    # 其它有平台对应位的字段仍走兜底
    assert cs_get(form, "爱好") == "蛋糕"


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
        "name": "小樱",
        "profile": "温柔坚定的魔法少女",
        "gender": "女",
        "species": "人类",
        "personality": {"summary": "外柔内刚"},
        "opening": {"note": "hi", "messages": ["最近好吗？", "在忙什么"]},
        "backstory": "一段往事",
        "likes": "草莓蛋糕",
    }
    form = persona_to_character_form(persona)
    assert form["name"] == "小樱"
    assert form["profile"] == "温柔坚定的魔法少女"
    assert form["gender"] == "female"
    assert form["species"] == "人类"
    assert form["disposition"] == "外柔内刚"
    # opening 两句 → opening_prologue，output_type=text
    assert form["opening_prologue"] == [
        {"text": "最近好吗？", "output_type": "text"},
        {"text": "在忙什么", "output_type": "text"},
    ]
    # 未直接映射的字段进 customized_settings（新 IDL: []GeneralTagInfo）
    assert cs_get(form, "成长经历") == "一段往事"
    assert cs_get(form, "爱好") == "草莓蛋糕"
    # 一等字段不重复出现；每个元素带齐 GeneralTagInfo 必需键
    assert not set(cs_keys(form)) & {"name", "opening", "personality"}
    for item in form["customized_settings"]:
        assert set(item) == {"tag_key", "tag_name", "tag_icon", "tag_value", "index"}


def test_persona_to_form_personality_as_string():
    # 字符串 personality → disposition；personality 不再进 customized_settings
    persona = {
        "name": "阿朗",
        "personality": "沉稳内敛",
    }
    form = persona_to_character_form(persona)
    assert form["disposition"] == "沉稳内敛"
    assert "personality" not in form.get("customized_settings", {})

    # 空 dict personality → 同样不出现
    persona_empty = {
        "name": "测试",
        "personality": {},
    }
    form_empty = persona_to_character_form(persona_empty)
    assert "personality" not in form_empty.get("customized_settings", {})


def test_persona_to_form_images_and_landing():
    img = {"image_type": "aigc", "media": {"bucket_name": "b", "object_key": "k", "object_type": "image"}, "is_main_pic": True}
    form = persona_to_character_form({"name": "A"}, images=[img], landing_url="https://cdn/x.html")
    assert form["images"] == [img]
    assert form["landing_page_url"] == "https://cdn/x.html"
