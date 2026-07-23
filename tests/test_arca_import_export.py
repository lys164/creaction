import json

import pytest

from app.arca_import_export import (
    ImportCharacterContractError,
    SETTING_KEYS,
    assert_valid_import_character_req,
    build_import_character_req,
    validate_import_character_req,
    validate_landing_html,
)


def _page_config():
    return {
        "character_tags": [
            {"tag_key": "专情", "tag_name": "專情"},
            {"tag_key": "职场恋爱", "tag_name": "職場戀愛"},
        ],
        "voices": [{"voice_id": "voice_001", "voice_name": "示例音色"}],
        "setting_options": [{"tag_key": key} for key in SETTING_KEYS],
        "species": [
            {"tag_key": "人类", "tag_name": "人類"},
            {"tag_key": "精灵", "tag_name": "精靈"},
            {"tag_key": "兽人", "tag_name": "獸人"},
            {"tag_key": "动物", "tag_name": "動物"},
            {"tag_key": "其他", "tag_name": "其他"},
        ],
    }


def _record():
    return {
        "char_id": "ext_10086",
        "source": "popop-pipeline",
        # Producer-facing business category.  It is deliberately not part of
        # ImportCharacterReq, whose top-level shape is fixed by the Skill.
        "style": "cute",
        "lang": "zh",
        "persona": {
            "name": "林晚秋",
            "gender": "女",
            "species": "人類",
            "tags": ["專情", "职场恋爱"],
            "voice": "voice_001",
            "style": "cute",
            "value": "28岁 / 165cm / 常穿深色针织衫",
            "appearance": "微卷黑发，笑起来有酒窝。",
            "profile": "28岁的咖啡店老板，晚上写小说。",
            "personality": "温柔细腻，偶尔有点毒舌。",
            "visibility": "public",
            "identity": "咖啡店老板/小说作者",
            "likes": ["手冲咖啡", "写作"],
            "opening": {
                "note": "问问她小说的事。",
                "messages": [
                    {"type": "text", "data": {"content": "欢迎光临，{user}。"}},
                    {"type": "voice", "data": {"content": "今天想喝点什么？"}},
                ],
            },
        },
    }


def _main_image():
    return [{
        "image_type": "upload",
        "is_main_pic": True,
        "media": {
            "bucket_name": "private-bucket",
            "object_key": "character_import/popop/ext_10086/main.png",
            "object_type": "image",
        },
    }]


def test_builds_complete_import_character_req_from_existing_persona():
    request = build_import_character_req(
        _record(), page_config=_page_config(), images=_main_image(),
        landing_page_url="https://public.example/landing/index.html",
        provider="popop-pipeline",
    )

    assert set(request) == {
        "external_character_id", "provider", "character_create_form"}
    assert '"source":' not in json.dumps(request, ensure_ascii=False)
    assert request["external_character_id"] == "ext_10086"
    assert request["provider"] == "popop-pipeline"
    form = request["character_create_form"]
    assert "style" not in form
    assert form["gender"] == "female"
    assert form["species"] == "人类"
    assert form["tags"] == ["专情", "职场恋爱"]
    assert form["visibility"] == "public"
    assert form["opening_prologue"] == [
        {"text": "欢迎光临，{{user}}。", "output_type": "text"},
        {"text": "今天想喝点什么？", "output_type": "tts", "tts_resource_id": ""},
    ]
    assert {item["tag_key"] for item in form["customized_settings"]} == {
        "appearance", "identity", "likes"}
    appearance = next(item["tag_value"] for item in form["customized_settings"]
                      if item["tag_key"] == "appearance")
    assert "28岁 / 165cm" in appearance and "微卷黑发" in appearance
    assert_valid_import_character_req(
        request, page_config=_page_config(), public_asset_hosts={"public.example"})


def test_species_resolves_via_page_config_localized_names():
    """species 用 page_config 的 species 枚举反解本地化 tag_name → 规范 tag_key。"""
    ko_pc = dict(_page_config())
    ko_pc["species"] = [
        {"tag_key": "人类", "tag_name": "인간"},
        {"tag_key": "精灵", "tag_name": "엘프"},
        {"tag_key": "兽人", "tag_name": "오크"},
        {"tag_key": "动物", "tag_name": "동물"},
        {"tag_key": "其他", "tag_name": "다른"},
    ]
    cases = [
        (_page_config(), "獸人", "兽人"),   # 繁中 tag_name 反解
        (_page_config(), "其他", "其他"),   # 直接是 tag_key
        (ko_pc, "인간", "人类"),            # 韩语 tag_name 反解
        (ko_pc, "동물", "动物"),            # 韩语 tag_name 反解
        (ko_pc, "human", "人类"),           # page_config 无英文，走内建别名兜底
    ]
    for pc, raw, expected in cases:
        record = _record()
        record["persona"]["species"] = raw
        request = build_import_character_req(
            record, page_config=pc, images=_main_image(),
            landing_page_url=None, provider="popop-pipeline")
        assert request["character_create_form"]["species"] == expected, raw


def test_validator_rejects_non_contract_values_instead_of_repairing_them():
    request = build_import_character_req(
        _record(), page_config=_page_config(), images=_main_image(),
        landing_page_url=None, provider="popop-pipeline")
    form = request["character_create_form"]
    form["tags"] = ["高冷"]
    form["voice_id"] = "invented-voice"
    form["images"][0]["is_main_pic"] = False

    paths = {issue.path for issue in validate_import_character_req(
        request, page_config=_page_config())}
    # voice_id 与主图仍是硬违约（SKILL 铁律 1/2 的必填与音色校验对导入无条件生效）
    assert "request.character_create_form.voice_id" in paths
    assert "request.character_create_form.images" in paths
    # SKILL 口径：tag 属软校验（不在集合内只记日志、不拒），集合外 tag 不进违约
    assert "request.character_create_form.tags[0]" not in paths


def test_tts_row_user_placeholder_is_not_a_violation():
    """SKILL 口径：tts 行的 {{user}} 由后端合成时剔除、存库文本同步改写，非违约。

    构建阶段应把 tts 行的 {{user}} 剔除；即便直接塞进 form 也不应被校验硬拒。"""
    request = build_import_character_req(
        _record(), page_config=_page_config(), images=_main_image(),
        landing_page_url=None, provider="popop-pipeline")
    form = request["character_create_form"]
    # 构建阶段：tts 行不应残留 {{user}}
    for row in form.get("opening_prologue", []):
        if row.get("output_type") == "tts":
            assert "{{user}}" not in row["text"]
    # 校验阶段：即便人为塞入，也不再作为违约
    tts_rows = [i for i, r in enumerate(form.get("opening_prologue", []))
                if r.get("output_type") == "tts"]
    if tts_rows:
        idx = tts_rows[0]
        form["opening_prologue"][idx]["text"] = "老样子？{{user}}"
        paths = {issue.path for issue in validate_import_character_req(
            request, page_config=_page_config())}
        assert f"request.character_create_form.opening_prologue[{idx}].text" not in paths


def test_landing_validator_rejects_inline_and_non_public_assets():
    bad = '''<html><body>
      <img src="data:image/png;base64,AAAA">
      <audio src="https://third-party.example/audio.mp3"></audio>
      <button>播放</button><script>document.querySelector('audio').play()</script>
    </body></html>'''
    paths = {issue.path for issue in validate_landing_html(bad, {"public.example"})}
    assert "landing.html" in paths
    assert "landing.html <img src>" in paths
    assert "landing.html <audio src>" in paths


def test_export_pipeline_writes_only_the_import_contract(monkeypatch):
    from app import arca_client, pipeline

    saved = {}
    calls = []

    def fake_upload(data, object_key, content_type, lang, public=False):
        calls.append((object_key, content_type, public))
        return {
            "bucket_name": "public-bucket" if public else "private-bucket",
            "object_key": object_key,
            "object_type": "image",
            "url": f"https://public.example/{object_key}",
        }

    monkeypatch.setattr(pipeline, "load_character", lambda _id: _record())
    monkeypatch.setattr(pipeline, "load_latest_landing", lambda _id: {
        "html": "<html><body>landing</body></html>"})
    monkeypatch.setattr(pipeline, "_image_bytes", lambda _cover: b"cover")
    monkeypatch.setattr(
        pipeline, "_tos_public_url",
        lambda _data, object_key, _lang: f"https://public.example/{object_key}")
    monkeypatch.setattr(pipeline, "save_character", lambda record: saved.update(record))
    monkeypatch.setattr(arca_client, "get_page_config_cached", lambda _lang: _page_config())
    monkeypatch.setattr(arca_client, "public_tos_hosts", lambda _lang: {"public.example"})
    monkeypatch.setattr(arca_client, "tos_upload", fake_upload)

    _, files, report = pipeline._build_character_files("ext_10086")
    exported = json.loads(dict(files)["character.json"])

    assert set(exported) == {
        "external_character_id", "provider", "character_create_form"}
    assert exported["external_character_id"] == "ext_10086"
    assert exported["provider"] == "popop-pipeline"
    assert exported["character_create_form"]["images"][0]["is_main_pic"] is True
    assert report["images"] == 1
    assert saved["exported"] is True
    assert calls == [
        ("character_import/popop-pipeline/ext_10086/main.png", "image/png", False),
        ("landing_import/popop-pipeline/ext_10086/index.html", "text/html; charset=utf-8", True),
    ]


def test_export_pipeline_stops_before_uploading_for_invalid_persona(monkeypatch):
    from app import arca_client, pipeline

    invalid = _record()
    invalid["persona"]["voice"] = "not-in-page-config"
    monkeypatch.setattr(pipeline, "load_character", lambda _id: invalid)
    monkeypatch.setattr(pipeline, "_image_bytes", lambda _cover: b"cover")
    monkeypatch.setattr(arca_client, "get_page_config_cached", lambda _lang: _page_config())
    monkeypatch.setattr(arca_client, "public_tos_hosts", lambda _lang: {"public.example"})
    monkeypatch.setattr(
        arca_client, "tos_upload",
        lambda *_args, **_kwargs: pytest.fail("invalid output must not upload assets"),
    )

    with pytest.raises(ImportCharacterContractError, match="voice_id"):
        pipeline._build_character_files("ext_10086")
