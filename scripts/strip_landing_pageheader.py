# -*- coding: utf-8 -*-
"""批量去掉互动卡片版落地页顶部的页眉块（`.page-h`：标签+名字+副标+cue），
让顶部直接是角色图卡片 `.card`。

只改存量落地页的原始 HTML（不重新走 LLM）：拉取 /api/landing/{cid} 的 html，
正则删除 <div class="page-h">…</div> 整段（含内部 .cue 等嵌套 div），
再 PUT /api/landing 覆盖保存。封面占位符（__IMG_URL__/__IMG_BASE64__）保持不动，
读取/导出时照常注入封面。

安全策略（每个角色）：
- 只处理 variant==interactive 且 html 里确实含 page-h 的页；
- 删除必须“恰好命中 1 段”且删后 page-h DOM 消失，否则跳过并计入 skipped（人工复核）；
- 幂等：已无 page-h 的页视为完成、跳过。
天然可续跑，进度写 data/strip_pageheader_state.json。

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

# <div ... class="... page-h ...">…</div> 直到紧邻 <div class="... card ..."> 之前。
# 锚定 card 作为结束边界，避免 page-h 内部 .cue 等嵌套 </div> 造成误截断。
# page-h 与 card 之间可能夹着 HTML 注释（如「<!-- 卡片… -->」），一并吃掉。
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
        print(f"      服务器不可用，等待恢复{(' ('+label+')') if label else ''} "
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
    raise RuntimeError(f"请求多次失败 {method} {url}: {last_err}")


def _fetch_targets(source: str) -> list[str]:
    r = _req("GET", f"{BASE}/api/characters", timeout=120)
    return [c["char_id"] for c in r.json()
            if (c.get("source") or "") == source
            and c.get("has_identity") and c.get("cover_url")]


def _strip(html: str) -> tuple[str, int]:
    """返回 (新 html, 删除段数)。只删 1 段，删后应无 page-h DOM。"""
    new, n = _PAGE_H_RE.subn("", html, count=1)
    return new, n


def _process_one(char_id: str) -> str:
    """返回状态：done / skipped-nohead / skipped-badmatch / notinteractive / nohtml。"""
    page = _req("GET", f"{BASE}/api/landing/{char_id}", timeout=30).json()
    if not (page and page.get("html")):
        return "nohtml"
    if (page.get("variant") or "default") != "interactive":
        return "notinteractive"
    html = page["html"]
    if not _HAS_PAGE_H.search(html):
        return "done"  # 幂等：已经没有页眉块了
    new, n = _strip(html)
    if n != 1 or _HAS_PAGE_H.search(new):
        return "skipped-badmatch"  # 结构异常，留待人工复核，不动它
    _req("PUT", f"{BASE}/api/landing", timeout=60,
         json={"char_id": char_id, "html": new})
    return "done"


def main() -> int:
    global STATE_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", dest="sources",
                    help="要处理的 source，可多次传；默认 chouxiang/feiren/image")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", type=str, default=str(STATE_PATH))
    args = ap.parse_args()

    STATE_PATH = Path(args.state)
    sources = args.sources or DEFAULT_SOURCES
    print(f"去页眉 source: {', '.join(sources)}")

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

    print(f"\n线上服务: {BASE}")
    print(f"待检查角色: {len(todo)}（已完成 {len(done)} 个跳过）\n")

    if args.dry_run:
        # 抽样看看会删成什么样
        for cid in todo[:3]:
            page = _req("GET", f"{BASE}/api/landing/{cid}", timeout=30).json()
            html = page.get("html") or ""
            _, n = _strip(html)
            print(f"  样例 {cid}: variant={page.get('variant')} "
                  f"has_page_h={bool(_HAS_PAGE_H.search(html))} 删除段数={n}")
        print(f"[DRY] 计划检查/去页眉 {len(todo)} 个角色。")
        return 0

    if not todo:
        print("没有待处理的角色，全部已完成。")
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
            print(f"[{idx}/{total}] {cid} 失败: {e}", flush=True)

    jobs = [(i + 1, cid) for i, cid in enumerate(todo)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run, jobs))

    print(f"\n完成: done={counters['done']} skipped={counters['skipped']} "
          f"err={counters['err']} / 共 {total}")
    if state["skipped"]:
        print(f"跳过(结构异常/待复核) {len(state['skipped'])} 个，见 state 文件 skipped 列表")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
