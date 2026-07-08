# -*- coding: utf-8 -*-
"""灵感库：性格库 + 职业库的领料/销账（仅 real track 使用）。

设计（与 prompts/pipeline 的分工）：
- 编排层"发牌"：每次生成前从库里随机发一小手候选（冷却过滤后），模型必须
  从性格/职业各选恰好一条（"选哪条"由模型按图片咬合度决定，但不能全弃）。
- 模型"回报"：在输出 JSON 顶层用 used_seeds 数组如实回报真正采用的条目
  （复用 ig feed 里 topic_seed 的输出义务模式）。
- 编排层"销账"：只对 used_seeds 里的条目记账进入冷却；没用的原样回池。
  冷却以"后续生产次数"计（不是时间），保证批量生产时的轮换覆盖。

不做的事：不让模型自由浏览全库（全库进上下文=对所有条目锚定+模型会反复挑
同几条最顺眼的，造成库内塌缩）；不做硬性组合去重（性格/职业是欠定自由度的
灵感，同条目在冷却期外复用是允许的，最终去重靠图片 grounding 与动态避让）。
"""
from __future__ import annotations

import json
import random
import threading
from pathlib import Path

from . import config

# 冷却期：一个条目被采用后，接下来 N 个角色的发牌不再包含它。
COOLDOWNS = {"personality": 15, "occupation": 25}

# 每手牌的张数。
HAND_SIZE = {"personality": 3, "occupation": 3}

# real track 是面向交友/聊天的真实人物，反派清单不参与发牌。
_EXCLUDED_GROUPS = {"反派角色分类"}

_LIB_FILES = {
    "personality": "personality_library.json",
    "occupation": "occupation_library.json",
}

_state_lock = threading.Lock()


def _flatten(node, path: tuple[str, ...], dim: str, out: list[dict]) -> None:
    """把 {分类: {子类: [ {序号, 内容} ]}} 的任意嵌套摊平成条目列表。"""
    if isinstance(node, dict):
        for key, child in node.items():
            if key in _EXCLUDED_GROUPS:
                continue
            _flatten(child, path + (key,), dim, out)
    elif isinstance(node, list):
        for item in node:
            if not isinstance(item, dict):
                continue
            text = str(item.get("内容") or "").strip()
            seq = item.get("序号")
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
    """发一手牌：每个维度冷却过滤后随机抽 HAND_SIZE 条。不修改台账。"""
    with _state_lock:
        state = _load_state(state_path)
    hand: list[dict] = []
    for dim, k in HAND_SIZE.items():
        pool = _available(dim, state)
        if not pool:  # 全在冷却中时放开限制，宁可复用不可断供
            pool = _ENTRIES.get(dim, [])
        hand.extend(random.sample(pool, min(k, len(pool))))
    return hand


def commit(used_ids: list[str], state_path: Path | None = None) -> list[str]:
    """销账：生产计数 +1，把真正被采用的条目盖上冷却戳。返回合法的已用 id。"""
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
    """把手牌渲染成注入人设 prompt 的文本块（随 diversity_block 拼接）。"""
    if not hand:
        return ""
    lines_zh = [
        "# 🎴 灵感手牌（本次随机发放；必须各选一条落进人设）",
        "下面是从性格库/职业库随机发的候选灵感。使用规则：",
        "- 【必须各选一条】从性格候选里选【恰好 1 条】、职业候选里选【恰好 1 条】，作为这个角色的性格底色与职业方向，不允许全部弃用。",
        "- 【和图片咬合着选】优先挑与图片观察、气质、创作补充要求最对得上的那条；选定后必须具体化成这个人的可拍事实、与图片信息融为一体，禁止照抄原句措辞、禁止生硬贴标签。",
        "- 【如实回报】在输出 JSON 顶层额外加字段 \"used_seeds\"：字符串数组，填本次采用的性格与职业条目编号（如 [\"personality:37\",\"occupation:12\"]）。它是生产台账，不是人设内容，其余字段不受影响。",
        "候选：",
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
