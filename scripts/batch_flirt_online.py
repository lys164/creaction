# -*- coding: utf-8 -*-
"""批量用「荷尔蒙张力(flirt track)」链路跑角色 —— 直接打线上服务(HTTP)。

需求对应：
- 每张图各生成一个角色（一图一组）。
- 按性别拼入灵感文本作为 user_hint：female 图配 女性向 文本、male 图配 男性向 文本。
  灵感按行切条（每行一条【场景】），无放回领取：拼过的不再拼；某性别灵感用尽则不拼（user_hint 留空）。
- 中日韩英四语言各生成一个本土化角色，source="heermeng"，track="flirt"。
- 生成封面（flirt 不拼画风词：cover_style_id 留空，服务端 flirt 分支按无画风渲染 + i2i 参考图）。
- 先不生成帖子。
- 全部数据落线上（服务器本身即线上存储）。
- 非图片文件自动跳过。

断点续跑：进度写 data/batch_flirt_online_state.json（已完成图、已用灵感、已建组）。

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

_STATE_LOCK = threading.Lock()   # 保护 state 读改写 + 落盘
_INSP_LOCK = threading.Lock()    # 保护同性别灵感池的无放回领取

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DL = Path.home() / "Downloads"
# (人物图文件夹, 性别)
IMAGE_FOLDERS = [
    (DL / "pinterest 女", "female"),
    (DL / "pinterest 男", "male"),
]
# 性别 → 灵感文本文件（直接配对：女图配女性向、男图配男性向）
INSPIRATION_FILES = {
    "female": DL / "女性向",
    "male": DL / "男性向",
}
LANGS = "zh,ja,ko,en"
SOURCE = "heermeng"
TRACK = "flirt"
COVER_STYLE = ""            # flirt 不拼画风词：留空，服务端 flirt 分支按无画风渲染
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "batch_flirt_online_state.json"

POLL_INTERVAL = 8
PERSONA_TIMEOUT = 1200      # 单组人设+封面(4语言并发)
DEFAULT_CONCURRENCY = 4
CREATE_RETRIES = 3          # 人设 JSON 截断/零角色时整组重试次数


def _list_images(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    return sorted(str(p) for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMG_EXTS
                  and not p.name.startswith("."))


def _load_inspiration(path: Path) -> list[str]:
    """把灵感文本按行切成条，去空行、去首尾空白。每行一条【场景】。"""
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
    """原子写盘（tmp+rename），并发下读者永远看到完整 JSON。调用方持有 _STATE_LOCK。"""
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
    delay = 5
    waited = 0
    while not _healthy():
        print(f"      ⏳ 服务器不可用，等待恢复{(' ('+label+')') if label else ''} "
              f"已等 {waited}s", flush=True)
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 60)


class TaskLost(Exception):
    """任务在服务器端丢失（进程重启，内存态任务清空 → /api/tasks 返回 404）。"""


def _req(method: str, url: str, allow_404: bool = False, **kw) -> requests.Response:
    """带重试/退避的 HTTP：对 5xx、429、连接错误重试；服务器宕机时先等恢复。"""
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
    """轮询任务直到 done/error/超时，返回 result。404（服务器重启丢任务）抛 TaskLost。"""
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


def _create_group_once(img: str, user_hint: str) -> dict:
    """提交一次「上传图→人设(flirt,4语言)+封面(无画风)」任务，返回 result。"""
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
    return _poll(task_id, PERSONA_TIMEOUT, "人设+封面")


def _create_group(img: str, user_hint: str) -> list[dict]:
    """建一组角色；LLM 人设 JSON 截断等导致零角色时，自动重试整组（最多 CREATE_RETRIES 次）。

    截断是概率性的（输出被切断/抢算力响应不稳），重试基本能救回；重试仍全空才判失败。
    """
    last_errs = None
    for attempt in range(1, CREATE_RETRIES + 1):
        result = _create_group_once(img, user_hint)
        chars = result.get("characters", [])
        if result.get("cover_errors"):
            print(f"      ⚠ cover_errors: {result['cover_errors']}", flush=True)
        if chars:
            if result.get("group_errors"):  # 部分语言失败但有产出：记录，不整组丢弃
                print(f"      ⚠ 部分语言失败(保留已成功的): {result['group_errors']}", flush=True)
            return chars
        last_errs = result.get("group_errors")
        print(f"      ⚠ 第{attempt}次零角色(group_errors: {last_errs})"
              + ("，重试…" if attempt < CREATE_RETRIES else "，放弃"), flush=True)
    print(f"      ✗ 重试 {CREATE_RETRIES} 次仍零角色: {last_errs}", flush=True)
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="同时并行的组数（默认 4）")
    ap.add_argument("--check-remaining", action="store_true",
                    help="只打印剩余待跑图片数并退出（供 supervisor 判断是否续跑）")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    person = {"female": [], "male": []}
    for folder, gender in IMAGE_FOLDERS:
        imgs = _list_images(folder)
        person[gender].extend(imgs)
        if not args.check_remaining:
            print(f"  {folder.name}: {len(imgs)} 张图 ({gender})")

    if args.check_remaining:
        st = load_state()
        done_set = set(st.get("done_person", []))
        all_imgs = set(person["female"]) | set(person["male"])
        print(len(all_imgs - done_set))
        return 0

    inspiration = {g: _load_inspiration(p) for g, p in INSPIRATION_FILES.items()}
    for g, items in inspiration.items():
        print(f"  灵感[{g}]: {len(items)} 条（{INSPIRATION_FILES[g].name}）")

    state = load_state()
    done = set(state["done_person"])
    used_insp = {g: set(state["used_inspiration"].get(g, [])) for g in ("female", "male")}
    # 各性别可用灵感池（无放回；剔除已用），打乱后领取
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

    print(f"\n线上服务: {BASE}")
    print(f"链路: {TRACK}；source: {SOURCE}；语言: {LANGS}；封面: 生成(无画风词)；帖子: 不生成")
    print(f"待跑角色: {len(todo)}（已完成 {len(done)}）\n")

    if args.dry_run:
        insp_left = {g: list(v) for g, v in insp_avail.items()}
        paired = {"female": 0, "male": 0}
        for _img, gender in todo:
            if insp_left.get(gender):
                insp_left[gender].pop()
                paired[gender] += 1
        print(f"[DRY] 计划跑 {len(todo)} 个角色。")
        for g in ("female", "male"):
            n_no = sum(1 for _i, gg in todo if gg == g) - paired[g]
            print(f"  {g}: {paired[g]} 个会拼灵感，{max(n_no,0)} 个灵感已用尽不拼")
        for img, gender in todo[:3]:
            samp = insp_avail[gender][0] if insp_avail.get(gender) else "(无)"
            print(f"  样例: {gender} {Path(img).name}  灵感←{samp}")
        return 0

    conc = max(1, args.concurrency)
    counters = {"ok": 0, "err": 0}

    def _take_inspiration(gender: str) -> str | None:
        """线程安全地无放回领一条同性别灵感；用尽返回 None（不拼）。"""
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
        # 灵感作为创作补充要求拼入人设 prompt；用尽则留空
        user_hint = (
            f"参考情境灵感（作为人设的关系张力与开场氛围来源，自然融入即可，不要照抄）：{insp}"
            if insp else ""
        )
        tag = f"[{idx}/{total}] {gender} {Path(img).name}" + (
            f"  灵感←{insp}" if insp else "  (无灵感)")
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
            if insp and not persona_built:  # 人设阶段就失败：灵感退回池，供后续图复用
                _return_inspiration(gender, insp)
            print(f"      ✗ 失败[{idx}]: {e}", flush=True)

    jobs = [(i, img, gender) for i, (img, gender) in enumerate(todo, 1)]
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_run_group, jobs))

    with _STATE_LOCK:
        done_n = len(state["done_person"])
    print(f"\n完成: 成功 {counters['ok']} 个, 失败 {counters['err']} 个。累计完成 {done_n} 个角色。")
    return 0 if counters["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
