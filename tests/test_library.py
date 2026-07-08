# -*- coding: utf-8 -*-
"""灵感库（性格/职业）领料-销账 + real track 发牌/验收接线的测试。"""
import json

from app import library


def test_libraries_loaded_and_flattened():
    assert len(library._ENTRIES["personality"]) > 100
    assert len(library._ENTRIES["occupation"]) > 100
    e = library._ENTRIES["personality"][0]
    assert set(e) == {"id", "dim", "group", "text"}
    assert e["id"].startswith("personality:")
    # 反派清单不进 real track 的牌池
    assert not any("反派角色分类" in e["group"]
                   for e in library._ENTRIES["personality"])


def test_checkout_hand_size_and_block(tmp_path):
    state = tmp_path / "state.json"
    hand = library.checkout(state_path=state)
    dims = [e["dim"] for e in hand]
    assert dims.count("personality") == library.HAND_SIZE["personality"]
    assert dims.count("occupation") == library.HAND_SIZE["occupation"]
    block = library.hand_block(hand, "zh")
    assert "灵感手牌" in block and "used_seeds" in block
    assert hand[0]["id"] in block
    block_ko = library.hand_block(hand, "ko")
    assert "영감 핸드" in block_ko and "used_seeds" in block_ko
    assert library.hand_block([], "zh") == ""


def test_commit_cooldown_cycle(tmp_path):
    state = tmp_path / "state.json"
    target = library._ENTRIES["personality"][0]["id"]
    # 非法 id 被过滤，合法 id 被记账
    assert library.commit([target, "bogus:1"], state_path=state) == [target]
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["counter"] == 1 and target in data["entries"]
    # 冷却期内不可用
    st = library._load_state(state)
    avail = {e["id"] for e in library._available("personality", st)}
    assert target not in avail
    # 冷却期过后回池
    for _ in range(library.COOLDOWNS["personality"]):
        library.commit([], state_path=state)
    st = library._load_state(state)
    avail = {e["id"] for e in library._available("personality", st)}
    assert target in avail


def test_real_track_wiring(monkeypatch, tmp_path):
    """real track 发牌+used_seeds 回收+冷读验收；light 不发牌不验收。"""
    from app import pipeline, config

    (tmp_path / "personas").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PERSONA_DIR", tmp_path / "personas")

    seen: list[list[dict]] = []
    hand_ids: list[str] = []

    def fake_chat_json(messages, **kw):
        seen.append(messages)
        sys_text = messages[0]["content"]
        if "从未见过这个角色生产过程" in sys_text:  # 冷读验收调用
            return {"retell": "她会把彩椒扣在眼睛上", "verdict": "pass",
                    "reason": "具体"}
        # 人设生成调用：如实回报手牌里第一条性格
        body = messages[1]["content"]
        text = body if isinstance(body, str) else body[0]["text"]
        used = []
        for line in text.splitlines():
            if line.startswith("- personality:"):
                used = [line.split()[1]]
                hand_ids.append(used[0])
                break
        return {"name": "测试", "gender": "女", "used_seeds": used}

    monkeypatch.setattr(pipeline.api_client, "chat_json", fake_chat_json)

    rec = pipeline.create_persona_one_lang([], "zh", track="real")
    gen_prompt = seen[0][1]["content"]
    gen_text = gen_prompt if isinstance(gen_prompt, str) else gen_prompt[0]["text"]
    assert "灵感手牌" in gen_text
    assert len(seen) == 2  # 生成 + 验收（pass 无重写）
    assert rec["used_seeds"] == hand_ids[:1]
    assert rec["charm_audit"][0]["verdict"] == "pass"
    assert "used_seeds" not in rec["persona"]
    # 销账落盘：counter=1 且条目进入冷却
    data = json.loads((tmp_path / "library_state.json").read_text("utf-8"))
    assert data["counter"] == 1 and hand_ids[0] in data["entries"]

    seen.clear()
    rec2 = pipeline.create_persona_one_lang([], "zh", track="light")
    gen2 = seen[0][1]["content"]
    gen2_text = gen2 if isinstance(gen2, str) else gen2[0]["text"]
    assert "灵感手牌" not in gen2_text
    assert len(seen) == 1  # 无验收调用
    assert rec2["used_seeds"] == [] and rec2["charm_audit"] is None


def test_real_track_audit_fail_retries(monkeypatch, tmp_path):
    from app import pipeline, config

    (tmp_path / "personas").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PERSONA_DIR", tmp_path / "personas")

    calls = {"gen": 0, "audit": 0}

    def fake_chat_json(messages, **kw):
        if "从未见过这个角色生产过程" in messages[0]["content"]:
            calls["audit"] += 1
            verdict = "fail" if calls["audit"] == 1 else "pass"
            return {"retell": "一个温柔的女生", "verdict": verdict,
                    "reason": "泛用形容词"}
        calls["gen"] += 1
        return {"name": f"测试{calls['gen']}", "gender": "女", "used_seeds": []}

    monkeypatch.setattr(pipeline.api_client, "chat_json", fake_chat_json)
    rec = pipeline.create_persona_one_lang([], "zh", track="real")
    assert calls == {"gen": 2, "audit": 2}  # 打回重写一次，重审一次
    assert rec["persona"]["name"] == "测试2"
    assert [a["verdict"] for a in rec["charm_audit"]] == ["fail", "pass"]
    # 重写调用里带了验收反馈
    assert rec["charm_audit"][0]["reason"] == "泛用形容词"


def test_selfie_ref_decoupled_from_source_for_real(tmp_path):
    """real track：没封面时不回退原图（防形似泄漏）；其他 track 保持原图兜底。"""
    from app import pipeline

    src = tmp_path / "s.png"
    src.write_bytes(b"fake")
    rec_real = {"track": "real", "source_images": [str(src)], "cover": {}}
    assert pipeline._ref_image_uri_for_selfie(rec_real) is None
    rec_light = {"track": "light", "source_images": [str(src)], "cover": {}}
    assert pipeline._ref_image_uri_for_selfie(rec_light) is not None
    # 有封面时两个 track 都锚封面
    cover = tmp_path / "c.png"
    cover.write_bytes(b"fake")
    rec_real["cover"] = {"local_path": str(cover)}
    assert pipeline._ref_image_uri_for_selfie(rec_real) is not None
