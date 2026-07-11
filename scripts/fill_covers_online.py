# -*- coding: utf-8 -*-
"""给线上「未生成封面、但有源图」的角色补封面（realistic_portrait）。

只补有源图的角色（无源图的跳过——real 链路无源图只能出无来源封面，按需求排除）。
串行 + 重试/健康门控 + 服务器重启后线上复核，逐个落进度，可断点续跑。

用法：
  PYTHONPATH=. python3 scripts/fill_covers_online.py            # 用 /tmp/nocover.json
  PYTHONPATH=. python3 scripts/fill_covers_online.py ids.json   # 自定义 {"with_src":[...]}
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
# 单个封面任务的最长等待。注意：客户端超时放弃 ≠ 服务器端任务停止（服务器仍会把
# 该图生图跑完）。若客户端超时过短、watchdog 又重新提交同一角色，会在服务器端制造
# 重复任务累积、把主机 load 打爆。故超时须 ≥ 服务器端实际耗时上限，给足时间自然完成，
# 避免重复提交。可用 COVER_TIMEOUT 覆盖。
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
        print(f"      ⏳ 服务器不可用，等待恢复 已等 {waited}s", flush=True)
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
    raise RuntimeError(f"请求多次失败 {method} {url}: {last}")


def _has_cover(char_id: str) -> bool:
    try:
        r = _req("GET", f"{BASE}/api/character/{char_id}", timeout=30)
        rec = r.json()
        return bool(rec.get("cover"))
    except Exception:  # noqa: BLE001
        return False


def _gen_cover(char_id: str) -> bool:
    """提交单角色封面任务并轮询。任务丢失(服务器重启)则线上复核是否已出。"""
    r = _req("POST", f"{BASE}/api/cover", json={
        "char_id": char_id, "style_id": COVER_STYLE, "mode": "fill_missing",
    }, timeout=60)
    tid = r.json()["task_id"]
    deadline = time.time() + COVER_TIMEOUT
    while time.time() < deadline:
        tr = _req("GET", f"{BASE}/api/tasks/{tid}", timeout=30, allow_404=True)
        if tr.status_code == 404:
            _wait_healthy()
            return _has_cover(char_id)  # 重启丢任务→看线上是否已出
        t = tr.json()
        if t.get("status") == "done":
            return True
        if t.get("status") == "error":
            err = str(t.get("error") or "")
            print(f"      任务错误: {err}", flush=True)
            if _has_cover(char_id):
                return True
            # 注意：上游的 "safety policy" 报错是非确定性的假阳性——同一角色重试常能成功。
            # 因此一律当作可重试失败（返回 False），交由外层轮次续跑，不做永久跳过。
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
    # 每角色最多尝试次数（safety policy 是非确定性假阳性，多试常能过）；超过则本轮记为
    # 放弃，避免 watchdog 无限重跑极个别真失败角色。可用 GIVEUP_ATTEMPTS 调整。
    attempts = state.setdefault("attempts", {})
    giveup = int(os.environ.get("GIVEUP_ATTEMPTS", "6"))
    gaveup = set(state.setdefault("gaveup", []))
    todo = [c for c in targets if c not in done and c not in gaveup]
    # 按历史尝试次数升序排：没试过/试得少的排前面先跑，反复失败的（多为上游持续
    # 过滤的 heermeng/chouxiang 类）自然沉到队尾，避免钉子户堵住队首拖慢整体。
    todo.sort(key=lambda c: attempts.get(c, 0))
    total = len(todo)
    print(f"补封面目标: {len(targets)}（已完成 {len(done)}，放弃 {len(gaveup)}，"
          f"待跑 {total}）；画风: {COVER_STYLE}；并发: {conc}\n", flush=True)

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
            print(f"[{i}/{total}] {cid} 已有封面，跳过", flush=True)
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
                    print(f"      ⊘ 放弃 {cid}（已尝试 {n} 次仍失败）", flush=True)
                else:
                    print(f"      ✗ 未生成 {cid}（第 {n} 次，稍后续跑重试）", flush=True)
        except Exception as e:  # noqa: BLE001
            _bump_attempt(cid)
            with lock:
                counters["err"] += 1
            print(f"      ✗ 失败 {cid}: {e}", flush=True)

    jobs = list(enumerate(todo, 1))
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_one, jobs))

    print(f"\n完成: 成功 {counters['ok']}，跳过 {counters['skip']}，失败 {counters['err']}，"
          f"放弃 {counters['giveup']}。累计已补 {len(state['done'])}，"
          f"累计放弃 {len(state['gaveup'])}。", flush=True)
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
