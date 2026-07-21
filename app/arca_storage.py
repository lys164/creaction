"""arca 儲存中臺（資料面）客戶端：X-Storage-Key 鑑權的通用 JSONB 集合儲存。

後端合同（讀 arca-i18n internal/storagehub 實現核實）：
- POST /storage/collections/create {name, description}   冪等（同名活躍集合僅更新描述）
- POST /storage/records/put {collection, key, data}      upsert；data ≤256KB JSON 物件；key ≤128
- GET  /storage/records/get?collection=&key=             未命中 HTTP 404 純文字「記錄不存在」
- POST /storage/records/query {collection, match, order_by, desc, limit(≤500), offset}
- POST /storage/records/delete {collection, key}
錯誤形態與業務 API 不同：非 {code,msg} 殼，是 http.Error 純文字 + 狀態碼
（401 鑑權失敗 / 400 引數非法 / 404 集合或記錄不存在 / 5xx 服務端）。
集合名須 ^[a-z0-9_]{1,64}$；不走 JWT / 簽名 / X-Language 等業務頭。
"""
import json
import logging
import time

import requests

from . import config

log = logging.getLogger("arca_storage")

# 瞬時故障重試：網路抖動 / 5xx / 429 會重試，業務性 4xx（400/401/404）立即拋。
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


class StorageError(Exception):
    pass


def _request(method: str, path: str, *, retry: bool = True, **kwargs):
    """帶指數退避重試的 HTTP 呼叫。僅對網路異常/5xx/429 重試；業務錯誤直接返回。"""
    attempts = _MAX_RETRIES if retry else 1
    last_exc: Exception | None = None
    caller = getattr(requests, method.lower())  # requests.get / requests.post
    for i in range(attempts):
        try:
            resp = caller(_url(path), headers=_headers(), **kwargs)
        except requests.RequestException as e:  # 連線/讀超時/DNS 等瞬時故障
            last_exc = e
            if i + 1 >= attempts:
                raise StorageError(f"storage 網路失敗 {method} {path}: {e}") from e
            time.sleep(0.5 * (2 ** i))
            log.warning("storage %s %s 網路失敗，重試 %d/%d: %s",
                        method, path, i + 1, attempts, e)
            continue
        if resp.status_code in _RETRY_STATUS and i + 1 < attempts:
            time.sleep(0.5 * (2 ** i))
            log.warning("storage %s %s HTTP %d，重試 %d/%d",
                        method, path, resp.status_code, i + 1, attempts)
            continue
        return resp
    # 理論不可達：迴圈要麼 return 要麼 raise
    raise StorageError(f"storage 呼叫失敗 {method} {path}: {last_exc}")


def enabled() -> bool:
    """是否啟用遠端儲存（未配 ARCA_STORAGE_KEY 時一切讀寫退化為純本地）。"""
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
    """註冊集合（冪等）。put 之前集合必須存在，否則 404「集合不存在」。"""
    r = _request("POST", "/storage/collections/create",
                 json={"name": name, "description": description}, timeout=15)
    _check(r)


def put_record(collection: str, key: str, data: dict) -> None:
    r = _request("POST", "/storage/records/put",
                 json={"collection": collection, "key": key, "data": data},
                 timeout=30)
    _check(r)


def get_record(collection: str, key: str) -> dict | None:
    """返回 data 物件；記錄不存在返回 None。"""
    r = _request("GET", "/storage/records/get",
                 params={"collection": collection, "key": key}, timeout=30)
    body = _check(r, allow_404=True)
    if body is None:
        return None
    data = body.get("data")
    if isinstance(data, str):  # 防禦：萬一 data 以字串形式返回
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    return data


def query_records(collection: str, match: dict | None = None,
                  order_by: str = "", desc: bool = False,
                  limit: int = 500, offset: int = 0) -> list[dict]:
    """返回 [{key, data, created_at, updated_at}, ...]；集合不存在視為空。"""
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
    rows = body.get("items") or []  # 響應 {items:[{key,data,created_at,updated_at}], total}
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
    """刪除記錄；記錄/集合不存在視為已刪（冪等）。瞬時故障自動重試。"""
    r = _request("POST", "/storage/records/delete",
                 json={"collection": collection, "key": key}, timeout=30)
    _check(r, allow_404=True)
