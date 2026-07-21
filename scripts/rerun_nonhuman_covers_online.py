# -*- coding: utf-8 -*-
"""用「非人物(nonhuman)」鏈路重新生成 feiren / chouxiang 角色的封面 —— 打線上服務(HTTP)。

需求：feiren、chouxiang 兩個 source 的角色（現皆 track=nonhuman），用當前生成鏈路
重出封面。style_id 留空 → 非人物底座（不套畫風、不拼畫風詞，純 identity + 源圖 i2i）。

模式 image_only：保留現有 identity 與 cover_spec，只重渲染封面圖（構圖/場景不變，
走當前渲染鏈路重出）。只處理【已有封面】的角色（無封面者無 i2i 參考，跳過）。

斷點續跑：進度落 data/rerun_nonhuman_covers_state.json，已完成的跳過。
伺服器 5xx/超時自動等待恢復重試；任務丟失(重啟)視為需重試。

用法：
  PYTHONPATH=. python3 scripts/rerun_nonhuman_covers_online.py --limit 5   # 先試 5 個
  PYTHONPATH=. python3 scripts/rerun_nonhuman_covers_online.py             # 全部
  PYTHONPATH=. python3 scripts/rerun_nonhuman_covers_online.py --sources feiren
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
SOURCES = {"feiren", "chouxiang"}
STYLE_ID = ""            # 空 = 非人物底座，不套畫風
MODE = "image_only"      # 保留 identity/cover_spec，只重渲染封面圖
POLL_INTERVAL = 8
COVER_TIMEOUT = 1200
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "rerun_nonhuman_covers_state.json"


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


def _targets(sources: set[str]) -> list[str]:
    """指定 source 且已有封面的角色 char_id。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=60)
    return [c["char_id"] for c in r.json()
            if c.get("source") in sources and c.get("cover_url") and c.get("char_id")]


def _rerun_one(char_id: str) -> bool:
    """提交單角色封面重渲染任務並輪詢。任務丟失(重啟)則視為需重試(返回 False)。"""
    r = _req("POST", f"{BASE}/api/cover", json={
        "char_id": char_id, "style_id": STYLE_ID, "mode": MODE,
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
    ap.add_argument("--sources", default=",".join(sorted(SOURCES)),
                    help="逗號分隔的 source（預設 feiren,chouxiang）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--reset-state", action="store_true", help="清空斷點續跑記錄")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.reset_state and STATE_PATH.exists():
        STATE_PATH.unlink()

    sources = {s.strip() for s in args.sources.split(",") if s.strip()}
    targets = _targets(sources)
    state = _load_state()
    done = set(state["done"])
    todo = [c for c in targets if c not in done]
    if args.limit:
        todo = todo[:args.limit]
    total = len(todo)
    print(f"source={sorted(sources)} 有封面角色: {len(targets)}"
          f"（已完成 {len(done)}，本次待跑 {total}）；"
          f"畫風: 非人物底座(空)；模式: {MODE}；併發: {args.concurrency}\n", flush=True)

    if args.dry_run:
        for c in todo[:5]:
            print(f"  樣例: {c}")
        return 0
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
        print(f"[{i}/{total}] {cid} 重出非人物封面…", flush=True)
        try:
            if _rerun_one(cid):
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
