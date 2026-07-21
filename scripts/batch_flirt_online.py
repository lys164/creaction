# -*- coding: utf-8 -*-
"""批次用「荷爾蒙張力(flirt track)」鏈路跑角色 —— 直接打線上服務(HTTP)。

需求對應：
- 每張圖各生成一個角色（一圖一組）。
- 按性別拼入靈感文字作為 user_hint：female 圖配 女性向 文字、male 圖配 男性向 文字。
  靈感按行切條（每行一條【場景】），無放回領取：拼過的不再拼；某性別靈感用盡則不拼（user_hint 留空）。
- 中日韓英四語言各生成一個本土化角色，source="heermeng"，track="flirt"。
- 生成封面（flirt 不拼畫風詞：cover_style_id 留空，服務端 flirt 分支按無畫風渲染 + i2i 參考圖）。
- 先不生成帖子。
- 全部資料落線上（伺服器本身即線上儲存）。
- 非圖片檔案自動跳過。

斷點續跑：進度寫 data/batch_flirt_online_state.json（已完成圖、已用靈感、已建組）。

用法：
  PYTHONPATH=. python3 scripts/batch_flirt_online.py [--limit N] [--concurrency N]
  PYTHONPATH=. python3 scripts/batch_flirt_online.py --dry-run
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
_INSP_LOCK = threading.Lock()    # 保護同性別靈感池的無放回領取

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DL = Path.home() / "Downloads"
# (人物圖資料夾, 性別)
IMAGE_FOLDERS = [
    (DL / "pinterest 女", "female"),
    (DL / "pinterest 男", "male"),
]
# 性別 → 靈感文字檔案（直接配對：女圖配女性向、男圖配男性向）
INSPIRATION_FILES = {
    "female": DL / "女性向",
    "male": DL / "男性向",
}
LANGS = "zh,ja,ko,en"
SOURCE = "heermeng"
TRACK = "flirt"
COVER_STYLE = ""            # flirt 不拼畫風詞：留空，服務端 flirt 分支按無畫風渲染
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "batch_flirt_online_state.json"

POLL_INTERVAL = 8
PERSONA_TIMEOUT = 1200      # 單組人設+封面(4語言併發)
DEFAULT_CONCURRENCY = 4
CREATE_RETRIES = 3          # 人設 JSON 截斷/零角色時整組重試次數


def _list_images(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    return sorted(str(p) for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMG_EXTS
                  and not p.name.startswith("."))


def _load_inspiration(path: Path) -> list[str]:
    """把靈感文字按行切成條，去空行、去首尾空白。每行一條【場景】。"""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip()]


def load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("done_person", [])
            s.setdefault("used_inspiration", {})
            s.setdefault("groups", [])
            for g in ("female", "male"):
                s["used_inspiration"].setdefault(g, [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done_person": [], "used_inspiration": {"female": [], "male": []}, "groups": []}


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
    """伺服器 502/宕機時阻塞等待其恢復（指數退避，最長 60s/次）。"""
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
    """輪詢任務直到 done/error/超時，返回 result。404（伺服器重啟丟任務）拋 TaskLost。"""
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


def _create_group_once(img: str, user_hint: str) -> dict:
    """提交一次「上傳圖→人設(flirt,4語言)+封面(無畫風)」任務，返回 result。"""
    handles = []
    try:
        fh = open(img, "rb")
        handles.append(fh)
        mime = mimetypes.guess_type(img)[0] or "image/png"
        files = [("files", (Path(img).name, fh, mime))]
        data = {
            "user_hint": user_hint, "one_per_image": "false", "langs": LANGS,
            "with_cover": "true", "cover_style_id": COVER_STYLE,
            "track": TRACK, "source": SOURCE,
        }
        r = _req("POST", f"{BASE}/api/personas", data=data, files=files, timeout=120)
        task_id = r.json()["task_id"]
    finally:
        for fh in handles:
            fh.close()
    return _poll(task_id, PERSONA_TIMEOUT, "人設+封面")


def _create_group(img: str, user_hint: str) -> list[dict]:
    """建一組角色；LLM 人設 JSON 截斷等導致零角色時，自動重試整組（最多 CREATE_RETRIES 次）。

    截斷是機率性的（輸出被切斷/搶算力響應不穩），重試基本能救回；重試仍全空才判失敗。
    """
    last_errs = None
    for attempt in range(1, CREATE_RETRIES + 1):
        result = _create_group_once(img, user_hint)
        chars = result.get("characters", [])
        if result.get("cover_errors"):
            print(f"      ⚠ cover_errors: {result['cover_errors']}", flush=True)
        if chars:
            if result.get("group_errors"):  # 部分語言失敗但有產出：記錄，不整組丟棄
                print(f"      ⚠ 部分語言失敗(保留已成功的): {result['group_errors']}", flush=True)
            return chars
        last_errs = result.get("group_errors")
        print(f"      ⚠ 第{attempt}次零角色(group_errors: {last_errs})"
              + ("，重試…" if attempt < CREATE_RETRIES else "，放棄"), flush=True)
    print(f"      ✗ 重試 {CREATE_RETRIES} 次仍零角色: {last_errs}", flush=True)
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="同時並行的組數（預設 4）")
    ap.add_argument("--check-remaining", action="store_true",
                    help="只列印剩餘待跑圖片數並退出（供 supervisor 判斷是否續跑）")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    person = {"female": [], "male": []}
    for folder, gender in IMAGE_FOLDERS:
        imgs = _list_images(folder)
        person[gender].extend(imgs)
        if not args.check_remaining:
            print(f"  {folder.name}: {len(imgs)} 張圖 ({gender})")

    if args.check_remaining:
        st = load_state()
        done_set = set(st.get("done_person", []))
        all_imgs = set(person["female"]) | set(person["male"])
        print(len(all_imgs - done_set))
        return 0

    inspiration = {g: _load_inspiration(p) for g, p in INSPIRATION_FILES.items()}
    for g, items in inspiration.items():
        print(f"  靈感[{g}]: {len(items)} 條（{INSPIRATION_FILES[g].name}）")

    state = load_state()
    done = set(state["done_person"])
    used_insp = {g: set(state["used_inspiration"].get(g, [])) for g in ("female", "male")}
    # 各性別可用靈感池（無放回；剔除已用），打亂後領取
    insp_avail = {g: [x for x in inspiration[g] if x not in used_insp[g]]
                  for g in ("female", "male")}
    for g in insp_avail:
        random.shuffle(insp_avail[g])

    todo = []
    for gender in ("female", "male"):
        for img in person[gender]:
            if img not in done:
                todo.append((img, gender))
    random.shuffle(todo)
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    print(f"\n線上服務: {BASE}")
    print(f"鏈路: {TRACK}；source: {SOURCE}；語言: {LANGS}；封面: 生成(無畫風詞)；帖子: 不生成")
    print(f"待跑角色: {len(todo)}（已完成 {len(done)}）\n")

    if args.dry_run:
        insp_left = {g: list(v) for g, v in insp_avail.items()}
        paired = {"female": 0, "male": 0}
        for _img, gender in todo:
            if insp_left.get(gender):
                insp_left[gender].pop()
                paired[gender] += 1
        print(f"[DRY] 計劃跑 {len(todo)} 個角色。")
        for g in ("female", "male"):
            n_no = sum(1 for _i, gg in todo if gg == g) - paired[g]
            print(f"  {g}: {paired[g]} 個會拼靈感，{max(n_no,0)} 個靈感已用盡不拼")
        for img, gender in todo[:3]:
            samp = insp_avail[gender][0] if insp_avail.get(gender) else "(無)"
            print(f"  樣例: {gender} {Path(img).name}  靈感←{samp}")
        return 0

    conc = max(1, args.concurrency)
    counters = {"ok": 0, "err": 0}

    def _take_inspiration(gender: str) -> str | None:
        """執行緒安全地無放回領一條同性別靈感；用盡返回 None（不拼）。"""
        with _INSP_LOCK:
            if insp_avail.get(gender):
                return insp_avail[gender].pop()
        return None

    def _return_inspiration(gender: str, line: str) -> None:
        with _INSP_LOCK:
            insp_avail[gender].append(line)

    total = len(todo)

    def _run_group(job: tuple[int, str, str]) -> None:
        idx, img, gender = job
        insp = _take_inspiration(gender)
        # 靈感作為創作補充要求拼入人設 prompt；用盡則留空
        user_hint = (
            f"參考情境靈感（作為人設的關係張力與開場氛圍來源，自然融入即可，不要照抄）：{insp}"
            if insp else ""
        )
        tag = f"[{idx}/{total}] {gender} {Path(img).name}" + (
            f"  靈感←{insp}" if insp else "  (無靈感)")
        print(tag, flush=True)
        persona_built = False
        try:
            chars = _create_group(img, user_hint)
            char_ids = [c["char_id"] for c in chars if c.get("char_id")]
            if not char_ids:
                raise RuntimeError("未返回任何角色 char_id")
            print(f"      角色[{idx}]: {', '.join(char_ids)}", flush=True)
            with _STATE_LOCK:
                state["done_person"].append(img)
                if insp:
                    state["used_inspiration"][gender].append(insp)
                state["groups"].append({
                    "person_image": img, "gender": gender, "inspiration": insp,
                    "group_id": chars[0].get("group_id"), "char_ids": char_ids,
                    "ts": int(time.time()),
                })
                save_state(state)
                counters["ok"] += 1
            persona_built = True
        except Exception as e:  # noqa: BLE001
            with _STATE_LOCK:
                counters["err"] += 1
            if insp and not persona_built:  # 人設階段就失敗：靈感退回池，供後續圖複用
                _return_inspiration(gender, insp)
            print(f"      ✗ 失敗[{idx}]: {e}", flush=True)

    jobs = [(i, img, gender) for i, (img, gender) in enumerate(todo, 1)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run_group, jobs))

    with _STATE_LOCK:
        done_n = len(state["done_person"])
    print(f"\n完成: 成功 {counters['ok']} 個, 失敗 {counters['err']} 個。累計完成 {done_n} 個角色。")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
