# -*- coding: utf-8 -*-
"""翻包组件（In My Bag）第 4 个物品批量删除（确定性修复）。

背景：翻包九宫格固定 3 列（.baggrid grid-template-columns:repeat(3,1fr)）。当一个
页面正好放了 4 个物品时，就会变成 3+1——第 4 个孤零零掉到第二行，视觉不齐。6 个
（3+3 满两行）或 3 个都没问题，只有"恰好 4 个"需要删到 3 个。

处理（仅当页面恰好 4 个 bagcell 时动手，其它数量原样跳过）：
  1) 删 <input class="bagin" ... id="bg4"> 那一行
  2) 删 flatlay 里 <label class="bagcell" for="bg4">...</label>
  3) 删 <div class="bagdetail d4">...</div> 整块
  4) 若 In My Bag 小标是"数字+量词"计数形态（4 件/4 items/04 ITEMS 等）→ 改成 3；
     自由文案（别乱碰/Top Secret 等）不动。
CSS 里 #bg4/.d4 的通用规则保留（母版就带 bg1~bg6，物品删掉后这些选择器永不命中，
无害死规则）。走 PUT /api/landing 保存，不走 LLM、天然幂等、可续跑。

用法：
  python3 scripts/fix_landing_bag_four_items.py --dry-run
  python3 scripts/fix_landing_bag_four_items.py                 # 全量实跑
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
    r'^(0?4)(\s*(?:件|个|個|점|개|品|コ|items?|ITEMS?|pcs)\s*)$', re.I)

_STATE_LOCK = threading.Lock()


# ----------------------------------------------------------------- 核心变换
def _count_cells(html: str) -> int:
    return len(_CELL_RE.findall(html))


def remove_fourth_bag_item(html: str):
    """返回 (new_html, changed)。仅当恰好 4 个 bagcell 时删第 4 个，否则不动。"""
    if "bagcell" not in html or "baggrid" not in html:
        return html, False
    if _count_cells(html) != 4:
        return html, False

    orig = html

    # 1) 删 id="bg4" 的 input
    html = re.sub(
        r'[ \t]*<input\b[^>]*\bclass="bagin"[^>]*\bid="bg4"[^>]*>\s*\n?',
        "", html, count=1,
    )
    # 2) 删 flatlay 里 for="bg4" 的 label
    html = re.sub(
        r'[ \t]*<label\b[^>]*\bclass="[^"]*\bbagcell\b[^"]*"[^>]*\bfor="bg4"[^>]*>.*?</label>\s*\n?',
        "", html, count=1, flags=re.S,
    )
    # 3) 删 bagdetail d4 整块（div 配平）
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

    # 4) 小标计数 4→3（仅计数形态）
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
        # 安全校验：删完恰好剩 3 cell / 3 detail、无 bg4 物品残留
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
    raise RuntimeError(f"请求多次失败 {method} {url}: {last}")


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
        raise RuntimeError("删除未生效（结构异常）")
    _req("PUT", f"{BASE}/api/landing", json={"char_id": cid, "html": new_html})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", dest="sources",
                    help="限定 source，可多次传；缺省=全部来源")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sources = args.sources
    print(f"扫描 source: {', '.join(sources) if sources else '全部'} "
          f"→ 目标：翻包恰好 4 个物品的页面，删掉第 4 个")

    state = load_state()
    done = set(state["done"])

    ids = _fetch_ids(sources)
    todo_ids = [c for c in ids if c not in done]
    print(f"完整角色 {len(ids)}，已完成 {len(ids) - len(todo_ids)}，待分类 {len(todo_ids)}")

    with ThreadPoolExecutor(max_workers=max(8, args.concurrency)) as ex:
        results = list(ex.map(_classify, todo_ids))

    to_fix = [(cid, html) for cid, (v, html) in zip(todo_ids, results) if v == "fix"]
    skips = [cid for cid, (v, _) in zip(todo_ids, results) if v == "skip"]
    print(f"  需要删第4个: {len(to_fix)}")
    print(f"  跳过(非4个/非互动): {len(skips)}")

    # 跳过项归档进 done，续跑不重复分类
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
            print(f"  [DRY] 将删除第4个物品 {cid}")
        print(f"[DRY] 计划处理 {len(to_fix)} 个页面。")
        return 0

    if not to_fix:
        print("没有需要处理的页面。")
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
            print(f"      失败 {cid}: {e}", flush=True)

    jobs = [(i + 1, cid, html) for i, (cid, html) in enumerate(to_fix)]
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        list(ex.map(_run, jobs))

    print(f"\n完成: ok={counters['ok']} err={counters['err']} / 共 {total}")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
