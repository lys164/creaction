# -*- coding: utf-8 -*-
"""扫描 heermeng+mengnv 线上落地页，找出两类需重跑的页面：
  A. 带聊天/回复/发消息 按钮或可点击 CTA（button/a/含 btn·cta·action class 且文案含召唤动词）
  B. 带固定浮层组件（position:fixed / position:sticky 的可见块，如底部悬浮消息条、悬浮 CTA）

输出并集 -> data/rerun_targets_btn_fixed.json ({"all":[...], "button":[...], "fixed":[...]})
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
    r"聊聊|聊天|回复|回覆|发消息|發消息|发条消息|传讯息|傳訊息|私信|说句话|說句話|"
    r"去对话|去對話|和\s*TA|跟\s*TA|开始聊|開始聊|立即回复|立即回覆|马上聊|馬上聊|"
    r"现在回复|現在回復|解锁|解鎖|打开\s*Popop|開啟\s*Popop|"
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
    """页面里存在 position:fixed 或 sticky 的定位（悬浮/吸附组件）。

    落地页设计规范是纯滚动阅读，不应有固定浮层。sticky 顶栏也算（截图里那种
    浮层预告条本质是脱离文档流的固定块）。忽略 backdrop/伪元素等噪声：只要
    出现 position:fixed / position:sticky 即判定。"""
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
        print(f"\n==== {src}: 扫描 {len(ids)} → 按钮 {len(btn)}，固定浮层 {len(fixed)}，并集 {len(both)} ====")
        result["button"] += btn
        result["fixed"] += fixed
        result["by_source"][src] = {"button": len(btn), "fixed": len(fixed), "union": len(both)}
        for cid in both:
            result["all"].append(cid)
    # 去重保序
    seen = set()
    result["all"] = [x for x in result["all"] if not (x in seen or seen.add(x))]
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n并集共 {len(result['all'])} 个待重跑，已写 {OUT}")
    print("分解:", result["by_source"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
