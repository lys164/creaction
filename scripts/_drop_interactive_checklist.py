# -*- coding: utf-8 -*-
"""從互動卡片版母版 prompt 裡去掉「完成揭示·勾選清單」(A 互動 / todo 清單卡片)。

把敘事互動從「A/B/C 三選一」收斂為「B/C 二選一」，刪淨 A 的描述子節 / HTML /
JS / 死 CSS(.check) / 元件表 check 行 / 賣點對映裡的 A 項，並同步所有引用文案。
每步 assert 命中，避免刪漏或留懸空引用。原子寫回 landing_prompts.json。
"""
import json
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "app" / "data" / "landing_prompts.json"
d = json.loads(P.read_text(encoding="utf-8"))
sp = d["PROMPT_VARIANTS"]["interactive"]["SYSTEM_PROMPT"]
orig_len = len(sp)


def cut(text: str, start_sub: str, end_sub: str, *, inclusive_end: bool = False) -> str:
    a = text.find(start_sub)
    assert a != -1, f"start not found: {start_sub[:40]!r}"
    b = text.find(end_sub, a + len(start_sub))
    assert b != -1, f"end not found: {end_sub[:40]!r}"
    if inclusive_end:
        b += len(end_sub)
    return text[:a] + text[b:]


def repl(text: str, old: str, new: str, n: int = 1) -> str:
    cnt = text.count(old)
    assert cnt == n, f"expected {n} of {old[:40]!r}, found {cnt}"
    return text.replace(old, new)


# 1) 刪 A 描述子節（### A ... 到 ### B 前）
sp = cut(sp, "### A. 完成揭示", "### B. 探索揭示")

# 2) 刪 A 的 HTML 塊（註釋 + 整個 data-interaction="A" 的 .sec，到 B 的 HTML 註釋前）
sp = cut(sp, "    <!-- 【A · 完成揭示】", "<!-- 【B")

# 3) 刪 A 的 JS 塊（指令碼註釋 + <script> 到 B 指令碼註釋前）
sp = cut(sp, "<!-- 【A】完成揭示指令碼 -->", "<!-- 【B")

# 4) 刪死掉的 .check CSS（含前導註釋行）
sp = cut(
    sp,
    "/* 完成揭示 · 勾選清單",
    ".check li.on span{color:var(--muted);text-decoration:line-through;"
    "text-decoration-color:var(--line-strong)}",
    inclusive_end=True,
)
# 清掉該 CSS 塊殘留的多餘空行（塌成一個空行）
sp = repl(sp, ".01em}\n\n\n/* 手機切片 */", ".01em}\n\n/* 手機切片 */")

# 5) 刪元件表裡的 check 清單行
sp = repl(
    sp,
    "| check 清單 | 待辦 / 心願 / 倒計時 | 勾到最後一項，暴露 ta 真正想要的其實不是這些 |\n",
    "",
)

# 6) 賣點對映：去掉 A 互動那一項
sp = repl(
    sp,
    '深夜 memo / 勾選清單解鎖真心話(A 互動) / 語音(voice)',
    '深夜 memo / 語音(voice)',
)

# 7) 所有 A/B/C 三選一 → B/C 二選一 的引用改寫
sp = repl(sp, "A/B/C 互動、可複用元件 HTML", "B/C 互動、可複用元件 HTML")
sp = repl(sp, "敘事互動（A/B/C）", "敘事互動（B/C）")
sp = repl(sp, "A/B/C 三種互動的完整 HTML 與 JS", "B/C 兩種互動的完整 HTML 與 JS")
sp = repl(
    sp,
    "3. **互動取捨**：A/B/C 三個 `data-interaction` 的 `.sec` 裡**只保留一個**，"
    "刪掉另外兩個對應的 `.sec` 與其 `<script>`。",
    "3. **互動取捨**：B/C 兩個 `data-interaction` 的 `.sec` 裡**只保留一個**，"
    "刪掉另一個對應的 `.sec` 與其 `<script>`。",
)
sp = repl(sp, "`<!-- 【A/B/C …】-->`", "`<!-- 【B/C …】-->`")
sp = repl(sp, "從下列三類中，按角色核心", "從下列兩類中，按角色核心")
sp = repl(
    sp,
    "把最深的那個賣點/隱藏面放到 A/B/C 之一的互動揭示終點，**保留那一個 "
    "`data-interaction` 的 `.sec` + 它的 `<script>`，刪掉另兩組**",
    "把最深的那個賣點/隱藏面放到 B/C 之一的互動揭示終點，**保留那一個 "
    "`data-interaction` 的 `.sec` + 它的 `<script>`，刪掉另一組**",
)
sp = repl(
    sp,
    "- [ ] 有且僅有一個敘事互動（另兩組 `.sec` 與其 `<script>` 已刪）",
    "- [ ] 有且僅有一個敘事互動（另一組 `.sec` 與其 `<script>` 已刪）",
)
sp = repl(
    sp,
    "互動（按選中的 A/B/C 保留對應一段）",
    "互動（按選中的 B/C 保留對應一段）",
)
sp = repl(
    sp,
    "    <!-- ========== 有且僅有一個「敘事互動」.sec：從下方三選一，刪掉其餘兩個 ========== -->",
    "    <!-- ========== 有且僅有一個「敘事互動」.sec：從下方二選一，刪掉其餘一個 ========== -->",
)

# 8) 修正 B 塊 HTML 註釋縮排（原 A 塊前的 4 空格隨刪除被吃掉，B 註釋頂了格）
sp = repl(
    sp,
    "========== -->\n\n<!-- 【B · 探索揭示】",
    "========== -->\n\n    <!-- 【B · 探索揭示】",
)

# 終檢：不應再有任何 A 互動 / todo 清單殘留
for token in ("__A_", "完成揭示", "Checklist", "data-tick", ".check",
              "集章", "拆封", "A/B/C", "三選一", "從下列三類", "check 清單"):
    assert token not in sp, f"residual token remains: {token!r}"

d["PROMPT_VARIANTS"]["interactive"]["SYSTEM_PROMPT"] = sp
tmp = P.with_suffix(".json.tmp")
tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
tmp.replace(P)
print(f"OK  SYSTEM_PROMPT: {orig_len} -> {len(sp)}  (-{orig_len - len(sp)} chars)")
