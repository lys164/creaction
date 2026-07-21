"""輕量後臺任務系統：長任務非同步執行 + 進度輪詢。

任務狀態存 SQLite（data/tasks.db，WAL 模式），跨程式共享——支援 uvicorn 多
worker：任一 worker 都能讀到任意 worker 建立/更新的任務狀態，避免輪詢被路由到
另一 worker 時誤報 404。任務的實際執行仍在“收到該請求的 worker”的本地執行緒池裡，
只有狀態經由 SQLite 共享。公共 API（create_task/bump/get_task/run）簽名不變。
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
_TTL = 3600                      # 已結束任務保留時長（秒）
_DB_PATH = Path(config.DATA_DIR) / "tasks.db"
_LOCAL = threading.local()       # 每執行緒一個 sqlite 連線（sqlite 連線非執行緒安全）
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
    """註冊一個任務，返回 task_id。"""
    _gc()
    tid = uuid.uuid4().hex[:12]
    _conn().execute(
        "INSERT INTO tasks (id,kind,status,total,done_count,result,error,"
        "traceback,created,ended) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tid, kind, "running", total, 0, None, None, None, _now(), 0),
    )
    return tid


def bump(tid: str, n: int = 1) -> None:
    """推進進度計數（跨程式原子自增）。"""
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
    """在後臺執行緒池裡執行 fn(tid)，把返回值存為 result。"""
    def _wrap():
        try:
            result = fn(tid)
            _conn().execute(
                "UPDATE tasks SET status='done', result=?, ended=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), _now(), tid),
            )
        except Exception as e:  # noqa: BLE001 任務內任何異常都要落到狀態裡
            _conn().execute(
                "UPDATE tasks SET status='error', error=?, traceback=?, "
                "ended=? WHERE id=?",
                (str(e), traceback.format_exc()[-2000:], _now(), tid),
            )

    _EXECUTOR.submit(_wrap)
