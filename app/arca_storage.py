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
import logging
import time

import requests

from . import config

log = logging.getLogger("arca_storage")

# 瞬时故障重试：网络抖动 / 5xx / 429 会重试，业务性 4xx（400/401/404）立即抛。
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


class StorageError(Exception):
    pass


def _request(method: str, path: str, *, retry: bool = True, **kwargs):
    """带指数退避重试的 HTTP 调用。仅对网络异常/5xx/429 重试；业务错误直接返回。"""
    attempts = _MAX_RETRIES if retry else 1
    last_exc: Exception | None = None
    caller = getattr(requests, method.lower())  # requests.get / requests.post
    for i in range(attempts):
        try:
            resp = caller(_url(path), headers=_headers(), **kwargs)
        except requests.RequestException as e:  # 连接/读超时/DNS 等瞬时故障
            last_exc = e
            if i + 1 >= attempts:
                raise StorageError(f"storage 网络失败 {method} {path}: {e}") from e
            time.sleep(0.5 * (2 ** i))
            log.warning("storage %s %s 网络失败，重试 %d/%d: %s",
                        method, path, i + 1, attempts, e)
            continue
        if resp.status_code in _RETRY_STATUS and i + 1 < attempts:
            time.sleep(0.5 * (2 ** i))
            log.warning("storage %s %s HTTP %d，重试 %d/%d",
                        method, path, resp.status_code, i + 1, attempts)
            continue
        return resp
    # 理论不可达：循环要么 return 要么 raise
    raise StorageError(f"storage 调用失败 {method} {path}: {last_exc}")


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
    r = _request("POST", "/storage/collections/create",
                 json={"name": name, "description": description}, timeout=15)
    _check(r)


def put_record(collection: str, key: str, data: dict) -> None:
    r = _request("POST", "/storage/records/put",
                 json={"collection": collection, "key": key, "data": data},
                 timeout=30)
    _check(r)


def get_record(collection: str, key: str) -> dict | None:
    """返回 data 对象；记录不存在返回 None。"""
    r = _request("GET", "/storage/records/get",
                 params={"collection": collection, "key": key}, timeout=30)
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
    r = _request("POST", "/storage/records/query", json=payload, timeout=30)
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
    """删除记录；记录/集合不存在视为已删（幂等）。瞬时故障自动重试。"""
    r = _request("POST", "/storage/records/delete",
                 json={"collection": collection, "key": key}, timeout=30)
    _check(r, allow_404=True)
