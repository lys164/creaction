"""arca 存储中台（数据面）客户端：X-Storage-Key 鉴权的通用 JSONB 集合存储。

后端合同（读 arca-i18n internal/storagehub 实现核实）：
- POST /storage/collections/create {name, description}   幂等（同名活跃集合仅更新描述）
- POST /storage/records/put {collection, key, data}      upsert；data ≤256KB JSON 对象；key ≤128
- GET  /storage/records/get?collection=&key=             未命中 HTTP 404 纯文本「记录不存在」
- POST /storage/records/query {collection, match, order_by, desc, limit(≤500), offset}
- POST /storage/records/delete {collection, key}
错误形态与业务 API 不同：非 {code,msg} 壳，是 http.Error 纯文本 + 状态码
（401 鉴权失败 / 400 参数非法 / 404 集合或记录不存在 / 5xx 服务端）。
集合名须 ^[a-z0-9_]{1,64}$；不走 JWT / 签名 / X-Language 等业务头。
"""
import json

import requests

from . import config


class StorageError(Exception):
    pass


def enabled() -> bool:
    """是否启用远端存储（未配 ARCA_STORAGE_KEY 时一切读写退化为纯本地）。"""
    return bool(config.ARCA_BASE_URL and config.ARCA_STORAGE_KEY)


def _headers() -> dict:
    return {"X-Storage-Key": config.ARCA_STORAGE_KEY,
            "Content-Type": "application/json"}


def _url(path: str) -> str:
    return f"{config.ARCA_BASE_URL}{path}"


def _check(resp, allow_404: bool = False):
    if resp.status_code == 404 and allow_404:
        return None
    if resp.status_code >= 400:
        raise StorageError(
            f"storage HTTP {resp.status_code} {resp.request.method} {resp.url}: "
            f"{(resp.text or '').strip()[:300]}")
    return resp.json() if resp.text else {}


def ensure_collection(name: str, description: str = "") -> None:
    """注册集合（幂等）。put 之前集合必须存在，否则 404「集合不存在」。"""
    r = requests.post(_url("/storage/collections/create"),
                      json={"name": name, "description": description},
                      headers=_headers(), timeout=15)
    _check(r)


def put_record(collection: str, key: str, data: dict) -> None:
    r = requests.post(_url("/storage/records/put"),
                      json={"collection": collection, "key": key, "data": data},
                      headers=_headers(), timeout=30)
    _check(r)


def get_record(collection: str, key: str) -> dict | None:
    """返回 data 对象；记录不存在返回 None。"""
    r = requests.get(_url("/storage/records/get"),
                     params={"collection": collection, "key": key},
                     headers=_headers(), timeout=30)
    body = _check(r, allow_404=True)
    if body is None:
        return None
    data = body.get("data")
    if isinstance(data, str):  # 防御：万一 data 以字符串形式返回
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    return data


def query_records(collection: str, match: dict | None = None,
                  order_by: str = "", desc: bool = False,
                  limit: int = 500, offset: int = 0) -> list[dict]:
    """返回 [{key, data, created_at, updated_at}, ...]；集合不存在视为空。"""
    payload: dict = {"collection": collection, "limit": limit, "offset": offset,
                     "desc": desc}
    if match:
        payload["match"] = match
    if order_by:
        payload["order_by"] = order_by
    r = requests.post(_url("/storage/records/query"), json=payload,
                      headers=_headers(), timeout=30)
    body = _check(r, allow_404=True)
    if body is None:
        return []
    rows = body.get("items") or []  # 响应 {items:[{key,data,created_at,updated_at}], total}
    out = []
    for row in rows:
        data = row.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                continue
        out.append({"key": row.get("key"), "data": data,
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at")})
    return out


def delete_record(collection: str, key: str) -> None:
    """删除记录；记录/集合不存在视为已删（幂等）。"""
    r = requests.post(_url("/storage/records/delete"),
                      json={"collection": collection, "key": key},
                      headers=_headers(), timeout=30)
    _check(r, allow_404=True)
