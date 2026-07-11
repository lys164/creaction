# -*- coding: utf-8 -*-
"""source=image 的角色：以【当前封面】为参考重跑封面链路，让新封面保留气质但漂离上传原图。

背景：image 源角色的封面当初以上传原图为 i2i 参考生成，太像原图。服务端新增
recook_from_cover 开关（见 pipeline.generate_cover）：开启后 cover_spec 规划与 i2i
渲染都改用角色当前封面作参考，源图不再进入封面生成的任何一步。

本脚本逐个（并发）调用 /api/cover?recook_from_cover=true 触发重跑并轮询任务：
  - 只处理 source=image 且【已有封面】的角色（无封面的没有参考图，跳过）。
  - 断点续跑：进度落 data/image_recook_state.json，已完成的跳过。
  - 服务器 5xx/超时自动等待恢复重试。

用法：
  PYTHONPATH=. python3 scripts/image_recook_covers_online.py --limit 5    # 先试 5 个
  PYTHONPATH=. python3 scripts/image_recook_covers_online.py              # 全部
  PYTHONPATH=. python3 scripts/image_recook_covers_online.py --style realistic_portrait --concurrency 3
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

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DEFAULT_STYLE = "realistic_portrait"
POLL_INTERVAL = 8
COVER_TIMEOUT = 1200
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "image_recook_state.json"


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


def _targets() -> list[str]:
    """source=image 且已有封面的角色 char_id。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=60)
    return [c["char_id"] for c in r.json()
            if c.get("source") == "image" and c.get("cover_url") and c.get("char_id")]


def _recook_one(char_id: str, style_id: str) -> bool:
    """提交单角色 recook 封面任务并轮询。任务丢失(重启)则视为需重试(返回 False)。"""
    r = _req("POST", f"{BASE}/api/cover", json={
        "char_id": char_id, "style_id": style_id,
        "mode": "fill_missing", "recook_from_cover": True,
    }, timeout=60)
    tid = r.json()["task_id"]
    deadline = time.time() + COVER_TIMEOUT
    while time.time() < deadline:
        tr = _req("GET", f"{BASE}/api/tasks/{tid}", timeout=30, allow_404=True)
        if tr.status_code == 404:
            _wait_healthy()
            return False
        t = tr.json()
        if t.get("status") == "done":
            return True
        if t.get("status") == "error":
            print(f"      任务错误 {char_id}: {t.get('error')}", flush=True)
            return False
        time.sleep(POLL_INTERVAL)
    return False


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default=DEFAULT_STYLE)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--reset-state", action="store_true", help="清空断点续跑记录")
    args = ap.parse_args()

    if args.reset_state and STATE_PATH.exists():
        STATE_PATH.unlink()

    targets = _targets()
    state = _load_state()
    done = set(state["done"])
    todo = [c for c in targets if c not in done]
    if args.limit:
        todo = todo[:args.limit]
    total = len(todo)
    print(f"image 源有封面角色: {len(targets)}（已完成 {len(done)}，本次待跑 {total}）；"
          f"画风: {args.style}；并发: {args.concurrency}\n", flush=True)
    if not todo:
        print("没有待处理角色。")
        return 0

    lock = threading.Lock()
    counters = {"ok": 0, "err": 0}

    def _mark_done(cid: str) -> None:
        with lock:
            state["done"].append(cid)
            _save_state(state)

    def _one(job: tuple[int, str]) -> None:
        i, cid = job
        print(f"[{i}/{total}] {cid} recook 封面…", flush=True)
        try:
            if _recook_one(cid, args.style):
                with lock:
                    counters["ok"] += 1
                _mark_done(cid)
                print(f"      ✓ 完成 {cid}", flush=True)
            else:
                with lock:
                    counters["err"] += 1
                print(f"      ✗ 未完成 {cid}（稍后可续跑重试）", flush=True)
        except Exception as e:  # noqa: BLE001
            with lock:
                counters["err"] += 1
            print(f"      ✗ 失败 {cid}: {e}", flush=True)

    jobs = list(enumerate(todo, 1))
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        list(ex.map(_one, jobs))

    print(f"\n完成：成功 {counters['ok']}，失败 {counters['err']}。"
          f"累计已跑 {len(state['done'])}。", flush=True)
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
