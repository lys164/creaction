# -*- coding: utf-8 -*-
"""翻包元件（In My Bag）第 4 個物品批次刪除（確定性修復）。

背景：翻包九宮格固定 3 列（.baggrid grid-template-columns:repeat(3,1fr)）。當一個
頁面正好放了 4 個物品時，就會變成 3+1——第 4 個孤零零掉到第二行，視覺不齊。6 個
（3+3 滿兩行）或 3 個都沒問題，只有"恰好 4 個"需要刪到 3 個。

處理（僅當頁面恰好 4 個 bagcell 時動手，其它數量原樣跳過）：
  1) 刪 <input class="bagin" ... id="bg4"> 那一行
  2) 刪 flatlay 裡 <label class="bagcell" for="bg4">...</label>
  3) 刪 <div class="bagdetail d4">...</div> 整塊
  4) 若 In My Bag 小標是"數字+量詞"計數形態（4 件/4 items/04 ITEMS 等）→ 改成 3；
     自由文案（別亂碰/Top Secret 等）不動。
CSS 裡 #bg4/.d4 的通用規則保留（母版就帶 bg1~bg6，物品刪掉後這些選擇器永不命中，
無害死規則）。走 PUT /api/landing 儲存，不走 LLM、天然冪等、可續跑。

用法：
  python3 scripts/fix_landing_bag_four_items.py --dry-run
  python3 scripts/fix_landing_bag_four_items.py                 # 全量實跑
  python3 scripts/fix_landing_bag_four_items.py --source feiren --source chouxiang
  python3 scripts/fix_landing_bag_four_items.py --concurrency 8 --limit 20
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

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "fix_landing_bag_four_items_state.json"

DEFAULT_CONCURRENCY = 6
REQ_TIMEOUT = 60

_CELL_RE = re.compile(
    r'<label[^>]*class="[^"]*\bbagcell\b[^"]*"[^>]*for="bg\d+"')
_COUNT_RE = re.compile(
    r'^(0?4)(\s*(?:件|個|個|점|개|品|コ|items?|ITEMS?|pcs)\s*)$', re.I)

_STATE_LOCK = threading.Lock()


# ----------------------------------------------------------------- 核心變換
def _count_cells(html: str) -> int:
    return len(_CELL_RE.findall(html))


def remove_fourth_bag_item(html: str):
    """返回 (new_html, changed)。僅當恰好 4 個 bagcell 時刪第 4 個，否則不動。"""
    if "bagcell" not in html or "baggrid" not in html:
        return html, False
    if _count_cells(html) != 4:
        return html, False

    orig = html

    # 1) 刪 id="bg4" 的 input
    html = re.sub(
        r'[ \t]*<input\b[^>]*\bclass="bagin"[^>]*\bid="bg4"[^>]*>\s*\n?',
        "", html, count=1,
    )
    # 2) 刪 flatlay 裡 for="bg4" 的 label
    html = re.sub(
        r'[ \t]*<label\b[^>]*\bclass="[^"]*\bbagcell\b[^"]*"[^>]*\bfor="bg4"[^>]*>.*?</label>\s*\n?',
        "", html, count=1, flags=re.S,
    )
    # 3) 刪 bagdetail d4 整塊（div 配平）
    m = re.search(r'<div\b[^>]*\bclass="[^"]*\bbagdetail\b[^"]*\bd4\b[^"]*"[^>]*>', html)
    if m:
        start = m.start()
        depth = 0
        end = None
        for tm in re.finditer(r'<div\b|</div>', html[start:]):
            if tm.group(0) == "</div>":
                depth -= 1
                if depth == 0:
                    end = start + tm.end()
                    break
            else:
                depth += 1
        if end is not None:
            lead = start
            while lead > 0 and html[lead - 1] in " \t":
                lead -= 1
            tail = end
            if tail < len(html) and html[tail] == "\n":
                tail += 1
            html = html[:lead] + html[tail:]

    # 4) 小標計數 4→3（僅計數形態）
    def _fix_count(mm):
        head, body, tl = mm.group(1), mm.group(2), mm.group(3)
        cm = _COUNT_RE.match(body.strip())
        if cm:
            return head + body.replace(cm.group(1), "3", 1) + tl
        return mm.group(0)

    html = re.sub(r'(In My Bag.*?<span class="x">)([^<]*)(</span>)',
                  _fix_count, html, count=1, flags=re.S)

    changed = html != orig
    if changed:
        # 安全校驗：刪完恰好剩 3 cell / 3 detail、無 bg4 物品殘留
        assert _count_cells(html) == 3, "cell count after != 3"
        assert len(re.findall(r'class="bagdetail d\d', html)) == 3, "detail != 3"
        assert 'for="bg4"' not in html, "residual label bg4"
        assert not re.search(r'class="bagdetail d4', html), "residual detail d4"
        assert 'id="bg1" checked' in html or re.search(
            r'id="bg1"[^>]*checked|checked[^>]*id="bg1"', html), "bg1 not checked"
    return html, changed


# ----------------------------------------------------------------- state / http
def load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            for k in ("done", "skipped", "failed"):
                s.setdefault(k, [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done": [], "skipped": [], "failed": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _req(method: str, url: str, **kw) -> requests.Response:
    last = None
    for attempt in range(5):
        try:
            r = requests.request(method, url, timeout=REQ_TIMEOUT, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                last = f"HTTP {r.status_code}"
                time.sleep(min(3 * (attempt + 1), 20))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = str(e)
            time.sleep(min(3 * (attempt + 1), 20))
    raise RuntimeError(f"請求多次失敗 {method} {url}: {last}")


def _fetch_ids(sources: list[str] | None) -> list[str]:
    r = _req("GET", f"{BASE}/api/characters")
    out = []
    for c in r.json():
        if not (c.get("has_identity") and c.get("cover_url")):
            continue
        if sources and (c.get("source") or "") not in sources:
            continue
        out.append(c["char_id"])
    return out


def _classify(cid: str):
    """返回 (verdict, html)。verdict: fix / skip。"""
    r = _req("GET", f"{BASE}/api/landing/{cid}")
    p = r.json()
    if not p or not p.get("html"):
        return "skip", ""
    if (p.get("variant") or "default") != "interactive":
        return "skip", ""
    html = p["html"]
    if _count_cells(html) == 4:
        return "fix", html
    return "skip", ""


def _apply(cid: str, html: str) -> None:
    new_html, changed = remove_fourth_bag_item(html)
    if not changed:
        raise RuntimeError("刪除未生效（結構異常）")
    _req("PUT", f"{BASE}/api/landing", json={"char_id": cid, "html": new_html})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", dest="sources",
                    help="限定 source，可多次傳；預設=全部來源")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sources = args.sources
    print(f"掃描 source: {', '.join(sources) if sources else '全部'} "
          f"→ 目標：翻包恰好 4 個物品的頁面，刪掉第 4 個")

    state = load_state()
    done = set(state["done"])

    ids = _fetch_ids(sources)
    todo_ids = [c for c in ids if c not in done]
    print(f"完整角色 {len(ids)}，已完成 {len(ids) - len(todo_ids)}，待分類 {len(todo_ids)}")

    with ThreadPoolExecutor(max_workers=max(8, args.concurrency)) as ex:
        results = list(ex.map(_classify, todo_ids))

    to_fix = [(cid, html) for cid, (v, html) in zip(todo_ids, results) if v == "fix"]
    skips = [cid for cid, (v, _) in zip(todo_ids, results) if v == "skip"]
    print(f"  需要刪第4個: {len(to_fix)}")
    print(f"  跳過(非4個/非互動): {len(skips)}")

    # 跳過項歸檔進 done，續跑不重複分類
    with _STATE_LOCK:
        for cid in skips:
            if cid not in done:
                state["done"].append(cid)
                done.add(cid)
        save_state(state)

    if args.limit and args.limit > 0:
        to_fix = to_fix[:args.limit]

    if args.dry_run:
        for cid, _ in to_fix[:8]:
            print(f"  [DRY] 將刪除第4個物品 {cid}")
        print(f"[DRY] 計劃處理 {len(to_fix)} 個頁面。")
        return 0

    if not to_fix:
        print("沒有需要處理的頁面。")
        return 0

    total = len(to_fix)
    counters = {"ok": 0, "err": 0}

    def _run(job):
        idx, cid, html = job
        print(f"[{idx}/{total}] {cid}", flush=True)
        try:
            _apply(cid, html)
            with _STATE_LOCK:
                state["done"].append(cid)
                if cid in state["failed"]:
                    state["failed"].remove(cid)
                save_state(state)
                counters["ok"] += 1
        except Exception as e:  # noqa: BLE001
            with _STATE_LOCK:
                if cid not in state["failed"]:
                    state["failed"].append(cid)
                save_state(state)
                counters["err"] += 1
            print(f"      失敗 {cid}: {e}", flush=True)

    jobs = [(i + 1, cid, html) for i, (cid, html) in enumerate(to_fix)]
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        list(ex.map(_run, jobs))

    print(f"\n完成: ok={counters['ok']} err={counters['err']} / 共 {total}")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
