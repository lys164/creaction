import importlib

import pytest


def _mod(monkeypatch):
    for k, v in {"ARCA_JWT_MODE": "local", "ARCA_ACCESS_SECRET": "s",
                 "ARCA_UID": "u1", "ARCA_BASE_URL": "https://arca.test",
                 "ARCA_SYNC_LANDING": "0"}.items():
        monkeypatch.setenv(k, v)
    import app.config as config
    importlib.reload(config)
    import app.arca_sync as sync
    importlib.reload(sync)
    # 测试不打真网络：page_config 一律返回空(跳过枚举对齐)；create 后的存活校验默认通过
    monkeypatch.setattr(sync.arca_client, "get_page_config_cached", lambda lang: {})
    monkeypatch.setattr(sync.arca_client, "character_exists", lambda cid, lang, **kw: True)
    monkeypatch.setattr(sync.arca_client, "list_my_characters", lambda lang: [])
    return sync


def test_sync_character_happy_path(monkeypatch, tmp_path):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "ja",
              "persona": {"name": "A", "gender": "女", "voice": "voice-123"},
              "cover": {"local_path": str(tmp_path / "cover.png")}}
    (tmp_path / "cover.png").write_bytes(b"PNG")

    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: saved.update(r))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [
        {"post_id": "post-1", "content": {"ja": "こんにちは"},
         "image": {"local_path": str(tmp_path / "p1.png")}}])
    (tmp_path / "p1.png").write_bytes(b"PNG")

    seen_form = {}
    monkeypatch.setattr(sync.arca_client, "tos_upload",
                        lambda data, key, ct, lang, public=False: {
                            "bucket_name": "b", "object_key": key, "object_type": "image", "url": "u"})

    def fake_create_character(form, lang, idempotency_key=None, **kw):
        seen_form.update(form)
        return "arca-c1"

    monkeypatch.setattr(sync.arca_client, "create_character", fake_create_character)
    monkeypatch.setattr(sync.arca_client, "create_post",
                        lambda cid, content, imgs, lang, visibility=None: "arca-p1")

    res = sync.sync_character("c1", sync_posts=True)
    assert res["arca_character_id"] == "arca-c1"
    assert res["posts"] == [{"post_id": "post-1", "arca_post_id": "arca-p1"}]
    assert not res["errors"]
    # 回写本地
    assert saved["arca_character_id"] == "arca-c1"
    assert saved["arca_post_ids"]["post-1"] == "arca-p1"
    # 封面必须包成 UserUploadImage 且 is_main_pic=True（arca 硬校验主图；裸 StorageObject 会 400）
    assert seen_form["images"][0]["is_main_pic"] is True
    assert seen_form["images"][0]["image_type"] == "aigc"
    assert seen_form["images"][0]["media"]["object_key"] == "creaction/c1/cover.png"
    # voice_id 必须透传（arca 硬校验「请选择音色」）
    assert seen_form["voice_id"] == "voice-123"


def test_sync_character_default_skips_posts(monkeypatch, tmp_path):
    # 默认(sync_posts=False)只同步角色本体，不应调 create_post
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "zh",
              "persona": {"name": "A", "voice": "v1"}}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: None)
    monkeypatch.setattr(sync, "_upload_cover", lambda rec, lang: list(_FAKE_MAIN_PIC))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [
        {"post_id": "p1", "content": {"zh": "一"}, "image": None}])
    monkeypatch.setattr(sync.arca_client, "create_character",
                        lambda form, lang, idempotency_key=None, **kw: "arca-c1")

    def _should_not_post(*a, **k):
        raise AssertionError("默认不应同步帖子")
    monkeypatch.setattr(sync.arca_client, "create_post", _should_not_post)

    res = sync.sync_character("c1")
    assert res["arca_character_id"] == "arca-c1"
    assert res["posts"] == []
    assert not res["errors"]


def test_sync_character_fails_fast_without_cover_or_voice(monkeypatch, tmp_path):
    sync = _mod(monkeypatch)
    # 无 cover、无 voice：不应发起 create_character，errors 给出可操作提示
    record = {"char_id": "c1", "lang": "zh", "persona": {"name": "A"}}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: None)
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])

    def _should_not_call(*a, **k):
        raise AssertionError("缺主图/音色时不应调 create_character")
    monkeypatch.setattr(sync.arca_client, "create_character", _should_not_call)

    res = sync.sync_character("c1")
    assert res["arca_character_id"] is None
    assert any("封面" in e for e in res["errors"])


def test_sync_character_updates_in_place_when_already_synced(monkeypatch):
    sync = _mod(monkeypatch)
    # 已同步但指纹缺失/过期 → 走 updateBasicInfo，不建新角色
    record = {"char_id": "c1", "lang": "zh", "persona": {"name": "A", "voice": "v1"},
              "arca_character_id": "existing"}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: saved.update(r))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])
    monkeypatch.setattr(sync.arca_client, "list_my_characters",
                        lambda lang: [{"character_id": "existing", "name": "A"}])

    def _should_not_call(*a, **k):
        raise AssertionError("已同步的角色不应走 create")
    monkeypatch.setattr(sync.arca_client, "create_character", _should_not_call)

    called = {}

    def fake_update(cid, form, lang):
        called.update(cid=cid, form=form)
    monkeypatch.setattr(sync.arca_client, "update_character_basic_info", fake_update)

    res = sync.sync_character("c1", force=False)
    assert res["updated"] is True
    assert res["skipped"] is False
    assert called["cid"] == "existing"
    assert called["form"]["name"] == "A"
    assert saved["arca_form_digest"]  # 指纹已回写


def test_sync_character_skips_when_form_unchanged(monkeypatch):
    sync = _mod(monkeypatch)
    import app.arca_mapping as am
    import hashlib as _h
    import json as _j
    persona = {"name": "A", "voice": "v1"}
    digest = _h.md5(_j.dumps(am.persona_to_character_form(persona),
                             ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]
    record = {"char_id": "c1", "lang": "zh", "persona": persona,
              "arca_character_id": "existing", "arca_form_digest": digest}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: None)
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])
    monkeypatch.setattr(sync.arca_client, "list_my_characters",
                        lambda lang: [{"character_id": "existing", "name": "A"}])

    def _boom(*a, **k):
        raise AssertionError("无变化不应调 update/create")
    monkeypatch.setattr(sync.arca_client, "create_character", _boom)
    monkeypatch.setattr(sync.arca_client, "update_character_basic_info", _boom)

    res = sync.sync_character("c1", force=False)
    assert res["skipped"] is True
    assert res["updated"] is False
    assert res["arca_character_id"] == "existing"


def test_update_stale_character_self_heals_by_recreating(monkeypatch):
    sync = _mod(monkeypatch)
    # 竞态场景：列表仍返回同名角色，但 update 时它已被删（报「角色不存在」）
    # → 自动清映射并重建
    record = {"char_id": "c1", "lang": "zh",
              "persona": {"name": "A", "voice": "v1"},
              "arca_character_id": "dead-cid",
              "arca_post_ids": {"p1": "old-post"}}
    saved = {}
    monkeypatch.setattr(sync.arca_client, "list_my_characters",
                        lambda lang: [{"character_id": "dead-cid", "name": "A"}])
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: (saved.clear(), saved.update(r)))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])
    monkeypatch.setattr(sync, "_upload_cover", lambda rec, lang: list(_FAKE_MAIN_PIC))

    def dead_update(cid, form, lang):
        raise sync.arca_client.ArcaError("arca 业务错误 code=-1 msg=角色不存在")
    monkeypatch.setattr(sync.arca_client, "update_character_basic_info", dead_update)

    seen_key = {}

    def fake_create(form, lang, idempotency_key=None, **kw):
        seen_key["key"] = idempotency_key
        return "fresh-cid"
    monkeypatch.setattr(sync.arca_client, "create_character", fake_create)

    res = sync.sync_character("c1")
    assert res["arca_character_id"] == "fresh-cid"
    assert any("自动重建" in e for e in res["errors"])
    # 幂等键必须换代，避免 arca 幂等缓存回放出已删除的旧角色 id
    assert seen_key["key"].endswith("-g1")
    # 旧帖子映射清空
    assert saved.get("arca_post_ids") in (None, {})


def test_force_rebuild_clears_stale_post_mapping(monkeypatch):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "zh",
              "persona": {"name": "A", "voice": "v1"},
              "arca_character_id": "old-cid",
              "arca_post_ids": {"p1": "old-post"}}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: (saved.clear(), saved.update(r)))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])
    monkeypatch.setattr(sync, "_upload_cover", lambda rec, lang: list(_FAKE_MAIN_PIC))
    monkeypatch.setattr(sync.arca_client, "create_character",
                        lambda form, lang, idempotency_key=None, **kw: "new-cid")

    res = sync.sync_character("c1", force=True)
    assert res["arca_character_id"] == "new-cid"
    # 旧角色的帖子映射必须清空，否则新角色名下帖子会被误跳过
    assert "arca_post_ids" not in saved or saved["arca_post_ids"] == {}


def test_create_character_failure_captured_in_errors(monkeypatch):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "zh", "persona": {"name": "A"}}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: saved.update(r))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])

    def _boom(*a, **k):
        raise sync.arca_client.ArcaError("余额不足")
    monkeypatch.setattr(sync.arca_client, "create_character", _boom)

    res = sync.sync_character("c1")  # 不应 raise
    assert res["arca_character_id"] is None
    assert any("建角色失败" in e for e in res["errors"])
    assert res["posts"] == []


def test_remove_from_arca_deletes_and_clears_mapping(monkeypatch):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "ja", "persona": {"name": "A"},
              "arca_character_id": "cid-1", "arca_form_digest": "d",
              "arca_post_ids": {"p1": "ap1"}}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: (saved.clear(), saved.update(r)))
    called = {}
    monkeypatch.setattr(sync.arca_client, "delete_character",
                        lambda cid, lang, reason="": called.update(cid=cid, lang=lang))

    res = sync.remove_from_arca("c1")
    assert res["deleted"] is True and not res["errors"]
    assert called["cid"] == "cid-1" and called["lang"] == "ja"
    # 本地映射清空（角色数据保留），并换代供下次重导避开幂等回放
    for k in ("arca_character_id", "arca_form_digest", "arca_post_ids"):
        assert k not in saved
    assert saved["persona"] == {"name": "A"}
    assert saved["arca_rebuild_gen"] == 1


def test_remove_from_arca_tolerates_already_deleted(monkeypatch):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "zh", "persona": {"name": "A"},
              "arca_character_id": "cid-1"}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: (saved.clear(), saved.update(r)))

    def gone(cid, lang, reason=""):
        raise sync.arca_client.ArcaError("arca 业务错误 code=-1 msg=角色不存在")
    monkeypatch.setattr(sync.arca_client, "delete_character", gone)

    res = sync.remove_from_arca("c1")
    assert res["deleted"] is True and not res["errors"]  # 幂等成功
    assert "arca_character_id" not in saved


def test_remove_from_arca_keeps_mapping_on_real_failure(monkeypatch):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "zh", "persona": {"name": "A"},
              "arca_character_id": "cid-1"}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))

    def _no_save(r):
        raise AssertionError("删除失败时不应改写本地映射")
    monkeypatch.setattr(sync.pipeline, "save_character", _no_save)
    monkeypatch.setattr(sync.arca_client, "delete_character",
                        lambda cid, lang, reason="": (_ for _ in ()).throw(
                            sync.arca_client.ArcaError("网络超时")))

    res = sync.remove_from_arca("c1")
    assert res["deleted"] is False
    assert any("删除失败" in e for e in res["errors"])


def test_remove_from_arca_skips_never_synced(monkeypatch):
    sync = _mod(monkeypatch)
    monkeypatch.setattr(sync.pipeline, "load_character",
                        lambda cid: {"char_id": "c1", "lang": "zh", "persona": {}})
    res = sync.remove_from_arca("c1")
    assert res["skipped"] is True and res["deleted"] is False


_FAKE_MAIN_PIC = [{"image_type": "aigc", "is_main_pic": True,
                   "media": {"bucket_name": "b", "object_key": "k", "object_type": "image"}}]


def test_landing_failure_does_not_block_character(monkeypatch):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "zh", "persona": {"name": "A", "voice": "v1"}}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: None)
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])
    monkeypatch.setattr(sync, "_upload_cover", lambda rec, lang: list(_FAKE_MAIN_PIC))

    def _land_boom(rec, lang):
        raise RuntimeError("cdn down")
    monkeypatch.setattr(sync, "_upload_landing", _land_boom)
    monkeypatch.setattr(sync.arca_client, "create_character",
                        lambda form, lang, idempotency_key=None, **kw: "arca-c1")

    res = sync.sync_character("c1", sync_landing=True)
    assert res["arca_character_id"] == "arca-c1"  # 落地页失败不阻断建角色
    assert res["landing_url"] is None
    assert any("landing 上传失败" in e for e in res["errors"])


def test_post_failure_does_not_halt_other_posts(monkeypatch):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "zh", "persona": {"name": "A", "voice": "v1"}}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: saved.update(r))
    monkeypatch.setattr(sync, "_upload_cover", lambda rec, lang: list(_FAKE_MAIN_PIC))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [
        {"post_id": "p1", "content": {"zh": "一"}, "image": None},
        {"post_id": "p2", "content": {"zh": "二"}, "image": None},
    ])
    monkeypatch.setattr(sync.arca_client, "create_character",
                        lambda form, lang, idempotency_key=None, **kw: "arca-c1")

    def _post(cid, content, imgs, lang, visibility=None):
        if content == "一":
            raise sync.arca_client.ArcaError("审核不通过")
        return "arca-p2"
    monkeypatch.setattr(sync.arca_client, "create_post", _post)

    res = sync.sync_character("c1", sync_posts=True)
    assert res["arca_character_id"] == "arca-c1"
    assert {"post_id": "p2", "arca_post_id": "arca-p2"} in res["posts"]
    assert any("帖子 p1 同步失败" in e for e in res["errors"])
    assert saved["arca_post_ids"] == {"p2": "arca-p2"}


def test_resync_after_remove_uses_new_generation_key(monkeypatch, tmp_path):
    sync = _mod(monkeypatch)
    # remove_from_arca 之后的 record：映射已清、gen=1
    record = {"char_id": "c1", "lang": "zh",
              "persona": {"name": "A", "voice": "v1"}, "arca_rebuild_gen": 1}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: (saved.clear(), saved.update(r)))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])
    monkeypatch.setattr(sync, "_upload_cover", lambda rec, lang: list(_FAKE_MAIN_PIC))
    seen = {}

    def fake_create(form, lang, idempotency_key=None, **kw):
        seen["key"] = idempotency_key
        return "fresh"
    monkeypatch.setattr(sync.arca_client, "create_character", fake_create)

    res = sync.sync_character("c1")
    assert res["arca_character_id"] == "fresh"
    assert seen["key"].endswith("-g1")  # 换代键，不会命中删除前的幂等缓存
    assert saved["arca_rebuild_gen"] == 1


def test_create_replaying_dead_character_retries_with_next_gen(monkeypatch, tmp_path):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "zh",
              "persona": {"name": "A", "voice": "v1"}}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: (saved.clear(), saved.update(r)))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])
    monkeypatch.setattr(sync, "_upload_cover", lambda rec, lang: list(_FAKE_MAIN_PIC))

    keys = []

    def fake_create(form, lang, idempotency_key=None, **kw):
        keys.append(idempotency_key)
        return "dead-cid" if len(keys) == 1 else "fresh-cid"
    monkeypatch.setattr(sync.arca_client, "create_character", fake_create)
    # 第一次返回的 cid 已死（幂等回放）→ 换代重试
    monkeypatch.setattr(sync.arca_client, "character_exists",
                        lambda cid, lang, **kw: cid != "dead-cid")

    res = sync.sync_character("c1")
    assert res["arca_character_id"] == "fresh-cid"
    assert len(keys) == 2
    assert not keys[0].endswith("-g1") and keys[1].endswith("-g1")
    assert any("换代重建" in e for e in res["errors"])
    assert saved["arca_rebuild_gen"] == 1


def test_name_match_rebinds_to_remote_same_name_character(monkeypatch):
    sync = _mod(monkeypatch)
    # 本地记录指向 old-cid，但远端同名角色是 remote-cid → 换绑并原地更新，帖子挂 remote-cid
    record = {"char_id": "c1", "lang": "zh",
              "persona": {"name": "小樱", "voice": "v1"},
              "arca_character_id": "old-cid", "arca_form_digest": "stale",
              "arca_post_ids": {"p0": "old-post"}}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: (saved.clear(), saved.update(r)))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [
        {"post_id": "p1", "content": {"zh": "你好"}, "image": None}])
    monkeypatch.setattr(sync.arca_client, "list_my_characters",
                        lambda lang: [{"character_id": "remote-cid", "name": "小樱"}])
    updated = {}
    monkeypatch.setattr(sync.arca_client, "update_character_basic_info",
                        lambda cid, form, lang: updated.update(cid=cid))
    posted = {}
    monkeypatch.setattr(sync.arca_client, "create_post",
                        lambda cid, content, imgs, lang, visibility=None:
                        (posted.update(cid=cid), "new-post")[1])

    def _no_create(*a, **k):
        raise AssertionError("存在同名角色时不应新建")
    monkeypatch.setattr(sync.arca_client, "create_character", _no_create)

    res = sync.sync_character("c1", sync_posts=True)
    assert res["arca_character_id"] == "remote-cid"
    assert updated["cid"] == "remote-cid"      # 原地更新同名角色
    assert posted["cid"] == "remote-cid"       # 帖子挂原(同名)角色
    assert saved["arca_character_id"] == "remote-cid"
    assert saved["arca_post_ids"] == {"p1": "new-post"}  # 旧映射已清、新帖挂新角色


def test_name_match_missing_remote_clears_stale_and_creates(monkeypatch, tmp_path):
    sync = _mod(monkeypatch)  # list_my_characters 默认 [] = 远端无同名
    record = {"char_id": "c1", "lang": "zh",
              "persona": {"name": "小樱", "voice": "v1"},
              "arca_character_id": "stale-cid", "arca_form_digest": "d"}
    saved = {}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: (saved.clear(), saved.update(r)))
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])
    monkeypatch.setattr(sync, "_upload_cover", lambda rec, lang: list(_FAKE_MAIN_PIC))
    seen = {}

    def fake_create(form, lang, idempotency_key=None, **kw):
        seen["key"] = idempotency_key
        return "fresh-cid"
    monkeypatch.setattr(sync.arca_client, "create_character", fake_create)

    def _no_update(*a, **k):
        raise AssertionError("远端无同名角色时不应走 update")
    monkeypatch.setattr(sync.arca_client, "update_character_basic_info", _no_update)

    res = sync.sync_character("c1")
    assert res["arca_character_id"] == "fresh-cid"
    assert "-g1" in seen["key"]  # 清过期映射后换代，避免幂等回放旧角色


def test_name_match_failure_falls_back_to_local_mapping(monkeypatch):
    sync = _mod(monkeypatch)
    record = {"char_id": "c1", "lang": "zh", "persona": {"name": "A", "voice": "v1"},
              "arca_character_id": "existing"}
    monkeypatch.setattr(sync.pipeline, "load_character", lambda cid: dict(record))
    monkeypatch.setattr(sync.pipeline, "save_character", lambda r: None)
    monkeypatch.setattr(sync, "_latest_posts", lambda cid: [])

    def _boom(lang):
        raise sync.arca_client.ArcaError("列表接口超时")
    monkeypatch.setattr(sync.arca_client, "list_my_characters", _boom)
    monkeypatch.setattr(sync.arca_client, "update_character_basic_info",
                        lambda cid, form, lang: None)

    res = sync.sync_character("c1")
    # fail-open：回退本地映射走 update，错误有记录但不阻断
    assert res["arca_character_id"] == "existing"
    assert any("按名匹配失败" in e for e in res["errors"])
