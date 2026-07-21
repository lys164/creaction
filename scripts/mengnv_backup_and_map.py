#!/usr/bin/env python3
"""備份線上 source=mengnv 且缺封面的角色，並按 group_id 聚合出真名清單。

只讀，不改任何線上資料。產出：
  data/_mengnv_online_backup_<ts>/<char_id>.json   每個角色完整詳情
  data/_mengnv_online_backup_<ts>/_groups.json      按 group_id 聚合的名字清單
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
OUT_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")


def _get(url, **kw):
    last = None
    for i in range(5):
        try:
            r = requests.get(url, timeout=60, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(min(3 * (i + 1), 15))
    raise RuntimeError(f"GET 失敗 {url}: {last}")


def main():
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUT_ROOT, f"_mengnv_online_backup_{ts}")
    os.makedirs(out, exist_ok=True)

    chars = _get(f"{BASE}/api/characters").json()
    targets = [c for c in chars
               if c.get("source") == "mengnv" and not c.get("cover_url")]
    print(f"目標 mengnv 缺封面角色: {len(targets)}", flush=True)

    details = {}

    def _one(c):
        cid = c["char_id"]
        d = _get(f"{BASE}/api/character/{cid}").json()
        with open(os.path.join(out, f"{cid}.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return cid, d

    with ThreadPoolExecutor(max_workers=6) as ex:
        for cid, d in ex.map(_one, targets):
            details[cid] = d
            print(f"  backed up {cid}", flush=True)

    # 按 group_id 聚合
    groups = {}
    for cid, d in details.items():
        gid = d.get("group_id") or f"_nogroup_{cid}"
        p = d.get("persona", {})
        groups.setdefault(gid, []).append({
            "char_id": cid,
            "lang": d.get("lang"),
            "name": p.get("name"),
        })

    with open(os.path.join(out, "_groups.json"), "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)

    print(f"\n備份完成: {out}")
    print(f"角色數: {len(details)}，group 數: {len(groups)}")


if __name__ == "__main__":
    main()
