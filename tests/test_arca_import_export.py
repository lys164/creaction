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


def test_validator_rejects_non_contract_values_instead_of_repairing_them():
    request = build_import_character_req(
        _record(), page_config=_page_config(), images=_main_image(),
        landing_page_url=None, provider="popop-pipeline")
    form = request["character_create_form"]
    form["tags"] = ["高冷"]
    form["voice_id"] = "invented-voice"
    form["images"][0]["is_main_pic"] = False
    form["opening_prologue"][1]["text"] = "你好，{{user}}"

    paths = {issue.path for issue in validate_import_character_req(
        request, page_config=_page_config())}
    assert "request.character_create_form.tags[0]" in paths
    assert "request.character_create_form.voice_id" in paths
    assert "request.character_create_form.images" in paths
    assert "request.character_create_form.opening_prologue[1].text" in paths


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
