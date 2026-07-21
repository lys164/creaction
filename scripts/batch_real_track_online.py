# -*- coding: utf-8 -*-
"""批次用「真實人設(real track)」鏈路跑角色 —— 直接打線上服務(HTTP)。

線上伺服器憑據有效，走它避免本地 key 失效問題。對應需求：
- 人物圖各是一個角色；50% 機率再從【同性別】非人物相簿拼一張成 2 張輸入。
- 圖片單次領料：選過的圖（人物/非人物）不再選；同性別非人物圖用盡則不拼。
- source="image"，中日韓英四語言各生成一個本土化角色。
- 生成人設時會拼入職業庫/性格庫（伺服器內部按冷卻期發放；這是線上既定行為，
  非「嚴格單次」——需嚴格單次請走本地 monkeypatch 版）。
- 帖子出配圖；先出封面(畫風 realistic_portrait)做自拍圖生圖的人臉錨點。
- 全部資料落線上（伺服器本身即線上儲存）。

斷點續跑：進度寫 data/batch_real_online_state.json（已完成人物圖、已消耗非人物圖）。

用法：
  PYTHONPATH=. python3 scripts/batch_real_track_online.py [--limit N] [--no-post-images]
  PYTHONPATH=. python3 scripts/batch_real_track_online.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

_STATE_LOCK = threading.Lock()   # 保護 state 讀改寫 + 落盤
_NP_LOCK = threading.Lock()      # 保護同性別非人物圖池的無放回領取

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DL = Path.home() / "Downloads"
FOLDERS = [
    (DL / "女人設40", "female", True),
    (DL / "男人設60", "male", True),
    (DL / "男人設60-非人物圖", "male", False),
    (DL / " 女人設40 非人物圖", "female", False),
]
LANGS = "zh,ja,ko,en"
SOURCE = "image"
COVER_STYLE = "realistic_portrait"
COMBINE_PROB = 0.5
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "batch_real_online_state.json"

# 輪詢：人設(含封面)較慢，帖子配圖更慢，給足超時
POLL_INTERVAL = 8
PERSONA_TIMEOUT = 2400      # 單組人設+封面(4語言)；伺服器被他人佔用時留足排隊時間
POSTS_TIMEOUT = 2400        # 單組帖子配圖(4角色×~9圖)
DEFAULT_CONCURRENCY = 4     # 同時並行的組數（--concurrency 覆蓋）


def _list_images(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    return sorted(str(p) for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMG_EXTS
                  and not p.name.startswith("."))


def load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            for k in ("done_person", "used_nonperson", "groups"):
                s.setdefault(k, [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done_person": [], "used_nonperson": [], "groups": []}


def save_state(state: dict) -> None:
    """原子寫盤（tmp+rename），併發下讀者永遠看到完整 JSON。呼叫方持有 _STATE_LOCK。"""
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
    """伺服器 502/宕機時阻塞等待其恢復（指數退避，最長 60s/次），避免空燒佇列。"""
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
    """帶重試/退避的 HTTP：對 5xx、429、連線錯誤重試；伺服器宕機時先等恢復。

    allow_404=True 時，404 直接返回響應（呼叫方判定），不當錯誤重試。
    """
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
    """輪詢任務直到 done/error/超時，返回 result。

    任務 404（伺服器重啟丟失記憶體態任務）拋 TaskLost，由呼叫方按線上真實狀態複核，
    而不是把可能已在後臺完成/持久化的工作誤判為失敗。
    """
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


def _posts_done_online(char_ids: list[str]) -> bool:
    """線上複核：該組全部角色都已有帖子（用於任務丟失後判斷是否真失敗）。"""
    for cid in char_ids:
        try:
            r = _req("GET", f"{BASE}/api/ig_posts/{cid}/latest", timeout=30)
            ig = r.json()
        except Exception:  # noqa: BLE001
            return False
        if not (ig and ig.get("posts")):
            return False
    return True


def _create_group(imgs: list[str]) -> list[dict]:
    """上傳一組圖 → 人設(real,4語言,source=image)+封面(realistic_portrait)。返回角色記錄。"""
    files = []
    handles = []
    try:
        for p in imgs:
            fh = open(p, "rb")
            handles.append(fh)
            mime = mimetypes.guess_type(p)[0] or "image/png"
            files.append(("files", (Path(p).name, fh, mime)))
        data = {
            "user_hint": "", "one_per_image": "false", "langs": LANGS,
            "with_cover": "true", "cover_style_id": COVER_STYLE,
            "track": "real", "source": SOURCE,
        }
        r = _req("POST", f"{BASE}/api/personas", data=data, files=files, timeout=120)
        task_id = r.json()["task_id"]
    finally:
        for fh in handles:
            fh.close()
    result = _poll(task_id, PERSONA_TIMEOUT, "人設+封面")
    chars = result.get("characters", [])
    if result.get("group_errors"):
        print(f"      ⚠ group_errors: {result['group_errors']}", flush=True)
    if result.get("cover_errors"):
        print(f"      ⚠ cover_errors: {result['cover_errors']}", flush=True)
    return chars


def _generate_posts(char_ids: list[str], with_images: bool) -> dict:
    """生成帖子；任務因伺服器重啟丟失時，先線上複核是否已完成，否則重試整批。"""
    for attempt in range(3):
        r = _req("POST", f"{BASE}/api/ig_posts/batch", json={
            "char_ids": char_ids, "style_id": COVER_STYLE,
            "with_images": with_images, "track": "real",
        }, timeout=120)
        task_id = r.json()["task_id"]
        try:
            return _poll(task_id, POSTS_TIMEOUT, "帖子")
        except TaskLost:
            _wait_healthy("posts")
            if _posts_done_online(char_ids):
                print("      ↻ 帖子任務丟失但線上已完成，視為成功", flush=True)
                return {"generated": [{"char_id": c} for c in char_ids], "errors": {}}
            print(f"      ↻ 帖子任務丟失，重試整批 (第 {attempt + 2} 次)", flush=True)
    raise RuntimeError("帖子多次因伺服器重啟丟失，暫緩（續跑會自動補）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-post-images", action="store_true",
                    help="帖子只出文案，不配圖")
    ap.add_argument("--no-posts", action="store_true",
                    help="完全不生成帖子，只出人設 + 封面（最輕負載）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="同時並行的組數（預設 4）")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    make_posts = not args.no_posts
    with_post_images = make_posts and not args.no_post_images

    person = {"female": [], "male": []}
    nonperson = {"female": [], "male": []}
    for folder, gender, is_person in FOLDERS:
        imgs = _list_images(folder)
        (person if is_person else nonperson)[gender].extend(imgs)
        print(f"  {folder.name}: {len(imgs)} 張 ({gender}, {'人物' if is_person else '非人物'})")

    state = load_state()
    done = set(state["done_person"])
    used_np = set(state["used_nonperson"])
    np_avail = {g: [p for p in nonperson[g] if p not in used_np] for g in nonperson}
    for g in np_avail:
        random.shuffle(np_avail[g])

    todo = []
    for gender in ("female", "male"):
        for img in person[gender]:
            if img not in done:
                todo.append((img, gender))
    random.shuffle(todo)
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    posts_desc = "不生成" if not make_posts else ("配圖" if with_post_images else "僅文案")
    print(f"\n線上服務: {BASE}")
    print(f"待跑角色組: {len(todo)}（已完成 {len(done)}）；"
          f"帖子: {posts_desc}；封面畫風: {COVER_STYLE}\n")

    if args.dry_run:
        np_left = {g: list(v) for g, v in np_avail.items()}
        combine = 0
        for _img, gender in todo:
            if random.random() < COMBINE_PROB and np_left.get(gender):
                np_left[gender].pop()
                combine += 1
        print(f"[DRY] 計劃跑 {len(todo)} 組，約 {combine} 組會拼第二張同性別非人物圖。")
        for img, gender in todo[:3]:
            print(f"  樣例: {gender} {Path(img).name}")
        return 0

    conc = max(1, args.concurrency)
    counters = {"ok": 0, "err": 0}

    def _take_nonperson(gender: str) -> str | None:
        """執行緒安全地無放回領一張同性別非人物圖（50% 機率觸發）。"""
        with _NP_LOCK:
            if random.random() < COMBINE_PROB and np_avail.get(gender):
                return np_avail[gender].pop()
        return None

    def _return_nonperson(gender: str, path: str) -> None:
        with _NP_LOCK:
            np_avail[gender].append(path)

    # 第 0 步：並行補跑「人設已建但帖子未完成」的歷史組（不重建人設，避免重複角色）。
    # --no-posts 模式跳過：本輪只出人設+封面，不碰帖子（欠的帖子留待日後補）。
    if make_posts:
        pending = [g for g in state["groups"]
                   if g.get("char_ids") and not g.get("posts_ok")]
        if pending:
            print(f"並行補跑 {len(pending)} 個「人設已建、帖子未完成」的組（併發 {conc}）：", flush=True)

            def _backfill(g: dict) -> None:
                print(f"  補帖子 {g.get('group_id')} {Path(g['person_image']).name}", flush=True)
                try:
                    res = _generate_posts(g["char_ids"], with_post_images)
                    if res.get("errors"):
                        print(f"      ⚠ 帖子 errors {g.get('group_id')}: {res['errors']}", flush=True)
                    with _STATE_LOCK:
                        g["posts_ok"] = True
                        save_state(state)
                except Exception as e:  # noqa: BLE001
                    print(f"      ✗ 補帖子失敗 {g.get('group_id')}: {e}", flush=True)

            with ThreadPoolExecutor(max_workers=conc) as ex:
                list(ex.map(_backfill, pending))

    total = len(todo)

    def _run_group(job: tuple[int, str, str]) -> None:
        idx, img, gender = job
        picked_np = _take_nonperson(gender)
        imgs = [img] + ([picked_np] if picked_np else [])
        tag = f"[{idx}/{total}] {gender} {Path(img).name}" + (
            f" +非人物 {Path(picked_np).name}" if picked_np else "")
        print(tag, flush=True)
        persona_built = False
        try:
            chars = _create_group(imgs)
            char_ids = [c["char_id"] for c in chars if c.get("char_id")]
            if not char_ids:
                raise RuntimeError("未返回任何角色 char_id")
            print(f"      角色[{idx}]: {', '.join(char_ids)}", flush=True)
            # 人設一建成即落賬（posts_ok=False）：帖子失敗續跑只補帖子，絕不重建人設。
            with _STATE_LOCK:
                state["done_person"].append(img)
                if picked_np:
                    state["used_nonperson"].append(picked_np)
                grp = {
                    "person_image": img, "nonperson_image": picked_np,
                    "gender": gender, "group_id": chars[0].get("group_id"),
                    "char_ids": char_ids, "posts_ok": False, "ts": int(time.time()),
                }
                state["groups"].append(grp)
                save_state(state)
            persona_built = True

            if not make_posts:  # 只出人設+封面：本組到此完成
                with _STATE_LOCK:
                    counters["ok"] += 1
                return
            posts_res = _generate_posts(char_ids, with_post_images)
            if posts_res.get("errors"):
                print(f"      ⚠ 帖子 errors[{idx}]: {posts_res['errors']}", flush=True)
            with _STATE_LOCK:
                grp["posts_ok"] = True
                save_state(state)
                counters["ok"] += 1
        except Exception as e:  # noqa: BLE001
            with _STATE_LOCK:
                counters["err"] += 1
            if picked_np and not persona_built:  # 人設階段就失敗：非人物圖退回池
                _return_nonperson(gender, picked_np)
            print(f"      ✗ 失敗[{idx}]: {e}", flush=True)

    jobs = [(i, img, gender) for i, (img, gender) in enumerate(todo, 1)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run_group, jobs))

    with _STATE_LOCK:
        pending_left = sum(1 for g in state["groups"]
                           if g.get("char_ids") and not g.get("posts_ok"))
        done_n = len(state["done_person"])
    print(f"\n完成: 成功 {counters['ok']} 組, 失敗 {counters['err']} 組。"
          f"累計人設完成 {done_n} 組，待補帖子 {pending_left} 組。")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
