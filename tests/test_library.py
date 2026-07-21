# -*- coding: utf-8 -*-
"""靈感庫（性格/職業）領料-銷賬 + real track 發牌/驗收接線的測試。"""
import json

import pytest

from app import library


def test_libraries_loaded_and_flattened():
    assert len(library._ENTRIES["personality"]) > 100
    assert len(library._ENTRIES["occupation"]) > 100
    e = library._ENTRIES["personality"][0]
    assert set(e) == {"id", "dim", "group", "text"}
    assert e["id"].startswith("personality:")
    # 反派清單不進 real track 的牌池
    assert not any("反派角色分類" in e["group"]
                   for e in library._ENTRIES["personality"])


def test_checkout_hand_size_and_block(tmp_path):
    state = tmp_path / "state.json"
    hand = library.checkout(state_path=state)
    dims = [e["dim"] for e in hand]
    assert dims.count("personality") == library.HAND_SIZE["personality"]
    assert dims.count("occupation") == library.HAND_SIZE["occupation"]
    block = library.hand_block(hand, "zh")
    assert "靈感手牌" in block and "used_seeds" in block
    assert hand[0]["id"] in block
    block_ko = library.hand_block(hand, "ko")
    assert "영감 핸드" in block_ko and "used_seeds" in block_ko
    assert library.hand_block([], "zh") == ""


def test_commit_cooldown_cycle(tmp_path):
    state = tmp_path / "state.json"
    target = library._ENTRIES["personality"][0]["id"]
    # 非法 id 被過濾，合法 id 被記賬
    assert library.commit([target, "bogus:1"], state_path=state) == [target]
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["counter"] == 1 and target in data["entries"]
    # 冷卻期內不可用
    st = library._load_state(state)
    avail = {e["id"] for e in library._available("personality", st)}
    assert target not in avail
    # 冷卻期過後回池
    for _ in range(library.COOLDOWNS["personality"]):
        library.commit([], state_path=state)
    st = library._load_state(state)
    avail = {e["id"] for e in library._available("personality", st)}
    assert target in avail


def test_real_track_wiring(monkeypatch, tmp_path):
    """real 發牌+used_seeds 回收+冷讀驗收；light 只驗收不發牌；kdrama 不發牌不驗收。"""
    from app import pipeline, config

    (tmp_path / "personas").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PERSONA_DIR", tmp_path / "personas")

    seen: list[list[dict]] = []
    hand_ids: list[str] = []

    def fake_chat_json(messages, **kw):
        seen.append(messages)
        sys_text = messages[0]["content"]
        if "從未見過這個角色生產過程" in sys_text:  # 冷讀驗收呼叫
            return {"retell": "她會把彩椒扣在眼睛上", "verdict": "pass",
                    "reason": "具體"}
        # 人設生成呼叫：如實回報手牌裡第一條性格
        body = messages[1]["content"]
        text = body if isinstance(body, str) else body[0]["text"]
        used = []
        for line in text.splitlines():
            if line.startswith("- personality:"):
                used = [line.split()[1]]
                hand_ids.append(used[0])
                break
        return {"name": "測試", "gender": "女", "used_seeds": used}

    monkeypatch.setattr(pipeline.api_client, "chat_json", fake_chat_json)

    rec = pipeline.create_persona_one_lang([], "zh", track="real")
    gen_prompt = seen[0][1]["content"]
    gen_text = gen_prompt if isinstance(gen_prompt, str) else gen_prompt[0]["text"]
    assert "靈感手牌" in gen_text
    assert len(seen) == 2  # 生成 + 驗收（pass 無重寫）
    assert rec["used_seeds"] == hand_ids[:1]
    assert rec["charm_audit"][0]["verdict"] == "pass"
    assert "used_seeds" not in rec["persona"]
    # 銷賬落盤：counter=1 且條目進入冷卻
    data = json.loads((tmp_path / "library_state.json").read_text("utf-8"))
    assert data["counter"] == 1 and hand_ids[0] in data["entries"]

    # light 對齊 real 的魅力方法論並走冷讀驗收，但【不發靈感手牌】（使用者明確不加職業/性格靈感）
    seen.clear()
    rec2 = pipeline.create_persona_one_lang([], "zh", track="light")
    gen2 = seen[0][1]["content"]
    gen2_text = gen2 if isinstance(gen2, str) else gen2[0]["text"]
    assert "靈感手牌" not in gen2_text
    assert len(seen) == 2  # 生成 + 驗收
    assert rec2["used_seeds"] == []
    assert rec2["charm_audit"][0]["verdict"] == "pass"

    # kdrama 仍不發牌不驗收
    seen.clear()
    rec3 = pipeline.create_persona_one_lang([], "zh", track="kdrama")
    gen3 = seen[0][1]["content"]
    gen3_text = gen3 if isinstance(gen3, str) else gen3[0]["text"]
    assert "靈感手牌" not in gen3_text
    assert len(seen) == 1  # 無驗收呼叫
    assert rec3["used_seeds"] == [] and rec3["charm_audit"] is None


def test_producer_business_style_is_validated_and_saved(monkeypatch, tmp_path):
    """`style` is producer metadata, independent from source and image style."""
    from app import config, pipeline

    (tmp_path / "personas").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PERSONA_DIR", tmp_path / "personas")
    monkeypatch.setattr(
        pipeline, "_generate_persona_real",
        lambda *_args: ({"name": "測試", "gender": "女"}, [], None),
    )
    monkeypatch.setattr(pipeline, "_randomize_voice", lambda persona, _lang: persona)

    record = pipeline.create_persona_one_lang(
        [], "zh", track="adult", source="imported-source", style="FANTASY")

    assert record["source"] == "imported-source"
    assert record["style"] == "fantasy"
    assert record["persona"]["style"] == "fantasy"
    assert pipeline.normalize_production_style(" cute ") == "cute"
    with pytest.raises(ValueError, match="fantasy, cute, real"):
        pipeline.normalize_production_style("watercolor")


def test_real_track_audit_fail_retries(monkeypatch, tmp_path):
    from app import pipeline, config

    (tmp_path / "personas").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PERSONA_DIR", tmp_path / "personas")

    calls = {"gen": 0, "audit": 0}

    def fake_chat_json(messages, **kw):
        if "從未見過這個角色生產過程" in messages[0]["content"]:
            calls["audit"] += 1
            verdict = "fail" if calls["audit"] == 1 else "pass"
            return {"retell": "一個溫柔的女生", "verdict": verdict,
                    "reason": "泛用形容詞"}
        calls["gen"] += 1
        return {"name": f"測試{calls['gen']}", "gender": "女", "used_seeds": []}

    monkeypatch.setattr(pipeline.api_client, "chat_json", fake_chat_json)
    rec = pipeline.create_persona_one_lang([], "zh", track="real")
    assert calls == {"gen": 2, "audit": 2}  # 打回重寫一次，重審一次
    assert rec["persona"]["name"] == "測試2"
    assert [a["verdict"] for a in rec["charm_audit"]] == ["fail", "pass"]
    # 重寫呼叫裡帶了驗收反饋
    assert rec["charm_audit"][0]["reason"] == "泛用形容詞"


def test_selfie_ref_decoupled_from_source_for_real(tmp_path):
    """real/kdrama/light：沒封面時不回退原圖（防形似洩漏）；adult 保持原圖兜底。"""
    from app import pipeline

    src = tmp_path / "s.png"
    src.write_bytes(b"fake")
    rec_real = {"track": "real", "source_images": [str(src)], "cover": {}}
    assert pipeline._ref_image_uri_for_selfie(rec_real) is None
    rec_light = {"track": "light", "source_images": [str(src)], "cover": {}}
    assert pipeline._ref_image_uri_for_selfie(rec_light) is None
    rec_adult = {"track": "adult", "source_images": [str(src)], "cover": {}}
    assert pipeline._ref_image_uri_for_selfie(rec_adult) is not None
    # 有封面時兩個 track 都錨封面
    cover = tmp_path / "c.png"
    cover.write_bytes(b"fake")
    rec_real["cover"] = {"local_path": str(cover)}
    assert pipeline._ref_image_uri_for_selfie(rec_real) is not None
