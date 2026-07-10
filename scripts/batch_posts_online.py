# -*- coding: utf-8 -*-
"""为「已完成角色(人设+封面)」批量生成 INS 帖子 —— 直接打线上服务(HTTP)。

对应需求：feiren / image 两个 source 的角色都已有完整人设和封面，
按各自链路补齐帖子。两者都走 /api/ig_posts/batch，差异只在 track / 封面画风：

- source="image"  → track="real"，帖子自拍以 realistic_portrait 封面做图生图人脸锚点。
- source="feiren" → track="nonhuman"（非人物链路：不套画风，按 identity + 原图出图）。

只处理【人设 + 封面都有】(has_identity 且 cover_url) 且【尚无帖子】的角色，天然幂等：
已有帖子的角色跳过，中断后重跑自动续。进度写 data/batch_posts_online_state.json。

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

# 每个 source 的链路参数：track + 封面/自拍锚点画风(nonhuman 不套画风 → style_id 为空)
SOURCE_CHAINS = {
    "image":  {"track": "real",     "style_id": "realistic_portrait"},
    "feiren": {"track": "nonhuman", "style_id": ""},
}

POLL_INTERVAL = 20         # 轮询间隔（单批出图数分钟，20s 足够，且大幅降低对 traefik 的请求压力）
POSTS_TIMEOUT = 2400        # 单角色一批帖子(~9 图)
DEFAULT_CONCURRENCY = 4     # 同时并行的角色数


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
    """原子写盘（tmp+rename）。调用方持有 _STATE_LOCK。"""
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
    """服务器 502/宕机时阻塞等待其恢复（指数退避，最长 60s/次）。"""
    delay, waited = 5, 0
    while not _healthy():
        print(f"      服务器不可用，等待恢复{(' ('+label+')') if label else ''} "
              f"已等 {waited}s", flush=True)
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 60)


class TaskLost(Exception):
    """任务在服务器端丢失（进程重启，内存态任务清空 → /api/tasks 返回 404）。"""


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
    raise RuntimeError(f"请求多次失败 {method} {url}: {last_err}")


def _poll(task_id: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        r = _req("GET", f"{BASE}/api/tasks/{task_id}", timeout=30, allow_404=True)
        if r.status_code == 404:
            raise TaskLost(f"{label} 任务 {task_id} 丢失（服务器疑似重启）")
        t = r.json()
        if t.get("done_count") != last:
            last = t.get("done_count")
            print(f"      {label} {t.get('done_count')}/{t.get('total')} "
                  f"({t.get('status')})", flush=True)
        if t.get("status") == "done":
            return t.get("result") or {}
        if t.get("status") == "error":
            raise RuntimeError(f"{label} 任务失败: {t.get('error')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{label} 轮询超时 ({timeout}s)")


def _has_posts_online(char_id: str) -> bool:
    try:
        r = _req("GET", f"{BASE}/api/ig_posts/{char_id}/latest", timeout=30)
        ig = r.json()
    except Exception:  # noqa: BLE001
        return False
    return bool(ig and ig.get("posts"))


def _all_nonempty_sources() -> list[str]:
    """线上现存的所有【非空】source（排除无来源 ""）。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=120)
    chars = r.json()
    srcs = {(c.get("source") or "") for c in chars}
    return sorted(s for s in srcs if s)


def _fetch_targets(source: str) -> list[dict]:
    """线上拉全部角色，筛出该 source 下【人设+封面都有】的角色。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=120)
    chars = r.json()
    return [c for c in chars
            if (c.get("source") or "") == source
            and c.get("has_identity") and c.get("cover_url")]


def _gen_posts(char_id: str, chain: dict, with_images: bool) -> dict:
    """为单个角色生成帖子；任务因服务器重启丢失时先线上复核，否则重试。

    chain 为 None 时：不指定 track/style_id，服务端按角色自身存储的 track 与
    style_id 生成（各 source 链路不同：light/flirt/kdrama/nonhuman…，硬编码会错）。
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
            # 任务丢失（服务器重启，内存态任务清空）：线上复核是否已完成。
            _wait_healthy("posts")
            if _has_posts_online(char_id):
                print(f"      帖子任务丢失但线上已完成，视为成功 {char_id}", flush=True)
                return {"generated": [{"char_id": char_id}], "errors": {}}
            print(f"      帖子任务丢失，重试 {char_id} (第 {attempt + 2} 次)", flush=True)
        except TimeoutError:
            # 轮询超时：高并发下出图排队久，任务往往仍在后台跑完。先线上复核，
            # 已有帖子就算成功（避免把其实成功的角色误判失败、下轮重复重跑）。
            if _has_posts_online(char_id):
                print(f"      帖子轮询超时但线上已完成，视为成功 {char_id}", flush=True)
                return {"generated": [{"char_id": char_id}], "errors": {}}
            print(f"      帖子轮询超时且线上无帖子，重试 {char_id} (第 {attempt + 2} 次)", flush=True)
    # 最后再复核一次，避免最后一轮刚好在超时后完成
    if _has_posts_online(char_id):
        return {"generated": [{"char_id": char_id}], "errors": {}}
    raise RuntimeError("帖子多次超时/丢失，暂缓（续跑会自动补）")


def main() -> int:
    global STATE_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all",
                    help="feiren / image / all(=feiren+image) / all_nonempty"
                         "(所有非空 source，按角色自身 track+style 生成) / 或任意具体 source 名")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="同时并行的角色数（默认 4）")
    ap.add_argument("--no-post-images", action="store_true",
                    help="帖子只出文案，不配图")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", type=str, default=str(STATE_PATH))
    args = ap.parse_args()

    STATE_PATH = Path(args.state)
    with_images = not args.no_post_images
    if args.source == "all":
        sources = ["feiren", "image"]
    elif args.source == "all_nonempty":
        sources = _all_nonempty_sources()
        print(f"all_nonempty → 覆盖非空 source: {', '.join(sources)}")
    else:
        sources = [args.source]

    def _chain_for(src: str) -> dict | None:
        """已知硬编码链路的用其配置；其余 source 返回 None，"""
        """由服务端按角色自身存储的 track+style_id 生成。"""
        return SOURCE_CHAINS.get(src)

    state = load_state()
    done = set(state["done"])

    # 组装待跑清单：每个 source 拉线上完整角色，跳过已有帖子/本地已记完成的。
    # 线上帖子探测并行化（几百个角色顺序探测太慢，会拖垮启动）。
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

    print(f"\n线上服务: {BASE}")
    for src in sources:
        ch = _chain_for(src)
        chain_desc = (f"track={ch['track']}, 画风={ch['style_id'] or '无'}"
                      if ch else "按角色自身 track+style")
        print(f"  {src}: 完整角色 {summary[src]['complete']}，待生成帖子 {summary[src]['todo']}"
              f"（{chain_desc}）")
    print(f"本轮实际生成: {len(todo)} 个角色；帖子: {'配图' if with_images else '仅文案'}\n")

    if args.dry_run:
        for cid, src in todo[:5]:
            print(f"  样例: {src}  {cid}  → {_chain_for(src) or '角色自身链路'}")
        print(f"[DRY] 计划为 {len(todo)} 个角色各生成一批 INS 帖子。")
        return 0

    if not todo:
        print("没有待生成的角色，全部已完成。")
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
            print(f"      失败[{idx}] {cid}: {e}", flush=True)

    jobs = [(i, cid, src) for i, (cid, src) in enumerate(todo, 1)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run, jobs))

    with _STATE_LOCK:
        done_n = len(state["done"])
    print(f"\n完成: 成功 {counters['ok']} 个, 失败 {counters['err']} 个。"
          f"累计帖子完成 {done_n} 个角色。")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
