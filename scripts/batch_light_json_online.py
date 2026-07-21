# -*- coding: utf-8 -*-
"""批次用「輕劇情(light track)」鏈路跑角色 —— 直接打線上服務(HTTP)。

需求（本指令碼）：
- 輸入是若干 JSON 檔案（小紅書匯出），一個 item = 一個角色。
- 每個角色只拼入該 item 的 desc（作為創作補充要求）+ images 裡的【所有】圖片（下載後作為視覺輸入）。
- 走線上鏈路，track=light（輕劇情），中日韓英四語言各生成一個本土化角色。
- source="mengnv"。
- 只生成角色（人設），先不生成帖子，也不出封面（最輕負載）。
- 全部資料落線上（伺服器本身即線上儲存）。

斷點續跑：進度寫 data/batch_light_json_online_state.json（已完成的 item 唯一鍵）。

用法：
  PYTHONPATH=. python3 scripts/batch_light_json_online.py [--limit N] [--concurrency 4]
  PYTHONPATH=. python3 scripts/batch_light_json_online.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

_STATE_LOCK = threading.Lock()

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DL = Path.home() / "Downloads"
JSON_FILES = [DL / "xhsTW.json", DL / "xhsTWN.json"]

LANGS = "zh,ja,ko,en"
SOURCE = "mengnv"
TRACK = "light"
COVER_STYLE = "realistic_portrait"   # 封面畫風（寫實雜誌感，兼做後續自拍 i2i 人臉錨點）
STATE_PATH = (Path(__file__).resolve().parent.parent
              / "data" / "batch_light_json_online_state.json")

POLL_INTERVAL = 8
PERSONA_TIMEOUT = 1800      # 單組人設(4語言併發) 給足超時
DEFAULT_CONCURRENCY = 4
DL_TIMEOUT = 120


def _item_key(item: dict) -> str:
    """item 的穩定唯一鍵：優先原始 url，否則對 desc+images 做雜湊。"""
    url = (item.get("url") or "").strip()
    if url:
        return url
    raw = json.dumps(
        {"desc": item.get("desc", ""), "images": item.get("images", [])},
        ensure_ascii=False, sort_keys=True)
    return "h:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _load_items() -> list[dict]:
    items: list[dict] = []
    for f in JSON_FILES:
        if not f.exists():
            print(f"  ⚠ 缺檔案: {f}")
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("data") or data.get("items") or [data]
        for it in data:
            if isinstance(it, dict):
                items.append(it)
        print(f"  {f.name}: {sum(1 for x in data if isinstance(x, dict))} 個 item")
    return items


def load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("done", [])
            s.setdefault("groups", [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done": [], "groups": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(STATE_PATH)


def _healthy() -> bool:
    try:
        r = requests.get(f"{BASE}/api/languages", timeout=20)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _wait_healthy(label: str = "") -> None:
    delay = 5
    waited = 0
    while not _healthy():
        print(f"      ⏳ 伺服器不可用，等待恢復{(' ('+label+')') if label else ''} "
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


_EXT_BY_CTYPE = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png",
                 "webp": ".webp", "gif": ".gif"}


def _download_all(urls: list[str], tmp_dir: Path) -> list[Path]:
    """下載該 item 的所有圖片到本地臨時目錄，返回本地路徑（失敗的圖跳過）。"""
    paths: list[Path] = []
    for i, u in enumerate(urls):
        if not isinstance(u, str) or not u.lower().startswith("http"):
            continue
        try:
            r = requests.get(u, timeout=DL_TIMEOUT)
            if not r.ok or not r.content:
                print(f"      ⚠ 圖片下載失敗 {r.status_code}: {u[:80]}", flush=True)
                continue
        except requests.RequestException as e:
            print(f"      ⚠ 圖片下載異常: {e}", flush=True)
            continue
        ctype = (r.headers.get("Content-Type") or "").lower()
        ext = ".png"
        for k, v in _EXT_BY_CTYPE.items():
            if k in ctype:
                ext = v
                break
        dest = tmp_dir / f"img_{i}{ext}"
        dest.write_bytes(r.content)
        paths.append(dest)
    return paths


def _create_group(desc: str, img_paths: list[Path],
                  with_cover: bool = True,
                  cover_style: str = COVER_STYLE) -> list[dict]:
    """上傳一個 item 的所有圖 + desc → 人設(light, 4語言, source=mengnv) + 封面。"""
    files = []
    handles = []
    try:
        for p in img_paths:
            fh = open(p, "rb")
            handles.append(fh)
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            files.append(("files", (p.name, fh, mime)))
        data = {
            "user_hint": desc or "",
            "one_per_image": "false",   # 該 item 的所有圖片合成一個角色
            "langs": LANGS,
            "with_cover": "true" if with_cover else "false",
            "cover_style_id": cover_style if with_cover else "",
            "track": TRACK,
            "source": SOURCE,
        }
        r = _req("POST", f"{BASE}/api/personas", data=data, files=files,
                 timeout=180)
        task_id = r.json()["task_id"]
    finally:
        for fh in handles:
            fh.close()
    result = _poll(task_id, PERSONA_TIMEOUT, "人設+封面" if with_cover else "人設")
    chars = result.get("characters", [])
    if result.get("group_errors"):
        print(f"      ⚠ group_errors: {result['group_errors']}", flush=True)
    if result.get("cover_errors"):
        print(f"      ⚠ cover_errors: {result['cover_errors']}", flush=True)
    return chars


def _batch_cover(char_ids: list[str], cover_style: str) -> dict:
    """為已建好的角色補封面（走服務端 /api/characters/batch_cover 非同步任務）。"""
    r = _req("POST", f"{BASE}/api/characters/batch_cover", json={
        "char_ids": char_ids, "style_id": cover_style,
        "use_reference": None, "mode": "fill_missing",
    }, timeout=120)
    task_id = r.json()["task_id"]
    return _poll(task_id, PERSONA_TIMEOUT, "補封面")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--cover-style", default=COVER_STYLE)
    ap.add_argument("--no-cover", action="store_true", help="不生成封面")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    make_cover = not args.no_cover

    print(f"線上服務: {BASE}")
    items = _load_items()
    state = load_state()
    done = set(state["done"])

    todo = []
    for it in items:
        key = _item_key(it)
        if key in done:
            continue
        imgs = [u for u in (it.get("images") or [])
                if isinstance(u, str) and u.lower().startswith("http")]
        desc = (it.get("desc") or "").strip()
        if not imgs and not desc:
            continue
        todo.append((key, desc, imgs))
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    cover_desc = f"生成({args.cover_style})" if make_cover else "不生成"
    print(f"\n待跑角色: {len(todo)}（已完成 {len(done)}）；track={TRACK} "
          f"source={SOURCE} langs={LANGS}；帖子=不生成，封面={cover_desc}\n")

    if args.dry_run:
        for key, desc, imgs in todo[:5]:
            print(f"  樣例: imgs={len(imgs)} desc={desc[:40]!r} key={key[:60]}")
        print(f"[DRY] 計劃跑 {len(todo)} 個角色（每個 4 語言）。")
        return 0

    if not todo:
        print("沒有待跑角色。")
        return 0

    conc = max(1, args.concurrency)
    counters = {"ok": 0, "err": 0}
    total = len(todo)
    tmp_root = STATE_PATH.parent / "batch_light_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)

    # 補跑歷史組的缺失封面（如首個"僅人設"組），不重建人設。
    if make_cover:
        pending_cov = [g for g in state["groups"]
                       if g.get("char_ids") and not g.get("cover_ok")]
        if pending_cov:
            print(f"補封面：{len(pending_cov)} 個歷史組（併發 {conc}）", flush=True)

            def _bf(g: dict) -> None:
                try:
                    res = _batch_cover(g["char_ids"], args.cover_style)
                    if res.get("errors"):
                        print(f"      ⚠ 補封面 errors {g.get('group_id')}: "
                              f"{res['errors']}", flush=True)
                    with _STATE_LOCK:
                        g["cover_ok"] = True
                        save_state(state)
                except Exception as e:  # noqa: BLE001
                    print(f"      ✗ 補封面失敗 {g.get('group_id')}: {e}", flush=True)

            with ThreadPoolExecutor(max_workers=conc) as ex:
                list(ex.map(_bf, pending_cov))

    def _run(job):
        idx, (key, desc, imgs) = job
        tag = f"[{idx}/{total}] imgs={len(imgs)} {desc[:30]!r}"
        print(tag, flush=True)
        item_tmp = tmp_root / hashlib.sha1(key.encode()).hexdigest()[:12]
        item_tmp.mkdir(parents=True, exist_ok=True)
        try:
            local = _download_all(imgs, item_tmp)
            if not local and not desc:
                raise RuntimeError("無可用圖片且無 desc")
            chars = _create_group(desc, local, with_cover=make_cover,
                                   cover_style=args.cover_style)
            char_ids = [c["char_id"] for c in chars if c.get("char_id")]
            if not char_ids:
                raise RuntimeError("未返回任何角色 char_id")
            print(f"      角色[{idx}]: {', '.join(char_ids)}", flush=True)
            with _STATE_LOCK:
                state["done"].append(key)
                state["groups"].append({
                    "key": key, "group_id": chars[0].get("group_id"),
                    "char_ids": char_ids, "n_imgs": len(local),
                    "cover_ok": make_cover, "ts": int(time.time()),
                })
                save_state(state)
                counters["ok"] += 1
        except Exception as e:  # noqa: BLE001
            with _STATE_LOCK:
                counters["err"] += 1
            print(f"      ✗ 失敗[{idx}]: {e}", flush=True)
        finally:
            for p in item_tmp.glob("*"):
                try:
                    p.unlink()
                except OSError:
                    pass
            try:
                item_tmp.rmdir()
            except OSError:
                pass

    jobs = list(enumerate(todo, 1))
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run, jobs))

    with _STATE_LOCK:
        done_n = len(state["done"])
    print(f"\n完成: 成功 {counters['ok']} 個, 失敗 {counters['err']} 個。"
          f"累計完成 {done_n} 個角色。")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
