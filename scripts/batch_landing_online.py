# -*- coding: utf-8 -*-
"""為「已完成角色(人設+封面)」批次生成落地頁 —— 直接打線上服務(HTTP)。

覆蓋所有【非空 source】(排除無來源 "") 下【人設+封面都有】(has_identity 且
cover_url) 且【尚無落地頁】的角色。用第一個變體 default(預設·長圖敘事頁)、
不指定 style_text(自由文字留空)。天然冪等：已有落地頁的角色跳過，中斷後重跑
自動續。進度寫 data/batch_landing_online_state.json。

用法：
  python3 scripts/batch_landing_online.py [--source all_nonempty|具體source]
  python3 scripts/batch_landing_online.py --dry-run
  python3 scripts/batch_landing_online.py --concurrency 16 --variant default
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
STATE_PATH = DATA_DIR / "batch_landing_online_state.json"

POLL_INTERVAL = 20
LANDING_TIMEOUT = 900       # 單角色一份落地頁 HTML（LLM 可能 1-3 分鐘）
DEFAULT_CONCURRENCY = 4
DEFAULT_VARIANT = "default"


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


def _has_landing_online(char_id: str) -> bool:
    try:
        r = _req("GET", f"{BASE}/api/landing/{char_id}", timeout=30)
        page = r.json()
    except Exception:  # noqa: BLE001
        return False
    return bool(page and page.get("html"))


def _all_nonempty_sources() -> list[str]:
    r = _req("GET", f"{BASE}/api/characters", timeout=120)
    srcs = {(c.get("source") or "") for c in r.json()}
    return sorted(s for s in srcs if s)


def _fetch_targets(source: str) -> list[dict]:
    r = _req("GET", f"{BASE}/api/characters", timeout=120)
    return [c for c in r.json()
            if (c.get("source") or "") == source
            and c.get("has_identity") and c.get("cover_url")]


def _gen_landing(char_id: str, variant: str) -> dict:
    """驅動單角色落地頁：POST /api/landing 拿 task_id 後輪詢；任務丟失則線上複核。"""
    for attempt in range(3):
        r = _req("POST", f"{BASE}/api/landing", timeout=60,
                 json={"char_id": char_id, "variant": variant})
        task_id = r.json().get("task_id")
        try:
            return _poll(task_id, LANDING_TIMEOUT, f"landing {char_id}")
        except TaskLost:
            if _has_landing_online(char_id):
                print(f"      任務丟失但線上已有落地頁，視為成功 {char_id}", flush=True)
                return {"char_id": char_id}
            print(f"      任務丟失，重試 {char_id} (第 {attempt + 2} 次)", flush=True)
        except TimeoutError:
            if _has_landing_online(char_id):
                print(f"      輪詢超時但線上已完成，視為成功 {char_id}", flush=True)
                return {"char_id": char_id}
            print(f"      輪詢超時，重試 {char_id} (第 {attempt + 2} 次)", flush=True)
    if _has_landing_online(char_id):
        return {"char_id": char_id}
    raise RuntimeError("落地頁多次超時/丟失，暫緩（續跑會自動補）")


def main() -> int:
    global STATE_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all_nonempty",
                    help="all_nonempty(所有非空 source，預設) / 或任意具體 source 名")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--variant", default=DEFAULT_VARIANT,
                    help="落地頁變體，預設 default(第一個選項·長圖敘事頁)")
    ap.add_argument("--force", action="store_true",
                    help="強制重跑：忽略線上已有落地頁與本地 done 記錄，"
                         "對所有【人設+封面齊全】的角色重新生成(覆蓋舊頁)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", type=str, default=str(STATE_PATH))
    args = ap.parse_args()

    STATE_PATH = Path(args.state)
    if args.source == "all_nonempty":
        sources = _all_nonempty_sources()
        print(f"all_nonempty → 覆蓋非空 source: {', '.join(sources)}")
    else:
        sources = [args.source]

    state = load_state()
    done = set(state["done"])

    todo: list[tuple[str, str]] = []
    summary = {}
    for src in sources:
        targets = _fetch_targets(src)
        if args.force:
            # 強制重跑：忽略 done 記錄與線上已有落地頁，全部重生成(覆蓋)
            need = [c["char_id"] for c in targets]
            summary[src] = {"complete": len(targets), "todo": len(need)}
            todo.extend((cid, src) for cid in need)
            continue
        unknown = [c["char_id"] for c in targets if c["char_id"] not in done]
        with ThreadPoolExecutor(max_workers=32) as ex:
            flags = list(ex.map(_has_landing_online, unknown))
        need, newly_done = [], []
        for cid, has in zip(unknown, flags):
            (newly_done if has else need).append(cid)
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

    print(f"\n線上服務: {BASE}  變體: {args.variant}")
    for src in sources:
        print(f"  {src}: 完整角色 {summary[src]['complete']}，待生成落地頁 {summary[src]['todo']}")
    print(f"本輪實際生成: {len(todo)} 個角色\n")

    if args.dry_run:
        for cid, src in todo[:5]:
            print(f"  樣例: {src}  {cid}")
        print(f"[DRY] 計劃為 {len(todo)} 個角色各生成一份落地頁(variant={args.variant})。")
        return 0

    if not todo:
        print("沒有待生成的角色，全部已完成。")
        return 0

    conc = max(1, args.concurrency)
    total = len(todo)
    counters = {"ok": 0, "err": 0}

    def _run(job: tuple[int, str, str]) -> None:
        idx, cid, src = job
        print(f"[{idx}/{total}] {src} {cid}", flush=True)
        try:
            _gen_landing(cid, args.variant)
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
            print(f"      失敗 {cid}: {e}", flush=True)

    jobs = [(i + 1, cid, src) for i, (cid, src) in enumerate(todo)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run, jobs))

    print(f"\n完成: ok={counters['ok']} err={counters['err']} / 共 {total}")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
