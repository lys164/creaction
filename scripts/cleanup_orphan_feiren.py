# -*- coding: utf-8 -*-
"""清理線上 source=feiren 的「孤兒組」重複角色。

安全保護（白名單）：
- 只刪除既不在萌寵 state(batch_nonhuman_mengchong_state.json)、
  也不在 meme state(batch_nonhuman_online_state.json) 記錄組內的角色。
- 萌寵 52 組 + meme 58 組 = 110 組永遠不動。

流程：
1. 實時拉線上，算出孤兒組 char_ids。
2. 把待刪清單存成帶時間戳的備份 JSON（可追溯）。
3. --dry-run 只匯出清單；不帶則分批調 /api/characters/delete 刪除。
4. 刪除後複核白名單組完好。

用法：
  PYTHONPATH=. python3 scripts/cleanup_orphan_feiren.py --dry-run
  PYTHONPATH=. python3 scripts/cleanup_orphan_feiren.py --confirm
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DATA = Path(__file__).resolve().parent.parent / "data"
MC_STATE = DATA / "batch_nonhuman_mengchong_state.json"
MEME_STATE = DATA / "batch_nonhuman_online_state.json"
BATCH = 20


def _protected_gids() -> tuple[set, set]:
    mc = json.loads(MC_STATE.read_text(encoding="utf-8"))
    meme = json.loads(MEME_STATE.read_text(encoding="utf-8"))
    return ({g["group_id"] for g in mc.get("groups", [])},
            {g["group_id"] for g in meme.get("groups", [])})


def _fetch_feiren() -> list[dict]:
    raw = urllib.request.urlopen(f"{BASE}/api/characters", timeout=30).read()
    return [c for c in json.loads(raw) if c.get("source") == "feiren"]


def _compute_orphans() -> tuple[list[dict], set, set]:
    mc_gids, meme_gids = _protected_gids()
    f = _fetch_feiren()
    protected = mc_gids | meme_gids
    orphan_chars = [c for c in f
                    if c.get("group_id") not in protected and c.get("char_id")]
    return orphan_chars, mc_gids, meme_gids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm", action="store_true",
                    help="真正執行刪除（不加則預設 dry-run）")
    args = ap.parse_args()

    orphan, mc_gids, meme_gids = _compute_orphans()
    orphan_gids = sorted(set(c["group_id"] for c in orphan))
    ids = [c["char_id"] for c in orphan]

    print(f"保護白名單：萌寵 {len(mc_gids)} 組 + meme {len(meme_gids)} 組")
    print(f"孤兒組：{len(orphan_gids)} 組 / {len(ids)} 角色（待刪）")

    # 備份清單
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DATA / f"orphan_cleanup_{ts}.json"
    backup.write_text(json.dumps(
        {"orphan_groups": orphan_gids,
         "orphan_chars": [{"char_id": c["char_id"], "group_id": c.get("group_id"),
                           "lang": c.get("lang"), "name": c.get("name")}
                          for c in orphan]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"待刪清單已備份：{backup}")

    if not args.confirm or args.dry_run:
        print("\n[DRY-RUN] 未刪除。確認無誤後加 --confirm 執行。")
        return 0

    deleted, errors = 0, {}
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        try:
            r = requests.post(f"{BASE}/api/characters/delete",
                              json={"char_ids": chunk}, timeout=120)
            r.raise_for_status()
            res = r.json()
            deleted += len(res.get("deleted", []))
            errors.update(res.get("errors", {}))
            print(f"  批 {i//BATCH+1}: 刪除 {len(res.get('deleted', []))} / {len(chunk)}",
                  flush=True)
        except requests.RequestException as e:
            print(f"  批 {i//BATCH+1} 失敗: {e}", flush=True)
            time.sleep(5)

    print(f"\n刪除完成：成功 {deleted} 個，失敗 {len(errors)} 個。")

    # 複核白名單完好
    f2 = _fetch_feiren()
    gids2 = set(c.get("group_id") for c in f2)
    print(f"複核：萌寵組仍在 {len(gids2 & mc_gids)}/{len(mc_gids)}，"
          f"meme組仍在 {len(gids2 & meme_gids)}/{len(meme_gids)}")
    print(f"複核：線上 feiren 現存 {len(f2)} 角色 / {len(gids2)} 組")
    return 0


if __name__ == "__main__":
    sys.exit(main())
