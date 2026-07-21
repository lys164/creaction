# -*- coding: utf-8 -*-
"""為【已有帖子但缺圖】的 post 批次補圖 —— 直接打線上服務(HTTP)。

背景：帖子批次判定"完成"只看有沒有 posts 記錄，不校驗每條 post 的圖是否真出成功。
早期出圖源不足/失敗時，文案生成了但圖沒出來，照樣被記為完成。本指令碼掃描所有角色，
找出 image 缺失(無 image 或 image.url 為空)的 post，逐條呼叫同步補圖介面重出。

補圖介面：POST /api/ig_posts/{char_id}/{post_id}/image （同步，直接返回出好圖的 post）。
出圖走服務端的 7 渠道全平攤 round-robin。天然冪等：已有圖的 post 跳過；中斷重跑自動續。
進度按 (char_id, post_id) 記在 data/backfill_post_images_state.json。

用法：
  python3 scripts/backfill_post_images_online.py [--concurrency 48] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

_STATE_LOCK = threading.Lock()
BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "backfill_post_images_state.json"
DEFAULT_CONCURRENCY = 48
IMAGE_TIMEOUT = 300         # 單張補圖(同步)：含 submit+poll+下載，給足餘量


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
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _healthy() -> bool:
    try:
        return requests.get(f"{BASE}/api/languages", timeout=20).status_code == 200
    except requests.RequestException:
        return False


def _wait_healthy(label: str = "") -> None:
    delay, waited = 5, 0
    while not _healthy():
        print(f"      伺服器不可用，等待恢復{(' ('+label+')') if label else ''} 已等 {waited}s",
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
                _wait_healthy(url.rsplit("/", 2)[-1])
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = str(e)
            _wait_healthy(url.rsplit("/", 2)[-1])
            time.sleep(min(5 * (attempt + 1), 30))
    raise RuntimeError(f"請求多次失敗 {method} {url}: {last_err}")


def _post_has_image(post: dict) -> bool:
    img = post.get("image") or {}
    return bool(isinstance(img, dict) and img.get("url"))


def _fetch_targets() -> list[tuple[str, str]]:
    """掃全部角色，返回所有【缺圖】post 的 (char_id, post_id) 列表。"""
    chars = _req("GET", f"{BASE}/api/characters", timeout=120).json()
    cand = [c["char_id"] for c in chars
            if c.get("has_identity") and c.get("cover_url")]

    def missing_of(cid: str) -> list[tuple[str, str]]:
        try:
            ig = _req("GET", f"{BASE}/api/ig_posts/{cid}/latest", timeout=30).json()
        except Exception:  # noqa: BLE001
            return []
        return [(cid, p["post_id"]) for p in (ig.get("posts") or [])
                if not _post_has_image(p)]

    out: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=32) as ex:
        for lst in ex.map(missing_of, cand):
            out.extend(lst)
    return out


def _rerender(cid: str, pid: str) -> bool:
    """對單條 post 補圖；返回 True=出圖成功。"""
    r = _req("POST", f"{BASE}/api/ig_posts/{cid}/{pid}/image",
             json={}, timeout=IMAGE_TIMEOUT)
    img = (r.json().get("post") or {}).get("image") or {}
    return bool(img.get("url"))


def main() -> int:
    global STATE_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", type=str, default=str(STATE_PATH))
    args = ap.parse_args()
    STATE_PATH = Path(args.state)

    state = load_state()
    done = set(tuple(x) for x in state["done"])

    print("掃描缺圖 post ...", flush=True)
    missing = _fetch_targets()
    todo = [k for k in missing if tuple(k) not in done]
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    print(f"\n線上服務: {BASE}")
    print(f"缺圖 post 總數: {len(missing)}；本輪待補: {len(todo)}（已完成 {len(done)}）\n")

    if args.dry_run:
        for cid, pid in todo[:5]:
            print(f"  樣例: {cid}  {pid}")
        print(f"[DRY] 計劃補 {len(todo)} 張。")
        return 0
    if not todo:
        print("沒有待補的 post，全部已有圖。")
        return 0

    conc = max(1, args.concurrency)
    total = len(todo)
    counters = {"ok": 0, "err": 0}

    def _run(job: tuple[int, str, str]) -> None:
        idx, cid, pid = job
        try:
            ok = _rerender(cid, pid)
            with _STATE_LOCK:
                if ok:
                    state["done"].append([cid, pid])
                    counters["ok"] += 1
                    tag = "完成"
                else:
                    if [cid, pid] not in state["failed"]:
                        state["failed"].append([cid, pid])
                    counters["err"] += 1
                    tag = "無圖返回"
                save_state(state)
            if idx % 20 == 0 or not ok:
                print(f"[{idx}/{total}] {cid}/{pid} {tag} "
                      f"(ok={counters['ok']} err={counters['err']})", flush=True)
        except Exception as e:  # noqa: BLE001
            with _STATE_LOCK:
                if [cid, pid] not in state["failed"]:
                    state["failed"].append([cid, pid])
                counters["err"] += 1
                save_state(state)
            print(f"[{idx}/{total}] {cid}/{pid} 失敗: {str(e)[:80]}", flush=True)

    jobs = [(i + 1, cid, pid) for i, (cid, pid) in enumerate(todo)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run, jobs))

    print(f"\n完成: ok={counters['ok']} err={counters['err']} / 共 {total}")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
