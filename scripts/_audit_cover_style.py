# -*- coding: utf-8 -*-
"""審計 heermeng+mengnv 線上落地頁的「角色圖(封面)」是否符合基礎卡規範。

基礎卡(C-01)規範:
  - 封面容器 aspect-ratio:4/4.6
  - 容器 border-radius:16px
  - 圖片 object-fit:cover
  - hero 區留白內嵌(不通欄貼邊)

判定不符合(命中需重跑)的啟發式(任一):
  - no_oc_cover        : 沒有 oc-cover 封面槽位
  - no_aspect_container: 找不到帶 aspect-ratio 的封面容器
  - aspect_off         : 封面容器寬高比不在 4/4.6 附近(0.83~0.90)
  - radius_missing     : 封面容器無圓角
  - radius_off         : 圓角明顯偏離(<8 或 >24)
輸出命中角色 -> data/cover_style_targets.json ({"heermeng":[],"mengnv":[],"all":[]})。
"""
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
OUT = Path(__file__).resolve().parent.parent / "data" / "cover_style_targets.json"


def _aspect_rules(html: str) -> list[str]:
    return re.findall(r'\.[\w-]+\s*\{[^}]*aspect-ratio[^}]*\}', html)


def audit(cid: str):
    try:
        d = requests.get(f"{BASE}/api/landing/{cid}", timeout=60).json()
    except Exception:  # noqa: BLE001
        return (cid, ["fetch_error"])
    html = d.get("html") or ""
    if not html:
        return (cid, ["empty"])
    issues = []

    if not re.search(r'class="[^"]*oc-cover', html):
        issues.append("no_oc_cover")

    rules = _aspect_rules(html)
    if not rules:
        issues.append("no_aspect_container")
    else:
        cover_rule = next(
            (r for r in rules if "overflow" in r or "oc-cover" in r), rules[0]
        )
        ar = re.search(r'aspect-ratio:\s*([\d.]+)\s*/\s*([\d.]+)', cover_rule)
        if ar:
            w, h = float(ar.group(1)), float(ar.group(2))
            ratio = (w / h) if h else 0
            if not (0.83 <= ratio <= 0.90):
                issues.append(f"aspect_off({ar.group(1)}/{ar.group(2)})")
        else:
            issues.append("aspect_off(none)")
        br = re.search(r'border-radius:\s*([\d.]+)px', cover_rule)
        if not br:
            issues.append("radius_missing")
        else:
            r = float(br.group(1))
            if r < 8 or r > 24:
                issues.append(f"radius_off({r})")

    return (cid, issues)


def main() -> int:
    chars = requests.get(f"{BASE}/api/characters", timeout=120).json()
    result = {"heermeng": [], "mengnv": [], "all": []}
    for src in ("heermeng", "mengnv"):
        ids = [c["char_id"] for c in chars
               if (c.get("source") or "") == src
               and c.get("has_identity") and c.get("cover_url")]
        with ThreadPoolExecutor(max_workers=16) as ex:
            rows = list(ex.map(audit, ids))
        hit = [(cid, iss) for cid, iss in rows if iss]
        print(f"\n==== {src}: 掃描 {len(ids)}，不符合 {len(hit)} ====")
        c = Counter()
        for _, iss in hit:
            for i in iss:
                c[re.sub(r'\(.*\)', '', i)] += 1
        for k, v in c.most_common():
            print(f"   {k}: {v}")
        for cid, iss in hit[:10]:
            print("    -", cid, iss)
        result[src] = [cid for cid, _ in hit]
    result["all"] = result["heermeng"] + result["mengnv"]
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n合計不符合 {len(result['all'])}，已寫 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
