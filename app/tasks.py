"""轻量后台任务系统：长任务异步执行 + 进度轮询。

任务状态存内存（单进程 uvicorn 足够）。每个任务在独立线程池里跑，
HTTP 接口立即返回 task_id，前端轮询 /api/tasks/{id} 获取进度与结果。
"""
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

_TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=4)
# 任务保留时长（秒），超时后清理，避免内存无限增长
_TTL = 3600


def _now() -> int:
    return int(time.time())


def _gc() -> None:
    cutoff = _now() - _TTL
    with _LOCK:
        stale = [
            tid for tid, t in _TASKS.items()
            if t["status"] in ("done", "error") and t.get("ended", 0) < cutoff
        ]
        for tid in stale:
            _TASKS.pop(tid, None)


def create_task(kind: str, total: int = 0) -> str:
    """注册一个任务，返回 task_id。"""
    _gc()
    tid = uuid.uuid4().hex[:12]
    with _LOCK:
        _TASKS[tid] = {
            "id": tid,
            "kind": kind,
            "status": "running",
            "total": total,
            "done_count": 0,
            "result": None,
            "error": None,
            "created": _now(),
            "ended": 0,
        }
    return tid


def bump(tid: str, n: int = 1) -> None:
    """推进进度计数。"""
    with _LOCK:
        t = _TASKS.get(tid)
        if t:
            t["done_count"] += n


def get_task(tid: str) -> dict | None:
    with _LOCK:
        t = _TASKS.get(tid)
        return dict(t) if t else None


def run(tid: str, fn) -> None:
    """在后台线程池里执行 fn(tid)，把返回值存为 result。"""
    def _wrap():
        try:
            result = fn(tid)
            with _LOCK:
                t = _TASKS.get(tid)
                if t:
                    t["status"] = "done"
                    t["result"] = result
                    t["ended"] = _now()
        except Exception as e:  # noqa: BLE001 任务内任何异常都要落到状态里
            with _LOCK:
                t = _TASKS.get(tid)
                if t:
                    t["status"] = "error"
                    t["error"] = str(e)
                    t["traceback"] = traceback.format_exc()[-2000:]
                    t["ended"] = _now()

    _EXECUTOR.submit(_wrap)
