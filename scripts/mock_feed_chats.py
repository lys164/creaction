#!/usr/bin/env python3
"""為 Feed Demo 新增的策展角色批量 mock「60 輪聊天」。

用途：第三方視角帖子 Demo 裡，T2（角色綁定號）把使用者×角色的真實聊天當
「共同記憶帳本」取材。新加進 FEED_CHAR_IDS 的角色沒有聊天記錄，冷啟動只能
鋪最輕的痕跡。本腳本為每個指定角色造一段自然的 60 輪對話並存成本地會話檔，
之後 T2 生成即可引用真實共同記憶。

實現：
- 角色回覆走正式聊天鏈路（chat.build_prompt + api_client.chat + _normalize_items），
  和線上聊天完全同構，產出 items 陣列。
- 使用者一側由「使用者模擬器」LLM 扮演：讀人設與當前對話，產出一句自然、口語、
  會推進關係的訊息（純文字）。
- 會話落盤為本地檔（data/chat/<char_id>/<session_id>.json），與 _chat_digest 的
  本地優先讀取一致；不主動推遠端共享儲存，避免污染 demo 之外的資料。

用法：
  python3 scripts/mock_feed_chats.py --rounds 60            # 跑內建的 5 個新角色
  python3 scripts/mock_feed_chats.py --char char_xxx        # 只跑指定角色
  python3 scripts/mock_feed_chats.py --dry-run              # 只印計劃不生成
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import uuid
from pathlib import Path

# --- 載入 .env 到環境（config 只讀 os.environ，不自動載入 .env）---
_ROOT = Path(__file__).resolve().parent.parent
_ENV = _ROOT / ".env"
if _ENV.exists():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.rstrip("\n")
        if not _line or _line.lstrip().startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ[_k.strip()] = _v

sys.path.insert(0, str(_ROOT))

from app import api_client, chat as chat_mod, config, pipeline  # noqa: E402

# Feed Demo 待 mock 的角色（預設＝最近一批新增的 zh-Hant tl 角色）。
# 之前的 5 個 mengnv 角色已生成過，如需重跑用 --char 指定即可。
DEFAULT_CHARS = [
    "char_1784089818_de4be8",  # zh-Hant 203號的沈先生
    "char_1784032995_ae7bd2",  # zh-Hant 203號許先生
    "char_1784033099_bd1327",  # zh-Hant 阿潮（南方澳輕浮船工）
    "char_1784089982_7f794b",  # zh-Hant 阿澈
    "char_1784089978_9a3724",  # zh-Hant 阿澈｜別去河邊
]


def _persona_gist(persona: dict) -> str:
    keys = ("name", "gender", "age", "social_status", "personality",
            "speech_style", "likes", "love_style", "relationship_with_user")
    out = {}
    for k in keys:
        v = persona.get(k)
        if isinstance(v, dict):
            v = v.get("summary") or v.get("zh") or next(iter(v.values()), "")
        if v:
            out[k] = v
    return json.dumps(out, ensure_ascii=False)[:1800]


def _lang_line(lang: str) -> str:
    return {
        "zh": "使用者用自然的簡體中文口語聊天。",
        "zh-Hant": "使用者用自然的繁體中文口語聊天。",
        "ko": "유저는 자연스러운 한국어 구어체로 대화한다.",
        "ja": "ユーザーは自然な日本語の口語で会話する。",
        "en": "The user chats in natural, casual English.",
    }.get(lang, "使用者用自然的口語聊天。")


def _user_turn(persona: dict, lang: str, history_text: str, round_i: int) -> str:
    """使用者模擬器：讀人設與對話，回一句自然、推進關係的訊息（純文字）。"""
    sys_prompt = (
        "你在扮演一個正在和某個 AI 角色聊天的真實使用者。你不是旁白、不是助手，"
        "就是一個有好奇心、會被角色吸引、想更靠近 TA 的普通人。\n"
        "規則：\n"
        "- 只輸出這一句要發出去的訊息本身，不要引號、不要解釋、不要角色名。\n"
        "- 一次一條，短，口語，像手機上打字。可以追問、吐槽、分享自己的小事、調侃、關心。\n"
        "- 順著上一句角色說的往下接，讓關係自然升溫；別每句都在採訪。\n"
        f"- {_lang_line(lang)}"
    )
    stage = ("剛開始聊，帶點試探和好奇" if round_i < 8 else
             "已經聊開了，比較放鬆熟絡" if round_i < 40 else
             "關係明顯近了，有默契和小暧昧")
    user_prompt = (
        f"# 你在和這個角色聊天\n{_persona_gist(persona)}\n\n"
        f"# 目前的對話（最近若干句）\n{history_text or '（還沒開始，角色剛發了開場白）'}\n\n"
        f"# 現在的關係階段\n{stage}\n\n"
        "輸出你接下來要發的那一條訊息："
    )
    raw = api_client.chat(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_prompt}],
        model=config.CHAT_MODEL, temperature=1.0, max_tokens=400,
    )
    return (raw or "").strip().strip('"').strip()


def _tail_history(session: dict, n: int = 12) -> str:
    lines: list[str] = []
    name = (session.get("_char_name") or "角色")
    for m in session.get("messages", [])[-n:]:
        if m.get("role") == "user":
            t = chat_mod._clean_text(m.get("content"))
            if t:
                lines.append(f"유저/使用者: {t}")
        elif m.get("role") == "assistant":
            parts = []
            for it in (m.get("items") or []):
                c = (it.get("data") or {}).get("content")
                if c:
                    parts.append(str(c))
            if parts:
                lines.append(f"{name}: " + " ".join(parts))
    return "\n".join(lines)


def _assistant_turn(record: dict, session: dict) -> list[dict]:
    """角色回覆：完全複用正式聊天鏈路。"""
    llm_messages = [
        {"role": "system", "content": chat_mod.build_prompt(
            record, session.get("context", {}), session.get("prompt_template"),
            mode="normal")},
        *chat_mod._history_messages(session),
    ]
    raw = api_client.chat(llm_messages, model=config.CHAT_MODEL,
                          temperature=0.9, max_tokens=4000)
    parsed = api_client.parse_json_text(raw)
    return chat_mod._normalize_items(parsed)


def _session_path(char_id: str, session_id: str) -> Path:
    d = config.CHAT_DIR / char_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session_id}.json"


def build_one(char_id: str, rounds: int) -> tuple[str, str]:
    record = pipeline.load_character(char_id)
    persona = record.get("persona") or {}
    char_name = persona.get("name") or char_id
    lang = record.get("lang") or "zh"

    session = {
        "session_id": f"chat_{int(time.time())}_{uuid.uuid4().hex[:6]}",
        "char_id": char_id,
        "mode": "normal",
        "created": int(time.time()),
        "updated": int(time.time()),
        "context": {},
        "prompt_template": "",
        "messages": [],
        "_char_name": char_name,  # 僅本腳本組裝歷史用，落盤前刪除
        "mock": True,
    }
    opening = chat_mod._opening_items(record)
    if opening:
        session["messages"].append({
            "role": "assistant", "items": opening,
            "raw": json.dumps(opening, ensure_ascii=False),
            "is_opening": True, "created": int(time.time()),
        })

    for i in range(rounds):
        hist = _tail_history(session)
        try:
            user_msg = _user_turn(persona, lang, hist, i)
        except Exception as e:  # noqa: BLE001
            user_msg = ""
        if not user_msg:
            user_msg = {"zh": "嗯？然後呢", "zh-Hant": "嗯？然後呢",
                        "ko": "응? 그래서?", "ja": "うん、それで？",
                        "en": "hm, and then?"}.get(lang, "然後呢")
        session["messages"].append(
            {"role": "user", "content": user_msg, "created": int(time.time())})
        try:
            items = _assistant_turn(record, session)
        except Exception as e:  # noqa: BLE001
            items = [{"type": "text", "data": {"content": "…"}}]
        session["messages"].append({
            "role": "assistant", "items": items,
            "raw": json.dumps(items, ensure_ascii=False),
            "created": int(time.time()),
        })
        # 每 5 輪 checkpoint 落盤，避免中途被殺丟失全部進度
        if (i + 1) % 5 == 0:
            _flush(char_id, session)
            print(f"  [{char_name}] {i + 1}/{rounds} 輪", file=sys.stderr, flush=True)

    _flush(char_id, session)
    return char_id, str(_session_path(char_id, session["session_id"]))


def _flush(char_id: str, session: dict) -> None:
    name = session.pop("_char_name", None)
    session["updated"] = int(time.time())
    path = _session_path(char_id, session["session_id"])
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    if name is not None:
        session["_char_name"] = name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", action="append", help="指定角色（可多次）；預設跑內建 5 個")
    ap.add_argument("--rounds", type=int, default=60, help="對話輪數（一輪=使用者+角色）")
    ap.add_argument("--workers", type=int, default=5, help="並行角色數")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    chars = args.char or DEFAULT_CHARS
    print(f"CHAT_MODEL={config.CHAT_MODEL}  角色數={len(chars)}  每個 {args.rounds} 輪",
          file=sys.stderr)
    for cid in chars:
        try:
            rec = pipeline.load_character(cid)
            nm = (rec.get("persona") or {}).get("name")
            print(f"  - {cid}  {nm}  lang={rec.get('lang')}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"  - {cid}  [載入失敗] {e}", file=sys.stderr)
    if args.dry_run:
        print("（dry-run，未生成。）", file=sys.stderr)
        return

    t0 = time.time()
    results: list[tuple[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(build_one, cid, args.rounds): cid for cid in chars}
        for fut in concurrent.futures.as_completed(futs):
            cid = futs[fut]
            try:
                results.append(fut.result())
                print(f"✓ {cid} 完成", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"✗ {cid} 失敗: {e}", file=sys.stderr)
    dt = int(time.time() - t0)
    print(f"\n完成 {len(results)}/{len(chars)}，耗時 {dt}s", file=sys.stderr)
    for cid, path in results:
        print(f"  {cid} -> {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
