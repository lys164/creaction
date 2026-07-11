from app.prompts import _persona_schema_doc

OLD_KEYS = ('"social_status"', '"situational_reactions"', '"fears"',
            '"premise"', '"perception"', '"hidden_side"', '"family"',
            '"summary"', '"decisive_event"', '"desire_outer"', '"healing"')
NEW_KEYS = ('"value"', '"identity"', '"online_chat_style"', '"behavior_patterns"',
            '"inner_structure"', '"dislikes"', '"worldview"',
            '"relationship"', '"description"')


def test_schema_doc_uses_new_keys_only_all_langs_tracks():
    for lang in ("zh", "ko"):
        for track in ("real", "light", "kdrama", ""):
            doc = _persona_schema_doc(lang, track=track or "light")
            for old in OLD_KEYS:
                assert old not in doc, f"{lang}/{track}: 残留旧字段 {old}"
            for new in NEW_KEYS:
                assert new in doc, f"{lang}/{track}: 缺新字段 {new}"
            # personality 是字符串字段而非对象
            assert '"personality": {' not in doc
