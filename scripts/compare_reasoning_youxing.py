"""遊星 帖子生成三版對比：有reasoning(gemini) / 無reasoning(gemini) / gpt-5(有reasoning)。

只對比【文案層】(build_ig_feed_messages_real 的 LLM 輸出)，不出圖。
- 同一個 persona（從 arca 遠端儲存拉取）、同一套 prompt、同一個 API 池。
- 唯一變數：是否注入 reasoning 塊；以及 model（gemini vs gpt-5）。
- "無reasoning" 透過把 build_ig_feed_messages_real 裡那三處 replace 逆向還原實現，
  等價於 reasoning 上線之前的原始 prompt（純 JSON 陣列、無 persona_read）。

用法：
    python3 scripts/compare_reasoning_youxing.py
產物：data/compare_youxing/{A_reasoning_gemini,B_noreason_gemini,C_reasoning_gpt5}.json
     data/compare_youxing/summary.md
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api_client, arca_storage, config, prompts  # noqa: E402

CHAR_ID = "char_1783573120_ae02ed"  # 遊星, zh, real
N_POSTS = 8
OUT_DIR = config.DATA_DIR / "compare_youxing"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# reasoning 注入的三處 replace（見 build_ig_feed_messages_real）。逆向還原即得
# "無 reasoning" 的原始 prompt。逐字複製自 prompts.py，保持一致。
_NEW_1 = prompts._REASON_BLOCK_ZH + "\n# 帖子型別（每條必須從三類裡選一個打標籤 post_type）"
_OLD_1 = "\n# 帖子型別（每條必須從三類裡選一個打標籤 post_type）"

_NEW_2 = (
    "- 輸出一個 JSON 物件：{\"persona_read\": {\"one_liner\":..., \"perception\":..., "
    "\"stances\":[...], \"visual_identity\":...}, \"posts\": [...]}。"
    "persona_read 是你上面第一步的判斷（簡明填寫、給自己定調）；posts 是"
)
_OLD_2 = "- 只輸出一個 JSON 陣列，"

_NEW_3 = (
    " 的帖子陣列，每個元素必須包含 content、post_type、format、image_type、selfie、"
    "photo_kind、photo_prompt、photo_schema、topic_seed 這些鍵，"
    "且每條都要對得上 persona_read（尤其 one_liner 與 visual_identity）。"
)
_OLD_3 = (
    "；每個元素必須包含 content、post_type、format、image_type、\n"
    "  selfie、photo_kind、photo_prompt、photo_schema、topic_seed 這些鍵。"
)


def strip_reasoning(messages: list[dict]) -> list[dict]:
    """把 reasoning 注入逆向還原成上線前的原始 prompt。"""
    out = []
    for m in messages:
        c = m["content"]
        if m["role"] == "user":
            c = c.replace(_NEW_1, _OLD_1)
            c = c.replace(_NEW_2, _OLD_2)
            c = c.replace(_NEW_3, _OLD_3)
        out.append({"role": m["role"], "content": c})
    return out


def parse_feed(raw):
    """把 LLM 輸出解析成 (persona_read, posts)，相容物件/陣列兩種形態。"""
    persona_read = None
    feed = raw
    if isinstance(feed, dict):
        if isinstance(feed.get("posts"), list):
            persona_read = feed.get("persona_read")
            feed = feed["posts"]
        else:
            feed = [feed]
    return persona_read, feed


def slim_post(p: dict) -> dict:
    """只保留對比關心的欄位，便於並排看文案與配圖規劃。"""
    return {
        "content": p.get("content"),
        "post_type": p.get("post_type"),
        "format": p.get("format"),
        "image_type": p.get("image_type"),
        "photo_kind": p.get("photo_kind"),
        "photo_prompt": p.get("photo_prompt"),
        "selfie_shooting": (p.get("selfie") or {}).get("shooting") if isinstance(p.get("selfie"), dict) else None,
        "topic_seed": p.get("topic_seed"),
    }


def run_variant(name: str, messages: list[dict], model: str | None) -> dict:
    print(f"\n=== [{name}] model={model or config.LLM_MODEL} ===", flush=True)
    raw = api_client.chat_json(messages, model=model, temperature=0.95, max_retries=3)
    persona_read, feed = parse_feed(raw)
    posts = [slim_post(p) for p in feed[:N_POSTS] if isinstance(p, dict)]
    print(f"  -> {len(posts)} posts, persona_read={'yes' if persona_read else 'no'}", flush=True)
    result = {"variant": name, "model": model or config.LLM_MODEL,
              "persona_read": persona_read, "n": len(posts), "posts": posts}
    (OUT_DIR / f"{name}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    record = arca_storage.get_record("personas", CHAR_ID)
    if not record:
        raise SystemExit(f"record not found in arca: {CHAR_ID}")
    persona = record["persona"]
    lang = record.get("lang", "zh")
    print(f"角色: {persona.get('name')} | lang={lang} | track={record.get('track')}")

    base_msgs = prompts.build_ig_feed_messages_real(persona, lang, n=N_POSTS)
    noreason_msgs = strip_reasoning(base_msgs)

    results = {
        "A_reasoning_gemini": run_variant("A_reasoning_gemini", base_msgs, None),
        "B_noreason_gemini": run_variant("B_noreason_gemini", noreason_msgs, None),
        "C_reasoning_gpt5": run_variant("C_reasoning_gpt5", base_msgs, "gpt-5"),
    }

    # 彙總 markdown，三版並排
    lines = [f"# 遊星 帖子三版對比（{persona.get('name')}, {lang}/real）\n",
             f"- A_reasoning_gemini: 當前線上邏輯（有 reasoning, {config.LLM_MODEL}）",
             f"- B_noreason_gemini: 去掉 reasoning（{config.LLM_MODEL}）",
             "- C_reasoning_gpt5: 有 reasoning, 換 gpt-5（同 API）\n"]
    for key, r in results.items():
        lines.append(f"\n## {key}  (model={r['model']}, {r['n']} 條)")
        if r.get("persona_read"):
            lines.append("persona_read:")
            lines.append("```json")
            lines.append(json.dumps(r["persona_read"], ensure_ascii=False, indent=2))
            lines.append("```")
        for i, p in enumerate(r["posts"], 1):
            tags = f"[{p.get('post_type')}/{p.get('format')}/{p.get('image_type') or '-'}"
            if p.get("photo_kind"):
                tags += f"/{p.get('photo_kind')}"
            tags += "]"
            lines.append(f"{i}. {tags} {p.get('content')}")
    (OUT_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n完成。產物在 {OUT_DIR}")


if __name__ == "__main__":
    main()
