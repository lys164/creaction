"""轻量后台任务系统：长任务异步执行 + 进度轮询。

任务状态存 SQLite（data/tasks.db，WAL 模式），跨进程共享——支持 uvicorn 多
worker：任一 worker 都能读到任意 worker 创建/更新的任务状态，避免轮询被路由到
另一 worker 时误报 404。任务的实际执行仍在“收到该请求的 worker”的本地线程池里，
只有状态经由 SQLite 共享。公共 API（create_task/bump/get_task/run）签名不变。
"""
import json
import os
import sqlite3
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

_EXECUTOR = ThreadPoolExecutor(max_workers=8)
_TTL = 3600                      # 已结束任务保留时长（秒）
_DB_PATH = Path(config.DATA_DIR) / "tasks.db"
_LOCAL = threading.local()       # 每线程一个 sqlite 连接（sqlite 连接非线程安全）
_INIT_LOCK = threading.Lock()
_INITED = False


def _conn() -> sqlite3.Connection:
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(_DB_PATH), timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        _LOCAL.conn = c
        _ensure_schema(c)
    return c


def _ensure_schema(c: sqlite3.Connection) -> None:
    global _INITED
    if _INITED:
        return
    with _INIT_LOCK:
        c.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                kind TEXT,
                status TEXT,
                total INTEGER,
                done_count INTEGER,
                result TEXT,
                error TEXT,
                traceback TEXT,
                created INTEGER,
                ended INTEGER
            )"""
        )
        _INITED = True


def _now() -> int:
    return int(time.time())


def _row_to_task(row: sqlite3.Row | tuple | None) -> dict | None:
    if row is None:
        return None
    keys = ["id", "kind", "status", "total", "done_count",
            "result", "error", "traceback", "created", "ended"]
    d = dict(zip(keys, row))
    if d.get("result") is not None:
        try:
            d["result"] = json.loads(d["result"])
        except (TypeError, ValueError):
            pass
    return d


def _gc() -> None:
    cutoff = _now() - _TTL
    try:
        _conn().execute(
            "DELETE FROM tasks WHERE status IN ('done','error') AND ended < ?",
            (cutoff,),
        )
    except sqlite3.Error:
        pass


def create_task(kind: str, total: int = 0) -> str:
    """注册一个任务，返回 task_id。"""
    _gc()
    tid = uuid.uuid4().hex[:12]
    _conn().execute(
        "INSERT INTO tasks (id,kind,status,total,done_count,result,error,"
        "traceback,created,ended) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tid, kind, "running", total, 0, None, None, None, _now(), 0),
    )
    return tid


def bump(tid: str, n: int = 1) -> None:
    """推进进度计数（跨进程原子自增）。"""
    _conn().execute(
        "UPDATE tasks SET done_count = done_count + ? WHERE id = ?", (n, tid)
    )


def get_task(tid: str) -> dict | None:
    cur = _conn().execute(
        "SELECT id,kind,status,total,done_count,result,error,traceback,"
        "created,ended FROM tasks WHERE id = ?",
        (tid,),
    )
    return _row_to_task(cur.fetchone())


def run(tid: str, fn) -> None:
    """在后台线程池里执行 fn(tid)，把返回值存为 result。"""
    def _wrap():
        try:
            result = fn(tid)
            _conn().execute(
                "UPDATE tasks SET status='done', result=?, ended=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), _now(), tid),
            )
        except Exception as e:  # noqa: BLE001 任务内任何异常都要落到状态里
            _conn().execute(
                "UPDATE tasks SET status='error', error=?, traceback=?, "
                "ended=? WHERE id=?",
                (str(e), traceback.format_exc()[-2000:], _now(), tid),
            )

    _EXECUTOR.submit(_wrap)
