"""把 外星人小歪 TSV 直接建成本地 persona 记录（不走图片→LLM 生成链路）。

存进本地 data/personas，并（若配置了 ARCA_STORAGE_KEY）同步到 arca 存储中台，
这样生产链路工具的「角色聊天」页（本地或部署版）都能选到它、直接测聊天。

用法:
  PYTHONPATH=. python3 scripts/import_xiaowai_local.py [/path/to/外星人小歪.tsv]
"""
import csv
import json
import sys
import time
from pathlib import Path

from app import config, pipeline

TSV_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / "外星人小歪.tsv"
LANG = "zh"
COVER_URL = ("https://cdn-prod-i18n-public.popop.ai/popop-fe-user-upload/"
             "images/1783481709562-df45a92f-11fe-4b90-ba0f-1c073c7ca917.jpg")

# 平台设定 tag_key → chat 页读的 persona 英文 schema 键
SETTING_TO_PERSONA = {
    "出生地": "hometown", "居住地": "residence", "职业": "social_status",
    "外貌": "appearance", "语言习惯": "speech_style", "穿衣风格": "appearance",
    "社交模式": "relationship_mode", "表达爱的方式": "love_style",
    "价值观": "value", "生活习惯": "life_details", "爱好": "likes",
    "讨厌的东西": "fears", "成长经历": "backstory", "家庭成员": "family",
    "社交关系": "social_network", "特殊背景/世界观": "premise", "愿望清单": "wishlist",
}


def _jload(text, default):
    text = (text or "").strip()
    if not text:
        return default
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].replace('""', '"')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def parse_latest_row(path: Path) -> dict:
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines(), delimiter="\t"))
    rows = [r for r in rows if len(r) >= 22 and r[2].strip()]
    if not rows:
        raise SystemExit(f"未从 {path} 解析出角色行")
    row = max(rows, key=lambda r: int(r[15] or 0))
    return {
        "name": row[2].strip(), "gender": row[4].strip(), "species": row[5].strip(),
        "voice_id": row[6].strip(), "profile": row[7].strip(),
        "tags": _jload(row[18], []), "settings_raw": _jload(row[19], {}),
        "opening_prologue": _jload(row[20], []), "disposition": row[21].strip(),
    }


def build_persona(d: dict) -> dict:
    persona = {
        "name": d["name"], "gender": d["gender"], "species": d["species"],
        "profile": d["profile"], "voice": d["voice_id"], "tags": d["tags"],
        "visibility": "public",
        "personality": {"summary": d["disposition"]},
    }
    raw = d["settings_raw"]
    pairs = raw.items() if isinstance(raw, dict) else [
        (x.get("tag_key"), x.get("tag_value")) for x in raw if isinstance(x, dict)]
    for key, value in pairs:
        pk = SETTING_TO_PERSONA.get(key)
        if not pk or not value:
            continue
        # 同一 persona 键（如 appearance 同时来自外貌/穿衣风格）合并不覆盖
        persona[pk] = f"{persona[pk]}；{value}" if persona.get(pk) else value
    # opening_prologue → chat 读的 opening.messages（system 旁白也作为 text 呈现）
    msgs = [{"type": "voice" if it.get("output_type") == "tts" else "text",
             "data": {"content": it.get("text", "")}}
            for it in d["opening_prologue"] if it.get("text")]
    persona["opening"] = {"note": d["profile"], "messages": msgs}
    return persona


def main():
    d = parse_latest_row(TSV_PATH)

    # 去重：本地已存在同名小歪则复用其 char_id（就地更新，不再新建）
    existing = next((c["char_id"] for c in pipeline.list_characters()
                     if c.get("name") == d["name"]), None)
    char_id = existing or pipeline._new_id("char")

    cover = pipeline._download_image(COVER_URL)  # 落地 data/uploads
    record = {
        "char_id": char_id, "lang": LANG, "group_id": char_id,
        "created": int(time.time()),
        "source_images": [cover] if cover else [],
        "user_hint": "", "track": "real", "source": "tsv_import",
        "import_source": {"tsv": str(TSV_PATH)},
        "persona": build_persona(d),
        "identity": None,
        "cover": {"local_path": cover} if cover else None,
        "style_id": None,
    }
    pipeline.save_character(record)

    from app import arca_storage
    print(f"{'更新' if existing else '新建'}本地角色: {char_id}  {d['name']}")
    print("persona 字段:", [k for k in record["persona"] if record["persona"][k]])
    print("opening 条数:", len(record["persona"]["opening"]["messages"]))
    print("远端存储中台同步:", "开启（部署页可见）" if arca_storage.enabled() else "未开启（仅本地可见）")


if __name__ == "__main__":
    main()
