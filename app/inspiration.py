"""真實帖子拆解靈感庫的檢索層。

來源：scripts/build_inspiration_library.py 從 decompositions.json 派生的
app/data/inspiration_library.json（約 2 萬條 frame 級拆解）。

用法：給定角色 persona 的 vibe 與某條帖子的 content，檢索若干條"成品範例"
作為生圖靈感（借結構/手法，不照抄具體衣物/物件）。

匹配策略：
  1) vibe ↔ persona（角色氣質）
  2) event/mood ↔ content（這條帖子在做什麼）
  3) 命中候選裡隨機抽 k 條，保證多樣、不塌縮到同一批
檢索打分優先用 embedding（若存在預計算向量檔案 + 端點可用），
否則回退到純詞重疊（無第三方依賴，永遠可用）。
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

from . import config

_LIB_PATH = config.DATA_DIR.parent / "app" / "data" / "inspiration_library.json"
_VEC_PATH = config.DATA_DIR.parent / "app" / "data" / "inspiration_vectors.npy"

# 拼進 prompt 的靈感欄位：只留【純氛圍】維度——畫面風格/拍攝手法/構圖手法。
# 刻意去掉 action/expression/makeup/clothes：那些繫結到某個人的具體造型，
# 會把整組帖子往同一個姿勢/穿搭帶偏、沖淡角色人設，正是質量坍縮的來源。
_INSPO_FIELDS = (
    "visual_style", "shooting_style", "framing",
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
    """預計算向量（可選，numpy .npy + 記憶體對映 + 行範數預算）。

    缺失/依賴不可用/行數不匹配都返回 (None, None)，檢索自動走詞重疊回退。
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
    """粗分詞：英文按詞、中文按 2-gram，夠做詞重疊打分。"""
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
    """給查詢串算 embedding；端點不可用則返回 None（觸發詞重疊回退）。"""
    if _VECTORS is None:
        return None
    try:
        from . import api_client
        vecs = api_client.embed([query])
        return vecs[0] if vecs else None
    except Exception:
        return None


def _scored_indices_vec(qvec, pool_size: int) -> list:
    """numpy 向量化餘弦：返回 top pool_size 下標。"""
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
    """按查詢給全庫打分，返回 top pool_size 下標（embedding 優先，詞重疊回退）。"""
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
    """檢索 k 條靈感 item。

    vibe: 角色氣質（list 或 str），配 persona；
    content: 這條帖子的文案，配 event/mood；
    query 以 content 為主（content 決定這條帖子拍什麼），vibe 只輕度帶入，
    避免不同帖子因共享同一 vibe 而召回高度雷同的候選池。
    exclude: 跨帖子已用過的 src 集合，命中則跳過，保證多樣。
    返回 (items, srcs)，srcs 供呼叫方累加進 exclude 做跨帖去重。
    庫缺失時返回 ([], [])。
    """
    if not _ITEMS:
        return [], []
    exclude = exclude or set()
    vibe_str = " ".join(vibe) if isinstance(vibe, (list, tuple)) else str(vibe or "")
    # content 為主、vibe 只帶一次，弱化共享氣質對候選池的支配
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
    "action": "動作", "expression": "表情", "makeup": "妝容",
    "clothes": "穿搭", "framing": "構圖", "visual_style": "畫面風格",
    "shooting_style": "拍攝手法",
}


def format_refs(items: list) -> str:
    """把檢索到的 item 拼成 prompt 參考串（每條列出有值的靈感欄位）。"""
    if not items:
        return ""
    blocks = []
    for n, it in enumerate(items, 1):
        lines = [
            f"    - {_FIELD_LABELS[f]}：{it[f]}"
            for f in _INSPO_FIELDS if it.get(f)
        ]
        if lines:
            blocks.append(f"  【範例{n}】\n" + "\n".join(lines))
    return "\n".join(blocks)


def available() -> bool:
    return bool(_ITEMS)
