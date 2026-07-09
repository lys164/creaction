# -*- coding: utf-8 -*-
"""扫缺封面：把线上所有 source=mengnv 且封面为空的角色补出封面。

用于主批跑完后收尾——部分角色的封面在生成时被生图供应商内容安全拦截
（kie "content could not be processed"），本脚本重试这些漏掉的封面。

策略：对每个缺封面角色调 /api/cover（mode=fill_missing）。默认 use_reference=None
（写实画风+有源图自动 i2i）；失败则依次尝试 use_reference=False（纯文生图，绕开被判
敏感的原图）再重试。全部数据落线上。

用法：
  PYTHONPATH=. python3 scripts/sweep_missing_covers_online.py [--source mengnv]
      [--style realistic_portrait] [--concurrency 3] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
POLL_INTERVAL = 8
COVER_TIMEOUT = 900


def _req(method: str, url: str, allow_404: bool = False, **kw) -> requests.Response:
    last_err = None
    for attempt in range(6):
        try:
            r = requests.request(method, url, **kw)
            if allow_404 and r.status_code == 404:
                return r
            if r.status_code >= 500 or r.status_code == 429:
                last_err = f"HTTP {r.status_code}"
                time.sleep(min(5 * (attempt + 1), 30))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(min(5 * (attempt + 1), 30))
    raise RuntimeError(f"请求多次失败 {method} {url}: {last_err}")


def _poll(task_id: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _req("GET", f"{BASE}/api/tasks/{task_id}", timeout=30, allow_404=True)
        if r.status_code == 404:
            raise RuntimeError(f"{label} 任务 {task_id} 丢失（服务器疑似重启）")
        t = r.json()
        if t.get("status") == "done":
            return t.get("result") or {}
        if t.get("status") == "error":
            raise RuntimeError(f"{label} 失败: {t.get('error')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{label} 轮询超时 ({timeout}s)")


def _list_missing(source: str) -> list[str]:
    """列出线上 source==source 且 cover 为空的角色 char_id。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=60)
    chars = r.json()
    missing = []
    for c in chars:
        cid = c.get("char_id")
        if not cid:
            continue
        if c.get("cover_url"):  # 列表已带 cover_url，非空即有封面
            continue
        # 需按 source 过滤：拉详情确认 source（列表不含 source）
        try:
            d = _req("GET", f"{BASE}/api/character/{cid}", timeout=30).json()
        except Exception:  # noqa: BLE001
            continue
        if d.get("source") != source:
            continue
        if not d.get("cover"):
            missing.append(cid)
    return missing


def _make_cover(cid: str, style: str) -> None:
    """补一个角色封面：先 auto(i2i)，失败再退纯文生图(use_reference=False)。"""
    last = None
    for use_ref in (None, False):
        try:
            r = _req("POST", f"{BASE}/api/cover", json={
                "char_id": cid, "style_id": style,
                "use_reference": use_ref, "mode": "fill_missing",
            }, timeout=120)
            _poll(r.json()["task_id"], COVER_TIMEOUT, f"封面 {cid}")
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise RuntimeError(f"{cid} 补封面失败: {last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="mengnv")
    ap.add_argument("--style", default="realistic_portrait")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"线上服务: {BASE}；扫 source={args.source} 缺封面角色…", flush=True)
    missing = _list_missing(args.source)
    if args.limit and args.limit > 0:
        missing = missing[:args.limit]
    print(f"缺封面角色: {len(missing)}", flush=True)
    if args.dry_run or not missing:
        for cid in missing[:20]:
            print(f"  {cid}")
        return 0

    counters = {"ok": 0, "err": 0}
    total = len(missing)

    def _run(job):
        idx, cid = job
        print(f"[{idx}/{total}] 补封面 {cid}", flush=True)
        try:
            _make_cover(cid, args.style)
            counters["ok"] += 1
            print(f"      ✓ {cid}", flush=True)
        except Exception as e:  # noqa: BLE001
            counters["err"] += 1
            print(f"      ✗ {cid}: {e}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        list(ex.map(_run, list(enumerate(missing, 1))))

    print(f"\n完成: 成功 {counters['ok']}, 失败 {counters['err']}（失败多为生图内容拦截）")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
