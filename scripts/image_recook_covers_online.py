# -*- coding: utf-8 -*-
"""source=image 的角色：以【當前封面】為參考重跑封面鏈路，讓新封面保留氣質但漂離上傳原圖。

背景：image 源角色的封面當初以上傳原圖為 i2i 參考生成，太像原圖。服務端新增
recook_from_cover 開關（見 pipeline.generate_cover）：開啟後 cover_spec 規劃與 i2i
渲染都改用角色當前封面作參考，源圖不再進入封面生成的任何一步。

本指令碼逐個（併發）呼叫 /api/cover?recook_from_cover=true 觸發重跑並輪詢任務：
  - 只處理 source=image 且【已有封面】的角色（無封面的沒有參考圖，跳過）。
  - 斷點續跑：進度落 data/image_recook_state.json，已完成的跳過。
  - 伺服器 5xx/超時自動等待恢復重試。

用法：
  PYTHONPATH=. python3 scripts/image_recook_covers_online.py --limit 5    # 先試 5 個
  PYTHONPATH=. python3 scripts/image_recook_covers_online.py              # 全部
  PYTHONPATH=. python3 scripts/image_recook_covers_online.py --style realistic_portrait --concurrency 3
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DEFAULT_STYLE = "realistic_portrait"
POLL_INTERVAL = 8
COVER_TIMEOUT = 1200
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "image_recook_state.json"


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


def _req(method: str, url: str, allow_404: bool = False, **kw) -> requests.Response:
    last = None
    for attempt in range(6):
        try:
            r = requests.request(method, url, **kw)
            if allow_404 and r.status_code == 404:
                return r
            if r.status_code >= 500 or r.status_code == 429:
                last = f"HTTP {r.status_code}"
                _wait_healthy()
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = str(e)
            _wait_healthy()
            time.sleep(min(5 * (attempt + 1), 30))
    raise RuntimeError(f"請求多次失敗 {method} {url}: {last}")


def _targets() -> list[str]:
    """source=image 且已有封面的角色 char_id。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=60)
    return [c["char_id"] for c in r.json()
            if c.get("source") == "image" and c.get("cover_url") and c.get("char_id")]


def _recook_one(char_id: str, style_id: str) -> bool:
    """提交單角色 recook 封面任務並輪詢。任務丟失(重啟)則視為需重試(返回 False)。"""
    r = _req("POST", f"{BASE}/api/cover", json={
        "char_id": char_id, "style_id": style_id,
        "mode": "fill_missing", "recook_from_cover": True,
    }, timeout=60)
    tid = r.json()["task_id"]
    deadline = time.time() + COVER_TIMEOUT
    while time.time() < deadline:
        tr = _req("GET", f"{BASE}/api/tasks/{tid}", timeout=30, allow_404=True)
        if tr.status_code == 404:
            _wait_healthy()
            return False
        t = tr.json()
        if t.get("status") == "done":
            return True
        if t.get("status") == "error":
            print(f"      任務錯誤 {char_id}: {t.get('error')}", flush=True)
            return False
        time.sleep(POLL_INTERVAL)
    return False


def _load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("done", [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done": []}


def _save_state(s: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default=DEFAULT_STYLE)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--reset-state", action="store_true", help="清空斷點續跑記錄")
    args = ap.parse_args()

    if args.reset_state and STATE_PATH.exists():
        STATE_PATH.unlink()

    targets = _targets()
    state = _load_state()
    done = set(state["done"])
    todo = [c for c in targets if c not in done]
    if args.limit:
        todo = todo[:args.limit]
    total = len(todo)
    print(f"image 源有封面角色: {len(targets)}（已完成 {len(done)}，本次待跑 {total}）；"
          f"畫風: {args.style}；併發: {args.concurrency}\n", flush=True)
    if not todo:
        print("沒有待處理角色。")
        return 0

    lock = threading.Lock()
    counters = {"ok": 0, "err": 0}

    def _mark_done(cid: str) -> None:
        with lock:
            state["done"].append(cid)
            _save_state(state)

    def _one(job: tuple[int, str]) -> None:
        i, cid = job
        print(f"[{i}/{total}] {cid} recook 封面…", flush=True)
        try:
            if _recook_one(cid, args.style):
                with lock:
                    counters["ok"] += 1
                _mark_done(cid)
                print(f"      ✓ 完成 {cid}", flush=True)
            else:
                with lock:
                    counters["err"] += 1
                print(f"      ✗ 未完成 {cid}（稍後可續跑重試）", flush=True)
        except Exception as e:  # noqa: BLE001
            with lock:
                counters["err"] += 1
            print(f"      ✗ 失敗 {cid}: {e}", flush=True)

    jobs = list(enumerate(todo, 1))
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        list(ex.map(_one, jobs))

    print(f"\n完成：成功 {counters['ok']}，失敗 {counters['err']}。"
          f"累計已跑 {len(state['done'])}。", flush=True)
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
