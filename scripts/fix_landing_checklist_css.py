# -*- coding: utf-8 -*-
"""給已生成的「互動卡片版」落地頁補上缺失的 `.check` 勾選清單 CSS（確定性修復）。

背景：互動版落地頁是自包含整份 HTML，CSS 在生成時從母版復刻並烘焙進頁面。早期母版
裡 A 互動「完成揭示」用了 `<ul class="check"><li data-tick><i></i><span>` 結構，但沒有
配套 `.check` CSS —— 於是瀏覽器按預設 <ul> 圓點渲染、方框記號不可見、行也不跟
textlist/info 行系統對齊（就是「樣式不符合規範」）。母版已補上 `.check` 規則，本指令碼
把同一段 CSS 注入到存量頁面裡，不走 LLM、不改文案、天然冪等。

與 reskin_landing_interactive.py 的區別：那個是 LLM 重新生成（慢、重擲內容）；本指令碼
只做一次字串注入 + PUT 儲存，快且確定。

判定與跳過：
- variant != interactive          → 跳過（預設頁無此結構）
- 頁面裡沒有 class="check"        → 跳過（該頁用的是 B/C 互動，無清單）
- 已含 `.check li{`               → 跳過（已修復，冪等）
- 找不到注入錨點 .textrow .note{} → 記為 skipped_no_anchor，留待人工核查

用法：
  python3 scripts/fix_landing_checklist_css.py --dry-run
  python3 scripts/fix_landing_checklist_css.py            # 實跑
  python3 scripts/fix_landing_checklist_css.py --source chouxiang --source feiren
  python3 scripts/fix_landing_checklist_css.py --concurrency 8 --limit 20
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "fix_landing_checklist_css_state.json"

DEFAULT_SOURCES = ["chouxiang", "feiren", "heermeng"]
DEFAULT_CONCURRENCY = 6
REQ_TIMEOUT = 60

# 注入錨點：母版基礎 CSS 裡 textlist 索引清單的最後一行，`.check` 緊隨其後。
ANCHOR = ('.textrow .note{font-size:var(--fs-note);color:var(--sub);'
          'line-height:1.68;margin:5px 0 0 34px;letter-spacing:-.01em}')

# 要注入的 `.check` 勾選清單樣式（與 landing_prompts.json 互動版母版逐字一致）。
CHECK_CSS = (
    "\n"
    "/* 完成揭示 · 勾選清單（對齊 textlist / info 行系統：細線行 + 方框記號） */\n"
    ".check{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}\n"
    ".check li{display:flex;align-items:center;gap:12px;padding:12px 2px;"
    "border-bottom:1px solid var(--line);font-size:var(--fs-body);color:var(--ink);"
    "letter-spacing:-.01em;transition:color .25s}\n"
    ".check li:last-child{border-bottom:0}\n"
    ".check li i{flex:0 0 auto;width:20px;height:20px;border-radius:7px;"
    "border:1.5px solid var(--line-strong);background:var(--bg);position:relative;"
    "transition:background .2s,border-color .2s}\n"
    ".check li i:after{content:\"\";position:absolute;left:6px;top:2px;width:5px;height:10px;"
    "border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg) scale(0);"
    "transform-origin:center;transition:transform .2s cubic-bezier(.16,1,.3,1)}\n"
    ".check li span{flex:1;min-width:0;line-height:1.5}\n"
    ".check li.on i{background:var(--accent);border-color:var(--accent)}\n"
    ".check li.on i:after{transform:rotate(45deg) scale(1)}\n"
    ".check li.on span{color:var(--muted);text-decoration:line-through;"
    "text-decoration-color:var(--line-strong)}"
)

_STATE_LOCK = threading.Lock()


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


def _fetch_targets(source: str) -> list[str]:
    r = _req("GET", f"{BASE}/api/characters")
    return [c["char_id"] for c in r.json()
            if (c.get("source") or "") == source
            and c.get("has_identity") and c.get("cover_url")]


def _needs_fix(html: str) -> str:
    """返回該頁處置：fix / skip_no_check / already / no_anchor。"""
    if 'class="check"' not in html and "class='check'" not in html:
        return "skip_no_check"
    if ".check li{" in html or ".check li " in html or ".check{" in html:
        return "already"
    if ANCHOR not in html:
        return "no_anchor"
    return "fix"


def _classify(cid: str) -> tuple[str, str]:
    """拉線上落地頁，判定處置。返回 (處置, html)。"""
    r = _req("GET", f"{BASE}/api/landing/{cid}")
    page = r.json()
    if not page or not page.get("html"):
        return "skip_no_page", ""
    if (page.get("variant") or "default") != "interactive":
        return "skip_not_interactive", ""
    html = page["html"]
    return _needs_fix(html), html


def _apply_fix(cid: str, html: str) -> None:
    """注入 CSS 後 PUT 儲存（PUT 不走 LLM，只覆蓋 html 並重算 html_filled）。"""
    new_html = html.replace(ANCHOR, ANCHOR + CHECK_CSS, 1)
    if new_html == html or ".check li{" not in new_html:
        raise RuntimeError("注入失敗（錨點未命中或結果異常）")
    _req("PUT", f"{BASE}/api/landing", json={"char_id": cid, "html": new_html})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", dest="sources",
                    help="要修的 source，可多次傳；預設 chouxiang/feiren/heermeng")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sources = args.sources or DEFAULT_SOURCES
    print(f"掃描 source: {', '.join(sources)}  → 目標：補 .check 勾選清單 CSS")

    state = load_state()
    done = set(state["done"])

    all_ids: list[str] = []
    for src in sources:
        all_ids.extend(_fetch_targets(src))
    todo_ids = [cid for cid in all_ids if cid not in done]
    print(f"完整角色 {len(all_ids)}，狀態已完成 {len(all_ids) - len(todo_ids)}，"
          f"待分類 {len(todo_ids)}")

    # 併發分類
    with ThreadPoolExecutor(max_workers=max(8, args.concurrency)) as ex:
        results = list(ex.map(_classify, todo_ids))

    buckets: dict[str, list[tuple[str, str]]] = {}
    for cid, (verdict, html) in zip(todo_ids, results):
        buckets.setdefault(verdict, []).append((cid, html))

    for k in sorted(buckets):
        print(f"  {k}: {len(buckets[k])}")

    to_fix = buckets.get("fix", [])
    if args.limit and args.limit > 0:
        to_fix = to_fix[:args.limit]
    print(f"\n本輪需要注入 CSS 的頁面: {len(to_fix)}")

    # 把非 fix 的確定性歸檔進 state（冪等，續跑不再重複分類）
    with _STATE_LOCK:
        for verdict in ("already", "skip_no_check", "skip_not_interactive",
                        "skip_no_page"):
            for cid, _ in buckets.get(verdict, []):
                if cid not in done:
                    state["done"].append(cid)
                    done.add(cid)
        for cid, _ in buckets.get("no_anchor", []):
            if cid not in state["skipped"]:
                state["skipped"].append(cid)
        save_state(state)

    if args.dry_run:
        for cid, _ in to_fix[:8]:
            print(f"  [DRY] 將修復 {cid}")
        print(f"[DRY] 計劃給 {len(to_fix)} 個頁面注入 .check CSS。"
              f"（no_anchor 待人工核查: {len(buckets.get('no_anchor', []))}）")
        return 0

    if not to_fix:
        print("沒有需要修復的頁面。")
        return 0

    total = len(to_fix)
    counters = {"ok": 0, "err": 0}

    def _run(job):
        idx, cid, html = job
        print(f"[{idx}/{total}] {cid}", flush=True)
        try:
            _apply_fix(cid, html)
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
