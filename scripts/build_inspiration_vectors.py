"""为精简灵感库预计算检索向量（火山 Ark），存为 numpy .npy。

给每条 item 的 vibe+event+mood 作检索文本算 embedding，输出到
app/data/inspiration_vectors.npy（float32，行序与 items 一一对应）。

Ark 向量模型不支持真批量，用线程池并发单条请求，支持断点续跑
（.ckpt.npy 保存进度，中断后重跑跳过已完成）。

用法：
  POPOP_EMBED_KEY=ark-xxx python scripts/build_inspiration_vectors.py [并发数]
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api_client, config  # noqa: E402

LIB = Path(__file__).resolve().parent.parent / "app" / "data" / "inspiration_library.json"
OUT = LIB.parent / "inspiration_vectors.npy"
CKPT = LIB.parent / "inspiration_vectors.ckpt.npy"
DONE = LIB.parent / "inspiration_vectors.done.npy"
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 8


def _text(item: dict) -> str:
    parts = list(item.get("vibe") or [])
    parts += list(item.get("event") or [])
    parts += list(item.get("mood") or [])
    return " ".join(parts) or "生活"


def main():
    items = json.loads(LIB.read_text(encoding="utf-8")).get("items", [])
    if not items:
        print("no items; run build_inspiration_library.py first")
        return
    # 先探一条拿维度
    probe = api_client.embed([_text(items[0])])[0]
    dim = len(probe)
    n_items = len(items)
    if CKPT.exists() and DONE.exists():
        mat = np.load(CKPT)
        done = np.load(DONE)
        if mat.shape != (n_items, dim):
            mat = np.zeros((n_items, dim), dtype=np.float32)
            done = np.zeros(n_items, dtype=bool)
    else:
        mat = np.zeros((n_items, dim), dtype=np.float32)
        done = np.zeros(n_items, dtype=bool)
    mat[0] = probe
    done[0] = True
    todo = [i for i in range(n_items) if not done[i]]
    print(f"total={n_items} dim={dim} todo={len(todo)} model={config.EMBED_MODEL} workers={WORKERS}")

    def _one(i):
        try:
            return i, api_client.embed([_text(items[i])])[0]
        except Exception as e:  # noqa: BLE001 单条失败不拖垮整批
            return i, ("__err__", str(e)[:120])

    t0 = time.time()
    n = 0
    errs = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(_one, i) for i in todo]
        for fut in as_completed(futs):
            i, vec = fut.result()
            if isinstance(vec, tuple) and vec and vec[0] == "__err__":
                errs += 1
                if errs <= 5:
                    print(f"  [skip {i}] {vec[1]}", flush=True)
                n += 1
                continue
            mat[i] = vec
            done[i] = True
            n += 1
            if n % 500 == 0:
                np.save(CKPT, mat)
                np.save(DONE, done)
                rate = n / (time.time() - t0)
                print(f"  {n}/{len(todo)}  {rate:.1f}/s  "
                      f"eta {(len(todo)-n)/rate/60:.1f}min errs={errs}", flush=True)

    n_done = int(done.sum())
    print(f"embedded {n_done}/{n_items} (errs this pass={errs})", flush=True)
    if n_done < n_items:
        # 保留 checkpoint，重跑本脚本会自动续跑未完成的条目
        np.save(CKPT, mat)
        np.save(DONE, done)
        print(f"incomplete: {n_items - n_done} left; re-run to continue "
              f"(checkpoint kept)", flush=True)
        return
    np.save(OUT, mat)
    CKPT.unlink(missing_ok=True)
    DONE.unlink(missing_ok=True)
    print(f"wrote {mat.shape} -> {OUT} ({OUT.stat().st_size/1e6:.1f} MB)", flush=True)


if __name__ == "__main__":
    main()
