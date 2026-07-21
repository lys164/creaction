# -*- coding: utf-8 -*-
"""靈感庫：性格庫 + 職業庫的領料/銷賬（僅 real track 使用）。

設計（與 prompts/pipeline 的分工）：
- 編排層"發牌"：每次生成前從庫裡隨機發一小手候選（冷卻過濾後），模型必須
  從性格/職業各選恰好一條（"選哪條"由模型按圖片咬合度決定，但不能全棄）。
- 模型"回報"：在輸出 JSON 頂層用 used_seeds 陣列如實回報真正採用的條目
  （複用 ig feed 裡 topic_seed 的輸出義務模式）。
- 編排層"銷賬"：只對 used_seeds 裡的條目記賬進入冷卻；沒用的原樣回池。
  冷卻以"後續生產次數"計（不是時間），保證批次生產時的輪換覆蓋。

不做的事：不讓模型自由瀏覽全庫（全庫進上下文=對所有條目錨定+模型會反覆挑
同幾條最順眼的，造成庫內塌縮）；不做硬性組合去重（性格/職業是欠定自由度的
靈感，同條目在冷卻期外複用是允許的，最終去重靠圖片 grounding 與動態避讓）。
"""
from __future__ import annotations

import json
import random
import threading
from pathlib import Path

from . import config

# 冷卻期：一個條目被採用後，接下來 N 個角色的發牌不再包含它。
COOLDOWNS = {"personality": 15, "occupation": 25}

# 每手牌的張數。
HAND_SIZE = {"personality": 3, "occupation": 3}

# real track 是面向交友/聊天的真實人物，反派清單不參與發牌。
_EXCLUDED_GROUPS = {"反派角色分類"}

_LIB_FILES = {
    "personality": "personality_library.json",
    "occupation": "occupation_library.json",
}

_state_lock = threading.Lock()


def _flatten(node, path: tuple[str, ...], dim: str, out: list[dict]) -> None:
    """把 {分類: {子類: [ {序號, 內容} ]}} 的任意巢狀攤平成條目列表。"""
    if isinstance(node, dict):
        for key, child in node.items():
            if key in _EXCLUDED_GROUPS:
                continue
            _flatten(child, path + (key,), dim, out)
    elif isinstance(node, list):
        for item in node:
            if not isinstance(item, dict):
                continue
            text = str(item.get("內容") or "").strip()
            seq = item.get("序號")
            if not text or seq is None:
                continue
            out.append({
                "id": f"{dim}:{seq}",
                "dim": dim,
                "group": "/".join(path),
                "text": text,
            })


def _load_entries() -> dict[str, list[dict]]:
    libs: dict[str, list[dict]] = {}
    base = Path(__file__).resolve().parent / "data"
    for dim, fname in _LIB_FILES.items():
        out: list[dict] = []
        try:
            data = json.loads((base / fname).read_text(encoding="utf-8"))
            _flatten(data, (), dim, out)
        except (OSError, json.JSONDecodeError):
            pass
        libs[dim] = out
    return libs


_ENTRIES = _load_entries()
_ENTRY_BY_ID = {e["id"]: e for lst in _ENTRIES.values() for e in lst}


def _state_path(state_path: Path | None = None) -> Path:
    return state_path or (config.DATA_DIR / "library_state.json")


def _load_state(state_path: Path | None = None) -> dict:
    p = _state_path(state_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("counter", 0)
            data.setdefault("entries", {})
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"counter": 0, "entries": {}}


def _save_state(state: dict, state_path: Path | None = None) -> None:
    p = _state_path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                 encoding="utf-8")


def _available(dim: str, state: dict) -> list[dict]:
    counter = state.get("counter", 0)
    cooldown = COOLDOWNS.get(dim, 0)
    used = state.get("entries", {})
    out = []
    for e in _ENTRIES.get(dim, []):
        info = used.get(e["id"])
        if info and counter - info.get("used_at", -10**9) < cooldown:
            continue
        out.append(e)
    return out


def checkout(state_path: Path | None = None) -> list[dict]:
    """發一手牌：每個維度冷卻過濾後隨機抽 HAND_SIZE 條。不修改臺賬。"""
    with _state_lock:
        state = _load_state(state_path)
    hand: list[dict] = []
    for dim, k in HAND_SIZE.items():
        pool = _available(dim, state)
        if not pool:  # 全在冷卻中時放開限制，寧可複用不可斷供
            pool = _ENTRIES.get(dim, [])
        hand.extend(random.sample(pool, min(k, len(pool))))
    return hand


def commit(used_ids: list[str], state_path: Path | None = None) -> list[str]:
    """銷賬：生產計數 +1，把真正被採用的條目蓋上冷卻戳。返回合法的已用 id。"""
    valid = [i for i in used_ids if isinstance(i, str) and i in _ENTRY_BY_ID]
    with _state_lock:
        state = _load_state(state_path)
        state["counter"] = state.get("counter", 0) + 1
        for i in valid:
            info = state["entries"].setdefault(i, {"uses": 0})
            info["used_at"] = state["counter"]
            info["uses"] = info.get("uses", 0) + 1
        _save_state(state, state_path)
    return valid


def hand_block(hand: list[dict], lang: str) -> str:
    """把手牌渲染成注入人設 prompt 的文字塊（隨 diversity_block 拼接）。"""
    if not hand:
        return ""
    lines_zh = [
        "# 🎴 靈感手牌（本次隨機發放；必須各選一條落進人設）",
        "下面是從性格庫/職業庫隨機發的候選靈感。使用規則：",
        "- 【必須各選一條】從性格候選裡選【恰好 1 條】、職業候選裡選【恰好 1 條】，作為這個角色的性格底色與職業方向，不允許全部棄用。",
        "- 【和圖片咬合著選】優先挑與圖片觀察、氣質、創作補充要求最對得上的那條；選定後必須具體化成這個人的可拍事實、與圖片資訊融為一體，禁止照抄原句措辭、禁止生硬貼標籤。",
        "- 【如實回報】在輸出 JSON 頂層額外加欄位 \"used_seeds\"：字串陣列，填本次採用的性格與職業條目編號（如 [\"personality:37\",\"occupation:12\"]）。它是生產臺賬，不是人設內容，其餘欄位不受影響。",
        "候選：",
    ]
    lines_ko = [
        "# 🎴 영감 핸드(이번에 무작위로 지급; 각 차원에서 반드시 하나씩 골라 인설에 반영한다)",
        "아래는 성격 라이브러리/직업 라이브러리에서 무작위로 뽑은 후보 영감이다. 사용 규칙:",
        "- 【각 차원 하나씩 필수】성격 후보에서 【정확히 1개】, 직업 후보에서 【정확히 1개】를 골라 이 캐릭터의 성격 바탕색과 직업 방향으로 삼는다. 전부 버리는 것은 허용되지 않는다.",
        "- 【이미지와 맞물리게 고른다】이미지 관찰·분위기·창작 보충 요구와 가장 잘 맞는 것을 우선 고른다; 고른 뒤에는 이 사람의 촬영 가능한 사실로 구체화해 이미지 정보와 하나로 녹인다. 원문 표현을 그대로 베끼지 말고, 딱딱하게 라벨 붙이지 말 것(원문이 중국어여도 출력은 전부 한국어).",
        "- 【정직 보고】출력 JSON 최상위에 \"used_seeds\" 필드를 추가한다: 이번에 채택한 성격·직업 항목 id의 문자열 배열(예: [\"personality:37\",\"occupation:12\"]). 생산 장부일 뿐 인설 내용이 아니며 다른 필드에 영향 없다.",
        "후보:",
    ]
    lines = lines_ko if lang == "ko" else lines_zh
    for e in hand:
        lines.append(f"- {e['id']} 【{e['group']}】{e['text']}")
    return "\n".join(lines)
