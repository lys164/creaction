# -*- coding: utf-8 -*-
"""為「已完成角色(人設+封面)」批次生成 INS 帖子 —— 直接打線上服務(HTTP)。

對應需求：feiren / image 兩個 source 的角色都已有完整人設和封面，
按各自鏈路補齊帖子。兩者都走 /api/ig_posts/batch，差異只在 track / 封面畫風：

- source="image"  → track="real"，帖子自拍以 realistic_portrait 封面做圖生圖人臉錨點。
- source="feiren" → track="nonhuman"（非人物鏈路：不套畫風，按 identity + 原圖出圖）。

只處理【人設 + 封面都有】(has_identity 且 cover_url) 且【尚無帖子】的角色，天然冪等：
已有帖子的角色跳過，中斷後重跑自動續。進度寫 data/batch_posts_online_state.json。

用法：
  PYTHONPATH=. python3 scripts/batch_posts_online.py [--source feiren|image|all]
  PYTHONPATH=. python3 scripts/batch_posts_online.py --dry-run
  PYTHONPATH=. python3 scripts/batch_posts_online.py --limit 20 --concurrency 4
  PYTHONPATH=. python3 scripts/batch_posts_online.py --no-post-images   # 只出文案
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

_STATE_LOCK = threading.Lock()

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "batch_posts_online_state.json"

# 每個 source 的鏈路引數：track + 封面/自拍錨點畫風(nonhuman 不套畫風 → style_id 為空)
SOURCE_CHAINS = {
    "image":  {"track": "real",     "style_id": "realistic_portrait"},
    "feiren": {"track": "nonhuman", "style_id": ""},
}

POLL_INTERVAL = 20         # 輪詢間隔（單批出圖數分鐘，20s 足夠，且大幅降低對 traefik 的請求壓力）
POSTS_TIMEOUT = 2400        # 單角色一批帖子(~9 圖)
DEFAULT_CONCURRENCY = 4     # 同時並行的角色數


def load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("done", [])
            s.setdefault("failed", [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done": [], "failed": []}


def save_state(state: dict) -> None:
    """原子寫盤（tmp+rename）。呼叫方持有 _STATE_LOCK。"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _healthy() -> bool:
    try:
        r = requests.get(f"{BASE}/api/languages", timeout=20)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _wait_healthy(label: str = "") -> None:
    """伺服器 502/宕機時阻塞等待其恢復（指數退避，最長 60s/次）。"""
    delay, waited = 5, 0
    while not _healthy():
        print(f"      伺服器不可用，等待恢復{(' ('+label+')') if label else ''} "
              f"已等 {waited}s", flush=True)
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 60)


class TaskLost(Exception):
    """任務在伺服器端丟失（程式重啟，記憶體態任務清空 → /api/tasks 返回 404）。"""


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
    raise RuntimeError(f"請求多次失敗 {method} {url}: {last_err}")


def _poll(task_id: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        r = _req("GET", f"{BASE}/api/tasks/{task_id}", timeout=30, allow_404=True)
        if r.status_code == 404:
            raise TaskLost(f"{label} 任務 {task_id} 丟失（伺服器疑似重啟）")
        t = r.json()
        if t.get("done_count") != last:
            last = t.get("done_count")
            print(f"      {label} {t.get('done_count')}/{t.get('total')} "
                  f"({t.get('status')})", flush=True)
        if t.get("status") == "done":
            return t.get("result") or {}
        if t.get("status") == "error":
            raise RuntimeError(f"{label} 任務失敗: {t.get('error')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{label} 輪詢超時 ({timeout}s)")


def _has_posts_online(char_id: str) -> bool:
    try:
        r = _req("GET", f"{BASE}/api/ig_posts/{char_id}/latest", timeout=30)
        ig = r.json()
    except Exception:  # noqa: BLE001
        return False
    return bool(ig and ig.get("posts"))


def _all_nonempty_sources() -> list[str]:
    """線上現存的所有【非空】source（排除無來源 ""）。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=120)
    chars = r.json()
    srcs = {(c.get("source") or "") for c in chars}
    return sorted(s for s in srcs if s)


def _fetch_targets(source: str) -> list[dict]:
    """線上拉全部角色，篩出該 source 下【人設+封面都有】的角色。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=120)
    chars = r.json()
    return [c for c in chars
            if (c.get("source") or "") == source
            and c.get("has_identity") and c.get("cover_url")]


def _gen_posts(char_id: str, chain: dict, with_images: bool) -> dict:
    """為單個角色生成帖子；任務因伺服器重啟丟失時先線上複核，否則重試。

    chain 為 None 時：不指定 track/style_id，服務端按角色自身儲存的 track 與
    style_id 生成（各 source 鏈路不同：light/flirt/kdrama/nonhuman…，硬編碼會錯）。
    """
    for attempt in range(3):
        payload = {"char_ids": [char_id], "with_images": with_images}
        if chain is not None:
            payload["style_id"] = chain["style_id"] or None
            payload["track"] = chain["track"]
        r = _req("POST", f"{BASE}/api/ig_posts/batch", json=payload, timeout=120)
        task_id = r.json()["task_id"]
        try:
            return _poll(task_id, POSTS_TIMEOUT, f"帖子 {char_id}")
        except TaskLost:
            # 任務丟失（伺服器重啟，記憶體態任務清空）：線上複核是否已完成。
            _wait_healthy("posts")
            if _has_posts_online(char_id):
                print(f"      帖子任務丟失但線上已完成，視為成功 {char_id}", flush=True)
                return {"generated": [{"char_id": char_id}], "errors": {}}
            print(f"      帖子任務丟失，重試 {char_id} (第 {attempt + 2} 次)", flush=True)
        except TimeoutError:
            # 輪詢超時：高併發下出圖排隊久，任務往往仍在後臺跑完。先線上複核，
            # 已有帖子就算成功（避免把其實成功的角色誤判失敗、下輪重複重跑）。
            if _has_posts_online(char_id):
                print(f"      帖子輪詢超時但線上已完成，視為成功 {char_id}", flush=True)
                return {"generated": [{"char_id": char_id}], "errors": {}}
            print(f"      帖子輪詢超時且線上無帖子，重試 {char_id} (第 {attempt + 2} 次)", flush=True)
    # 最後再複核一次，避免最後一輪剛好在超時後完成
    if _has_posts_online(char_id):
        return {"generated": [{"char_id": char_id}], "errors": {}}
    raise RuntimeError("帖子多次超時/丟失，暫緩（續跑會自動補）")


def main() -> int:
    global STATE_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all",
                    help="feiren / image / all(=feiren+image) / all_nonempty"
                         "(所有非空 source，按角色自身 track+style 生成) / 或任意具體 source 名")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="同時並行的角色數（預設 4）")
    ap.add_argument("--no-post-images", action="store_true",
                    help="帖子只出文案，不配圖")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", type=str, default=str(STATE_PATH))
    args = ap.parse_args()

    STATE_PATH = Path(args.state)
    with_images = not args.no_post_images
    if args.source == "all":
        sources = ["feiren", "image"]
    elif args.source == "all_nonempty":
        sources = _all_nonempty_sources()
        print(f"all_nonempty → 覆蓋非空 source: {', '.join(sources)}")
    else:
        sources = [args.source]

    def _chain_for(src: str) -> dict | None:
        """已知硬編碼鏈路的用其配置；其餘 source 返回 None，"""
        """由服務端按角色自身儲存的 track+style_id 生成。"""
        return SOURCE_CHAINS.get(src)

    state = load_state()
    done = set(state["done"])

    # 組裝待跑清單：每個 source 拉線上完整角色，跳過已有帖子/本地已記完成的。
    # 線上帖子探測並行化（幾百個角色順序探測太慢，會拖垮啟動）。
    todo: list[tuple[str, str]] = []   # (char_id, source)
    summary = {}
    for src in sources:
        targets = _fetch_targets(src)
        unknown = [c["char_id"] for c in targets if c["char_id"] not in done]
        with ThreadPoolExecutor(max_workers=32) as ex:
            flags = list(ex.map(_has_posts_online, unknown))
        need = []
        newly_done = []
        for cid, has in zip(unknown, flags):
            if has:
                newly_done.append(cid)
            else:
                need.append(cid)
        with _STATE_LOCK:
            for cid in newly_done:
                if cid not in done:
                    state["done"].append(cid)
                    done.add(cid)
        summary[src] = {"complete": len(targets), "todo": len(need)}
        todo.extend((cid, src) for cid in need)
    save_state(state)

    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    print(f"\n線上服務: {BASE}")
    for src in sources:
        ch = _chain_for(src)
        chain_desc = (f"track={ch['track']}, 畫風={ch['style_id'] or '無'}"
                      if ch else "按角色自身 track+style")
        print(f"  {src}: 完整角色 {summary[src]['complete']}，待生成帖子 {summary[src]['todo']}"
              f"（{chain_desc}）")
    print(f"本輪實際生成: {len(todo)} 個角色；帖子: {'配圖' if with_images else '僅文案'}\n")

    if args.dry_run:
        for cid, src in todo[:5]:
            print(f"  樣例: {src}  {cid}  → {_chain_for(src) or '角色自身鏈路'}")
        print(f"[DRY] 計劃為 {len(todo)} 個角色各生成一批 INS 帖子。")
        return 0

    if not todo:
        print("沒有待生成的角色，全部已完成。")
        return 0

    conc = max(1, args.concurrency)
    total = len(todo)
    counters = {"ok": 0, "err": 0}

    def _run(job: tuple[int, str, str]) -> None:
        idx, cid, src = job
        chain = _chain_for(src)
        track_desc = chain["track"] if chain else "自身"
        print(f"[{idx}/{total}] {src} {cid} (track={track_desc})", flush=True)
        try:
            res = _gen_posts(cid, chain, with_images)
            if res.get("errors"):
                print(f"      帖子 errors {cid}: {res['errors']}", flush=True)
                raise RuntimeError(str(res["errors"]))
            with _STATE_LOCK:
                state["done"].append(cid)
                if cid in state["failed"]:
                    state["failed"].remove(cid)
                save_state(state)
                counters["ok"] += 1
            print(f"      完成 {cid}", flush=True)
        except Exception as e:  # noqa: BLE001
            with _STATE_LOCK:
                if cid not in state["failed"]:
                    state["failed"].append(cid)
                save_state(state)
                counters["err"] += 1
            print(f"      失敗[{idx}] {cid}: {e}", flush=True)

    jobs = [(i, cid, src) for i, (cid, src) in enumerate(todo, 1)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run, jobs))

    with _STATE_LOCK:
        done_n = len(state["done"])
    print(f"\n完成: 成功 {counters['ok']} 個, 失敗 {counters['err']} 個。"
          f"累計帖子完成 {done_n} 個角色。")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
