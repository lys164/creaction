# -*- coding: utf-8 -*-
"""掃描 heermeng+mengnv 線上落地頁，找出「文字居中」較多的頁面。

判據:統計 text-align:center 出現次數(CSS 規則 + 內聯 style)。
落地頁應以左對齊為主，個別裝飾元素居中可接受，故設閾值:
  出現 >=2 次 text-align:center 即判定為需重跑(命中)。
輸出 -> data/center_text_targets.json ({"heermeng":[],"mengnv":[],"all":[]})
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
OUT = Path(__file__).resolve().parent.parent / "data" / "center_text_targets.json"
THRESH = 2


def scan(cid: str):
    try:
        d = requests.get(f"{BASE}/api/landing/{cid}", timeout=60).json()
    except Exception:  # noqa: BLE001
        return (cid, -1)
    html = d.get("html") or ""
    if not html:
        return (cid, -1)
    n = len(re.findall(r'text-align\s*:\s*center', html, re.I))
    return (cid, n)


def main() -> int:
    chars = requests.get(f"{BASE}/api/characters", timeout=120).json()
    result = {"heermeng": [], "mengnv": [], "all": []}
    for src in ("heermeng", "mengnv"):
        ids = [c["char_id"] for c in chars
               if (c.get("source") or "") == src
               and c.get("has_identity") and c.get("cover_url")]
        with ThreadPoolExecutor(max_workers=16) as ex:
            rows = list(ex.map(scan, ids))
        hit = [(cid, n) for cid, n in rows if n >= THRESH]
        dist = {}
        for _, n in rows:
            if n >= 0:
                dist[n] = dist.get(n, 0) + 1
        print(f"\n==== {src}: 掃描 {len(ids)}，居中>={THRESH} 命中 {len(hit)} ====")
        print("   center計數分佈:", dict(sorted(dist.items())))
        for cid, n in hit[:10]:
            print(f"    - {cid}  center×{n}")
        result[src] = [cid for cid, _ in hit]
    result["all"] = result["heermeng"] + result["mengnv"]
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n合計命中 {len(result['all'])}，已寫 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
