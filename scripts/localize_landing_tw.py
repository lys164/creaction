# -*- coding: utf-8 -*-
"""LLM-localize landing pages to Taiwan Traditional Chinese (sentence-level).

Strategy (structure-safe):
- Pull each zh character's landing HTML from the live server.
- Parse with BeautifulSoup; collect only text nodes that contain Chinese and are
  NOT inside <script>/<style>. Attributes/URLs/tags are never sent.
- Send the ordered list of strings to the LLM; it returns the same-length list
  rewritten in natural Taiwan Traditional Chinese (vocabulary + phrasing), while
  preserving the protected character name, {user}, emoji, and English brand terms.
- Write nodes back into the SAME DOM (zero structural drift), PUT to the server.

Char name is protected via token and restored verbatim.
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
from bs4 import BeautifulSoup, Comment, NavigableString

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
SKIP_PARENTS = {"script", "style"}


def has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def collect_nodes(soup: BeautifulSoup) -> list[NavigableString]:
    out = []
    for node in soup.find_all(string=True):
        if isinstance(node, Comment):
            continue
        parent = node.parent.name if node.parent else ""
        if parent in SKIP_PARENTS:
            continue
        if has_cjk(str(node)):
            out.append(node)
    return out


def system_prompt() -> str:
    return (
        "你是臺灣繁體中文的行銷文案在地化編輯。輸入是一個 JSON 陣列，每個元素是網頁上的一段可見文字。\n"
        "把每一段改寫成自然、道地、有臺灣社群感的繁體中文：\n"
        "- 用臺灣慣用詞，不要大陸用語（例如 視頻→影片、質量→品質、屏幕→螢幕、信息→資訊、"
        "在線→線上、數據→資料、默認→預設、菜單→選單、靠譜→可靠、水平→水準/程度、"
        "軟件→軟體、網絡→網路、點贊→按讚、創可貼→OK繃）。\n"
        "- 語氣可更利落、有梗、像真人；但不得新增/刪除資訊，不改人設、年齡、關係、職業、物種、核心秘密。\n"
        "- 若某段是古風/奇幻/非現代語境，只把語氣改自然，不要硬塞臺灣地名或品牌。\n"
        "硬性規則：\n"
        f"1. 只輸出合法 JSON 陣列，長度與輸入完全一致，一一對應，不能有 Markdown 或解釋。\n"
        f"2. 保護 token 原樣保留，不得翻譯或改動：{NAME_TOKEN}（角色名）、{USER_TOKEN}（使用者佔位）。\n"
        "3. 保留英文品牌/作品名/emoji/數字/標點結構；不要把純英文段翻成中文。\n"
        "4. 每段若本來沒有中文可原樣返回。"
    )


def localize_strings(strings: list[str], char_name: str, model: str,
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
                v = v.replace(USER_TOKEN, "{user}")
                out.append(v)
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(4 * (attempt + 1), 20))
    raise RuntimeError(f"localize failed after {retries}: {last}")


def process_char(cid: str, name: str, model: str, temperature: float,
                 retries: int, backup: Path, dry: bool) -> dict:
    page = requests.get(f"{BASE}/api/landing/{cid}", timeout=60).json()
    html = page.get("html") or ""
    if not html:
        return {"cid": cid, "status": "no_html"}
    soup = BeautifulSoup(html, "html.parser")
    nodes = collect_nodes(soup)
    if not nodes:
        return {"cid": cid, "status": "no_cjk"}
    originals = [str(n) for n in nodes]
    localized = localize_strings(originals, name, model, temperature, retries)
    for node, new in zip(nodes, localized):
        node.replace_with(NavigableString(new))
    new_html = str(soup)
    if not dry:
        (backup / f"{cid}.html").write_text(html, encoding="utf-8")
        for attempt in range(3):
            r = requests.put(f"{BASE}/api/landing",
                             json={"char_id": cid, "html": new_html}, timeout=90)
            if r.status_code == 200:
                break
            time.sleep(2)
        if r.status_code != 200:
            return {"cid": cid, "status": f"put_{r.status_code}"}
    return {"cid": cid, "status": "ok", "nodes": len(nodes)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", default="/tmp/export_ids.txt")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--model", default=config.LLM_MODEL)
    ap.add_argument("--temperature", type=float, default=0.4)
    ap.add_argument("--retries", type=int, default=3)
    args = ap.parse_args()
    dry = not args.apply

    ids = [l.strip() for l in Path(args.ids_file).read_text("utf-8").splitlines() if l.strip()]
    if args.limit:
        ids = ids[:args.limit]
    chars = {c["char_id"]: c.get("name", "")
             for c in requests.get(f"{BASE}/api/characters", timeout=60).json()}

    backup = config.DATA_DIR / f"_backup_landing_loc_{time.strftime('%Y%m%d_%H%M%S')}"
    if not dry:
        backup.mkdir(parents=True, exist_ok=True)
    print(f"landing localize: {len(ids)} chars | dry={dry} | model={args.model}", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as ex:
        futs = {ex.submit(process_char, cid, chars.get(cid, ""), args.model,
                          args.temperature, args.retries, backup, dry): cid
                for cid in ids}
        for i, fut in enumerate(as_completed(futs), 1):
            cid = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                res = {"cid": cid, "status": f"err:{str(e)[:80]}"}
            results.append(res)
            if i % 20 == 0 or res["status"] != "ok":
                print(f"  {i}/{len(ids)} {cid} -> {res['status']}", flush=True)

    import collections
    st = collections.Counter(r["status"] if r["status"] in ("ok", "no_html", "no_cjk")
                             else r["status"].split(":")[0] for r in results)
    print("=== done ===", dict(st))
    bad = [r for r in results if r["status"] != "ok"]
    if bad:
        print("problems:", bad[:20])
    (config.DATA_DIR / "landing_localize_summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
