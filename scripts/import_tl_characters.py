#!/usr/bin/env python3
"""把「350角色」匯出格式的 character.json 匯入 pipeline persona 存檔。

匯出格式（Downloads/.../<角色名>/character.json）與 pipeline 存檔 schema 有差異：
- 匯出：cover_url 是字串；persona.identity / persona.personality 是文字；lang=zh-Hant；
  track=human；style=fantasy；帶 posts 陣列。
- pipeline：cover 是 dict（{url|local_path}）；頂層 identity 是視覺 DNA dict（生圖用，
  聊天不需要）；track/style_id 有固定枚舉。

本腳本只做「忠實搬運 + 最小規整」，寫入本地 data/personas/<char_id>.json：
- cover_url(str) -> cover={"url": ...}，讓 _served_image_url 能出圖。
- 僅在來源 style 有明確等價畫風時才轉成 style_id；`fantasy` 不做猜測，交給封面
  參考圖保留原始作畫語言。
- 保留 persona 原樣（聊天鏈路對字串型 personality/identity 有雙讀兜底，不會崩）。
- 保留 posts、source、track，並記 import_source=原始檔，便於回溯。
- 不寫遠端共享儲存（只落本地檔），避免污染 demo 之外的資料。

用法：
  python3 scripts/import_tl_characters.py            # 匯入內建 5 個
  python3 scripts/import_tl_characters.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 來源目錄（使用者指定的 5 個角色）
SRC_BASE = Path("/Users/a0/Downloads/350角色（包括第一批）/zh-hant")
SRC_DIRS = [
    "203號的沈先生_de4be8",
    "203號許先生_ae7bd2",
    "阿潮｜南方澳輕浮船工_bd1327",
    "阿澈_7f794b",
    "阿澈｜別去河邊_9a3724",
]

# 匯出 style -> pipeline style_id
# `fantasy` 是來源系統自己的寬泛枚舉，不能推斷出厚塗、賽璐璐、韓漫或其他任一畫風。
# 因此不把它翻成 pipeline 的任一自訂風格詞；帖子只做圖生圖，讓封面參考圖決定畫風。
STYLE_MAP = {
    "realistic": "realistic_portrait",
    "portrait": "realistic_portrait",
    "comic": "comic_portrait",
    "webtoon": "webtoon_lineart",
}
DEFAULT_STYLE_ID = None

PERSONA_DIR = _ROOT / "data" / "personas"


def _map_record(c: dict) -> dict:
    char_id = c["char_id"]
    style_id = STYLE_MAP.get((c.get("style") or "").lower(), DEFAULT_STYLE_ID)
    cover_url = c.get("cover_url")
    cover = {"url": cover_url} if cover_url else None
    return {
        "char_id": char_id,
        "lang": c.get("lang") or "zh-Hant",
        "name": c.get("name"),
        "source": c.get("source") or "tl",
        "track": c.get("track") or "human",
        "style_id": style_id,
        "cover": cover,
        "persona": c.get("persona") or {},
        "posts": c.get("posts") or [],
        "created": int(time.time()),
        "import_source": c,   # 原始匯出檔，便於回溯/重擴寫
        "imported_from": "350角色（包括第一批）/zh-hant",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    PERSONA_DIR.mkdir(parents=True, exist_ok=True)
    plan = []
    for d in SRC_DIRS:
        src = SRC_BASE / d / "character.json"
        if not src.exists():
            print(f"[skip] 找不到: {src}", file=sys.stderr)
            continue
        c = json.loads(src.read_text(encoding="utf-8"))
        rec = _map_record(c)
        plan.append(rec)
        p = rec["persona"]
        print(f"  {rec['char_id']} | {rec['name']} | lang={rec['lang']} | "
              f"track={rec['track']} | style_id={rec['style_id']} | "
              f"cover={bool(rec['cover'])} | opening_msgs="
              f"{len((p.get('opening') or {}).get('messages') or [])} | "
              f"posts={len(rec['posts'])}")

    if args.dry_run:
        print(f"\n（dry-run）{len(plan)} 個角色待匯入，未寫入。", file=sys.stderr)
        return 0

    for rec in plan:
        out = PERSONA_DIR / f"{rec['char_id']}.json"
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"✓ 寫入 {out}")
    print(f"\n完成：匯入 {len(plan)} 個角色到 {PERSONA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
