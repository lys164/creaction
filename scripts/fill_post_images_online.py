# -*- coding: utf-8 -*-
"""給線上「應有圖卻缺圖」的 IG 帖子補圖（format=image_text 但 image.url 為空）。

/api/ig_posts/{cid}/{pid}/image 是同步重渲染：請求內直接出圖，成功返回帶 image.url 的 post。
健康門控 + 重試 + 斷點續跑（data/fill_posts_state.json）。可配合 watchdog 持續跑。

用法：
  PYTHONPATH=. python3 scripts/fill_post_images_online.py            # 用 /tmp/posts_to_fill.json
  PYTHONPATH=. python3 scripts/fill_post_images_online.py jobs.json --concurrency=2
其中 jobs.json 形如 {"char_id":[post_id,...], ...}
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "fill_posts_state.json"
JOBS_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/posts_to_fill.json")
# 每條帖子圖最多嘗試次數（safety policy 是非確定性假陽性，重試常能過）。
GIVEUP = int(os.environ.get("GIVEUP_ATTEMPTS", "6"))
# 單次同步重渲染的超時：出圖約 2-3 分鐘，給足 10 分鐘避免中斷正在成功的請求。
RENDER_TIMEOUT = int(os.environ.get("RENDER_TIMEOUT", "600"))


def _healthy() -> bool:
    try:
        return requests.get(f"{BASE}/api/languages", timeout=20).status_code == 200
    except requests.RequestException:
        return False


def _wait_healthy() -> None:
    delay, waited = 5, 0
    while not _healthy():
        print(f"      ⏳ 伺服器不可用，等待恢復 已等 {waited}s", flush=True)
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 60)


def _load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("done", [])
            s.setdefault("attempts", {})
            s.setdefault("gaveup", [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done": [], "attempts": {}, "gaveup": []}


def _save_state(s: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _post_has_image(cid: str, pid: str) -> bool:
    """線上複核該帖子是否已有圖（用於重啟/超時後確認）。"""
    try:
        r = requests.get(f"{BASE}/api/ig_posts/{cid}/latest", timeout=30)
        for p in r.json().get("posts", []):
            if p.get("post_id") == pid:
                return bool((p.get("image") or {}).get("url"))
    except requests.RequestException:
        pass
    return False


def _fill_one_image(cid: str, pid: str) -> bool:
    """同步重渲染一條帖子的圖。成功返回 True。"""
    url = f"{BASE}/api/ig_posts/{cid}/{pid}/image"
    try:
        r = requests.post(url, json={}, timeout=RENDER_TIMEOUT)
        if r.status_code >= 500 or r.status_code == 429:
            _wait_healthy()
            return _post_has_image(cid, pid)
        r.raise_for_status()
        post = (r.json() or {}).get("post") or {}
        if (post.get("image") or {}).get("url"):
            return True
        return _post_has_image(cid, pid)
    except requests.RequestException as e:
        print(f"      請求失敗: {str(e)[:120]}", flush=True)
        _wait_healthy()
        return _post_has_image(cid, pid)


def main() -> int:
    conc = 2
    for a in sys.argv[2:]:
        if a.startswith("--concurrency="):
            conc = max(1, int(a.split("=", 1)[1]))

    jobs_map = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    # 展開成 (cid,pid) 列表，key = "cid/pid"
    all_jobs = [(cid, pid) for cid, pids in jobs_map.items() for pid in pids]
    state = _load_state()
    done = set(state["done"])
    gaveup = set(state["gaveup"])
    attempts = state["attempts"]
    todo = [(c, p) for (c, p) in all_jobs
            if f"{c}/{p}" not in done and f"{c}/{p}" not in gaveup]
    todo.sort(key=lambda cp: attempts.get(f"{cp[0]}/{cp[1]}", 0))
    total = len(todo)
    print(f"補帖子圖目標: {len(all_jobs)}（已完成 {len(done)}，放棄 {len(gaveup)}，"
          f"待跑 {total}）；併發: {conc}\n", flush=True)

    lock = threading.Lock()
    counters = {"ok": 0, "err": 0, "giveup": 0}

    def _mark_done(key: str) -> None:
        with lock:
            state["done"].append(key)
            _save_state(state)

    def _bump(key: str) -> int:
        with lock:
            n = attempts.get(key, 0) + 1
            attempts[key] = n
            if n >= GIVEUP and key not in state["gaveup"]:
                state["gaveup"].append(key)
            _save_state(state)
            return n

    def _one(job) -> None:
        i, (cid, pid) = job
        key = f"{cid}/{pid}"
        if _post_has_image(cid, pid):
            _mark_done(key)
            print(f"[{i}/{total}] {key} 已有圖，跳過", flush=True)
            return
        print(f"[{i}/{total}] {key} 補圖…", flush=True)
        try:
            if _fill_one_image(cid, pid):
                with lock:
                    counters["ok"] += 1
                _mark_done(key)
                print(f"      ✓ 完成 {key}", flush=True)
            else:
                n = _bump(key)
                with lock:
                    counters["err"] += 1
                if n >= GIVEUP:
                    with lock:
                        counters["giveup"] += 1
                    print(f"      ⊘ 放棄 {key}（已試 {n} 次）", flush=True)
                else:
                    print(f"      ✗ 未生成 {key}（第 {n} 次，稍後續跑）", flush=True)
        except Exception as e:  # noqa: BLE001
            _bump(key)
            with lock:
                counters["err"] += 1
            print(f"      ✗ 失敗 {key}: {e}", flush=True)

    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_one, list(enumerate(todo, 1))))

    print(f"\n完成: 成功 {counters['ok']}，失敗 {counters['err']}，放棄 {counters['giveup']}。"
          f"累計已補 {len(state['done'])}，累計放棄 {len(state['gaveup'])}。", flush=True)
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
