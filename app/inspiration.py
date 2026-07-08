"""真实帖子拆解灵感库的检索层。

来源：scripts/build_inspiration_library.py 从 decompositions.json 派生的
app/data/inspiration_library.json（约 2 万条 frame 级拆解）。

用法：给定角色 persona 的 vibe 与某条帖子的 content，检索若干条"成品范例"
作为生图灵感（借结构/手法，不照抄具体衣物/物件）。

匹配策略：
  1) vibe ↔ persona（角色气质）
  2) event/mood ↔ content（这条帖子在做什么）
  3) 命中候选里随机抽 k 条，保证多样、不塌缩到同一批
检索打分优先用 embedding（若存在预计算向量文件 + 端点可用），
否则回退到纯词重叠（无第三方依赖，永远可用）。
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

from . import config

_LIB_PATH = config.DATA_DIR.parent / "app" / "data" / "inspiration_library.json"
_VEC_PATH = config.DATA_DIR.parent / "app" / "data" / "inspiration_vectors.npy"

# 拼进 prompt 的生图灵感字段（对齐 SELFIE_SCHEMA 维度）
_INSPO_FIELDS = (
    "action", "expression", "makeup", "clothes",
    "framing", "visual_style", "shooting_style",
)


def _load_items() -> list:
    try:
        data = json.loads(_LIB_PATH.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []
    except (OSError, json.JSONDecodeError):
        return []


_ITEMS = _load_items()


def _load_vectors():
    """预计算向量（可选，numpy .npy + 内存映射 + 行范数预算）。

    缺失/依赖不可用/行数不匹配都返回 (None, None)，检索自动走词重叠回退。
    """
    if not _ITEMS or not _VEC_PATH.exists():
        return None, None
    try:
        import numpy as np
        mat = np.load(_VEC_PATH, mmap_mode="r")
        if mat.shape[0] != len(_ITEMS):
            return None, None
        norms = np.linalg.norm(np.asarray(mat), axis=1)
        norms[norms == 0] = 1.0
        return mat, norms
    except Exception:
        return None, None


_VECTORS, _VEC_NORMS = _load_vectors()

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+")


def _tokens(text: str) -> set:
    """粗分词：英文按词、中文按 2-gram，够做词重叠打分。"""
    toks: set = set()
    for m in _TOKEN_RE.findall((text or "").lower()):
        if m.isascii():
            toks.add(m)
        else:
            toks.update(m[i:i + 2] for i in range(max(len(m) - 1, 1)))
    return toks


def _item_tags(item: dict) -> str:
    parts = list(item.get("vibe") or [])
    parts += list(item.get("event") or [])
    parts += list(item.get("mood") or [])
    return " ".join(parts)


def _query_vector(query: str):
    """给查询串算 embedding；端点不可用则返回 None（触发词重叠回退）。"""
    if _VECTORS is None:
        return None
    try:
        from . import api_client
        vecs = api_client.embed([query])
        return vecs[0] if vecs else None
    except Exception:
        return None


def _scored_indices_vec(qvec, pool_size: int) -> list:
    """numpy 向量化余弦：返回 top pool_size 下标。"""
    import numpy as np
    q = np.asarray(qvec, dtype=np.float32)
    qn = np.linalg.norm(q) or 1.0
    sims = (np.asarray(_VECTORS) @ q) / (_VEC_NORMS * qn)
    k = min(pool_size, sims.shape[0])
    idx = np.argpartition(-sims, k - 1)[:k]
    return idx[np.argsort(-sims[idx])].tolist()


def _scored_indices_lexical(query: str, pool_size: int) -> list:
    qtok = _tokens(query)
    scores = (
        (len(qtok & _tokens(_item_tags(_ITEMS[i]))), i)
        for i in range(len(_ITEMS))
    )
    ranked = sorted(scores, key=lambda x: x[0], reverse=True)
    return [i for score, i in ranked[:pool_size] if score > 0]


def _scored_indices(query: str, pool_size: int) -> list:
    """按查询给全库打分，返回 top pool_size 下标（embedding 优先，词重叠回退）。"""
    if not _ITEMS:
        return []
    qvec = _query_vector(query)
    if qvec is not None and _VECTORS is not None:
        try:
            return _scored_indices_vec(qvec, pool_size)
        except Exception:
            pass
    return _scored_indices_lexical(query, pool_size)


def retrieve(vibe, content: str = "", k: int = 3, pool: int = 80,
             exclude: set | None = None) -> "tuple[list, list]":
    """检索 k 条灵感 item。

    vibe: 角色气质（list 或 str），配 persona；
    content: 这条帖子的文案，配 event/mood；
    query 以 content 为主（content 决定这条帖子拍什么），vibe 只轻度带入，
    避免不同帖子因共享同一 vibe 而召回高度雷同的候选池。
    exclude: 跨帖子已用过的 src 集合，命中则跳过，保证多样。
    返回 (items, srcs)，srcs 供调用方累加进 exclude 做跨帖去重。
    库缺失时返回 ([], [])。
    """
    if not _ITEMS:
        return [], []
    exclude = exclude or set()
    vibe_str = " ".join(vibe) if isinstance(vibe, (list, tuple)) else str(vibe or "")
    # content 为主、vibe 只带一次，弱化共享气质对候选池的支配
    query = f"{content} {content} {vibe_str}".strip() if content else vibe_str
    cand = _scored_indices(query, pool) if query else []
    if not cand:
        cand = list(range(len(_ITEMS)))
    cand = [i for i in cand if _ITEMS[i].get("src") not in exclude] or cand
    random.shuffle(cand)
    picked = cand[:min(k, len(cand))]
    items = [_ITEMS[i] for i in picked]
    srcs = [_ITEMS[i].get("src") for i in picked]
    return items, srcs


_FIELD_LABELS = {
    "action": "动作", "expression": "表情", "makeup": "妆容",
    "clothes": "穿搭", "framing": "构图", "visual_style": "画面风格",
    "shooting_style": "拍摄手法",
}


def format_refs(items: list) -> str:
    """把检索到的 item 拼成 prompt 参考串（每条列出有值的灵感字段）。"""
    if not items:
        return ""
    blocks = []
    for n, it in enumerate(items, 1):
        lines = [
            f"    - {_FIELD_LABELS[f]}：{it[f]}"
            for f in _INSPO_FIELDS if it.get(f)
        ]
        if lines:
            blocks.append(f"  【范例{n}】\n" + "\n".join(lines))
    return "\n".join(blocks)


def available() -> bool:
    return bool(_ITEMS)
