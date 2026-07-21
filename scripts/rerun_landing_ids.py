# -*- coding: utf-8 -*-
"""按給定角色 id 名單重跑落地頁(default 變體)，打線上服務。

複用 batch_landing_online 的健壯輪詢(_gen_landing / _poll / 斷線重試 / 任務丟失
線上複核)。名單預設取 data/chat_button_targets.json 的 "all"。併發預設 3。

進度寫 data/rerun_ids_state.json，天然冪等：done 裡的跳過。中斷後重跑自動續。

用法：
  python3 scripts/rerun_landing_ids.py                       # 跑 chat_button_targets.json 的 all
  python3 scripts/rerun_landing_ids.py --targets path.json --key heermeng
  python3 scripts/rerun_landing_ids.py --ids char_a,char_b   # 直接指定
  python3 scripts/rerun_landing_ids.py --concurrency 3 --dry-run
"""
from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 複用同目錄 batch_landing_online 的底層能力
import batch_landing_online as base  # noqa: E402

_LOCK = threading.Lock()
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "rerun_ids_state.json"
DEFAULT_TARGETS = DATA_DIR / "chat_button_targets.json"


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=str(DEFAULT_TARGETS))
    ap.add_argument("--key", default="all", help="targets json 裡的鍵(all/heermeng/mengnv)")
    ap.add_argument("--ids", default="", help="逗號分隔的 id，優先於 --targets")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--variant", default="default")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.ids.strip():
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    else:
        data = json.loads(Path(args.targets).read_text(encoding="utf-8"))
        ids = list(data.get(args.key) or [])
    # 去重保序
    seen = set()
    ids = [x for x in ids if not (x in seen or seen.add(x))]

    state = load_state()
    done = set(state["done"])
    todo = [c for c in ids if c not in done]

    print(f"線上服務: {base.BASE}  變體: {args.variant}")
    print(f"名單共 {len(ids)}，已完成跳過 {len(ids) - len(todo)}，本輪重跑 {len(todo)}")

    if args.dry_run:
        for c in todo[:8]:
            print("  樣例:", c)
        print(f"[DRY] 計劃重跑 {len(todo)} 個。")
        return 0
    if not todo:
        print("沒有待重跑的角色。")
        return 0

    total = len(todo)
    counters = {"ok": 0, "err": 0}

    def _run(job: tuple[int, str]) -> None:
        idx, cid = job
        print(f"[{idx}/{total}] {cid}", flush=True)
        try:
            base._gen_landing(cid, args.variant)
            with _LOCK:
                if cid not in state["done"]:
                    state["done"].append(cid)
                if cid in state["failed"]:
                    state["failed"].remove(cid)
                save_state(state)
                counters["ok"] += 1
            print(f"      完成 {cid}", flush=True)
        except Exception as e:  # noqa: BLE001
            with _LOCK:
                if cid not in state["failed"]:
                    state["failed"].append(cid)
                save_state(state)
                counters["err"] += 1
            print(f"      失敗 {cid}: {e}", flush=True)

    jobs = [(i + 1, cid) for i, cid in enumerate(todo)]
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        list(ex.map(_run, jobs))

    print(f"\n完成: ok={counters['ok']} err={counters['err']} / 共 {total}")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
