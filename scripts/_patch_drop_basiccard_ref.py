# -*- coding: utf-8 -*-
"""删除封面规范里对「基础卡」的外部指代（模型无从得知基础卡是什么），
只保留自包含的硬数值规格。逐语言精确替换，幂等。"""
import json
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "app" / "data" / "landing_prompts.json"

REPL = {
    "zh-CN": ("竖构图人物封面，严格对齐基础卡规范——封面图容器固定 aspect-ratio:4/4.6",
              "竖构图人物封面。封面图容器固定 aspect-ratio:4/4.6"),
    "zh-TW": ("直構圖人物封面，嚴格對齊基礎卡規範——封面圖容器固定 aspect-ratio:4/4.6",
              "直構圖人物封面。封面圖容器固定 aspect-ratio:4/4.6"),
    "ko": ("세로 구도 인물 커버, 기본 카드 규범에 엄격히 맞춤——커버 이미지 컨테이너는 aspect-ratio:4/4.6",
           "세로 구도 인물 커버. 커버 이미지 컨테이너는 aspect-ratio:4/4.6"),
    "en": ("a vertical-composition character cover, strictly matching the basic card spec—the cover image container is fixed at aspect-ratio:4/4.6",
           "a vertical-composition character cover. The cover image container is fixed at aspect-ratio:4/4.6"),
    "ja": ("縦構図の人物カバー。ベーシックカード規範に厳密に合わせる——カバー画像コンテナは aspect-ratio:4/4.6",
           "縦構図の人物カバー。カバー画像コンテナは aspect-ratio:4/4.6"),
}


def main() -> int:
    d = json.loads(P.read_text(encoding="utf-8"))
    packs = d.get("PROMPT_PACKS") or {}
    changed = []
    for lang, (old, new) in REPL.items():
        pack = packs.get(lang)
        if not pack:
            continue
        sp = pack.get("SP_TEMPLATE", "")
        if old in sp:
            pack["SP_TEMPLATE"] = sp.replace(old, new, 1)
            changed.append(lang)
        elif new in sp:
            print(f"[skip] {lang} 已处理")
        else:
            print(f"[WARN] {lang} 未命中旧句")
    # 顶层(与 zh-CN 同源)
    old, new = REPL["zh-CN"]
    if old in d.get("SP_TEMPLATE", ""):
        d["SP_TEMPLATE"] = d["SP_TEMPLATE"].replace(old, new, 1)
        changed.append("(top)")

    if not changed:
        print("无改动"); return 0
    P.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已更新:", ", ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
