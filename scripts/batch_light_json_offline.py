# -*- coding: utf-8 -*-
"""批次用「輕劇情(light track)」鏈路跑角色 —— 本地(離線)生成，資料雙寫線上。

為什麼離線：線上伺服器磁碟 100% 滿，上傳大圖觸發 nginx 500 落盤失敗。本地直接
調 pipeline 出人設，人設 JSON 經 storage.save_json 寫本地 + arca 儲存中臺（線上），
源圖經 storage.save_file 寫本地 + OSS（火山 TOS，獨立於伺服器磁碟）——全程不碰
伺服器磁碟，線上伺服器隨即可查到（list_json 會合並 arca 遠端記錄）。

需求（同 online 版）：
- 輸入若干 JSON（小紅書匯出），一個 item = 一個角色。
- 每個角色只拼入該 item 的 desc（創作補充要求）+ images 的【所有】圖片（視覺輸入）。
- track=light（輕劇情），中日韓英四語言各生成一個本土化角色，source="mengnv"。
- 只生成角色（人設），不出封面、不出帖子。

斷點續跑：進度寫 data/batch_light_json_offline_state.json；首次會併入 online 版
已完成的 item（避免重複生成第一個）。

用法：
  export POPOP_API_KEY=sk-...
  PYTHONPATH=. python3 scripts/batch_light_json_offline.py [--limit N] [--concurrency 3]
  PYTHONPATH=. python3 scripts/batch_light_json_offline.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from app import config, pipeline, storage

_STATE_LOCK = threading.Lock()

DL = Path.home() / "Downloads"
JSON_FILES = [DL / "xhsTW.json", DL / "xhsTWN.json"]
LANGS = ["zh", "ja", "ko", "en"]
SOURCE = "mengnv"
TRACK = "light"
COVER_STYLE = "realistic_portrait"   # 封面畫風（寫實雜誌感，兼做後續自拍 i2i 人臉錨點）
DL_TIMEOUT = 120
DEFAULT_CONCURRENCY = 3

STATE_PATH = config.DATA_DIR / "batch_light_json_offline_state.json"
ONLINE_STATE_PATH = config.DATA_DIR / "batch_light_json_online_state.json"

_EXT_BY_CTYPE = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png",
                 "webp": ".webp", "gif": ".gif"}


def _item_key(item: dict) -> str:
    url = (item.get("url") or "").strip()
    if url:
        return url
    raw = json.dumps({"desc": item.get("desc", ""), "images": item.get("images", [])},
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
        n = 0
        for it in data:
            if isinstance(it, dict):
                items.append(it)
                n += 1
        print(f"  {f.name}: {n} 個 item")
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
    # 首次：併入 online 版已完成，避免重複生成第一個 item
    seed_done: list[str] = []
    try:
        o = json.loads(ONLINE_STATE_PATH.read_text(encoding="utf-8"))
        seed_done = list(o.get("done", []))
    except (OSError, json.JSONDecodeError):
        pass
    return {"done": seed_done, "groups": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _download_all(urls: list[str], key: str) -> list[str]:
    """下載 item 的所有圖片，storage.save_file 落本地 uploads + OSS。返回本地路徑。"""
    paths: list[str] = []
    kh = hashlib.sha1(key.encode()).hexdigest()[:10]
    for i, u in enumerate(urls):
        if not isinstance(u, str) or not u.lower().startswith("http"):
            continue
        try:
            r = requests.get(u, timeout=DL_TIMEOUT)
            if not r.ok or not r.content:
                print(f"      ⚠ 圖片下載失敗 {r.status_code}: {u[:70]}", flush=True)
                continue
        except requests.RequestException as e:
            print(f"      ⚠ 圖片下載異常: {e}", flush=True)
            continue
        ctype = (r.headers.get("Content-Type") or "").lower()
        ext = next((v for k, v in _EXT_BY_CTYPE.items() if k in ctype), ".png")
        dest = config.UPLOAD_DIR / f"mengnv_{kh}_{i}{ext}"
        content_type = ctype.split(";")[0] or "image/png"
        storage.save_file(dest, r.content, content_type)  # 本地 + OSS
        paths.append(str(dest))
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--cover-style", default=COVER_STYLE)
    ap.add_argument("--no-cover", action="store_true", help="不生成封面")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    make_cover = not args.no_cover

    if not config.API_KEY or not config.API_KEY.startswith("sk-"):
        print("✗ 未配置 POPOP_API_KEY（export POPOP_API_KEY=sk-...）")
        return 2
    if not storage.arca_storage.enabled():
        print("✗ arca 儲存未啟用，本地資料不會同步到線上（需 ARCA_STORAGE_KEY）")
        return 2

    print(f"本地生成 → 雙寫線上 arca 中臺 ({config.ARCA_BASE_URL})")
    print(f"LLM providers: {[p['base'] for p in config.LLM_API_PROVIDERS]}")
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
          f"source={SOURCE} langs={','.join(LANGS)}；帖子=不生成，封面={cover_desc}\n")

    if args.dry_run:
        for key, desc, imgs in todo[:5]:
            print(f"  樣例: imgs={len(imgs)} desc={desc[:36]!r} key={key[:56]}")
        print(f"[DRY] 計劃跑 {len(todo)} 個角色（每個 4 語言）。")
        return 0
    if not todo:
        print("沒有待跑角色。")
        return 0

    conc = max(1, args.concurrency)
    counters = {"ok": 0, "err": 0}
    total = len(todo)

    def _run(job):
        idx, (key, desc, imgs) = job
        print(f"[{idx}/{total}] imgs={len(imgs)} {desc[:30]!r}", flush=True)
        try:
            local = _download_all(imgs, key)
            if not local and not desc:
                raise RuntimeError("無可用圖片且無 desc")
            recs = pipeline.create_personas_from_images(
                local, LANGS, user_hint=desc, track=TRACK, source=SOURCE)
            char_ids = [r["char_id"] for r in recs if r.get("char_id")]
            if not char_ids:
                raise RuntimeError("未返回任何角色 char_id")
            print(f"      角色[{idx}]: {', '.join(char_ids)}", flush=True)

            cover_errors: dict[str, str] = {}
            if make_cover:
                def _cover(cid: str):
                    try:
                        pipeline.generate_cover(cid, args.cover_style,
                                                use_reference=None,
                                                mode="fill_missing")
                        return cid, None
                    except Exception as ce:  # noqa: BLE001
                        return cid, str(ce)
                with ThreadPoolExecutor(
                        max_workers=min(len(char_ids), config.MAX_WORKERS)) as cex:
                    for cid, err in cex.map(_cover, char_ids):
                        if err:
                            cover_errors[cid] = err
                if cover_errors:
                    print(f"      ⚠ 封面 errors[{idx}]: {cover_errors}", flush=True)

            with _STATE_LOCK:
                state["done"].append(key)
                state["groups"].append({
                    "key": key, "group_id": recs[0].get("group_id"),
                    "char_ids": char_ids, "n_imgs": len(local),
                    "cover_ok": make_cover and not cover_errors,
                    "cover_errors": cover_errors or None,
                    "ts": int(time.time()),
                })
                save_state(state)
                counters["ok"] += 1
        except Exception as e:  # noqa: BLE001
            with _STATE_LOCK:
                counters["err"] += 1
            print(f"      ✗ 失敗[{idx}]: {type(e).__name__}: {e}", flush=True)

    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run, list(enumerate(todo, 1))))

    with _STATE_LOCK:
        done_n = len(state["done"])
    print(f"\n完成: 成功 {counters['ok']} 個, 失敗 {counters['err']} 個。"
          f"累計完成 {done_n} 個角色。")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
