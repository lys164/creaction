# -*- coding: utf-8 -*-
"""補跑線上「source=feiren」缺封面角色的封面 —— 直接打線上服務(HTTP)。

背景：meme 非人物(nonhuman)批處理跑人設時，部分角色的封面被上游安全策略/
內容稽核拒了(cover_errors)，人設已入賬但缺封面。本指令碼掃描線上所有
source=feiren 且無 cover 的角色，呼叫 /api/characters/batch_cover 逐個補封面。

- nonhuman 鏈路封面不套畫風：generate_cover 內部對 track=nonhuman 忽略 style_id，
  按 identity+原圖生成。這裡 style_id 只是佔位(伺服器會忽略)。
- mode=fill_missing：缺 identity/cover_spec 會自動補齊再出圖。
- 斷點續跑：每輪重新掃描線上，只補仍缺封面的；成功即被下輪掃描排除。

用法：
  PYTHONPATH=. python3 scripts/backfill_meme_covers.py [--batch N] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import time

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
SOURCE = "feiren"
STYLE_ID = "realistic_portrait"   # nonhuman 會忽略；僅佔位
POLL_INTERVAL = 8
BATCH_TIMEOUT = 3600
DEFAULT_BATCH = 8                 # 每批補多少個角色(伺服器內部再併發)


def _healthy() -> bool:
    try:
        return requests.get(f"{BASE}/api/languages", timeout=20).status_code == 200
    except requests.RequestException:
        return False


def _wait_healthy(label: str = "") -> None:
    delay, waited = 5, 0
    while not _healthy():
        print(f"   ⏳ 伺服器不可用，等待恢復{(' ('+label+')') if label else ''} 已等 {waited}s",
              flush=True)
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 60)


def _req(method: str, url: str, **kw) -> requests.Response:
    last_err = None
    for attempt in range(6):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                last_err = f"HTTP {r.status_code}"
                _wait_healthy(url.rsplit("/", 1)[-1])
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = str(e)
            _wait_healthy(url.rsplit("/", 1)[-1])
            time.sleep(min(5 * (attempt + 1), 30))
    raise RuntimeError(f"請求多次失敗 {method} {url}: {last_err}")


def _missing_cover_ids() -> list[str]:
    """線上掃描 source=feiren 且無 cover_url 的角色 char_id。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=60)
    chars = r.json()
    return [c["char_id"] for c in chars
            if c.get("source") == SOURCE and not c.get("cover_url") and c.get("char_id")]


def _poll(task_id: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        r = _req("GET", f"{BASE}/api/tasks/{task_id}", timeout=30)
        t = r.json()
        if t.get("done_count") != last:
            last = t.get("done_count")
            print(f"   {label} {t.get('done_count')}/{t.get('total')} "
                  f"({t.get('status')})", flush=True)
        if t.get("status") == "done":
            return t.get("result") or {}
        if t.get("status") == "error":
            raise RuntimeError(f"{label} 任務失敗: {t.get('error')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{label} 輪詢超時 ({timeout}s)")


def _cover_batch(char_ids: list[str]) -> dict:
    r = _req("POST", f"{BASE}/api/characters/batch_cover", json={
        "char_ids": char_ids, "style_id": STYLE_ID, "mode": "fill_missing",
    }, timeout=120)
    task_id = r.json()["task_id"]
    return _poll(task_id, BATCH_TIMEOUT, "補封面")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids = _missing_cover_ids()
    print(f"線上服務: {BASE}")
    print(f"source={SOURCE} 缺封面角色: {len(ids)} 個；每批 {args.batch} 個\n")
    if args.dry_run:
        for cid in ids[:10]:
            print("  ", cid)
        print(f"[DRY] 將分 {(len(ids)+args.batch-1)//args.batch} 批補封面。")
        return 0
    if not ids:
        print("沒有缺封面的角色，無需補。")
        return 0

    ok_total, err_total = 0, 0
    batch_no = 0
    while True:
        batch = ids[:args.batch]
        if not batch:
            break
        batch_no += 1
        print(f"[批 {batch_no}] 補 {len(batch)} 個: {', '.join(batch)}", flush=True)
        try:
            res = _cover_batch(batch)
            covered = res.get("covered", [])
            errors = res.get("errors", {})
            ok_total += len(covered)
            err_total += len(errors)
            if errors:
                print(f"   ⚠ 本批 {len(errors)} 個仍失敗: "
                      f"{list(errors.items())[:2]}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"   ✗ 本批異常: {e}", flush=True)
        # 重新掃描：成功的會被排除；失敗的仍在，但為避免死迴圈，
        # 用「已嘗試」集合推進——這裡直接從剩餘列表切掉已處理的這批。
        ids = ids[args.batch:]

    print(f"\n完成: 成功補 {ok_total} 個封面, 失敗 {err_total} 個。")
    print("提示: 失敗多為上游內容稽核拒絕，可再次執行本指令碼重掃重試。")
    return 0 if err_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
