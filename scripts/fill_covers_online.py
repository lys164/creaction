# -*- coding: utf-8 -*-
"""給線上「未生成封面、但有源圖」的角色補封面（realistic_portrait）。

只補有源圖的角色（無源圖的跳過——real 鏈路無源圖只能出無來源封面，按需求排除）。
序列 + 重試/健康門控 + 伺服器重啟後線上複核，逐個落進度，可斷點續跑。

用法：
  PYTHONPATH=. python3 scripts/fill_covers_online.py            # 用 /tmp/nocover.json
  PYTHONPATH=. python3 scripts/fill_covers_online.py ids.json   # 自定義 {"with_src":[...]}
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
COVER_STYLE = "realistic_portrait"
POLL_INTERVAL = 8
# 單個封面任務的最長等待。注意：客戶端超時放棄 ≠ 伺服器端任務停止（伺服器仍會把
# 該圖生圖跑完）。若客戶端超時過短、watchdog 又重新提交同一角色，會在伺服器端製造
# 重複任務累積、把主機 load 打爆。故超時須 ≥ 伺服器端實際耗時上限，給足時間自然完成，
# 避免重複提交。可用 COVER_TIMEOUT 覆蓋。
COVER_TIMEOUT = int(os.environ.get("COVER_TIMEOUT", "600"))
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "fill_covers_state.json"
IDS_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/nocover.json")


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


def _has_cover(char_id: str) -> bool:
    try:
        r = _req("GET", f"{BASE}/api/character/{char_id}", timeout=30)
        rec = r.json()
        return bool(rec.get("cover"))
    except Exception:  # noqa: BLE001
        return False


def _gen_cover(char_id: str) -> bool:
    """提交單角色封面任務並輪詢。任務丟失(伺服器重啟)則線上複核是否已出。"""
    r = _req("POST", f"{BASE}/api/cover", json={
        "char_id": char_id, "style_id": COVER_STYLE, "mode": "fill_missing",
    }, timeout=60)
    tid = r.json()["task_id"]
    deadline = time.time() + COVER_TIMEOUT
    while time.time() < deadline:
        tr = _req("GET", f"{BASE}/api/tasks/{tid}", timeout=30, allow_404=True)
        if tr.status_code == 404:
            _wait_healthy()
            return _has_cover(char_id)  # 重啟丟任務→看線上是否已出
        t = tr.json()
        if t.get("status") == "done":
            return True
        if t.get("status") == "error":
            err = str(t.get("error") or "")
            print(f"      任務錯誤: {err}", flush=True)
            if _has_cover(char_id):
                return True
            # 注意：上游的 "safety policy" 報錯是非確定性的假陽性——同一角色重試常能成功。
            # 因此一律當作可重試失敗（返回 False），交由外層輪次續跑，不做永久跳過。
            return False
        time.sleep(POLL_INTERVAL)
    return _has_cover(char_id)


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
    import threading
    from concurrent.futures import ThreadPoolExecutor
    conc = 3
    for a in sys.argv[2:]:
        if a.startswith("--concurrency="):
            conc = max(1, int(a.split("=", 1)[1]))

    ids = json.loads(IDS_PATH.read_text(encoding="utf-8"))
    targets = ids.get("with_src", []) if isinstance(ids, dict) else list(ids)
    state = _load_state()
    done = set(state["done"])
    # 每角色最多嘗試次數（safety policy 是非確定性假陽性，多試常能過）；超過則本輪記為
    # 放棄，避免 watchdog 無限重跑極個別真失敗角色。可用 GIVEUP_ATTEMPTS 調整。
    attempts = state.setdefault("attempts", {})
    giveup = int(os.environ.get("GIVEUP_ATTEMPTS", "6"))
    gaveup = set(state.setdefault("gaveup", []))
    todo = [c for c in targets if c not in done and c not in gaveup]
    # 按歷史嘗試次數升序排：沒試過/試得少的排前面先跑，反覆失敗的（多為上游持續
    # 過濾的 heermeng/chouxiang 類）自然沉到隊尾，避免釘子戶堵住隊首拖慢整體。
    todo.sort(key=lambda c: attempts.get(c, 0))
    total = len(todo)
    print(f"補封面目標: {len(targets)}（已完成 {len(done)}，放棄 {len(gaveup)}，"
          f"待跑 {total}）；畫風: {COVER_STYLE}；併發: {conc}\n", flush=True)

    lock = threading.Lock()
    counters = {"ok": 0, "err": 0, "skip": 0, "giveup": 0}

    def _mark_done(cid: str) -> None:
        with lock:
            state["done"].append(cid)
            _save_state(state)

    def _bump_attempt(cid: str) -> int:
        with lock:
            n = attempts.get(cid, 0) + 1
            attempts[cid] = n
            if n >= giveup and cid not in state["gaveup"]:
                state["gaveup"].append(cid)
            _save_state(state)
            return n

    def _one(job: tuple[int, str]) -> None:
        i, cid = job
        if _has_cover(cid):
            with lock:
                counters["skip"] += 1
            _mark_done(cid)
            print(f"[{i}/{total}] {cid} 已有封面，跳過", flush=True)
            return
        print(f"[{i}/{total}] {cid} 生成封面…", flush=True)
        try:
            if _gen_cover(cid):
                with lock:
                    counters["ok"] += 1
                _mark_done(cid)
                print(f"      ✓ 完成 {cid}", flush=True)
            else:
                n = _bump_attempt(cid)
                with lock:
                    counters["err"] += 1
                if n >= giveup:
                    with lock:
                        counters["giveup"] += 1
                    print(f"      ⊘ 放棄 {cid}（已嘗試 {n} 次仍失敗）", flush=True)
                else:
                    print(f"      ✗ 未生成 {cid}（第 {n} 次，稍後續跑重試）", flush=True)
        except Exception as e:  # noqa: BLE001
            _bump_attempt(cid)
            with lock:
                counters["err"] += 1
            print(f"      ✗ 失敗 {cid}: {e}", flush=True)

    jobs = list(enumerate(todo, 1))
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_one, jobs))

    print(f"\n完成: 成功 {counters['ok']}，跳過 {counters['skip']}，失敗 {counters['err']}，"
          f"放棄 {counters['giveup']}。累計已補 {len(state['done'])}，"
          f"累計放棄 {len(state['gaveup'])}。", flush=True)
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
