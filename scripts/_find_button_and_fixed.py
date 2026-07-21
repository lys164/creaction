# -*- coding: utf-8 -*-
"""掃描 heermeng+mengnv 線上落地頁，找出兩類需重跑的頁面：
  A. 帶聊天/回覆/發訊息 按鈕或可點選 CTA（button/a/含 btn·cta·action class 且文案含召喚動詞）
  B. 帶固定浮層元件（position:fixed / position:sticky 的可見塊，如底部懸浮訊息條、懸浮 CTA）

輸出並集 -> data/rerun_targets_btn_fixed.json ({"all":[...], "button":[...], "fixed":[...]})
"""
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
OUT = Path(__file__).resolve().parent.parent / "data" / "rerun_targets_btn_fixed.json"

CTA_KW = re.compile(
    r"聊聊|聊天|回覆|回覆|發訊息|發訊息|發條訊息|傳訊息|傳訊息|私信|說句話|說句話|"
    r"去對話|去對話|和\s*TA|跟\s*TA|開始聊|開始聊|立即回覆|立即回覆|馬上聊|馬上聊|"
    r"現在回覆|現在回復|解鎖|解鎖|開啟\s*Popop|開啟\s*Popop|"
    r"답장|대화|메시지\s*보내|채팅|지금\s*답장|열어|"
    r"返信|話しに|メッセージ|チャット|開いて|"
    r"reply|message|chat|talk\s*to|say\s*hi|open\s*popop|start\s*chat|unlock",
    re.I,
)


def _visible(frag: str) -> str:
    return re.sub(r"<[^>]+>", "", frag).strip()


def _has_button(html: str) -> bool:
    for m in re.finditer(r"<(button|a)\b[^>]*>(.*?)</\1>", html, re.S | re.I):
        if CTA_KW.search(_visible(m.group(2))):
            return True
    for m in re.finditer(
        r'<(\w+)\b[^>]*class="[^"]*\b(?:btn|cta|action-btn|action)\b[^"]*"[^>]*>(.*?)</\1>',
        html, re.S | re.I,
    ):
        if CTA_KW.search(_visible(m.group(2))):
            return True
    return False


def _has_fixed(html: str) -> bool:
    """頁面裡存在 position:fixed 或 sticky 的定位（懸浮/吸附元件）。

    落地頁設計規範是純滾動閱讀，不應有固定浮層。sticky 頂欄也算（截圖裡那種
    浮層預告條本質是脫離檔案流的固定塊）。忽略 backdrop/偽元素等噪聲：只要
    出現 position:fixed / position:sticky 即判定。"""
    return bool(re.search(r'position\s*:\s*(fixed|sticky)', html, re.I))


def scan(cid: str):
    try:
        d = requests.get(f"{BASE}/api/landing/{cid}", timeout=60).json()
    except Exception:  # noqa: BLE001
        return (cid, False, False)
    html = d.get("html") or ""
    if not html:
        return (cid, False, False)
    return (cid, _has_button(html), _has_fixed(html))


def main() -> int:
    chars = requests.get(f"{BASE}/api/characters", timeout=120).json()
    result = {"all": [], "button": [], "fixed": [], "by_source": {}}
    for src in ("heermeng", "mengnv"):
        ids = [c["char_id"] for c in chars
               if (c.get("source") or "") == src
               and c.get("has_identity") and c.get("cover_url")]
        with ThreadPoolExecutor(max_workers=16) as ex:
            rows = list(ex.map(scan, ids))
        btn = [cid for cid, b, f in rows if b]
        fixed = [cid for cid, b, f in rows if f]
        both = [cid for cid, b, f in rows if b or f]
        print(f"\n==== {src}: 掃描 {len(ids)} → 按鈕 {len(btn)}，固定浮層 {len(fixed)}，並集 {len(both)} ====")
        result["button"] += btn
        result["fixed"] += fixed
        result["by_source"][src] = {"button": len(btn), "fixed": len(fixed), "union": len(both)}
        for cid in both:
            result["all"].append(cid)
    # 去重保序
    seen = set()
    result["all"] = [x for x in result["all"] if not (x in seen or seen.add(x))]
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n並集共 {len(result['all'])} 個待重跑，已寫 {OUT}")
    print("分解:", result["by_source"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
