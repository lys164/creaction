# -*- coding: utf-8 -*-
"""监视上游视觉(vision)API 是否恢复，一旦恢复正常就自动开跑人设+封面批量。

判定"恢复"：连续 PROBE_OK_STREAK 次带图 vision 探测都在 PROBE_OK_SEC 秒内成功。
探测通过服务器内网执行（docker exec），用真实 key，真实链路，不产生生成费用
（只是最小 vision 调用）。恢复后启动 batch_real_track_online.py（串行、--no-posts）。

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
PROBE_OK_SEC = 120         # 单语言人设(含vision) ≤ 该秒数算"正常"
PROBE_OK_STREAK = 2        # 连续几次正常才算恢复
PROBE_POLL_TIMEOUT = 200   # 单次探测任务最长等待
CHECK_INTERVAL = 120       # 未恢复时每隔多少秒再探
ROOT = Path(__file__).resolve().parent.parent

# 探测用的小图（真实源图之一，走公网 HTTP 上传，真实 vision 链路）。
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
    """HTTP 端到端探测：上传 1 图生成【单语言、无封面】人设，看是否正常返回角色。

    返回耗时秒数；失败/超时返回 None。探测本身会在服务器建 1 个探测角色
    （source=probe），可忽略/后续清理，不影响正式批次去重。
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
    print("监视上游 vision 恢复中……（正常阈值 ≤%ds，连续 %d 次）"
          % (PROBE_OK_SEC, PROBE_OK_STREAK), flush=True)
    streak = 0
    while True:
        sec = probe()
        ts = time.strftime("%H:%M:%S")
        if sec is not None and sec <= PROBE_OK_SEC:
            streak += 1
            print(f"[{ts}] vision OK {sec:.1f}s  (连续 {streak}/{PROBE_OK_STREAK})",
                  flush=True)
            if streak >= PROBE_OK_STREAK:
                print(f"[{ts}] 上游已恢复，启动批量！", flush=True)
                break
        else:
            streak = 0
            desc = f"{sec:.1f}s 偏慢" if sec is not None else "超时/失败"
            print(f"[{ts}] vision 未恢复（{desc}），{CHECK_INTERVAL}s 后再探", flush=True)
            time.sleep(CHECK_INTERVAL)
            continue
        time.sleep(5)

    # 恢复 → 启动批量（串行、只出人设+封面）。前台执行，日志直连本进程。
    cmd = [sys.executable, "scripts/batch_real_track_online.py",
           "--seed", "42", "--concurrency", "1", "--no-posts"]
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    print("exec:", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


if __name__ == "__main__":
    sys.exit(main())
