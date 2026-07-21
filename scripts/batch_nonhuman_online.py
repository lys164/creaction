# -*- coding: utf-8 -*-
"""批次用「非人物(nonhuman)」鏈路跑角色 —— 直接打線上服務(HTTP)。

對應需求：
- 一張圖 = 一個角色組（one_per_image=true）；不做人物/非人物拼圖。
- 中日韓英四語言各生成一個本土化角色（共享 group_id）。
- track="nonhuman"（非人物鏈路：不套畫風、按 identity+原圖生成）。
- source="feiren"。
- 只生成人設（+封面，nonhuman 封面不套畫風），先不生成帖子。
- 全部資料落線上（伺服器本身即線上儲存）。

斷點續跑：進度寫 data/batch_nonhuman_online_state.json（已完成的圖）。

用法：
  PYTHONPATH=. python3 scripts/batch_nonhuman_online.py [--limit N] [--concurrency N]
  PYTHONPATH=. python3 scripts/batch_nonhuman_online.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

_STATE_LOCK = threading.Lock()   # 保護 state 讀改寫 + 落盤

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
IMAGE_DIR = Path.home() / "Downloads" / "meme"
LANGS = "zh,ja,ko,en"
SOURCE = "feiren"
TRACK = "nonhuman"
WITH_COVER = True                # nonhuman 封面不套畫風，伺服器內部處理
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "batch_nonhuman_online_state.json"

POLL_INTERVAL = 8
PERSONA_TIMEOUT = 1200      # 單組人設+封面(4語言併發)
DEFAULT_CONCURRENCY = 4


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
            for k in ("done", "groups"):
                s.setdefault(k, [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done": [], "groups": []}


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
    """帶重試/退避的 HTTP：對 5xx、429、連線錯誤重試；伺服器宕機時先等恢復。"""
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
    """輪詢任務直到 done/error/超時，返回 result。"""
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


def _create_group(img: str) -> list[dict]:
    """上傳一張圖 → 非人物人設(nonhuman,4語言,source=feiren)+封面。返回角色記錄。"""
    handles = []
    try:
        fh = open(img, "rb")
        handles.append(fh)
        mime = mimetypes.guess_type(img)[0] or "image/png"
        files = [("files", (Path(img).name, fh, mime))]
        data = {
            "user_hint": "", "one_per_image": "true", "langs": LANGS,
            "with_cover": "true" if WITH_COVER else "false", "cover_style_id": "",
            "track": TRACK, "source": SOURCE,
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


def main() -> int:
    global STATE_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="同時並行的組數（預設 4）")
    ap.add_argument("--image-dir", type=str, default=str(IMAGE_DIR),
                    help="圖片目錄（每張圖 = 一個角色組）")
    ap.add_argument("--state", type=str, default=str(STATE_PATH),
                    help="斷點續跑進度檔案路徑")
    args = ap.parse_args()

    STATE_PATH = Path(args.state)
    image_dir = Path(args.image_dir)

    all_imgs = _list_images(image_dir)
    print(f"  {image_dir}: {len(all_imgs)} 張圖")

    state = load_state()
    done = set(state["done"])
    todo = [p for p in all_imgs if p not in done]
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    print(f"\n線上服務: {BASE}")
    print(f"鏈路: {TRACK}（非人物）；source={SOURCE}；語言={LANGS}；帖子: 不生成")
    print(f"待跑角色: {len(todo)} 張圖（已完成 {len(done)}）\n")

    if args.dry_run:
        print(f"[DRY] 計劃跑 {len(todo)} 張圖，每張 = 1 組（{LANGS} 各一角色）。")
        for img in todo[:3]:
            print(f"  樣例: {Path(img).name}")
        return 0

    conc = max(1, args.concurrency)
    total = len(todo)
    counters = {"ok": 0, "err": 0}

    def _run_group(job: tuple[int, str]) -> None:
        idx, img = job
        print(f"[{idx}/{total}] {Path(img).name}", flush=True)
        try:
            chars = _create_group(img)
            char_ids = [c["char_id"] for c in chars if c.get("char_id")]
            if not char_ids:
                raise RuntimeError("未返回任何角色 char_id")
            print(f"      角色[{idx}]: {', '.join(char_ids)}", flush=True)
            with _STATE_LOCK:
                state["done"].append(img)
                state["groups"].append({
                    "image": img, "group_id": chars[0].get("group_id"),
                    "char_ids": char_ids, "ts": int(time.time()),
                })
                save_state(state)
                counters["ok"] += 1
        except Exception as e:  # noqa: BLE001
            with _STATE_LOCK:
                counters["err"] += 1
            print(f"      ✗ 失敗[{idx}]: {e}", flush=True)

    jobs = [(i, img) for i, img in enumerate(todo, 1)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run_group, jobs))

    with _STATE_LOCK:
        done_n = len(state["done"])
    print(f"\n完成: 成功 {counters['ok']} 組, 失敗 {counters['err']} 組。"
          f"累計人設完成 {done_n} 張圖。")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
