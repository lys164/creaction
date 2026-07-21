"""持久層合成：arca 儲存中臺(JSON 記錄) + arca OSS(圖片) 為主，本地檔案為快取。

語義（未配 ARCA_STORAGE_KEY 時 enabled()=False，所有函式退化為純本地=歷史行為）：
- save_json  : 寫本地 + put 遠端（遠端失敗只 warn，不阻斷本地——可用性優先，遷移可補推）
- load_json  : 本地命中直接用；缺失時從遠端拉並回寫本地快取
- list_json  : 本地 glob ∪ 遠端 query 合併（遠端獨有的回寫本地快取）
- delete_json: 雙刪（遠端冪等）
- save_file  : 二進位制寫本地 + OSS put；ensure_file: 本地缺失時從 OSS 拉回
集合在首次寫入時惰性 ensure_collection（冪等），程式內記憶。
"""
import json
import logging
import os
import threading
import uuid
from pathlib import Path

from . import arca_storage, config

log = logging.getLogger("storage")

# OSS 裡的檔案物件統一字首（相對 data/ 的路徑作 key）
_OSS_PREFIX = "creaction-data"
_ensured: set[str] = set()
_ensured_lock = threading.Lock()


def _ensure(collection: str) -> None:
    with _ensured_lock:
        if collection in _ensured:
            return
    arca_storage.ensure_collection(collection, description="creaction 本地資料接管")
    with _ensured_lock:
        _ensured.add(collection)


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp+rename 原子寫：併發讀永遠看到完整檔案，不會讀到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- JSON 記錄
_OSS_FIELD_MARK = "__oss_key__"  # hub 記錄裡大欄位的佔位標記


def _field_oss_key(collection: str, key: str, field: str) -> str:
    return f"{_OSS_PREFIX}/hubfields/{collection}/{key}/{field}"


def _split_oss_fields(collection: str, key: str, obj: dict,
                      oss_fields: list[str]) -> dict:
    """把 obj 的指定大欄位(str)上傳 OSS，返回 hub 版記錄（欄位替換為佔位）。

    落地頁 HTML 等大文字不進 storage_hub（單條 data 上限 256KB），
    hub 只存後設資料 + `{"__oss_key__": ...}` 佔位。
    """
    slim = dict(obj)
    bkt = _oss_bucket()
    for field in oss_fields:
        val = obj.get(field)
        if not isinstance(val, str) or not val:
            continue
        okey = _field_oss_key(collection, key, field)
        bkt.put_object(okey, val.encode("utf-8"),
                       headers={"Content-Type": "text/plain; charset=utf-8"})
        slim[field] = {_OSS_FIELD_MARK: okey}
    return slim


def _restore_oss_fields(obj: dict) -> dict:
    """把遠端記錄裡的 OSS 佔位欄位拉回原文（拉取失敗置 None，不阻斷）。"""
    for field, val in list(obj.items()):
        if isinstance(val, dict) and _OSS_FIELD_MARK in val:
            try:
                data = _oss_bucket().get_object(val[_OSS_FIELD_MARK]).read()
                obj[field] = data.decode("utf-8")
            except Exception as e:  # noqa: BLE001
                log.warning("storage oss 欄位回源 %s 失敗: %s", val[_OSS_FIELD_MARK], e)
                obj[field] = None
    return obj


def save_json(collection: str, key: str, obj: dict, local: Path,
              oss_fields: list[str] | None = None,
              strict_remote: bool = False) -> None:
    """寫本地 + put 遠端。

    預設（strict_remote=False）：遠端失敗只 warn、不阻斷本地——可用性優先，
    生成類寫入偶發遠端抖動可靠後續遷移補推。

    strict_remote=True：遠端失敗直接 raise。用於刪除類寫入（刪帖/刪批次）——
    這類寫的是「刪除後的新狀態」，若遠端沒同步成功，本地已刪而遠端仍是舊資料，
    後續 load_json 在本地快取未命中時會回源遠端舊資料，把刪掉的內容「復活」。
    嚴格模式讓呼叫方能把失敗暴露給前端重試，避免這種靜默不一致。
    """
    text = json.dumps(obj, ensure_ascii=False, indent=2)

    if not arca_storage.enabled():
        _atomic_write_text(local, text)
        return

    # 嚴格模式：先推遠端、成功後再寫本地。這樣遠端失敗時本地保持舊狀態（帖子仍在），
    # 呼叫方 raise 給前端後，重試會重新 load 到含該帖的本地資料、再刪一次直到兩端一致；
    # 若反過來先寫本地，遠端失敗後本地已刪，重試 load 不到該帖 → 報 not found → 遠端
    # 永遠補不上，反而卡死。
    #
    # 非嚴格模式：保持歷史行為——先寫本地保證可用性，遠端失敗只 warn。
    if strict_remote:
        _ensure(collection)
        remote_obj = obj
        if oss_fields:
            remote_obj = _split_oss_fields(collection, key, obj, oss_fields)
        arca_storage.put_record(collection, key, remote_obj)  # 失敗即 raise
        _atomic_write_text(local, text)
        return

    _atomic_write_text(local, text)
    try:
        _ensure(collection)
        remote_obj = obj
        if oss_fields:
            remote_obj = _split_oss_fields(collection, key, obj, oss_fields)
        arca_storage.put_record(collection, key, remote_obj)
    except Exception as e:  # noqa: BLE001 遠端失敗不阻斷本地（遷移可補推）
        log.warning("storage put %s/%s 失敗: %s", collection, key, e)


def load_json(collection: str, key: str, local: Path) -> dict | None:
    if local.exists():
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    if not arca_storage.enabled():
        return None
    try:
        obj = arca_storage.get_record(collection, key)
    except Exception as e:  # noqa: BLE001
        log.warning("storage get %s/%s 失敗: %s", collection, key, e)
        return None
    if obj is not None:  # 回源命中 → 還原 OSS 大欄位佔位 → 回寫本地完整快取
        obj = _restore_oss_fields(obj)
        try:
            _atomic_write_text(local, json.dumps(obj, ensure_ascii=False, indent=2))
        except OSError:
            pass
    return obj


def _query_all(collection: str, match: dict | None) -> list[dict]:
    """分頁拉全遠端記錄（單頁 500，無 offset 翻頁會靜默截斷）。"""
    rows: list[dict] = []
    offset = 0
    while True:
        page = arca_storage.query_records(collection, match=match,
                                          limit=500, offset=offset)
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += 500


def query_all(collection: str, match: dict | None = None) -> list[dict]:
    """公開的分頁拉全（不吞錯版）：刪除性守衛等需要嚴格語義的呼叫方使用。"""
    return _query_all(collection, match)


def list_json(collection: str, local_dir: Path, pattern: str = "*.json",
              match: dict | None = None,
              remote_key_to_local=None) -> dict[str, dict]:
    """返回 {本地key: obj}。本地 glob（key=去副檔名檔名）∪ 遠端 query 合併。

    - match: 遠端 JSONB 包含過濾（如 {"char_id": cid}），避免整集合拉取與跨角色汙染。
    - remote_key_to_local: 遠端 key → 本地檔名(不含副檔名) 的對映（遠端用複合鍵
      如 "char__batch" 而本地檔案是 "batch.json" 時必傳）；返回 None 表示該行不屬於
      本目錄，跳過。預設為恆等對映。
    - 遠端獨有的行回寫本地快取（原子寫），檔名用對映後的本地 key。
    """
    out: dict[str, dict] = {}
    if local_dir.exists():
        for p in sorted(local_dir.glob(pattern)):
            if p.name.endswith(".tmp"):
                continue
            try:
                out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    if not arca_storage.enabled():
        return out
    try:
        rows = _query_all(collection, match)
    except Exception as e:  # noqa: BLE001
        log.warning("storage query %s 失敗: %s", collection, e)
        return out
    for row in rows:
        rkey, obj = row.get("key"), row.get("data")
        if not rkey or not isinstance(obj, dict):
            continue
        lkey = remote_key_to_local(rkey) if remote_key_to_local else rkey
        if not lkey or lkey in out:
            continue
        out[lkey] = obj
        try:  # 遠端獨有 → 回寫本地快取（換機/冷啟動自動恢復）
            _atomic_write_text(local_dir / f"{lkey}.json",
                               json.dumps(obj, ensure_ascii=False, indent=2))
        except OSError:
            pass
    return out


def delete_json(collection: str, key: str, local: Path) -> None:
    """刪本地 + 遠端。遠端刪除失敗會 raise（否則已刪資料會被 list/load 回源"復活"），
    呼叫方負責把錯誤暴露給使用者或重試。"""
    local.unlink(missing_ok=True)
    if not arca_storage.enabled():
        return
    arca_storage.delete_record(collection, key)


# ---------------------------------------------------------------- 二進位制檔案(OSS)
def _oss_key(local: Path) -> str:
    """data/ 下的相對路徑作 OSS key；data 外的檔案用檔名兜底。"""
    try:
        rel = local.resolve().relative_to(config.DATA_DIR.resolve())
        return f"{_OSS_PREFIX}/{rel.as_posix()}"
    except ValueError:
        return f"{_OSS_PREFIX}/misc/{local.name}"


def save_file(local: Path, data: bytes, content_type: str = "application/octet-stream") -> None:
    """寫本地 + 上傳 OSS 私有桶（遠端失敗只 warn）。"""
    _atomic_write_bytes(local, data)
    if not arca_storage.enabled():
        return
    try:
        from . import arca_client
        arca_client.tos_upload(data, _oss_key(local), content_type, lang="zh")
    except Exception as e:  # noqa: BLE001
        log.warning("storage oss put %s 失敗: %s", local.name, e)


def migrate_all(progress=None) -> dict:
    """把本地 data/ 存量全量推到 arca（JSON→儲存中臺，圖片→OSS）。

    冪等：JSON put 即 upsert；圖片按 OSS 已存在跳過。返回逐類統計。
    progress(done_delta) 可選，用於任務進度條。
    """
    if not arca_storage.enabled():
        raise arca_storage.StorageError("未配置 ARCA_STORAGE_KEY，無法遷移")
    stats = {"personas": 0, "post_batches": 0, "ig_batches": 0, "landings": 0,
             "chats": 0, "styles": 0, "images": 0, "uploads": 0, "errors": []}

    def _bump():
        if progress:
            progress(1)

    def _put(coll: str, key: str, local: Path,
             oss_fields: list[str] | None = None):
        try:
            obj = json.loads(local.read_text(encoding="utf-8"))
            if coll == "styles" and isinstance(obj, list):
                obj = {"styles": obj}  # data 須是 JSON 物件
            if not isinstance(obj, dict):
                return
            _ensure(coll)
            if oss_fields:
                obj = _split_oss_fields(coll, key, obj, oss_fields)
            arca_storage.put_record(coll, key, obj)
            stats[coll] += 1
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"{coll}/{key}: {e}")
        _bump()

    for p in sorted(config.PERSONA_DIR.glob("*.json")):
        _put("personas", p.stem, p)
    for char_dir in sorted(config.POST_DIR.iterdir() if config.POST_DIR.exists() else []):
        if not char_dir.is_dir():
            continue
        for p in sorted(char_dir.glob("*.json")):
            if p.name == "ig_latest.json":
                _put("ig_batches", char_dir.name, p)
            elif "__" in p.stem:
                continue  # 遠端回寫的複合鍵快取檔案，遠端已有記錄，重遷會繁殖髒鍵
            else:
                _put("post_batches", f"{char_dir.name}__{p.stem}", p)
    for char_dir in sorted(config.LANDING_DIR.iterdir() if config.LANDING_DIR.exists() else []):
        p = char_dir / "landing_latest.json"
        if char_dir.is_dir() and p.exists():
            _put("landings", char_dir.name, p,
                 oss_fields=["html", "html_filled"])  # html 走 OSS 不進 hub
    for char_dir in sorted(config.CHAT_DIR.iterdir() if config.CHAT_DIR.exists() else []):
        if not char_dir.is_dir():
            continue
        for p in sorted(char_dir.glob("*.json")):
            _put("chats", f"{char_dir.name}__{p.stem}", p)
    styles_file = config.DATA_DIR / "styles.json"
    if styles_file.exists():
        _put("styles", "styles", styles_file)

    # 圖片/上傳源圖 → OSS（已存在跳過）
    try:
        import oss2

        from . import arca_client
        r = arca_client._post(  # noqa: SLF001
            f"{config.ARCA_BASE_URL}/file/tos_credential",
            {"use_public": False, "expires_in": 3600},
            headers=arca_client._headers("zh"), timeout=30)
        cred = arca_client._data(r)
        auth = oss2.StsAuth(cred["access_key_id"], cred["secret_access_key"],
                            cred["session_token"])
        bkt = oss2.Bucket(auth, cred["endpoint"], cred["bucket"])
        for label, folder in (("images", config.IMAGE_DIR), ("uploads", config.UPLOAD_DIR)):
            for p in sorted(folder.glob("*") if folder.exists() else []):
                if not p.is_file() or p.name.endswith(".tmp"):
                    continue
                key = _oss_key(p)
                try:
                    if not bkt.object_exists(key):
                        bkt.put_object(key, p.read_bytes())
                        stats[label] += 1
                except Exception as e:  # noqa: BLE001
                    stats["errors"].append(f"{label}/{p.name}: {e}")
                _bump()
    except Exception as e:  # noqa: BLE001 憑證/網路失敗整段跳過
        stats["errors"].append(f"圖片遷移失敗: {e}")
    return stats


_OSS_BUCKET_CACHE: dict = {"bkt": None, "exp": 0.0}
_OSS_BUCKET_LOCK = threading.Lock()
# STS 憑證簽發有效期 3600s；提前 600s 過期換新，留足時鐘偏移/在途請求餘量。
_OSS_CRED_TTL = 3600 - 600


def _oss_bucket():
    """取 OSS Bucket 客戶端（帶 STS 憑證快取）。

    此前每次呼叫都取一次 STS 臨時憑證——批次刪除/批次取圖時會放大成幾十上百次
    往返，明顯拖慢。憑證有效 3600s，這裡按程式快取 bucket，到期前複用同一個。
    """
    import time as _time
    now = _time.time()
    bkt = _OSS_BUCKET_CACHE["bkt"]
    if bkt is not None and now < _OSS_BUCKET_CACHE["exp"]:
        return bkt
    with _OSS_BUCKET_LOCK:
        bkt = _OSS_BUCKET_CACHE["bkt"]
        if bkt is not None and now < _OSS_BUCKET_CACHE["exp"]:
            return bkt
        import oss2

        from . import arca_client
        r = arca_client._post(  # noqa: SLF001
            f"{config.ARCA_BASE_URL}/file/tos_credential",
            {"use_public": False, "expires_in": 3600},
            headers=arca_client._headers("zh"), timeout=30)
        cred = arca_client._data(r)
        auth = oss2.StsAuth(cred["access_key_id"], cred["secret_access_key"],
                            cred["session_token"])
        bkt = oss2.Bucket(auth, cred["endpoint"], cred["bucket"])
        _OSS_BUCKET_CACHE["bkt"] = bkt
        _OSS_BUCKET_CACHE["exp"] = now + _OSS_CRED_TTL
        return bkt


def delete_oss_prefix(rel_prefix: str) -> int:
    """按 data/ 相對字首刪除 OSS 物件（盡力而為，失敗只 warn）。返回刪除數。

    用於刪除角色時清理 OSS 上的圖片/上傳件，避免永久殘留並被 /img 回源復活。
    """
    return delete_oss_prefixes([rel_prefix])


def delete_oss_prefixes(rel_prefixes: list[str]) -> int:
    """一次性清理多個 data/ 相對字首下的 OSS 物件（盡力而為，失敗只 warn）。

    相比逐字首呼叫 delete_oss_prefix：只取一次 STS 憑證、複用同一個 Bucket，並用
    batch_delete_object 每批最多刪 1000 個 key，把刪除一個角色的 OSS 往返從
    “N 字首 × (取憑證 + 逐物件刪)”壓到“一次取憑證 + 每 1000 個一次批刪”。
    """
    if not arca_storage.enabled() or not rel_prefixes:
        return 0
    try:
        import oss2
        bkt = _oss_bucket()
        n = 0
        batch: list[str] = []
        for rel_prefix in rel_prefixes:
            for obj in oss2.ObjectIterator(bkt, prefix=f"{_OSS_PREFIX}/{rel_prefix}"):
                batch.append(obj.key)
                if len(batch) >= 1000:  # OSS DeleteMultipleObjects 單次上限
                    bkt.batch_delete_objects(batch)
                    n += len(batch)
                    batch = []
        if batch:
            bkt.batch_delete_objects(batch)
            n += len(batch)
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("storage oss 多字首刪除 %s 失敗: %s", rel_prefixes, e)
        return 0


def delete_oss_file(local: Path) -> None:
    """刪除單個本地路徑對應的 OSS 物件（盡力而為）。"""
    if not arca_storage.enabled():
        return
    try:
        _oss_bucket().delete_object(_oss_key(local))
    except Exception as e:  # noqa: BLE001
        log.warning("storage oss 刪除 %s 失敗: %s", local.name, e)


def ensure_file(local: Path) -> bool:
    """本地缺失時從 OSS 拉回。返回檔案最終是否存在。"""
    if local.exists():
        return True
    if not arca_storage.enabled():
        return False
    try:
        data = _oss_bucket().get_object(_oss_key(local)).read()
        _atomic_write_bytes(local, data)  # 原子落盤：併發讀不會拿到截斷檔案
        return True
    except Exception as e:  # noqa: BLE001 未命中/網路失敗都視為不存在
        log.info("storage oss 回源 %s 未命中: %s", local.name, e)
        return False
