#!/usr/bin/env python3
"""為 source=mengnv 缺封面的角色批次補封面（線上）。

用 /api/characters/batch_cover 提交後臺任務並輪詢 /api/tasks/{id}。
用法:
  python3 scripts/mengnv_fill_covers.py --limit 5        # 先試 5 個
  python3 scripts/mengnv_fill_covers.py                  # 全部缺封面
  python3 scripts/mengnv_fill_covers.py --style realistic_portrait
"""
from __future__ import annotations

import argparse
import json
import time

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"


def _missing():
    d = requests.get(f"{BASE}/api/characters", timeout=60).json()
    return [c["char_id"] for c in d
            if c.get("source") == "mengnv" and not c.get("cover_url")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default="realistic_portrait")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    miss = _missing()
    if args.limit:
        miss = miss[:args.limit]
    print(f"待補封面: {len(miss)} 個，style={args.style}", flush=True)
    if not miss:
        return

    r = requests.post(f"{BASE}/api/characters/batch_cover", json={
        "char_ids": miss, "style_id": args.style,
        "use_reference": None, "mode": "fill_missing",
    }, timeout=60)
    r.raise_for_status()
    tid = r.json()["task_id"]
    print(f"task_id={tid}，輪詢中…", flush=True)

    start = time.time()
    while True:
        time.sleep(10)
        t = requests.get(f"{BASE}/api/tasks/{tid}", timeout=30).json()
        st = t.get("status")
        done = t.get("done", 0)
        total = t.get("total", len(miss))
        el = int(time.time() - start)
        print(f"  [{el}s] status={st} {done}/{total}", flush=True)
        if st == "done":
            res = t.get("result") or {}
            cov = res.get("covered", [])
            errs = res.get("errors", {})
            print(f"\n完成: 成功 {len(cov)}, 失敗 {len(errs)}")
            for cid, e in list(errs.items())[:20]:
                print(f"  ✗ {cid}: {e}")
            break
        if st == "error":
            print(f"任務失敗: {t.get('error')}")
            break


if __name__ == "__main__":
    main()
