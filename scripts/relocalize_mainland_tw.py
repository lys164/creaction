# -*- coding: utf-8 -*-
"""Re-localize persona fields & IG post contents that still carry Mainland-Chinese
vocabulary into natural Taiwan Traditional Chinese (sentence-level, via LLM).

Only records that actually contain a Mainland term are sent (targeted, not a full
re-translation). Structure/keys/ids untouched; char name protected & restored.

- persona: only string leaf fields containing a Mainland term are localized, in one
  batched JSON-array call per character; result written back via PUT /api/persona.
- posts: only post contents containing a Mainland term are localized (batched array),
  written back per-post via PUT /api/ig_posts/{cid}/{pid}.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_envp = ROOT / ".env"
if _envp.exists():
    for _raw in _envp.read_text(encoding="utf-8").splitlines():
        _l = _raw.strip()
        if not _l or _l.startswith("#") or "=" not in _l:
            continue
        _k, _v = _l.split("=", 1)
        _k = _k.strip(); _v = _v.strip()
        if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ("'", '"'):
            _v = _v[1:-1]
        os.environ.setdefault(_k, _v)

from app import api_client, config  # noqa: E402

BASE = os.environ.get("POPOP_BASE_URL",
                      "http://popop-pipeline.internal-app.imaginewithu.com")
NAME_TOKEN = "\u27e6NAME\u27e7"
USER_TOKEN = "\u27e6USER\u27e7"

# Mainland terms that trigger re-localization (detection only; LLM does the rewrite).
MAINLAND = [
    "視頻", "質量", "軟件", "硬件", "網絡", "信息", "屏幕", "內存", "默認", "激活",
    "鼠標", "打印", "文件夾", "博客", "在線", "賬號", "賬戶", "登錄", "點贊",
    "服務器", "數據", "代碼", "菜單", "拷貝", "創可貼", "方便面", "冰激凌",
    "出租車", "公交車", "人工智能", "初創", "靠譜", "朋友圈", "視頻號", "水平",
]
SKIP_KEYS = {"name", "voice", "visibility"}
SKIP_SUFFIXES = ("_id", "_url", "_path", "url", "local_path")


def has_mainland(s: str) -> bool:
    return isinstance(s, str) and any(t in s for t in MAINLAND)


def system_prompt() -> str:
    return (
        "你是臺灣繁體中文在地化編輯。輸入是一個 JSON 陣列，每個元素是一段角色文案。\n"
        "把每段改寫成自然、道地的臺灣繁體中文，重點是把大陸用語換成臺灣慣用說法，並讓語氣通順像真人：\n"
        "視頻→影片、質量→品質、屏幕→螢幕、信息→資訊、在線→線上、數據→資料、默認→預設、\n"
        "菜單→選單、靠譜→可靠/穩、水平→水準或程度（看語境）、軟件→軟體、網絡→網路、\n"
        "點贊→按讚、創可貼→OK繃、人工智能→人工智慧、初創→新創、博客→部落格、朋友圈→動態、\n"
        "代碼→程式碼、拷貝→複製、出租車→計程車、方便面→泡麵。\n"
        "不得新增/刪除資訊，不改人設、年齡、關係、職業、物種、核心秘密；古風/奇幻語境只調語氣。\n"
        "硬性規則：\n"
        f"1. 只輸出合法 JSON 陣列，長度與輸入完全一致、一一對應，無 Markdown 無解釋。\n"
        f"2. 保護 token 原樣保留不得改動：{NAME_TOKEN}（角色名）、{USER_TOKEN}（使用者佔位）。\n"
        "3. 保留英文品牌/作品名/emoji/數字；純英文段原樣返回。\n"
        "4. 若某段本來就沒有大陸用語，仍可潤飾使其更自然，但務必保持原意。"
    )


def localize(strings: list[str], char_name: str, model: str,
             temperature: float, retries: int) -> list[str]:
    masked = []
    for s in strings:
        m = s
        if char_name:
            m = m.replace(char_name, NAME_TOKEN)
        m = m.replace("{user}", USER_TOKEN)
        masked.append(m)
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": json.dumps(masked, ensure_ascii=False)},
    ]
    last = None
    for attempt in range(retries):
        try:
            raw = api_client.chat(messages, model=model, temperature=temperature,
                                  max_retries=3, timeout=240, max_tokens=16000)
            s = raw.strip()
            if s.startswith("```"):
                s = s.strip("`")
                if s[:4].lower() == "json":
                    s = s[4:]
            i = s.find("["); j = s.rfind("]")
            if i >= 0 and j > i:
                s = s[i:j + 1]
            arr = json.loads(s)
            if not isinstance(arr, list) or len(arr) != len(strings):
                raise ValueError(f"length mismatch {len(arr)} != {len(strings)}")
            out = []
            for v in arr:
                v = str(v)
                if char_name:
                    v = v.replace(NAME_TOKEN, char_name)
                out.append(v.replace(USER_TOKEN, "{user}"))
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(4 * (attempt + 1), 20))
    raise RuntimeError(f"localize failed after {retries}: {last}")


def _collect(obj, path, hits, key=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SKIP_KEYS or any(k.endswith(s) for s in SKIP_SUFFIXES):
                continue
            _collect(v, path + [k], hits, k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _collect(v, path + [i], hits, key)
    elif isinstance(obj, str) and has_mainland(obj):
        hits.append((list(path), obj))


def _set_by_path(obj, path, value):
    cur = obj
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value


def process_persona(cid, name, model, temperature, retries, backup, dry):
    persona = requests.get(f"{BASE}/api/character/{cid}", timeout=60).json().get("persona") or {}
    hits = []
    _collect(persona, [], hits)
    if not hits:
        return {"cid": cid, "kind": "persona", "status": "clean"}
    strings = [h[1] for h in hits]
    localized = localize(strings, name, model, temperature, retries)
    new_persona = copy.deepcopy(persona)
    for (path, _old), new in zip(hits, localized):
        _set_by_path(new_persona, path, new)
    if isinstance(persona.get("name"), str):
        new_persona["name"] = persona["name"]
    if not dry:
        (backup / f"persona_{cid}.json").write_text(
            json.dumps(persona, ensure_ascii=False, indent=2), encoding="utf-8")
        r = requests.put(f"{BASE}/api/persona",
                         json={"char_id": cid, "persona": new_persona}, timeout=90)
        if r.status_code != 200:
            return {"cid": cid, "kind": "persona", "status": f"put_{r.status_code}"}
    return {"cid": cid, "kind": "persona", "status": "ok", "fields": len(hits)}


def process_posts(cid, name, model, temperature, retries, backup, dry):
    ig = requests.get(f"{BASE}/api/ig_posts/{cid}/latest", timeout=60).json()
    posts = ig.get("posts") if isinstance(ig, dict) else None
    if not isinstance(posts, list):
        return {"cid": cid, "kind": "posts", "status": "no_posts"}
    targets = [(p.get("post_id"), p.get("content")) for p in posts
               if isinstance(p, dict) and isinstance(p.get("content"), str)
               and has_mainland(p.get("content"))]
    if not targets:
        return {"cid": cid, "kind": "posts", "status": "clean"}
    strings = [t[1] for t in targets]
    localized = localize(strings, name, model, temperature, retries)
    if not dry:
        (backup / f"posts_{cid}.json").write_text(
            json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")
        for (pid, _old), new in zip(targets, localized):
            for attempt in range(3):
                r = requests.put(f"{BASE}/api/ig_posts/{cid}/{pid}",
                                 json={"content": new}, timeout=90)
                if r.status_code == 200:
                    break
                time.sleep(2)
            if r.status_code != 200:
                return {"cid": cid, "kind": "posts", "status": f"put_{r.status_code}@{pid}"}
    return {"cid": cid, "kind": "posts", "status": "ok", "posts": len(targets)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", default="/tmp/export_ids.txt")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--model", default=config.LLM_MODEL)
    ap.add_argument("--temperature", type=float, default=0.4)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--only", choices=["persona", "posts", "both"], default="both")
    args = ap.parse_args()
    dry = not args.apply

    ids = [l.strip() for l in Path(args.ids_file).read_text("utf-8").splitlines() if l.strip()]
    if args.limit:
        ids = ids[:args.limit]
    chars = {c["char_id"]: c.get("name", "")
             for c in requests.get(f"{BASE}/api/characters", timeout=60).json()}

    backup = config.DATA_DIR / f"_backup_relocalize_{time.strftime('%Y%m%d_%H%M%S')}"
    if not dry:
        backup.mkdir(parents=True, exist_ok=True)
    print(f"relocalize: {len(ids)} chars | only={args.only} | dry={dry} | model={args.model}",
          flush=True)

    jobs = []
    for cid in ids:
        nm = chars.get(cid, "")
        if args.only in ("persona", "both"):
            jobs.append(("persona", cid, nm))
        if args.only in ("posts", "both"):
            jobs.append(("posts", cid, nm))

    results = []

    def run(job):
        kind, cid, nm = job
        fn = process_persona if kind == "persona" else process_posts
        try:
            return fn(cid, nm, args.model, args.temperature, args.retries, backup, dry)
        except Exception as e:  # noqa: BLE001
            return {"cid": cid, "kind": kind, "status": f"err:{str(e)[:80]}"}

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as ex:
        futs = [ex.submit(run, j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            results.append(res)
            if i % 30 == 0 or res["status"] not in ("ok", "clean", "no_posts"):
                print(f"  {i}/{len(jobs)} {res['kind']} {res['cid']} -> {res['status']}",
                      flush=True)

    import collections
    st = collections.Counter(
        f"{r['kind']}:{r['status']}" if r["status"] in ("ok", "clean", "no_posts")
        else f"{r['kind']}:{r['status'].split(':')[0].split('@')[0]}" for r in results)
    print("=== done ===", dict(st))
    bad = [r for r in results if r["status"] not in ("ok", "clean", "no_posts")]
    if bad:
        print("problems:", bad[:20])
    (config.DATA_DIR / "relocalize_summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
