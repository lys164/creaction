#!/usr/bin/env python3
"""按對映表批次替換 persona 裡的愛豆真名 -> 化名。

用法:
    python3 scripts/rename_persona_names.py            # 應用改名(會先備份)
    python3 scripts/rename_persona_names.py --dry-run  # 只預覽,不寫檔案

對映表 RENAME_MAP:
    key   = persona 檔名(不含 .json)
    value = [(舊串, 新串), ...]  會對整份 JSON 文字按順序做替換。
            注意:同一檔案內若有子串包含關係,請把更長的串排在前面,
            避免先替換短串導致長串失配。
"""

import argparse
import json
import os
import shutil
import time

PERSONA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "personas")

# 方案A:最接近原名的化名(聽得出來像,但不完全一樣)
# 原型愛豆核心名 Sion / 시온 -> Shion / 시언
RENAME_MAP = {
    # 中文版: 吳是溫 -> 吳時溫 (是->時, 同音 shi)
    "char_1783589646_2810ac": [
        ("吳是溫", "吳時溫"),
        ("Sion", "Shion"),
        ("sion", "shion"),
    ],
    # 英文版: Sion Oh -> Shion Oh; user_hint 裡還有中文原文名
    "char_1783589533_93a996": [
        ("吳是溫", "吳時溫"),
        ("Sion", "Shion"),
        ("sion", "shion"),
    ],
    # 日文版: 瀬名 涼 -> 瀬名 諒; user_hint 裡還有中文原文名
    "char_1783589534_fbeef6": [
        ("吳是溫", "吳時溫"),
        ("涼", "諒"),
        ("Sion", "Shion"),
        ("sion", "shion"),
    ],
    # 韓文版: 오시온 / 시온 -> 오시언 / 시언; user_hint 裡還有中文原文名
    "char_1783589551_1a6b20": [
        ("吳是溫", "吳時溫"),
        ("시온", "시언"),
        ("Sion", "Shion"),
        ("sion", "shion"),
    ],
}


def process(dry_run: bool):
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(PERSONA_DIR, f"_backup_rename_{ts}")
    changed = 0

    for stem, rules in RENAME_MAP.items():
        path = os.path.join(PERSONA_DIR, f"{stem}.json")
        if not os.path.exists(path):
            print(f"[skip] 檔案不存在: {path}")
            continue

        raw = open(path, encoding="utf-8").read()
        # 校驗是合法 JSON(替換後仍需合法)
        try:
            json.loads(raw)
        except Exception as e:
            print(f"[warn] {stem} 不是合法 JSON,跳過: {e}")
            continue

        new = raw
        counts = []
        for old, repl in rules:
            n = new.count(old)
            new = new.replace(old, repl)
            counts.append((old, repl, n))

        if new == raw:
            print(f"[no-op] {stem}: 無匹配")
            continue

        # 替換後仍須是合法 JSON
        try:
            json.loads(new)
        except Exception as e:
            print(f"[error] {stem} 替換後 JSON 非法,已跳過: {e}")
            continue

        summary = ", ".join(f"{o!r}->{r!r} x{n}" for o, r, n in counts if n)
        print(f"[{'dry' if dry_run else 'edit'}] {stem}: {summary}")

        if not dry_run:
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copy2(path, os.path.join(backup_dir, f"{stem}.json"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
        changed += 1

    if not dry_run and changed:
        print(f"\n備份已存到: {backup_dir}")
    print(f"\n完成: {changed} 個檔案{'將' if dry_run else '已'}改動。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只預覽不寫檔案")
    args = ap.parse_args()
    process(args.dry_run)
