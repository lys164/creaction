# -*- coding: utf-8 -*-
"""补跑线上「source=feiren」缺封面角色的封面 —— 直接打线上服务(HTTP)。

背景：meme 非人物(nonhuman)批处理跑人设时，部分角色的封面被上游安全策略/
内容审核拒了(cover_errors)，人设已入账但缺封面。本脚本扫描线上所有
source=feiren 且无 cover 的角色，调用 /api/characters/batch_cover 逐个补封面。

- nonhuman 链路封面不套画风：generate_cover 内部对 track=nonhuman 忽略 style_id，
  按 identity+原图生成。这里 style_id 只是占位(服务器会忽略)。
- mode=fill_missing：缺 identity/cover_spec 会自动补齐再出图。
- 断点续跑：每轮重新扫描线上，只补仍缺封面的；成功即被下轮扫描排除。

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
STYLE_ID = "realistic_portrait"   # nonhuman 会忽略；仅占位
POLL_INTERVAL = 8
BATCH_TIMEOUT = 3600
DEFAULT_BATCH = 8                 # 每批补多少个角色(服务器内部再并发)


def _healthy() -> bool:
    try:
        return requests.get(f"{BASE}/api/languages", timeout=20).status_code == 200
    except requests.RequestException:
        return False


def _wait_healthy(label: str = "") -> None:
    delay, waited = 5, 0
    while not _healthy():
        print(f"   ⏳ 服务器不可用，等待恢复{(' ('+label+')') if label else ''} 已等 {waited}s",
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
    raise RuntimeError(f"请求多次失败 {method} {url}: {last_err}")


def _missing_cover_ids() -> list[str]:
    """线上扫描 source=feiren 且无 cover_url 的角色 char_id。"""
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
            raise RuntimeError(f"{label} 任务失败: {t.get('error')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{label} 轮询超时 ({timeout}s)")


def _cover_batch(char_ids: list[str]) -> dict:
    r = _req("POST", f"{BASE}/api/characters/batch_cover", json={
        "char_ids": char_ids, "style_id": STYLE_ID, "mode": "fill_missing",
    }, timeout=120)
    task_id = r.json()["task_id"]
    return _poll(task_id, BATCH_TIMEOUT, "补封面")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids = _missing_cover_ids()
    print(f"线上服务: {BASE}")
    print(f"source={SOURCE} 缺封面角色: {len(ids)} 个；每批 {args.batch} 个\n")
    if args.dry_run:
        for cid in ids[:10]:
            print("  ", cid)
        print(f"[DRY] 将分 {(len(ids)+args.batch-1)//args.batch} 批补封面。")
        return 0
    if not ids:
        print("没有缺封面的角色，无需补。")
        return 0

    ok_total, err_total = 0, 0
    batch_no = 0
    while True:
        batch = ids[:args.batch]
        if not batch:
            break
        batch_no += 1
        print(f"[批 {batch_no}] 补 {len(batch)} 个: {', '.join(batch)}", flush=True)
        try:
            res = _cover_batch(batch)
            covered = res.get("covered", [])
            errors = res.get("errors", {})
            ok_total += len(covered)
            err_total += len(errors)
            if errors:
                print(f"   ⚠ 本批 {len(errors)} 个仍失败: "
                      f"{list(errors.items())[:2]}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"   ✗ 本批异常: {e}", flush=True)
        # 重新扫描：成功的会被排除；失败的仍在，但为避免死循环，
        # 用「已尝试」集合推进——这里直接从剩余列表切掉已处理的这批。
        ids = ids[args.batch:]

    print(f"\n完成: 成功补 {ok_total} 个封面, 失败 {err_total} 个。")
    print("提示: 失败多为上游内容审核拒绝，可再次运行本脚本重扫重试。")
    return 0 if err_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
