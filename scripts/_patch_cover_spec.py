# -*- coding: utf-8 -*-
"""把 default 落地頁各語言 SP_TEMPLATE 裡「頭圖 hero」那句松規格，
替換成對齊基礎卡(C-01)的寫死封面規範：
  - 封面容器 aspect-ratio:4/4.6（固定）
  - 容器 border-radius:16px；外層卡片 border-radius:26px
  - object-fit:cover；hero 區四周留白內嵌，不通欄貼邊
  - 仍注入 class="oc-cover"，src 留空（無封面用 CSS 漸變/紋理佔位）

冪等：命中新規格標記(MARK)則跳過。逐 pack 精確替換舊句。
"""
import json
import re
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "app" / "data" / "landing_prompts.json"
MARK = "aspect-ratio:4/4.6"  # 新規格標記

OLD_RE = {
    "zh-CN": r"- \*\*頭圖 hero（在最頂）\*\*：豎構圖人物封面（約 4:4\.5~4:4\.6 比例、圓角、object-fit:cover）。[^\n]*",
    "zh-TW": r"- \*\*頭圖 hero（在最頂）\*\*：直構圖人物封面（約 4:4\.5~4:4\.6 比例、圓角、object-fit:cover）。[^\n]*",
    "ko": r"- \*\*헤더 이미지 hero\(맨 위\)\*\*: 세로 구도 인물 커버\(약 4:4\.5~4:4\.6 비율, 라운드, object-fit:cover\)\. [^\n]*",
    "en": r"- \*\*Hero image \(at the very top\)\*\*: a vertical-composition character cover \(about 4:4\.5~4:4\.6 ratio, rounded corners, object-fit:cover\)\.[^\n]*",
    "ja": r"- \*\*ヘッダー畫像 hero（一番上）\*\*：縦構図の人物カバー（約 4:4\.5~4:4\.6 比率、角丸、object-fit:cover）。[^\n]*",
}

NEW = {
    "zh-CN": (
        "- **頭圖 hero（在最頂）**：豎構圖人物封面，嚴格對齊基礎卡規範——"
        "封面圖容器固定 aspect-ratio:4/4.6、border-radius:16px、object-fit:cover；"
        "hero 區四周留白（圖片內嵌在卡片裡，不通欄貼邊、不做整屏大圖），外層卡片 border-radius:26px。"
        "渲染器會把封面注入到 class=\"oc-cover\" 的元素中，你只需留好槽位、src 留空"
        "（無封面則用 CSS 漸變/紋理做抽象佔位，同樣保持 4/4.6 比例與 16px 圓角）。"
    ),
    "zh-TW": (
        "- **頭圖 hero（在最頂）**：直構圖人物封面，嚴格對齊基礎卡規範——"
        "封面圖容器固定 aspect-ratio:4/4.6、border-radius:16px、object-fit:cover；"
        "hero 區四周留白（圖片內嵌在卡片裡，不通欄貼邊、不做整屏大圖），外層卡片 border-radius:26px。"
        "算繪器會把封面注入到 class=\"oc-cover\" 的元素中，你只需留好槽位、src 留空"
        "（無封面則用 CSS 漸層/紋理做抽象佔位，同樣保持 4/4.6 比例與 16px 圓角）。"
    ),
    "ko": (
        "- **헤더 이미지 hero(맨 위)**: 세로 구도 인물 커버, 기본 카드 규범에 엄격히 맞춤——"
        "커버 이미지 컨테이너는 aspect-ratio:4/4.6, border-radius:16px, object-fit:cover 로 고정; "
        "hero 영역은 사방 여백을 두고(이미지를 카드 안에 내장, 화면 꽉 채우는 통짜 이미지 금지), 바깥 카드 border-radius:26px. "
        "렌더러가 커버를 class=\"oc-cover\" 요소에 주입하므로 슬롯만 남기고 src는 비워 둘 것"
        "(커버가 없으면 CSS 그라디언트/텍스처로 추상 플레이스홀더, 마찬가지로 4/4.6 비율과 16px 라운드 유지)."
    ),
    "en": (
        "- **Hero image (at the very top)**: a vertical-composition character cover, strictly matching the basic card spec—"
        "the cover image container is fixed at aspect-ratio:4/4.6, border-radius:16px, object-fit:cover; "
        "the hero area has padding on all sides (the image is embedded inside the card, never full-bleed edge-to-edge or a full-screen image), "
        "and the outer card is border-radius:26px. "
        "The renderer injects the cover into the element with class=\"oc-cover\", so leave the slot with an empty src "
        "(if there is no cover, use a CSS gradient/texture placeholder, still keeping the 4/4.6 ratio and 16px corners)."
    ),
    "ja": (
        "- **ヘッダー畫像 hero（一番上）**：縦構図の人物カバー。ベーシックカード規範に厳密に合わせる——"
        "カバー畫像コンテナは aspect-ratio:4/4.6、border-radius:16px、object-fit:cover に固定; "
        "hero 領域は四方に餘白を取り（畫像はカード內に埋め込み、全幅ベタ塗り・全畫面大畫像は禁止）、外側カードは border-radius:26px。"
        "レンダラーがカバーを class=\"oc-cover\" の要素に注入するので、スロットだけ殘して src は空にする"
        "（カバーがなければ CSS グラデーション/テクスチャで抽象プレースホルダー、同様に 4/4.6 比率と 16px 角丸を保つ）。"
    ),
}


def _patch(sp: str, lang: str):
    if MARK in sp:
        return sp, False
    old = OLD_RE[lang]
    if not re.search(old, sp):
        return sp, None
    return re.sub(old, lambda m: NEW[lang], sp, count=1), True


def main() -> int:
    d = json.loads(P.read_text(encoding="utf-8"))
    packs = d.get("PROMPT_PACKS") or {}
    changed = []
    for lang in NEW:
        pack = packs.get(lang)
        if not pack:
            print(f"[skip] {lang} 無 pack"); continue
        sp2, ok = _patch(pack.get("SP_TEMPLATE", ""), lang)
        if ok is True:
            pack["SP_TEMPLATE"] = sp2; changed.append(lang)
        elif ok is None:
            print(f"[WARN] {lang} 錨點未命中，跳過")
        else:
            print(f"[skip] {lang} 已含新規格")
    top, ok = _patch(d.get("SP_TEMPLATE", ""), "zh-CN")
    if ok is True:
        d["SP_TEMPLATE"] = top; changed.append("(top)")
    if not changed:
        print("無改動"); return 0
    P.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已更新:", ", ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
