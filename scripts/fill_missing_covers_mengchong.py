# -*- coding: utf-8 -*-
"""仅补「萌宠」本轮缺失的封面（fill_missing），不重生人设。

- 只针对 batch_nonhuman_mengchong_state.json 记录的组内、当前线上缺 cover_url 的角色。
- 调 /api/characters/batch_cover，mode=fill_missing，style_id 传空（nonhuman 不套画风）。
- 分批提交，避免单个任务过大；带重试/轮询/服务器重启自愈。

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
        print(f"      ⏳ 服务器不可用，等待恢复{(' ('+label+')') if label else ''} 已等 {waited}s", flush=True)
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
    raise RuntimeError(f"请求多次失败 {method} {url}: {last_err}")


def _poll(task_id: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        r = _req("GET", f"{BASE}/api/tasks/{task_id}", timeout=30, allow_404=True)
        if r.status_code == 404:
            raise RuntimeError(f"{label} 任务 {task_id} 丢失（服务器疑似重启）")
        t = r.json()
        if t.get("done_count") != last:
            last = t.get("done_count")
            print(f"      {label} {t.get('done_count')}/{t.get('total')} ({t.get('status')})", flush=True)
        if t.get("status") == "done":
            return t.get("result") or {}
        if t.get("status") == "error":
            raise RuntimeError(f"{label} 任务失败: {t.get('error')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{label} 轮询超时 ({timeout}s)")


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
                    help="补封面后仍可能有个别失败，最多重扫补几轮")
    args = ap.parse_args()

    for rnd in range(1, args.max_rounds + 1):
        ids = _missing_cover_ids()
        print(f"\n第 {rnd} 轮：本轮萌宠缺封面角色 {len(ids)} 个")
        if not ids:
            print("✓ 所有萌宠角色封面已补齐。")
            return 0
        if args.dry_run:
            print(f"[DRY] 将分 {args.batch_size} 个/批提交 batch_cover(fill_missing)。样例:", ids[:5])
            return 0

        ok = 0
        for i in range(0, len(ids), args.batch_size):
            chunk = ids[i:i + args.batch_size]
            print(f"  提交批 {i//args.batch_size + 1}: {len(chunk)} 个角色", flush=True)
            try:
                r = _req("POST", f"{BASE}/api/characters/batch_cover",
                         json={"char_ids": chunk, "style_id": "", "mode": "fill_missing"},
                         timeout=120)
                task_id = r.json()["task_id"]
                result = _poll(task_id, COVER_TIMEOUT, "补封面")
                errs = result.get("errors") or result.get("cover_errors") or {}
                if errs:
                    print(f"      ⚠ 本批封面失败 {len(errs)} 个: {list(errs)[:3]}...", flush=True)
                ok += len(chunk) - len(errs)
            except Exception as e:  # noqa: BLE001
                print(f"      ✗ 批失败: {e}", flush=True)
        print(f"  第 {rnd} 轮完成，本轮成功约 {ok} 个。")
        time.sleep(3)

    left = _missing_cover_ids()
    print(f"\n达到最大轮次。仍缺封面 {len(left)} 个（多为供应商偶发拒图，可再跑一次）。")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
