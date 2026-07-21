# -*- coding: utf-8 -*-
"""監視上游視覺(vision)API 是否恢復，一旦恢復正常就自動開跑人設+封面批次。

判定"恢復"：連續 PROBE_OK_STREAK 次帶圖 vision 探測都在 PROBE_OK_SEC 秒內成功。
探測透過伺服器內網執行（docker exec），用真實 key，真實鏈路，不產生生成費用
（只是最小 vision 呼叫）。恢復後啟動 batch_real_track_online.py（序列、--no-posts）。

用法：
  PYTHONPATH=. python3 scripts/watch_and_run.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
PROBE_OK_SEC = 120         # 單語言人設(含vision) ≤ 該秒數算"正常"
PROBE_OK_STREAK = 2        # 連續幾次正常才算恢復
PROBE_POLL_TIMEOUT = 200   # 單次探測任務最長等待
CHECK_INTERVAL = 120       # 未恢復時每隔多少秒再探
ROOT = Path(__file__).resolve().parent.parent

# 探測用的小圖（真實源圖之一，走公網 HTTP 上傳，真實 vision 鏈路）。
_PROBE_IMG = ROOT / "data" / "uploads"


def _probe_image() -> Path | None:
    if not _PROBE_IMG.exists():
        return None
    for p in sorted(_PROBE_IMG.glob("*.png")):
        return p
    for p in sorted(_PROBE_IMG.glob("*.jpg")):
        return p
    return None


def probe() -> float | None:
    """HTTP 端到端探測：上傳 1 圖生成【單語言、無封面】人設，看是否正常返回角色。

    返回耗時秒數；失敗/超時返回 None。探測本身會在伺服器建 1 個探測角色
    （source=probe），可忽略/後續清理，不影響正式批次去重。
    """
    img = _probe_image()
    if img is None:
        return None
    t0 = time.time()
    try:
        with open(img, "rb") as fh:
            r = requests.post(f"{BASE}/api/personas",
                              data={"langs": "en", "one_per_image": "false",
                                    "with_cover": "false", "track": "real",
                                    "source": "probe"},
                              files=[("files", (img.name, fh, "image/png"))],
                              timeout=60)
        r.raise_for_status()
        tid = r.json()["task_id"]
    except (requests.RequestException, KeyError, ValueError):
        return None
    deadline = time.time() + PROBE_POLL_TIMEOUT
    while time.time() < deadline:
        try:
            t = requests.get(f"{BASE}/api/tasks/{tid}", timeout=20).json()
        except (requests.RequestException, ValueError):
            time.sleep(5)
            continue
        if t.get("status") == "done":
            res = t.get("result") or {}
            chars = res.get("characters", [])
            return time.time() - t0 if chars else None
        if t.get("status") == "error":
            return None
        time.sleep(5)
    return None


def main() -> int:
    print("監視上游 vision 恢復中……（正常閾值 ≤%ds，連續 %d 次）"
          % (PROBE_OK_SEC, PROBE_OK_STREAK), flush=True)
    streak = 0
    while True:
        sec = probe()
        ts = time.strftime("%H:%M:%S")
        if sec is not None and sec <= PROBE_OK_SEC:
            streak += 1
            print(f"[{ts}] vision OK {sec:.1f}s  (連續 {streak}/{PROBE_OK_STREAK})",
                  flush=True)
            if streak >= PROBE_OK_STREAK:
                print(f"[{ts}] 上游已恢復，啟動批次！", flush=True)
                break
        else:
            streak = 0
            desc = f"{sec:.1f}s 偏慢" if sec is not None else "超時/失敗"
            print(f"[{ts}] vision 未恢復（{desc}），{CHECK_INTERVAL}s 後再探", flush=True)
            time.sleep(CHECK_INTERVAL)
            continue
        time.sleep(5)

    # 恢復 → 啟動批次（序列、只出人設+封面）。前臺執行，日誌直連本程式。
    cmd = [sys.executable, "scripts/batch_real_track_online.py",
           "--seed", "42", "--concurrency", "1", "--no-posts"]
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    print("exec:", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


if __name__ == "__main__":
    sys.exit(main())
