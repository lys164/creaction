"""arca-i18n HTTP 客戶端：JWT / TOS 上傳 / 建角色(非同步) / 發帖。"""
import json
import re
import threading
import time

import jwt as pyjwt
import requests

from . import config


class ArcaError(Exception):
    pass


# 日誌脫敏：憑證類欄位只留前 8 位（含 gen_jwt/tos_credential 響應裡的 token/秘鑰）
_REDACT_JSON = re.compile(
    r'("(?:jwt_token|session_token|secret_access_key|access_key_id|help_header)"\s*:\s*")([^"]{8})[^"]*(")')
_BODY_LIMIT = 4000000


def _redact(text: str) -> str:
    return _REDACT_JSON.sub(r"\1\2…\3", text or "")


def _clip(text: str) -> str:
    text = text or ""
    if len(text) > _BODY_LIMIT:
        return f"{text[:_BODY_LIMIT]}…(截斷，共{len(text)}字元)"
    return text


def _post(url: str, payload: dict, headers: dict, timeout: int):
    """所有 arca HTTP 請求的收口：ARCA_DEBUG=1 時列印原始請求/響應（脫敏）。"""
    if config.ARCA_DEBUG:
        print(f"\n--- arca 請求 ---\nPOST {url}")
        for k, v in (headers or {}).items():
            if k.lower() == "authorization":
                v = f"{v[:22]}…<redacted>"
            print(f"{k}: {v}")
        body = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        print(f"\n{_clip(_redact(body))}")
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if config.ARCA_DEBUG:
        print(f"--- arca 響應 HTTP {resp.status_code} ---\n{_clip(_redact(resp.text))}\n")
    return resp


def get_token() -> str:
    """返回裸 JWT（不含 Bearer 字首）。"""
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
    # local 自籤
    if not config.ARCA_ACCESS_SECRET:
        raise ArcaError("ARCA_ACCESS_SECRET 未配置（本地自籤模式必需）")
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
    """取 arca 統一響應殼 {code,msg,data}；code!=0 拋業務錯誤。

    go-zero 引數解析失敗返回 HTTP 400 純文字、JWT 失敗 401 空 body——
    把響應體帶進異常，否則只剩 "400 Bad Request" 沒法定位。
    """
    if resp.status_code >= 400:
        raise ArcaError(
            f"arca HTTP {resp.status_code} {resp.request.method} {resp.url}: "
            f"{(resp.text or '').strip()[:500]}")
    body = resp.json() or {}
    if body.get("code", 0) not in (0, None):
        raise ArcaError(f"arca 業務錯誤 code={body.get('code')} msg={body.get('msg')}")
    return body.get("data") or {}


def _region_for_lang(lang: str) -> str:
    """按角色語言解析 X-Region（兩位大寫國家碼）。

    優先順序：語言標籤自帶的地區碼（zh-HK/zh-Hant-TW/en-GB）→ ARCA_REGION_BY_LANG
    （精確標籤，再退語言主碼，如 zh-Hant→zh）→ 全域性 ARCA_REGION。
    zh 系歸屬 TW/HK 等繁中地區（arca 中文統一 zh-Hant；CN 被 RegionBlock 拒 403）。
    """
    tag = (lang or "").strip().replace("_", "-")
    parts = tag.split("-")
    for p in parts[1:]:  # 標籤裡帶地區碼就直接用
        if len(p) == 2 and p.isalpha():
            return p.upper()
    by = config.ARCA_REGION_BY_LANG
    region = by.get(tag) or by.get(parts[0], "") or config.ARCA_REGION
    return (region or "").upper()


def _headers(lang: str, idempotency_key: str | None = None) -> dict:
    h = auth_header()
    h["Content-Type"] = "application/json"
    h["X-Language"] = lang or "zh"
    # arca 強校驗 X-Region 兩位大寫國家碼（缺失/非法 400）
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
                raise ArcaError(f"任務成功但無 character_id: {str(result)[:200]}")
            return cid
        if status == "failed":
            raise ArcaError(f"建角色失敗 code={st.get('error_code')} msg={st.get('error_message')}")
        if time.time() > deadline:
            raise ArcaError(f"建角色輪詢超時 task_id={task_id}")
        if poll_interval:
            time.sleep(poll_interval)


def import_character(request: dict, lang: str) -> dict:
    """POST /internal/import/character（SKILL 的內部導入接口，同步返回）。

    request 為完整 ImportCharacterReq {external_character_id, provider,
    character_create_form}。該路由組無簽名校驗，但仍必須帶創建者 JWT——全局
    OptionalJWT 中間件把 uid 注入 context，缺失會報「未授權：無法獲取用戶ID」。
    (provider, external_character_id) 是導入幂等鍵：重複導入命中同一角色，
    new_created=false。返回 data：{character_id, new_created, character:{...}}。
    """
    if not config.ARCA_BASE_URL:
        raise ArcaError("ARCA_BASE_URL 未配置")
    r = _post(
        f"{config.ARCA_BASE_URL}/internal/import/character",
        request, headers=_headers(lang), timeout=120,
    )
    return _data(r)


def _get(url: str, headers: dict, timeout: int):
    """GET 請求收口：ARCA_DEBUG=1 時列印原始請求/響應（脫敏）。"""
    if config.ARCA_DEBUG:
        print(f"\n--- arca 請求 ---\nGET {url}")
        for k, v in (headers or {}).items():
            if k.lower() == "authorization":
                v = f"{v[:22]}…<redacted>"
            print(f"{k}: {v}")
    resp = requests.get(url, headers=headers, timeout=timeout)
    if config.ARCA_DEBUG:
        print(f"--- arca 響應 HTTP {resp.status_code} ---\n{_clip(_redact(resp.text))}\n")
    return resp


def get_page_config(lang: str) -> dict:
    """GET /character/page_config：平臺的角色配置列舉。

    返回 data：{genders, character_tags, setting_options, species,
    voices, appearance_styles, landing_page_styles}。
    tag 條目形如 {tag_key, tag_name, tag_icon, index?, tag_value?}；
    voices 條目含 voice_id/voice_name/language。結果隨 X-Language 本地化。
    """
    if not config.ARCA_BASE_URL:
        raise ArcaError("ARCA_BASE_URL 未配置")
    r = _get(f"{config.ARCA_BASE_URL}/character/page_config",
             headers=_headers(lang), timeout=30)
    return _data(r)


_PAGE_CONFIG_CACHE: dict[str, dict] = {}


def get_page_config_cached(lang: str) -> dict:
    """page_config 的程式內快取版（列舉很少變，一次同步批次裡只拉一次/語言）。"""
    if lang not in _PAGE_CONFIG_CACHE:
        _PAGE_CONFIG_CACHE[lang] = get_page_config(lang)
    return _PAGE_CONFIG_CACHE[lang]


def update_character_basic_info(character_id: str, form: dict, lang: str) -> None:
    """原地更新已同步角色（POST /character/updateBasicInfo，同步介面）。

    後端只消費 name/gender/species/profile/voice_id/opening_prologue/visibility，
    區域性更新（非空才覆蓋），每次變更插入新 character_version 快照；
    tags/disposition/anonymous_tags/images/landing_page_url 傳了也會被忽略。
    """
    if not config.ARCA_BASE_URL:
        raise ArcaError("ARCA_BASE_URL 未配置")
    r = _post(
        f"{config.ARCA_BASE_URL}/character/updateBasicInfo",
        {"character_id": character_id, "character_info": form},
        headers=_headers(lang), timeout=60,
    )
    _data(r)  # 成功返回空 data；業務失敗（角色失效/音色不存在/非本人）在此拋 ArcaError


def list_my_characters(lang: str) -> list[dict]:
    """列出當前 uid 名下全部自建角色 [{character_id, name}]（遊標翻頁拉全）。

    POST /character/list_my_characters；僅返回本人未刪角色（讀實現核實），
    天然滿足「同建立者」條件。
    """
    out: list[dict] = []
    cursor = ""
    for _ in range(50):  # 翻頁保險上限
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
    """判斷角色是否存活（軟刪/失效返回 False）。網路等非業務錯誤上拋。

    注意：/character/detail 的實現【不過濾 is_deleted】（讀了 Go 原始碼核實：
    GetByCharacterID 僅按 character_id 查，使用者自刪只置 is_deleted），對軟刪
    角色會返回成功——不能用它判活。可靠探針是 updateBasicInfo：其實現先查
    IsDeleted/Status，軟刪/失效會返回「角色不存在/角色已失效」。
    傳 probe_name 時用 update 探針（帶 name+visibility 的最小更新：值與現狀
    相同，僅多插一個內容相同的 version 快照，無業務副作用；visibility 必帶，
    否則 update 會把 is_public 無條件置 false）。不傳則退回 detail（僅適用於
    「記錄被硬刪除」的場景，識別不了軟刪）。
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
    """刪除 arca 上的角色（POST /character/delete，同步、軟刪、僅限本人角色）。

    重複刪除會返回業務錯誤「角色不存在」——呼叫方可視為冪等成功。
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
    """用阿里雲 OSS SDK(oss2) + STS 臨時憑證 PUT 一個物件。隔離成函式便於測試打樁。

    /file/tos_credential 簽發的是阿里雲 OSS 的 STS 憑證（後端走 OssHelper），
    必須用 oss2 的 StsAuth 簽名直傳，不能用火山 TOS SDK。
    """
    import oss2
    if config.ARCA_DEBUG:
        print(f"\n--- OSS PUT ---\nPUT {endpoint} bucket={bucket} key={key} "
              f"content-type={content_type} bytes={len(content)}\n")
    auth = oss2.StsAuth(ak, sk, token)
    # connect/read 超時兜底：oss2 預設無讀超時，網路卡死會永久掛住匯出執行緒。
    bucket_obj = oss2.Bucket(
        auth, endpoint, bucket,
        connect_timeout=config.OSS_PUT_TIMEOUT)
    bucket_obj.put_object(
        key, content, headers={"Content-Type": content_type})


# TOS STS 憑證快取：憑證 expires_in=3600，批次上傳時按 (public,lang) 複用，
# 避免每傳一張圖都往 api.popop.dev 要一次憑證（幾百次匯出會拖垮吞吐）。
_TOS_CRED_CACHE: dict[tuple, tuple[float, dict]] = {}
_TOS_CRED_LOCK = threading.Lock()
_TOS_CRED_TTL = 1800  # 秒；比 3600 保守，留足直傳餘量


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
    """拿 /file/tos_credential 的 OSS STS 憑證，直傳物件到阿里雲 OSS，返回 StorageObject。

    public=True 用公有桶(落地頁 HTML 等需公網直鏈)，否則私有桶(角色圖片，後端簽名讀取)。
    憑證按 (public,lang) 快取複用，批次上傳不再逐張重新簽發。
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
    cdn_domain = (str(cred.get("cdn_domain") or "").strip()
                  .removeprefix("https://").removeprefix("http://").rstrip("/"))
    # importing-arca-character 要求落地頁及頁內資產使用 public bucket 的 CDN URL。
    # 私有主圖仍保留 bucket/object_key，URL 只是讀取兜底。
    url = (f"https://{cdn_domain}/{object_key}" if public and cdn_domain
           else f"https://{bucket}.{host}/{object_key}")
    return {"bucket_name": bucket, "object_key": object_key,
            "object_type": "image", "url": url}


def public_tos_hosts(lang: str) -> set[str]:
    """Return hosts that identify the current public OSS bucket/CDN."""
    cred = _tos_credential(True, lang)
    hosts: set[str] = set()
    cdn_domain = str(cred.get("cdn_domain") or "").strip()
    if cdn_domain:
        hosts.add(cdn_domain.removeprefix("https://").removeprefix("http://").split("/")[0])
    endpoint = str(cred.get("endpoint") or "").replace("https://", "").replace("http://", "")
    bucket = str(cred.get("bucket") or "").strip()
    if bucket and endpoint:
        hosts.add(f"{bucket}.{endpoint}")
    return hosts


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
    """補償設定帖子可見性（後端 /post/create 會忽略請求裡的 visibility，
    按角色 is_public 推導；只有顯式配置覆蓋時才需要調本介面）。"""
    r = _post(
        f"{config.ARCA_BASE_URL}/post/update_visibility",
        {"post_id": post_id, "visibility": visibility},
        headers=_headers(lang), timeout=30,
    )
    _data(r)
