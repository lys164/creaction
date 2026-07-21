# -*- coding: utf-8 -*-
"""批次去掉互動卡片版落地頁頂部的頁首塊（`.page-h`：標籤+名字+副標+cue），
讓頂部直接是角色圖卡片 `.card`。

只改存量落地頁的原始 HTML（不重新走 LLM）：拉取 /api/landing/{cid} 的 html，
正則刪除 <div class="page-h">…</div> 整段（含內部 .cue 等巢狀 div），
再 PUT /api/landing 覆蓋儲存。封面佔位符（__IMG_URL__/__IMG_BASE64__）保持不動，
讀取/匯出時照常注入封面。

安全策略（每個角色）：
- 只處理 variant==interactive 且 html 裡確實含 page-h 的頁；
- 刪除必須“恰好命中 1 段”且刪後 page-h DOM 消失，否則跳過並計入 skipped（人工複核）；
- 冪等：已無 page-h 的頁視為完成、跳過。
天然可續跑，進度寫 data/strip_pageheader_state.json。

用法：
  python3 scripts/strip_landing_pageheader.py --dry-run
  python3 scripts/strip_landing_pageheader.py --concurrency 16
  python3 scripts/strip_landing_pageheader.py --source chouxiang --source feiren --source image
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

_STATE_LOCK = threading.Lock()

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "strip_pageheader_state.json"

DEFAULT_SOURCES = ["chouxiang", "feiren", "image"]
DEFAULT_CONCURRENCY = 8

# <div ... class="... page-h ...">…</div> 直到緊鄰 <div class="... card ..."> 之前。
# 錨定 card 作為結束邊界，避免 page-h 內部 .cue 等巢狀 </div> 造成誤截斷。
# page-h 與 card 之間可能夾著 HTML 註釋（如「<!-- 卡片… -->」），一併吃掉。
_PAGE_H_RE = re.compile(
    r'(?:<!--.*?-->\s*)*'
    r'<div\b[^>]*\bclass="[^"]*\bpage-h\b[^"]*"[^>]*>.*?</div>\s*'
    r'(?:<!--.*?-->\s*)*'
    r'(?=<div\b[^>]*\bclass="[^"]*\bcard\b[^"]*")',
    re.S,
)
_HAS_PAGE_H = re.compile(r'<div\b[^>]*\bclass="[^"]*\bpage-h\b', re.S)


def load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("done", [])
            s.setdefault("failed", [])
            s.setdefault("skipped", [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done": [], "failed": [], "skipped": []}


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


def _req(method: str, url: str, **kw) -> requests.Response:
    last_err = None
    for attempt in range(6):
        try:
            r = requests.request(method, url, **kw)
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


def _fetch_targets(source: str) -> list[str]:
    r = _req("GET", f"{BASE}/api/characters", timeout=120)
    return [c["char_id"] for c in r.json()
            if (c.get("source") or "") == source
            and c.get("has_identity") and c.get("cover_url")]


def _strip(html: str) -> tuple[str, int]:
    """返回 (新 html, 刪除段數)。只刪 1 段，刪後應無 page-h DOM。"""
    new, n = _PAGE_H_RE.subn("", html, count=1)
    return new, n


def _process_one(char_id: str) -> str:
    """返回狀態：done / skipped-nohead / skipped-badmatch / notinteractive / nohtml。"""
    page = _req("GET", f"{BASE}/api/landing/{char_id}", timeout=30).json()
    if not (page and page.get("html")):
        return "nohtml"
    if (page.get("variant") or "default") != "interactive":
        return "notinteractive"
    html = page["html"]
    if not _HAS_PAGE_H.search(html):
        return "done"  # 冪等：已經沒有頁首塊了
    new, n = _strip(html)
    if n != 1 or _HAS_PAGE_H.search(new):
        return "skipped-badmatch"  # 結構異常，留待人工複核，不動它
    _req("PUT", f"{BASE}/api/landing", timeout=60,
         json={"char_id": char_id, "html": new})
    return "done"


def main() -> int:
    global STATE_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", dest="sources",
                    help="要處理的 source，可多次傳；預設 chouxiang/feiren/image")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", type=str, default=str(STATE_PATH))
    args = ap.parse_args()

    STATE_PATH = Path(args.state)
    sources = args.sources or DEFAULT_SOURCES
    print(f"去頁首 source: {', '.join(sources)}")

    state = load_state()
    done = set(state["done"])

    todo: list[str] = []
    for src in sources:
        for cid in _fetch_targets(src):
            if cid not in done:
                todo.append(cid)
    # 去重保序
    seen: set[str] = set()
    todo = [c for c in todo if not (c in seen or seen.add(c))]

    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    print(f"\n線上服務: {BASE}")
    print(f"待檢查角色: {len(todo)}（已完成 {len(done)} 個跳過）\n")

    if args.dry_run:
        # 抽樣看看會刪成什麼樣
        for cid in todo[:3]:
            page = _req("GET", f"{BASE}/api/landing/{cid}", timeout=30).json()
            html = page.get("html") or ""
            _, n = _strip(html)
            print(f"  樣例 {cid}: variant={page.get('variant')} "
                  f"has_page_h={bool(_HAS_PAGE_H.search(html))} 刪除段數={n}")
        print(f"[DRY] 計劃檢查/去頁首 {len(todo)} 個角色。")
        return 0

    if not todo:
        print("沒有待處理的角色，全部已完成。")
        return 0

    conc = max(1, args.concurrency)
    total = len(todo)
    counters = {"done": 0, "skipped": 0, "err": 0}

    def _run(job: tuple[int, str]) -> None:
        idx, cid = job
        try:
            st = _process_one(cid)
            with _STATE_LOCK:
                if st == "done":
                    state["done"].append(cid)
                    counters["done"] += 1
                    if cid in state["failed"]:
                        state["failed"].remove(cid)
                else:
                    if cid not in state["skipped"]:
                        state["skipped"].append(cid)
                    counters["skipped"] += 1
                save_state(state)
            print(f"[{idx}/{total}] {cid} -> {st}", flush=True)
        except Exception as e:  # noqa: BLE001
            with _STATE_LOCK:
                if cid not in state["failed"]:
                    state["failed"].append(cid)
                counters["err"] += 1
                save_state(state)
            print(f"[{idx}/{total}] {cid} 失敗: {e}", flush=True)

    jobs = [(i + 1, cid) for i, cid in enumerate(todo)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run, jobs))

    print(f"\n完成: done={counters['done']} skipped={counters['skipped']} "
          f"err={counters['err']} / 共 {total}")
    if state["skipped"]:
        print(f"跳過(結構異常/待複核) {len(state['skipped'])} 個，見 state 檔案 skipped 列表")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
