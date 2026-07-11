"""arca-i18n HTTP 客户端：JWT / TOS 上传 / 建角色(异步) / 发帖。"""
import json
import re
import threading
import time

import jwt as pyjwt
import requests

from . import config


class ArcaError(Exception):
    pass


# 日志脱敏：凭证类字段只留前 8 位（含 gen_jwt/tos_credential 响应里的 token/秘钥）
_REDACT_JSON = re.compile(
    r'("(?:jwt_token|session_token|secret_access_key|access_key_id|help_header)"\s*:\s*")([^"]{8})[^"]*(")')
_BODY_LIMIT = 4000000


def _redact(text: str) -> str:
    return _REDACT_JSON.sub(r"\1\2…\3", text or "")


def _clip(text: str) -> str:
    text = text or ""
    if len(text) > _BODY_LIMIT:
        return f"{text[:_BODY_LIMIT]}…(截断，共{len(text)}字符)"
    return text


def _post(url: str, payload: dict, headers: dict, timeout: int):
    """所有 arca HTTP 请求的收口：ARCA_DEBUG=1 时打印原始请求/响应（脱敏）。"""
    if config.ARCA_DEBUG:
        print(f"\n--- arca 请求 ---\nPOST {url}")
        for k, v in (headers or {}).items():
            if k.lower() == "authorization":
                v = f"{v[:22]}…<redacted>"
            print(f"{k}: {v}")
        body = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        print(f"\n{_clip(_redact(body))}")
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if config.ARCA_DEBUG:
        print(f"--- arca 响应 HTTP {resp.status_code} ---\n{_clip(_redact(resp.text))}\n")
    return resp


def get_token() -> str:
    """返回裸 JWT（不含 Bearer 前缀）。"""
    if config.ARCA_JWT_MODE == "endpoint":
        if not config.ARCA_BASE_URL:
            raise ArcaError("ARCA_BASE_URL 未配置")
        r = _post(
            f"{config.ARCA_BASE_URL}/internal/tool/gen_jwt_token",
            {"uid": config.ARCA_UID, "expires_in": config.ARCA_JWT_EXPIRES},
            headers=None, timeout=15,
        )
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        tok = data.get("jwt_token")
        if not tok:
            raise ArcaError(f"gen_jwt_token 未返回 token: {r.text[:200]}")
        return tok
    # local 自签
    if not config.ARCA_ACCESS_SECRET:
        raise ArcaError("ARCA_ACCESS_SECRET 未配置（本地自签模式必需）")
    if not config.ARCA_UID:
        raise ArcaError("ARCA_UID 未配置")
    payload = {
        "uid": config.ARCA_UID,
        "exp": int(time.time()) + config.ARCA_JWT_EXPIRES,
    }
    return pyjwt.encode(payload, config.ARCA_ACCESS_SECRET, algorithm="HS256")


def auth_header() -> dict:
    return {"Authorization": f"Bearer {get_token()}"}


def _data(resp) -> dict:
    """取 arca 统一响应壳 {code,msg,data}；code!=0 抛业务错误。

    go-zero 参数解析失败返回 HTTP 400 纯文本、JWT 失败 401 空 body——
    把响应体带进异常，否则只剩 "400 Bad Request" 没法定位。
    """
    if resp.status_code >= 400:
        raise ArcaError(
            f"arca HTTP {resp.status_code} {resp.request.method} {resp.url}: "
            f"{(resp.text or '').strip()[:500]}")
    body = resp.json() or {}
    if body.get("code", 0) not in (0, None):
        raise ArcaError(f"arca 业务错误 code={body.get('code')} msg={body.get('msg')}")
    return body.get("data") or {}


def _region_for_lang(lang: str) -> str:
    """按角色语言解析 X-Region（两位大写国家码）。

    优先级：语言标签自带的地区码（zh-HK/zh-Hant-TW/en-GB）→ ARCA_REGION_BY_LANG
    （精确标签，再退语言主码，如 zh-Hant→zh）→ 全局 ARCA_REGION。
    zh 系归属 TW/HK 等繁中地区（arca 中文统一 zh-Hant；CN 被 RegionBlock 拒 403）。
    """
    tag = (lang or "").strip().replace("_", "-")
    parts = tag.split("-")
    for p in parts[1:]:  # 标签里带地区码就直接用
        if len(p) == 2 and p.isalpha():
            return p.upper()
    by = config.ARCA_REGION_BY_LANG
    region = by.get(tag) or by.get(parts[0], "") or config.ARCA_REGION
    return (region or "").upper()


def _headers(lang: str, idempotency_key: str | None = None) -> dict:
    h = auth_header()
    h["Content-Type"] = "application/json"
    h["X-Language"] = lang or "zh"
    # arca 强校验 X-Region 两位大写国家码（缺失/非法 400）
    region = _region_for_lang(lang)
    if region:
        h["X-Region"] = region
    if config.ARCA_APP_VERSION:
        h["X-App-Version"] = config.ARCA_APP_VERSION
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key
    return h


def _get_task_status(task_id: str, lang: str) -> dict:
    r = _post(
        f"{config.ARCA_BASE_URL}/task/get_status",
        {"task_id": task_id}, headers=_headers(lang), timeout=30,
    )
    return _data(r)


def create_character(form: dict, lang: str, idempotency_key: str | None = None,
                     poll_interval: float = 3.0, timeout: float = 300.0) -> str:
    if not config.ARCA_BASE_URL:
        raise ArcaError("ARCA_BASE_URL 未配置")
    r = _post(
        f"{config.ARCA_BASE_URL}/character/create",
        {"character_create_form": form, "source": "character"},
        headers=_headers(lang, idempotency_key), timeout=60,
    )
    task_id = _data(r).get("task_id")
    if not task_id:
        raise ArcaError(f"create 未返回 task_id: {r.text[:200]}")

    deadline = time.time() + timeout
    while True:
        st = _get_task_status(task_id, lang)
        status = st.get("status")
        if status == "ready":
            result = st.get("result") or "{}"
            cid = (json.loads(result) if isinstance(result, str) else result).get("character_id")
            if not cid:
                raise ArcaError(f"任务成功但无 character_id: {str(result)[:200]}")
            return cid
        if status == "failed":
            raise ArcaError(f"建角色失败 code={st.get('error_code')} msg={st.get('error_message')}")
        if time.time() > deadline:
            raise ArcaError(f"建角色轮询超时 task_id={task_id}")
        if poll_interval:
            time.sleep(poll_interval)


def _get(url: str, headers: dict, timeout: int):
    """GET 请求收口：ARCA_DEBUG=1 时打印原始请求/响应（脱敏）。"""
    if config.ARCA_DEBUG:
        print(f"\n--- arca 请求 ---\nGET {url}")
        for k, v in (headers or {}).items():
            if k.lower() == "authorization":
                v = f"{v[:22]}…<redacted>"
            print(f"{k}: {v}")
    resp = requests.get(url, headers=headers, timeout=timeout)
    if config.ARCA_DEBUG:
        print(f"--- arca 响应 HTTP {resp.status_code} ---\n{_clip(_redact(resp.text))}\n")
    return resp


def get_page_config(lang: str) -> dict:
    """GET /character/page_config：平台的角色配置枚举。

    返回 data：{genders, character_tags, setting_options, species,
    voices, appearance_styles, landing_page_styles}。
    tag 条目形如 {tag_key, tag_name, tag_icon, index?, tag_value?}；
    voices 条目含 voice_id/voice_name/language。结果随 X-Language 本地化。
    """
    if not config.ARCA_BASE_URL:
        raise ArcaError("ARCA_BASE_URL 未配置")
    r = _get(f"{config.ARCA_BASE_URL}/character/page_config",
             headers=_headers(lang), timeout=30)
    return _data(r)


_PAGE_CONFIG_CACHE: dict[str, dict] = {}


def get_page_config_cached(lang: str) -> dict:
    """page_config 的进程内缓存版（枚举很少变，一次同步批量里只拉一次/语言）。"""
    if lang not in _PAGE_CONFIG_CACHE:
        _PAGE_CONFIG_CACHE[lang] = get_page_config(lang)
    return _PAGE_CONFIG_CACHE[lang]


def update_character_basic_info(character_id: str, form: dict, lang: str) -> None:
    """原地更新已同步角色（POST /character/updateBasicInfo，同步接口）。

    后端只消费 name/gender/species/profile/voice_id/opening_prologue/visibility，
    局部更新（非空才覆盖），每次变更插入新 character_version 快照；
    tags/disposition/anonymous_tags/images/landing_page_url 传了也会被忽略。
    """
    if not config.ARCA_BASE_URL:
        raise ArcaError("ARCA_BASE_URL 未配置")
    r = _post(
        f"{config.ARCA_BASE_URL}/character/updateBasicInfo",
        {"character_id": character_id, "character_info": form},
        headers=_headers(lang), timeout=60,
    )
    _data(r)  # 成功返回空 data；业务失败（角色失效/音色不存在/非本人）在此抛 ArcaError


def list_my_characters(lang: str) -> list[dict]:
    """列出当前 uid 名下全部自建角色 [{character_id, name}]（游标翻页拉全）。

    POST /character/list_my_characters；仅返回本人未删角色（读实现核实），
    天然满足「同创建者」条件。
    """
    out: list[dict] = []
    cursor = ""
    for _ in range(50):  # 翻页保险上限
        r = _post(
            f"{config.ARCA_BASE_URL}/character/list_my_characters",
            {"cursor": cursor, "limit": 200},
            headers=_headers(lang), timeout=30,
        )
        data = _data(r)
        for item in data.get("characters") or []:
            bi = (item or {}).get("basic_info") or {}
            if bi.get("character_id"):
                out.append({"character_id": bi["character_id"],
                            "name": (bi.get("name") or "").strip()})
        cursor = data.get("next_cursor") or ""
        if not data.get("has_more") or not cursor:
            break
    return out


def character_exists(character_id: str, lang: str,
                     probe_name: str | None = None,
                     probe_visibility: str = "public") -> bool:
    """判断角色是否存活（软删/失效返回 False）。网络等非业务错误上抛。

    注意：/character/detail 的实现【不过滤 is_deleted】（读了 Go 源码核实：
    GetByCharacterID 仅按 character_id 查，用户自删只置 is_deleted），对软删
    角色会返回成功——不能用它判活。可靠探针是 updateBasicInfo：其实现先查
    IsDeleted/Status，软删/失效会返回「角色不存在/角色已失效」。
    传 probe_name 时用 update 探针（带 name+visibility 的最小更新：值与现状
    相同，仅多插一个内容相同的 version 快照，无业务副作用；visibility 必带，
    否则 update 会把 is_public 无条件置 false）。不传则退回 detail（仅适用于
    「记录被硬删除」的场景，识别不了软删）。
    """
    if probe_name:
        r = _post(
            f"{config.ARCA_BASE_URL}/character/updateBasicInfo",
            {"character_id": character_id,
             "character_info": {"name": probe_name,
                                "visibility": probe_visibility}},
            headers=_headers(lang), timeout=30,
        )
    else:
        r = _post(
            f"{config.ARCA_BASE_URL}/character/detail",
            {"character_id": character_id},
            headers=_headers(lang), timeout=30,
        )
    try:
        _data(r)
        return True
    except ArcaError as e:
        if "角色不存在" in str(e) or "角色已失效" in str(e):
            return False
        raise


def delete_character(character_id: str, lang: str, reason: str = "") -> None:
    """删除 arca 上的角色（POST /character/delete，同步、软删、仅限本人角色）。

    重复删除会返回业务错误「角色不存在」——调用方可视为幂等成功。
    """
    if not config.ARCA_BASE_URL:
        raise ArcaError("ARCA_BASE_URL 未配置")
    r = _post(
        f"{config.ARCA_BASE_URL}/character/delete",
        {"character_id": character_id, "delete_type": 1, "reason": reason},
        headers=_headers(lang), timeout=30,
    )
    _data(r)


def _oss_put_object(endpoint, bucket, key, content, ak, sk, token, content_type):
    """用阿里云 OSS SDK(oss2) + STS 临时凭证 PUT 一个对象。隔离成函数便于测试打桩。

    /file/tos_credential 签发的是阿里云 OSS 的 STS 凭证（后端走 OssHelper），
    必须用 oss2 的 StsAuth 签名直传，不能用火山 TOS SDK。
    """
    import oss2
    if config.ARCA_DEBUG:
        print(f"\n--- OSS PUT ---\nPUT {endpoint} bucket={bucket} key={key} "
              f"content-type={content_type} bytes={len(content)}\n")
    auth = oss2.StsAuth(ak, sk, token)
    # connect/read 超时兜底：oss2 默认无读超时，网络卡死会永久挂住导出线程。
    bucket_obj = oss2.Bucket(
        auth, endpoint, bucket,
        connect_timeout=config.OSS_PUT_TIMEOUT)
    bucket_obj.put_object(
        key, content, headers={"Content-Type": content_type})


# TOS STS 凭证缓存：凭证 expires_in=3600，批量上传时按 (public,lang) 复用，
# 避免每传一张图都往 api.popop.dev 要一次凭证（几百次导出会拖垮吞吐）。
_TOS_CRED_CACHE: dict[tuple, tuple[float, dict]] = {}
_TOS_CRED_LOCK = threading.Lock()
_TOS_CRED_TTL = 1800  # 秒；比 3600 保守，留足直传余量


def _tos_credential(public: bool, lang: str) -> dict:
    key = (bool(public), lang or "")
    now = time.time()
    with _TOS_CRED_LOCK:
        hit = _TOS_CRED_CACHE.get(key)
        if hit and now - hit[0] < _TOS_CRED_TTL:
            return hit[1]
    r = _post(
        f"{config.ARCA_BASE_URL}/file/tos_credential",
        {"use_public": public, "expires_in": 3600},
        headers=_headers(lang), timeout=30,
    )
    cred = _data(r)
    with _TOS_CRED_LOCK:
        _TOS_CRED_CACHE[key] = (now, cred)
    return cred


def tos_upload(data: bytes, object_key: str, content_type: str,
               lang: str, public: bool = False) -> dict:
    """拿 /file/tos_credential 的 OSS STS 凭证，直传对象到阿里云 OSS，返回 StorageObject。

    public=True 用公有桶(落地页 HTML 等需公网直链)，否则私有桶(角色图片，后端签名读取)。
    凭证按 (public,lang) 缓存复用，批量上传不再逐张重新签发。
    """
    if not config.ARCA_BASE_URL:
        raise ArcaError("ARCA_BASE_URL 未配置")
    cred = _tos_credential(public, lang)
    bucket = cred.get("bucket")
    endpoint = cred.get("endpoint")  # 形如 https://oss-ap-northeast-1.aliyuncs.com
    _oss_put_object(
        endpoint, bucket, object_key, data,
        cred.get("access_key_id"), cred.get("secret_access_key"),
        cred.get("session_token"), content_type,
    )
    host = endpoint.replace("https://", "").replace("http://", "")
    url = f"https://{bucket}.{host}/{object_key}"
    return {"bucket_name": bucket, "object_key": object_key,
            "object_type": "image", "url": url}


def create_post(character_id: str, content: str, image_objs: list[dict],
                lang: str, visibility: int | None = None) -> str:
    images = [
        {"image_type": "aigc", "is_main_pic": i == 0, "media": obj}
        for i, obj in enumerate(image_objs or [])
    ]
    payload = {
        "character_id": character_id,
        "content": content or "",
        "images": images,
        "visibility": visibility if visibility is not None else config.ARCA_POST_VISIBILITY,
    }
    r = _post(
        f"{config.ARCA_BASE_URL}/post/create",
        payload, headers=_headers(lang), timeout=60,
    )
    pid = _data(r).get("post_id")
    if not pid:
        raise ArcaError(f"create_post 未返回 post_id: {r.text[:200]}")
    return pid


def set_post_visibility(post_id: str, visibility: int, lang: str) -> None:
    """补偿设置帖子可见性（后端 /post/create 会忽略请求里的 visibility，
    按角色 is_public 推导；只有显式配置覆盖时才需要调本接口）。"""
    r = _post(
        f"{config.ARCA_BASE_URL}/post/update_visibility",
        {"post_id": post_id, "visibility": visibility},
        headers=_headers(lang), timeout=30,
    )
    _data(r)
