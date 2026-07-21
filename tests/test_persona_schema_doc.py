from app.prompts import _persona_schema_doc

OLD_KEYS = ('"social_status"', '"situational_reactions"', '"fears"',
            '"premise"', '"perception"', '"hidden_side"', '"family"')
NEW_KEYS = ('"value"', '"identity"', '"online_chat_style"', '"behavior_patterns"',
            '"inner_structure"', '"dislikes"', '"worldview"',
            '"relationship"', '"description"')
# personality 是因果鏈物件（dict），這些子欄位必須出現在 schema doc 裡
CHAIN_SUBKEYS = ('"summary"', '"decisive_event"', '"imprint"', '"response"',
                 '"cost"', '"desire_outer"', '"desire_inner"',
                 '"desire_bottom_line"', '"healing"')


def test_schema_doc_uses_new_keys_only_all_langs_tracks():
    for lang in ("zh", "ko"):
        for track in ("real", "light", "kdrama", ""):
            doc = _persona_schema_doc(lang, track=track or "light")
            for old in OLD_KEYS:
                assert old not in doc, f"{lang}/{track}: 殘留舊欄位 {old}"
            for new in NEW_KEYS:
                assert new in doc, f"{lang}/{track}: 缺新欄位 {new}"
            # personality 是因果鏈物件而非字串
            assert '"personality": {' in doc, f"{lang}/{track}: personality 應為物件"
            for sub in CHAIN_SUBKEYS:
                assert sub in doc, f"{lang}/{track}: 缺因果鏈子欄位 {sub}"
