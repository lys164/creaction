# -*- coding: utf-8 -*-
"""僅補「萌寵」本輪缺失的封面（fill_missing），不重生人設。

- 只針對 batch_nonhuman_mengchong_state.json 記錄的組內、當前線上缺 cover_url 的角色。
- 調 /api/characters/batch_cover，mode=fill_missing，style_id 傳空（nonhuman 不套畫風）。
- 分批提交，避免單個任務過大；帶重試/輪詢/伺服器重啟自愈。

用法：
  PYTHONPATH=. python3 scripts/fill_missing_covers_mengchong.py [--batch-size N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "batch_nonhuman_mengchong_state.json"
POLL_INTERVAL = 8
COVER_TIMEOUT = 1800
DEFAULT_BATCH = 8


def _healthy() -> bool:
    try:
        return requests.get(f"{BASE}/api/languages", timeout=20).status_code == 200
    except requests.RequestException:
        return False


def _wait_healthy(label: str = "") -> None:
    delay, waited = 5, 0
    while not _healthy():
        print(f"      ⏳ 伺服器不可用，等待恢復{(' ('+label+')') if label else ''} 已等 {waited}s", flush=True)
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 60)


def _req(method: str, url: str, allow_404: bool = False, **kw) -> requests.Response:
    last_err = None
    for attempt in range(6):
        try:
            r = requests.request(method, url, **kw)
            if allow_404 and r.status_code == 404:
                return r
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


def _poll(task_id: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        r = _req("GET", f"{BASE}/api/tasks/{task_id}", timeout=30, allow_404=True)
        if r.status_code == 404:
            raise RuntimeError(f"{label} 任務 {task_id} 丟失（伺服器疑似重啟）")
        t = r.json()
        if t.get("done_count") != last:
            last = t.get("done_count")
            print(f"      {label} {t.get('done_count')}/{t.get('total')} ({t.get('status')})", flush=True)
        if t.get("status") == "done":
            return t.get("result") or {}
        if t.get("status") == "error":
            raise RuntimeError(f"{label} 任務失敗: {t.get('error')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{label} 輪詢超時 ({timeout}s)")


def _missing_cover_ids() -> list[str]:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    mine = {g["group_id"] for g in st.get("groups", [])}
    raw = urllib.request.urlopen(f"{BASE}/api/characters", timeout=25).read()
    chars = json.loads(raw)
    return [c["char_id"] for c in chars
            if c.get("source") == "feiren"
            and c.get("group_id") in mine
            and not c.get("cover_url")
            and c.get("char_id")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=6,
                    help="補封面後仍可能有個別失敗，最多重掃補幾輪")
    args = ap.parse_args()

    for rnd in range(1, args.max_rounds + 1):
        ids = _missing_cover_ids()
        print(f"\n第 {rnd} 輪：本輪萌寵缺封面角色 {len(ids)} 個")
        if not ids:
            print("✓ 所有萌寵角色封面已補齊。")
            return 0
        if args.dry_run:
            print(f"[DRY] 將分 {args.batch_size} 個/批提交 batch_cover(fill_missing)。樣例:", ids[:5])
            return 0

        ok = 0
        for i in range(0, len(ids), args.batch_size):
            chunk = ids[i:i + args.batch_size]
            print(f"  提交批 {i//args.batch_size + 1}: {len(chunk)} 個角色", flush=True)
            try:
                r = _req("POST", f"{BASE}/api/characters/batch_cover",
                         json={"char_ids": chunk, "style_id": "", "mode": "fill_missing"},
                         timeout=120)
                task_id = r.json()["task_id"]
                result = _poll(task_id, COVER_TIMEOUT, "補封面")
                errs = result.get("errors") or result.get("cover_errors") or {}
                if errs:
                    print(f"      ⚠ 本批封面失敗 {len(errs)} 個: {list(errs)[:3]}...", flush=True)
                ok += len(chunk) - len(errs)
            except Exception as e:  # noqa: BLE001
                print(f"      ✗ 批失敗: {e}", flush=True)
        print(f"  第 {rnd} 輪完成，本輪成功約 {ok} 個。")
        time.sleep(3)

    left = _missing_cover_ids()
    print(f"\n達到最大輪次。仍缺封面 {len(left)} 個（多為供應商偶發拒圖，可再跑一次）。")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
