import importlib.util
import json
from pathlib import Path

import pytest

from app import api_client, daily, feed_posts, prompts


def test_t2_prompt_drops_title_and_keeps_plain_language_rules():
    messages = feed_posts._build_t2_messages(
        {"name": "測試角色", "occupation": "氣象站觀測員"},
        "使用者", "", {},
    )
    prompt = messages[1]["content"]

    assert '"title": {' not in prompt
    assert "關鍵事實用具體、日常的詞說清楚" in prompt
    assert "第一行＝卡片鉤子行" in prompt
    # 每帖必配：mandate 只在標題點一次，schema 也沒有 none 選項
    assert "配圖（每帖必配一張" in prompt
    assert "就不配" not in prompt
    assert "|none" not in prompt


def test_image_prompt_always_requires_an_image():
    # 產品決策：一定出圖，已刪除「優先配圖」開關。prompt 恒定要求每帖必配、無 none 出口。
    messages = feed_posts._build_t2_messages(
        {"name": "測試角色"}, "使用者", "", {},
    )
    prompt = messages[1]["content"]

    assert "配圖（每帖必配一張" in prompt
    assert '就不配（kind="none"' not in prompt
    assert "本次不出圖" not in prompt
    assert "|none" not in prompt


def test_image_form_guidance_does_not_default_third_party_posts_to_candid():
    assert "不要因為是第三方帖子" in feed_posts.IMAGE_SPEC_RULES
    assert "不要以「raw」作為沒有明確版式時的默認答案" in feed_posts.IMAGE_SPEC_RULES
    assert "人物可以自然出現，也可以完全不出現" in feed_posts.IMAGE_SPEC_RULES


def test_t1_prompt_requires_an_event_not_just_character_charm():
    prompt = feed_posts._build_t1_messages(
        {"name": "測試角色", "occupation": "氣象站觀測員"},
    )[1]["content"]

    assert "事件先成立，角色魅力才從事件裡長出來" in prompt
    assert "發帖人能放出的那一塊證據" in prompt
    assert "反差，也可以是能力、選擇、代價、立場或後果" in prompt


def test_t2_prompt_separates_public_clue_from_private_follow_up():
    prompt = feed_posts._build_t2_messages(
        {"name": "測試角色"}, "使用者",
        "使用者：明天我會去拿那份資料。", {},
        day_digest={"highlight": "角色把資料留在櫃檯"},
    )[1]["content"]

    assert "公開帖只放②和③，私信才補④" in prompt
    assert "不要把同一個聊天梗換句話重講" in prompt
    assert "預設不直接 @使用者、不公開使用者身分" in prompt
    assert "公開帖與評論區沒有的新信息、私人反應或下一步邀請" in prompt


def test_daily_forward_uses_t2_content_first_line_as_hook():
    data = {"mobile_messages": []}
    post = {
        "post_id": "fp_1",
        "kind": "t2",
        "subtype": "witness",
        "data": {
            "content": {"zh": "觀測員把值班室的糖全分給同事\n\n這是正文第二段"},
            "char_dm": [{"zh": "你也看到了？"}],
        },
    }

    daily._inject_forward_message(data, post)

    assert data["mobile_messages"][0]["forward_post"]["hook"] == "觀測員把值班室的糖全分給同事"


def test_own_post_comments_reuse_comment_chain_and_persist(monkeypatch):
    batch = {
        "char_id": "c1",
        "posts": [{
            "post_id": "ig_1",
            "content": {"zh": "下班後做了一鍋番茄牛肉湯。"},
            "post_type": "life",
            "format": "image_text",
            "image_type": "photo",
        }],
    }
    saved = {}
    monkeypatch.setattr(feed_posts.pipeline, "load_latest_ig", lambda _: batch)
    monkeypatch.setattr(feed_posts.pipeline, "load_character", lambda _: {
        "persona": {"name": "小林", "likes": "做飯"},
    })
    monkeypatch.setattr(feed_posts.api_client, "chat", lambda *_, **__: json.dumps({
        "comments": [{
            "author": {"zh": "湯碗收集家"}, "content": {"zh": "這鍋看起來太適合下雨天了"},
            "likes": 12, "char_self": True,
        }],
        "stats": {"likes": 87, "comment_count": 0},
    }))
    monkeypatch.setattr(feed_posts.storage, "save_json",
                        lambda *args, **_: saved.update(batch=args[2]))

    result = feed_posts.generate_own_post_comments("c1", "ig_1")

    post = result["post"]
    assert post["comments"][0]["char_self"] is False
    assert post["stats"] == {"likes": 87, "comment_count": 1}
    assert saved["batch"]["posts"][0]["comments"] == post["comments"]


def test_own_post_comment_prompt_uses_existing_comment_craft():
    prompt = feed_posts._build_own_post_comment_messages(
        {"name": "小林"}, {"content": {"zh": "晚餐好了"}, "post_type": "life"},
    )[1]["content"]

    assert "評論不是正文的附庸" in prompt
    assert "角色本人公開帖的評論" in prompt
    assert "角色本人不下場" in prompt


def test_anime_prompt_treats_the_style_and_reference_as_hard_anchors():
    prompt = prompts.compose_image_prompt(
        {"hair_color": "silver", "eyes": "violet eyes"},
        {"expression": "calm"}, {"location": "riverside"},
        "High-quality 2D painterly fantasy anime illustration; NOT realistic photography.",
    )

    assert "[ART STYLE LOCK — NON-NEGOTIABLE]" in prompt
    assert "canonical character-design and style anchor" in prompt
    assert "Do NOT drift into generic photorealistic camera imagery" in prompt


def test_graphic_post_keeps_selected_art_style_and_cover_reference(monkeypatch):
    captured = {}
    monkeypatch.setattr(feed_posts.styles, "get_style", lambda _id: {
        "prompt": "High-quality 2D painterly fantasy anime illustration; NOT realistic photography.",
    })
    monkeypatch.setattr(feed_posts.pipeline, "_ref_image_uri_for_selfie",
                        lambda _record: "cover-reference")

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["image_urls"] = kwargs["image_urls"]
        return {"url": "https://example.test/image.png", "local_path": "/tmp/image.png"}

    monkeypatch.setattr(feed_posts.api_client, "generate_image", fake_generate)
    result = feed_posts._render_graphic_image(
        {
            "char_id": "anime-char",
            "style_id": "painterly_anime",
            "identity": {
                "hair_color": "silver", "hair_length_style": "long braid",
                "eyes": "sharp violet eyes", "eye_color": "violet",
                "distinguishing_marks": "star-shaped cheek mark",
            },
        },
        "post-1",
        {"kind": "toon", "graphic": {"person": "running through the rain"}},
    )

    assert result["used_reference"] is True
    assert captured["image_urls"] == ["cover-reference"]
    assert "[CHARACTER ART STYLE]" in captured["prompt"]
    assert "[ART STYLE LOCK — NON-NEGOTIABLE]" in captured["prompt"]
    assert "star-shaped cheek mark" in captured["prompt"]


def test_fantasy_import_does_not_guess_an_anime_style():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "import_tl_characters.py"
    spec = importlib.util.spec_from_file_location("import_tl_characters", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    record = module._map_record({"char_id": "c1", "style": "fantasy"})

    assert record["style_id"] is None


def test_unstyled_fantasy_graphic_uses_only_the_visual_reference(monkeypatch):
    captured = {}
    monkeypatch.setattr(feed_posts.pipeline, "_ref_image_uri_for_selfie",
                        lambda _record: "cover-reference")

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["image_urls"] = kwargs["image_urls"]
        return {"url": "https://example.test/image.png", "local_path": "/tmp/image.png"}

    monkeypatch.setattr(feed_posts.api_client, "generate_image", fake_generate)
    feed_posts._render_graphic_image(
        {"char_id": "fantasy-char", "style_id": None,
         "identity": {"hair_color": "silver"}},
        "post-1", {"kind": "toon", "graphic": {"person": "walking"}},
    )

    assert captured["image_urls"] == ["cover-reference"]
    assert "[CHARACTER ART STYLE]" not in captured["prompt"]
    assert "[ART STYLE LOCK" not in captured["prompt"]
    assert "painterly_anime" not in captured["prompt"]
    assert "reference image's own visual medium" in captured["prompt"]
    assert "A realistic casual photo" not in captured["prompt"]
    assert "must never replace the reference image's original visual medium" in captured["prompt"]


def test_kie_never_downgrades_a_data_uri_reference_to_text_to_image(monkeypatch):
    def should_not_submit(*_args, **_kwargs):
        raise AssertionError("KIE must not submit text-to-image when a data URI was supplied")

    monkeypatch.setattr(api_client.requests, "post", should_not_submit)

    with pytest.raises(api_client.APIError, match="image-to-image requires public HTTP"):
        api_client._submit_image_kie(
            {"base": "https://kie.example", "key": "test-key"},
            "render the character", "3:4", "1K",
            ["data:image/png;base64,abc"], 30,
        )
