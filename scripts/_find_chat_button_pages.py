# -*- coding: utf-8 -*-
"""扫描 heermeng+mengnv 线上落地页，找出仍带「聊天/回复/发消息」按钮或可点击 CTA 的角色。

判定为命中(需要重跑)的条件（任一）：
  1. 存在 <button> 标签，且其可见文案含聊天类动词；
  2. 存在 <a> 链接，其可见文案含聊天类动词（call-to-action 链接）；
  3. 存在 class 含 btn/cta/action-btn 的元素，且其可见文案含聊天类动词。
纯静态"消息气泡/预告文字"（无按钮语义、无点击动词）不算命中。

输出：data/chat_button_targets.json = {"heermeng":[...], "mengnv":[...], "all":[...]}
并打印每个命中角色的证据文案，供人工快速核对。
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
OUT = Path(__file__).resolve().parent.parent / "data" / "chat_button_targets.json"

# 聊天类召唤动词/文案（中/繁/日/韩/英）
CTA_KW = re.compile(
    r"聊聊|聊天|回复|回覆|发消息|發消息|发条消息|传讯息|傳訊息|私信|说句话|說句話|"
    r"去对话|去對話|和\s*TA|跟\s*TA|开始聊|開始聊|立即回复|立即回覆|马上聊|馬上聊|"
    r"现在回复|現在回復|解锁|解鎖|打开\s*Popop|開啟\s*Popop|"
    r"답장|대화|메시지\s*보내|채팅|지금\s*답장|열어|"
    r"返信|話しに|メッセージ|チャット|開いて|"
    r"reply|message|chat|talk\s*to|say\s*hi|open\s*popop|start\s*chat|unlock",
    re.I,
)


def _visible(fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", fragment).strip()


def check(cid: str):
    try:
        d = requests.get(f"{BASE}/api/landing/{cid}", timeout=60).json()
    except Exception:  # noqa: BLE001
        return (cid, None, [])
    html = d.get("html") or ""
    if not html:
        return (cid, None, [])
    evidence = []

    # 1+2: button / a 标签，文案含 CTA 词
    for m in re.finditer(r"<(button|a)\b[^>]*>(.*?)</\1>", html, re.S | re.I):
        txt = _visible(m.group(2))
        if txt and CTA_KW.search(txt):
            evidence.append(f"<{m.group(1)}> {txt[:40]}")

    # 3: class 含 btn/cta/action 的任意元素，文案含 CTA 词
    for m in re.finditer(
        r'<(\w+)\b[^>]*class="[^"]*\b(?:btn|cta|action-btn|action)\b[^"]*"[^>]*>(.*?)</\1>',
        html, re.S | re.I,
    ):
        txt = _visible(m.group(2))
        if txt and CTA_KW.search(txt):
            ev = f"[{m.group(1)}.btn] {txt[:40]}"
            if ev not in evidence:
                evidence.append(ev)

    return (cid, bool(evidence), evidence)


def main() -> int:
    chars = requests.get(f"{BASE}/api/characters", timeout=120).json()
    by_src = {"heermeng": [], "mengnv": []}
    for c in chars:
        src = c.get("source") or ""
        if src in by_src:
            by_src[src].append(c["char_id"])

    result = {"heermeng": [], "mengnv": [], "all": []}
    for src, ids in by_src.items():
        with ThreadPoolExecutor(max_workers=16) as ex:
            rows = list(ex.map(check, ids))
        hits = [(cid, ev) for cid, hit, ev in rows if hit]
        print(f"\n==== {src}: 扫描 {len(ids)}，命中(带聊天按钮) {len(hits)} ====")
        for cid, ev in hits:
            print(f"  {cid}")
            for e in ev[:3]:
                print(f"      {e}")
            result[src].append(cid)
    result["all"] = result["heermeng"] + result["mengnv"]
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n合计命中 {len(result['all'])} 个，已写 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
