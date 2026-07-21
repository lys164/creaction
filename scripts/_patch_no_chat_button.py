# -*- coding: utf-8 -*-
"""在 default 落地頁各語言 SP_TEMPLATE 的「設計要求」段落前，插入一條
硬約束：結尾只做文字預告/期待感，禁止生成任何聊天/回覆/發訊息按鈕或可點選 CTA。

冪等：已插入過(檢測標記 MARK)則跳過。改完列印每個 pack 的命中情況。
所有規則文字用全形引號「」，規避與 Python 字串的半形引號衝突。
"""
import json
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "app" / "data" / "landing_prompts.json"

MARK = "禁止聊天按鈕"  # 冪等檢測標記

RULES = {
    "zh-CN": (
        "# 設計要求：",
        "# 結尾硬約束（禁止聊天按鈕）：\n"
        "結尾只用文字營造「即將收到 TA 訊息」的期待感，"
        "嚴禁生成任何聊天/回覆/發訊息/開啟 Popop 之類的按鈕、連結或可點選 CTA"
        "（不要 <button>、不要 class 含 btn/cta/action 的可點選塊、"
        "不要「立即回覆」「去聊聊」「開啟 Popop 回覆」等文案）。"
        "可以有靜態的訊息氣泡/預告文字，但它不是按鈕、不可點選、不含召喚點選的動詞。\n\n",
    ),
    "zh-TW": (
        "# 設計要求：",
        "# 結尾硬約束（禁止聊天按鈕）：\n"
        "結尾只用文字營造「即將收到 TA 訊息」的期待感，"
        "嚴禁生成任何聊天/回覆/傳訊息/開啟 Popop 之類的按鈕、連結或可點選 CTA"
        "（不要 <button>、不要 class 含 btn/cta/action 的可點選區塊、"
        "不要「立即回覆」「去聊聊」「開啟 Popop 回覆」等文案）。"
        "可以有靜態的訊息氣泡/預告文字，但它不是按鈕、不可點選、不含召喚點選的動詞。\n\n",
    ),
    "ko": (
        "# 디자인 요구:",
        "# 마무리 하드 제약(禁止聊天按鈕 / 채팅 버튼 금지):\n"
        "마무리는 텍스트로만 「곧 TA의 메시지가 올 것 같은」 기대감을 조성하고, "
        "채팅/답장/메시지 보내기/Popop 열기 같은 버튼·링크·클릭 가능한 CTA를 절대 생성하지 말 것"
        "(<button> 금지, class에 btn/cta/action이 포함된 클릭 요소 금지, "
        "「지금 답장」「대화하러 가기」「Popop 열어 답장」 등 문구 금지). "
        "정적인 메시지 말풍선/예고 문구는 가능하되, 버튼이 아니고 클릭 불가하며 클릭을 유도하는 동사를 넣지 않는다.\n\n",
    ),
    "en": (
        "# Design requirements:",
        "# Closing hard constraint (禁止聊天按鈕 / no chat button):\n"
        "The closing may only build anticipation in text (a message from them is coming); "
        "never generate any chat/reply/send-message/open-Popop button, link, or clickable CTA "
        "(no <button>, no clickable block whose class contains btn/cta/action, "
        "no copy like 「Reply now」, 「Open Popop to reply」, 「Start chatting」). "
        "A static message bubble / teaser text is fine, but it must not be a button, "
        "must not be clickable, and must not contain a click-inviting verb.\n\n",
    ),
    "ja": (
        "# デザイン要件：",
        "# 締めのハード制約（禁止聊天按鈕 / チャットボタン禁止）：\n"
        "締めはテキストだけで「もうすぐ TA からメッセージが來る」期待感を作り、"
        "チャット/返信/メッセージ送信/Popop を開く といったボタン・リンク・クリック可能な CTA を一切生成しないこと"
        "（<button> 禁止、class に btn/cta/action を含むクリック要素禁止、"
        "「今すぐ返信」「話しに行く」「Popop を開いて返信」などの文言禁止）。"
        "靜的なメッセージ吹き出し/予告テキストは可だが、ボタンではなく、"
        "クリック不可で、クリックを促す動詞を含めない。\n\n",
    ),
}


def main() -> int:
    d = json.loads(P.read_text(encoding="utf-8"))
    packs = d.get("PROMPT_PACKS") or {}
    changed = []
    for lang, (anchor, rule) in RULES.items():
        pack = packs.get(lang)
        if not pack:
            print(f"[skip] pack {lang} 不存在")
            continue
        sp = pack.get("SP_TEMPLATE", "")
        if MARK in sp or "禁止聊天按鈕" in sp:  # 簡/繁標記都算已打過
            print(f"[skip] {lang} 已含禁令標記")
            continue
        if anchor not in sp:
            print(f"[WARN] {lang} 找不到錨點 {anchor!r}，跳過")
            continue
        pack["SP_TEMPLATE"] = sp.replace(anchor, rule + anchor, 1)
        changed.append(lang)

    top = d.get("SP_TEMPLATE", "")
    if MARK not in top and RULES["zh-CN"][0] in top:
        anchor, rule = RULES["zh-CN"]
        d["SP_TEMPLATE"] = top.replace(anchor, rule + anchor, 1)
        changed.append("(top)")

    if not changed:
        print("無改動(可能已打過補丁)")
        return 0
    P.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已更新:", ", ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
