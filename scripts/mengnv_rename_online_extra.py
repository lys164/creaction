#!/usr/bin/env python3
"""補充：處理 5 個組裡殘留的愛豆真名（這些角色有封面，不在缺封面備份內）。

按同組已有化名風格對齊。備份取 _mengnv_online_backup_extra_* 最新目錄。
用法同主指令碼：預設 dry-run，--apply 才寫回線上。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import time

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
ROOT = os.path.join(os.path.dirname(__file__), "..", "data")

RENAME = {
    # grp_1783598470_fee9c8 (同組: en Jace / ko 차은태 / zh 田柾國 / ja ジョングク)
    "char_1783598568_1c0f3e": [("ジョングク", "ジェイス")],
    "char_1783598679_ebd868": [("田柾國", "江祈")],
    # grp_1783598976_05314c (同組: zh 星野 / ja 將太郎 / ko 송지안 / en Shotaro)
    "char_1783599051_2281bc": [("Shotaro", "Hoshino")],
    # grp_1783602243_a5ab8b (同組: ja 圭吾 / ko 권시우 / zh 江曜 / en Jungkook)
    "char_1783602325_316b02": [("Jungkook", "Keigo")],
    # grp_1783607188_fb1b2a (同組: ja 愛莉 / ko 미유 / zh 內永枝裡 / en Aeri)
    "char_1783607262_c6de43": [("Aeri", "Miyu")],
    # grp_1783608383_624c76 (同組: en Faye / ja アエラ / ko 도희 / zh Aeri)
    "char_1783608465_e842b1": [("Aeri", "愛菈")],
}


def _latest_extra():
    dirs = sorted(glob.glob(os.path.join(ROOT, "_mengnv_online_backup_extra_*")))
    if not dirs:
        raise SystemExit("找不到 extra 備份目錄")
    return dirs[-1]


def _ctx(raw, token, n=5):
    out = []
    for m in list(re.finditer(re.escape(token), raw))[:n]:
        s = max(0, m.start() - 20)
        e = min(len(raw), m.end() + 20)
        out.append(raw[s:e].replace("\n", " "))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    bak = _latest_extra()
    print(f"備份目錄: {bak}\n")

    plan = []
    for cid, rules in RENAME.items():
        d = json.load(open(os.path.join(bak, f"{cid}.json"), encoding="utf-8"))
        text = json.dumps(d.get("persona", {}), ensure_ascii=False)
        new = text
        rep = []
        for old, nw in rules:
            rep.append((old, nw, new.count(old), _ctx(new, old)))
            new = new.replace(old, nw)
        try:
            np = json.loads(new)
        except Exception as e:
            print(f"[ERROR] {cid} JSON 非法: {e}")
            continue
        print(f"=== {cid}  {d.get('persona',{}).get('name')!r} -> {np.get('name')!r} ===")
        for old, nw, cnt, ctx in rep:
            print(f"    {old!r}->{nw!r}  x{cnt}")
            for c in ctx:
                print(f"        …{c}…")
        plan.append((cid, np))

    print(f"\n共 {len(plan)} 個待改。")
    if not args.apply:
        print("（dry-run）")
        return
    ok = err = 0
    for cid, persona in plan:
        try:
            r = requests.put(f"{BASE}/api/persona",
                             json={"char_id": cid, "persona": persona}, timeout=60)
            r.raise_for_status()
            ok += 1
            print(f"  ✓ {cid}")
        except Exception as e:
            err += 1
            print(f"  ✗ {cid}: {e}")
        time.sleep(0.3)
    print(f"\n完成: 成功 {ok}, 失敗 {err}")


if __name__ == "__main__":
    main()
