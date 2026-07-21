# -*- coding: utf-8 -*-
"""掃描落地頁，找出「中間模組整塊正文居中」的頁面(需重跑) —— 最嚴版。

使用者標準:
  - 結尾文字居中 OK(排除 ending/outro/closing/final/end/teaser 等結尾/預告類)
  - 金句/引用/時間戳/標籤/姓名 居中 OK(排除)
  - 只有"中間正文模組整塊居中"才算怪(場景鋪墊/獨白/自述/描述/介紹等)

命中: CSS 選擇器帶 text-align:center 且 font-size 12~16，類名屬"中間正文"
  且不在排除名單。
輸出 -> data/center_paragraph_targets.json
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
OUT = Path(__file__).resolve().parent.parent / "data" / "center_paragraph_targets.json"

EXCLUDE = re.compile(
    r'quote|serif|punchline|motto|slogan|time|stamp|status|system|header|'
    r'notice|hint|kicker|badge|label|meta|name|role|title|head|tag|sign|seal|script|'
    r'ending|outro|closing|final|end|teaser|footer|lead|cta|message|msg|popop',
    re.I,
)
BODY_MID = re.compile(
    r'context|scene|monologue|desc|narr|story|intro|about|bio|copy|body|para|'
    r'setting|moment|note|content|inner|self',
    re.I,
)


def analyze(html: str):
    hits = []
    for m in re.finditer(r'([.#][\w-]+(?:[ ,>][.#\w-]+)*)\s*\{([^}]*)\}', html):
        sel, body = m.group(1).strip(), m.group(2)
        if not re.search(r'text-align\s*:\s*center', body, re.I):
            continue
        fs = re.search(r'font-size\s*:\s*([\d.]+)px', body)
        if not (fs and 12 <= float(fs.group(1)) <= 16):
            continue
        if EXCLUDE.search(sel):
            continue
        if BODY_MID.search(sel):
            hits.append(f"{sel[:30]}@{fs.group(1)}")
    return hits


def scan(cid: str):
    try:
        d = requests.get(f"{BASE}/api/landing/{cid}", timeout=60).json()
    except Exception:  # noqa: BLE001
        return (cid, None)
    html = d.get("html") or ""
    if not html:
        return (cid, None)
    return (cid, analyze(html))


def main() -> int:
    chars = requests.get(f"{BASE}/api/characters", timeout=120).json()
    result = {"heermeng": [], "mengnv": [], "all": []}
    for src in ("heermeng", "mengnv"):
        ids = [c["char_id"] for c in chars
               if (c.get("source") or "") == src
               and c.get("has_identity") and c.get("cover_url")]
        with ThreadPoolExecutor(max_workers=16) as ex:
            rows = list(ex.map(scan, ids))
        hit = [(cid, h) for cid, h in rows if h]
        print(f"\n==== {src}: 掃描 {len(ids)}，中間模組正文居中命中 {len(hit)} ====")
        for cid, h in hit[:20]:
            print(f"    - {cid}  {h[:3]}")
        result[src] = [cid for cid, _ in hit]
    result["all"] = result["heermeng"] + result["mengnv"]
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n合計命中 {len(result['all'])}，已寫 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
