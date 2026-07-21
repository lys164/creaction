#!/usr/bin/env python3
"""線上 mengnv 愛豆真名 -> 化名批次替換。

流程：
  1. 從最近一次備份目錄讀取每個角色的完整詳情（離線，安全）。
  2. 按 RENAME[char_id] 的 (old,new) 規則對整份 persona JSON 文字替換。
  3. --dry-run: 只列印命中次數/上下文 + JSON 合法性校驗，不碰線上。
  4. --apply : 透過 PUT /api/persona 寫回線上（只發 persona 欄位）。

化名原則：音近/形近但不同，組內跨語言保持一致。
只處理識別為“真人愛豆真名”的組；純原創名不動。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
ROOT = os.path.join(os.path.dirname(__file__), "..", "data")

# char_id -> [(舊串, 新串), ...]（長串排前，避免子串先被替換）
RENAME = {
    # --- BTS 田柾國 Jungkook (組15) ---
    "char_1783592117_0b9f02": [("Jungkook", "Jungho")],
    "char_1783592234_3bebcc": [("田柾國", "田正宇")],
    # --- BTS 田柾國 Jungkook (組66) ---
    "char_1783662227_ca5f8e": [("田柾國", "田正宇")],
    "char_1783662304_51ed36": [("ジョングク", "ジョンホ")],
    "char_1783662314_d818ef": [("Jungkook", "Jungho")],
    "char_1783662391_674ba6": [("권태하", "권태호")],
    # --- BTS 金碩珍 Jin 本名 (組22) ---
    "char_1783597142_10b3d3": [("김석진", "김서진")],
    # --- IVE 張元英 Wonyoung (組44) ---
    "char_1783605660_e28868": [("ウォニョン", "ウォナ")],
    "char_1783605671_0ca6f0": [("장원영", "장원아")],
    "char_1783605800_74fd66": [("張元英", "張媛映")],
    # --- IVE Wonyoung (組54) ---
    "char_1783608163_37dc40": [("Wonyoung", "Wonya")],
    # --- SNH48 鞠婧禕 (組45) ---
    "char_1783605981_261636": [("鞠婧禕", "鞠靜怡")],
    "char_1783605903_a739ee": [("鞠", "毬")],  # 單字，dry-run 需核對上下文
    # --- Red Velvet Seulgi 禹澀琪 (組46) ---
    "char_1783606059_06153a": [("Seulgi", "Seulki")],
    "char_1783606092_b1c8a2": [("禹澀琪", "禹瑟綺")],
    "char_1783606073_948e69": [("ウ・スルギ", "ウ・スルキ")],
    # --- NCT Shotaro (組16) ---
    "char_1783592443_77cb52": [("Shotaro", "Shotaru")],
    # --- NCT Ten (組3) ---
    "char_1783583604_28ada5": [("Ten", "Tenn")],
    # --- aespa 系 Aeri (組50) ---
    "char_1783607213_1b0d28": [("Aeri", "Aera")],
    "char_1783607212_09799f": [("애리", "애라")],
    # --- Aeri (組56) ---
    "char_1783608468_4a4f84": [("アエリ", "アエラ")],
    # --- 內永枝利 (組51) ---
    "char_1783607409_af3836": [("內永枝利", "內永枝裡")],
    # --- Sion 系 (組2/4/23/37)：與之前本地一致 Sion->Shion / 시온->시언 ---
    "char_1783583437_d9f0fb": [("Sion Wu", "Shion Wu"), ("Sion", "Shion")],
    "char_1783583452_5c065a": [("오시온", "오시언")],
    "char_1783583640_5135f5": [("Sion", "Shion")],
    "char_1783597698_665eeb": [("오세온", "오세언")],
    "char_1783603584_b56ddc": [("Sion Oh", "Shion Oh"), ("Sion", "Shion")],
}


def _latest_backup() -> str:
    dirs = sorted(glob.glob(os.path.join(ROOT, "_mengnv_online_backup_*")))
    if not dirs:
        sys.exit("找不到備份目錄，請先跑 mengnv_backup_and_map.py")
    return dirs[-1]


def _ctx(raw: str, token: str, n: int = 3):
    out = []
    for m in list(re.finditer(re.escape(token), raw))[:n]:
        s = max(0, m.start() - 20)
        e = min(len(raw), m.end() + 20)
        out.append(raw[s:e].replace("\n", " "))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="寫回線上（否則只 dry-run）")
    args = ap.parse_args()
    bak = _latest_backup()
    print(f"備份目錄: {bak}\n")

    plan = []  # (char_id, new_persona_dict)
    for cid, rules in RENAME.items():
        path = os.path.join(bak, f"{cid}.json")
        if not os.path.exists(path):
            print(f"[skip] 無備份: {cid}")
            continue
        d = json.load(open(path, encoding="utf-8"))
        persona_text = json.dumps(d.get("persona", {}), ensure_ascii=False)
        new_text = persona_text
        report = []
        for old, new in rules:
            cnt = new_text.count(old)
            report.append((old, new, cnt, _ctx(new_text, old)))
            new_text = new_text.replace(old, new)
        try:
            new_persona = json.loads(new_text)
        except Exception as e:
            print(f"[ERROR] {cid} 替換後 JSON 非法: {e}")
            continue
        name_before = d.get("persona", {}).get("name")
        name_after = new_persona.get("name")
        print(f"=== {cid}  name: {name_before!r} -> {name_after!r} ===")
        for old, new, cnt, ctx in report:
            print(f"    {old!r}->{new!r}  x{cnt}")
            for c in ctx:
                print(f"        …{c}…")
        plan.append((cid, new_persona))

    print(f"\n共 {len(plan)} 個角色待改。")
    if not args.apply:
        print("（dry-run，未寫回線上。加 --apply 才會寫。）")
        return

    ok = err = 0
    for cid, persona in plan:
        try:
            r = requests.put(f"{BASE}/api/persona",
                             json={"char_id": cid, "persona": persona},
                             timeout=60)
            r.raise_for_status()
            ok += 1
            print(f"  ✓ {cid}")
        except Exception as e:
            err += 1
            print(f"  ✗ {cid}: {e}")
        time.sleep(0.3)
    print(f"\n完成: 成功 {ok}, 失敗 {err}")


if __name__ == "__main__":
    main()
