"""持久层合成：arca 存储中台(JSON 记录) + arca OSS(图片) 为主，本地文件为缓存。

语义（未配 ARCA_STORAGE_KEY 时 enabled()=False，所有函数退化为纯本地=历史行为）：
- save_json  : 写本地 + put 远端（远端失败只 warn，不阻断本地——可用性优先，迁移可补推）
- load_json  : 本地命中直接用；缺失时从远端拉并回写本地缓存
- list_json  : 本地 glob ∪ 远端 query 合并（远端独有的回写本地缓存）
- delete_json: 双删（远端幂等）
- save_file  : 二进制写本地 + OSS put；ensure_file: 本地缺失时从 OSS 拉回
集合在首次写入时惰性 ensure_collection（幂等），进程内记忆。
"""
import json
import logging
import os
import threading
import uuid
from pathlib import Path

from . import arca_storage, config

log = logging.getLogger("storage")

# OSS 里的文件对象统一前缀（相对 data/ 的路径作 key）
_OSS_PREFIX = "creaction-data"
_ensured: set[str] = set()
_ensured_lock = threading.Lock()


def _ensure(collection: str) -> None:
    with _ensured_lock:
        if collection in _ensured:
            return
    arca_storage.ensure_collection(collection, description="creaction 本地数据接管")
    with _ensured_lock:
        _ensured.add(collection)


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp+rename 原子写：并发读永远看到完整文件，不会读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- JSON 记录
def save_json(collection: str, key: str, obj: dict, local: Path) -> None:
    _atomic_write_text(local, json.dumps(obj, ensure_ascii=False, indent=2))
    if not arca_storage.enabled():
        return
    try:
        _ensure(collection)
        arca_storage.put_record(collection, key, obj)
    except Exception as e:  # noqa: BLE001 远端失败不阻断本地（迁移可补推）
        log.warning("storage put %s/%s 失败: %s", collection, key, e)


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
        log.warning("storage get %s/%s 失败: %s", collection, key, e)
        return None
    if obj is not None:  # 回源命中 → 回写本地缓存
        try:
            _atomic_write_text(local, json.dumps(obj, ensure_ascii=False, indent=2))
        except OSError:
            pass
    return obj


def _query_all(collection: str, match: dict | None) -> list[dict]:
    """分页拉全远端记录（单页 500，无 offset 翻页会静默截断）。"""
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
    """公开的分页拉全（不吞错版）：删除性守卫等需要严格语义的调用方使用。"""
    return _query_all(collection, match)


def list_json(collection: str, local_dir: Path, pattern: str = "*.json",
              match: dict | None = None,
              remote_key_to_local=None) -> dict[str, dict]:
    """返回 {本地key: obj}。本地 glob（key=去扩展名文件名）∪ 远端 query 合并。

    - match: 远端 JSONB 包含过滤（如 {"char_id": cid}），避免整集合拉取与跨角色污染。
    - remote_key_to_local: 远端 key → 本地文件名(不含扩展名) 的映射（远端用复合键
      如 "char__batch" 而本地文件是 "batch.json" 时必传）；返回 None 表示该行不属于
      本目录，跳过。缺省为恒等映射。
    - 远端独有的行回写本地缓存（原子写），文件名用映射后的本地 key。
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
        log.warning("storage query %s 失败: %s", collection, e)
        return out
    for row in rows:
        rkey, obj = row.get("key"), row.get("data")
        if not rkey or not isinstance(obj, dict):
            continue
        lkey = remote_key_to_local(rkey) if remote_key_to_local else rkey
        if not lkey or lkey in out:
            continue
        out[lkey] = obj
        try:  # 远端独有 → 回写本地缓存（换机/冷启动自动恢复）
            _atomic_write_text(local_dir / f"{lkey}.json",
                               json.dumps(obj, ensure_ascii=False, indent=2))
        except OSError:
            pass
    return out


def delete_json(collection: str, key: str, local: Path) -> None:
    """删本地 + 远端。远端删除失败会 raise（否则已删数据会被 list/load 回源"复活"），
    调用方负责把错误暴露给用户或重试。"""
    local.unlink(missing_ok=True)
    if not arca_storage.enabled():
        return
    arca_storage.delete_record(collection, key)


# ---------------------------------------------------------------- 二进制文件(OSS)
def _oss_key(local: Path) -> str:
    """data/ 下的相对路径作 OSS key；data 外的文件用文件名兜底。"""
    try:
        rel = local.resolve().relative_to(config.DATA_DIR.resolve())
        return f"{_OSS_PREFIX}/{rel.as_posix()}"
    except ValueError:
        return f"{_OSS_PREFIX}/misc/{local.name}"


def save_file(local: Path, data: bytes, content_type: str = "application/octet-stream") -> None:
    """写本地 + 上传 OSS 私有桶（远端失败只 warn）。"""
    _atomic_write_bytes(local, data)
    if not arca_storage.enabled():
        return
    try:
        from . import arca_client
        arca_client.tos_upload(data, _oss_key(local), content_type, lang="zh")
    except Exception as e:  # noqa: BLE001
        log.warning("storage oss put %s 失败: %s", local.name, e)


def migrate_all(progress=None) -> dict:
    """把本地 data/ 存量全量推到 arca（JSON→存储中台，图片→OSS）。

    幂等：JSON put 即 upsert；图片按 OSS 已存在跳过。返回逐类统计。
    progress(done_delta) 可选，用于任务进度条。
    """
    if not arca_storage.enabled():
        raise arca_storage.StorageError("未配置 ARCA_STORAGE_KEY，无法迁移")
    stats = {"personas": 0, "post_batches": 0, "ig_batches": 0, "landings": 0,
             "chats": 0, "styles": 0, "images": 0, "uploads": 0, "errors": []}

    def _bump():
        if progress:
            progress(1)

    def _put(coll: str, key: str, local: Path):
        try:
            obj = json.loads(local.read_text(encoding="utf-8"))
            if coll == "styles" and isinstance(obj, list):
                obj = {"styles": obj}  # data 须是 JSON 对象
            if not isinstance(obj, dict):
                return
            _ensure(coll)
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
                continue  # 远端回写的复合键缓存文件，远端已有记录，重迁会繁殖脏键
            else:
                _put("post_batches", f"{char_dir.name}__{p.stem}", p)
    for char_dir in sorted(config.LANDING_DIR.iterdir() if config.LANDING_DIR.exists() else []):
        p = char_dir / "landing_latest.json"
        if char_dir.is_dir() and p.exists():
            _put("landings", char_dir.name, p)
    for char_dir in sorted(config.CHAT_DIR.iterdir() if config.CHAT_DIR.exists() else []):
        if not char_dir.is_dir():
            continue
        for p in sorted(char_dir.glob("*.json")):
            _put("chats", f"{char_dir.name}__{p.stem}", p)
    styles_file = config.DATA_DIR / "styles.json"
    if styles_file.exists():
        _put("styles", "styles", styles_file)

    # 图片/上传源图 → OSS（已存在跳过）
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
    except Exception as e:  # noqa: BLE001 凭证/网络失败整段跳过
        stats["errors"].append(f"图片迁移失败: {e}")
    return stats


def _oss_bucket():
    """取 OSS STS 凭证并构造 Bucket 客户端（内部工具，调用方自行捕获异常）。"""
    import oss2

    from . import arca_client
    r = arca_client._post(  # noqa: SLF001
        f"{config.ARCA_BASE_URL}/file/tos_credential",
        {"use_public": False, "expires_in": 3600},
        headers=arca_client._headers("zh"), timeout=30)
    cred = arca_client._data(r)
    auth = oss2.StsAuth(cred["access_key_id"], cred["secret_access_key"],
                        cred["session_token"])
    return oss2.Bucket(auth, cred["endpoint"], cred["bucket"])


def delete_oss_prefix(rel_prefix: str) -> int:
    """按 data/ 相对前缀删除 OSS 对象（尽力而为，失败只 warn）。返回删除数。

    用于删除角色时清理 OSS 上的图片/上传件，避免永久残留并被 /img 回源复活。
    """
    if not arca_storage.enabled():
        return 0
    try:
        import oss2
        bkt = _oss_bucket()
        n = 0
        for obj in oss2.ObjectIterator(bkt, prefix=f"{_OSS_PREFIX}/{rel_prefix}"):
            bkt.delete_object(obj.key)
            n += 1
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("storage oss 前缀删除 %s 失败: %s", rel_prefix, e)
        return 0


def delete_oss_file(local: Path) -> None:
    """删除单个本地路径对应的 OSS 对象（尽力而为）。"""
    if not arca_storage.enabled():
        return
    try:
        _oss_bucket().delete_object(_oss_key(local))
    except Exception as e:  # noqa: BLE001
        log.warning("storage oss 删除 %s 失败: %s", local.name, e)


def ensure_file(local: Path) -> bool:
    """本地缺失时从 OSS 拉回。返回文件最终是否存在。"""
    if local.exists():
        return True
    if not arca_storage.enabled():
        return False
    try:
        data = _oss_bucket().get_object(_oss_key(local)).read()
        _atomic_write_bytes(local, data)  # 原子落盘：并发读不会拿到截断文件
        return True
    except Exception as e:  # noqa: BLE001 未命中/网络失败都视为不存在
        log.info("storage oss 回源 %s 未命中: %s", local.name, e)
        return False
