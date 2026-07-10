"""Global configuration for the POPOP production pipeline."""
import json
import os
from pathlib import Path

# ---- API (APIMart / aiuxu) ----
# 国内可用域名（apimart.ai 海外 IP 在国内可能被墙，优先用全球域名）。
# 第一个为默认；client 会在连接失败时按顺序故障切换到后续域名。
API_BASES = os.environ.get(
    "POPOP_API_BASES",
    "https://api.aiuxu.com/v1,https://api.apib.ai/v1,"
    "https://api.aishuch.com/v1,https://api.apimart.ai/v1",
).split(",")
API_BASES = [b.strip() for b in API_BASES if b.strip()]
API_BASE = os.environ.get("POPOP_API_BASE", API_BASES[0])
API_KEY = os.environ.get("POPOP_API_KEY", "sk-ucowuTBV99jgB3CXhGJw3MTGPsqNSEF6zAZFYCjyfLZOuVxR")
_DEFAULT_API_PROVIDERS = [{"base": b, "key": API_KEY} for b in API_BASES]


def _load_provider_pool(env_name: str, fallback: list[dict]) -> list[dict]:
    """Load provider pool from env JSON.

    Expected JSON:
      [{"base":"https://api.example.com/v1","key":"sk-..."}]
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return fallback
    try:
        providers = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{env_name} must be valid JSON") from e
    out = []
    for item in providers:
        if not isinstance(item, dict):
            continue
        base = str(item.get("base", "")).strip().rstrip("/")
        key = str(item.get("key", "")).strip()
        if base and key:
            # 透传 base/key 以外的字段（如 "kind":"kie"），供 api_client 分派协议
            p = dict(item)
            p["base"] = base
            p["key"] = key
            out.append(p)
    return out or fallback


# OpenAI/APIMart-compatible provider pools. Tasks are round-robin distributed
# across these providers; image polling stays on the provider that accepted
# the task. KIE uses a different image task API, so it should not be added here
# unless wrapped to this same protocol.
API_PROVIDERS = _load_provider_pool("POPOP_API_PROVIDERS", _DEFAULT_API_PROVIDERS)
LLM_API_PROVIDERS = _load_provider_pool("POPOP_LLM_API_PROVIDERS", API_PROVIDERS)
IMAGE_API_PROVIDERS = _load_provider_pool("POPOP_IMAGE_API_PROVIDERS", API_PROVIDERS)

# ---- KIE (kie.ai) 供应商：与 APIMart 协议不同（chat 把模型名放进 URL 路径；
# 图片走 /api/v1/jobs/createTask + /recordInfo 异步轮询）。api_client 依据
# provider 的 "kind":"kie" 标记自动切换协议。设置 POPOP_KIE_KEY 即把 KIE
# 同时挂进 LLM 池与图片池做分流；也可用 POPOP_*_API_PROVIDERS JSON 手动加带
# "kind":"kie" 的条目。
KIE_BASE = os.environ.get("POPOP_KIE_BASE", "https://api.kie.ai").rstrip("/")
KIE_KEY = os.environ.get("POPOP_KIE_KEY", "").strip()
# KIE 上对应现有 gemini-3.1-pro-preview / gemini-3.5-flash 的模型名（型号齐全）。
# KIE 的 chat 把模型名放进 URL 路径，且路径段与 body 的 model 可能不同
# （如 flash：路径 gemini-3-5-flash-openai、body gemini-3-5-flash），故分开配。
KIE_LLM_MODEL = os.environ.get("POPOP_KIE_LLM_MODEL", "gemini-3.1-pro")
KIE_LLM_PATH_PRO = os.environ.get("POPOP_KIE_LLM_PATH_PRO", "gemini-3.1-pro")
KIE_LLM_MODEL_PRO = os.environ.get("POPOP_KIE_LLM_MODEL_PRO", "gemini-3.1-pro")
KIE_LLM_PATH_FLASH = os.environ.get("POPOP_KIE_LLM_PATH_FLASH", "gemini-3-5-flash-openai")
KIE_LLM_MODEL_FLASH = os.environ.get("POPOP_KIE_LLM_MODEL_FLASH", "gemini-3-5-flash")
# 出图与现有 gpt-image-2 同源；按有无参考图切 t2i / i2i（见 api_client）。
KIE_IMAGE_MODEL_T2I = os.environ.get("POPOP_KIE_IMAGE_MODEL_T2I", "gpt-image-2-text-to-image")
KIE_IMAGE_MODEL_I2I = os.environ.get("POPOP_KIE_IMAGE_MODEL_I2I", "gpt-image-2-image-to-image")

if KIE_KEY:
    _kie_provider = {"base": KIE_BASE, "key": KIE_KEY, "kind": "kie"}
    if not any(p.get("kind") == "kie" for p in LLM_API_PROVIDERS):
        LLM_API_PROVIDERS = LLM_API_PROVIDERS + [_kie_provider]
    if not any(p.get("kind") == "kie" for p in IMAGE_API_PROVIDERS):
        IMAGE_API_PROVIDERS = IMAGE_API_PROVIDERS + [_kie_provider]

# ---- bbww (api.bbww.top) 优先供应商 ----
# 全 OpenAI 兼容：chat 走 /v1/chat/completions（与现有链路一致）；出图走同步
# /v1/images/generations、改图走 /v1/images/edits（区别于 APIMart 的异步 submit+
# poll，见 api_client 的 "kind":"bbww" 分支）。设置 POPOP_BBWW_KEY 即把 bbww
# 以【最高优先级】挂进 LLM 池与图片池：每次请求先打 bbww（更快），失败再回退
# 到其余供应商。图片模型默认 gpt-image-2（bbww 上支持、最高 4K）；注意该模型需
# token 所在【分组】已开通对应通道，否则 New API 会返回 "No available channel for
# model ... under group ..."（分组未配通道，而非模型不存在），此时会自动回退到池中
# 其它供应商。可用 POPOP_BBWW_IMAGE_MODEL 覆盖为该分组实际可达的型号。
BBWW_BASE = os.environ.get("POPOP_BBWW_BASE", "https://api.bbww.top/v1").rstrip("/")
BBWW_IMAGE_MODEL = os.environ.get("POPOP_BBWW_IMAGE_MODEL", "gpt-image-2")
# 支持多个 bbww key 分摊限流：POPOP_BBWW_KEYS（逗号分隔）优先，其次单个 POPOP_BBWW_KEY。
# 多个 key 都以最高优先级挂入，且在优先层内部 round-robin（见 api_client._ordered_providers），
# 让请求把额度分摊到不同 key 上，减少 429。
_bbww_keys = [
    k.strip() for k in
    (os.environ.get("POPOP_BBWW_KEYS", "") or os.environ.get("POPOP_BBWW_KEY", "")).split(",")
    if k.strip()
]
# bbww 是否挂进 LLM 池：默认开；设 POPOP_BBWW_LLM=0 关闭。
# 当 bbww 分组没有开通 gemini 通道时，它对每次 LLM 调用都返回 503（约 2.5s/次），
# 排在最高优先级反而使每次 chat 白白多等数秒 → 关掉可显著加速文本/视觉调用。
_BBWW_IN_LLM = os.environ.get("POPOP_BBWW_LLM", "1").strip().lower() not in ("0", "false", "no")
if _BBWW_IN_LLM and _bbww_keys and not any(p.get("kind") == "bbww" for p in LLM_API_PROVIDERS):
    _bbww_llms = [{"base": BBWW_BASE, "key": k, "kind": "bbww", "priority": True}
                  for k in _bbww_keys]
    LLM_API_PROVIDERS = _bbww_llms + LLM_API_PROVIDERS
# bbww 是否参与【出图】：默认关（设 POPOP_BBWW_IMAGE=1 才挂进图片池）。
# 出图改由 OpenAI 直连 + lk888（见 POPOP_EXTRA_IMAGE_PROVIDERS）承担，bbww 只出文本。
_BBWW_IN_IMAGE = os.environ.get("POPOP_BBWW_IMAGE", "0").strip().lower() not in ("0", "false", "no")
if _BBWW_IN_IMAGE and _bbww_keys and not any(p.get("kind") == "bbww" for p in IMAGE_API_PROVIDERS):
    _bbww_imgs = [{"base": BBWW_BASE, "key": k, "kind": "bbww", "priority": True,
                   "image_model": BBWW_IMAGE_MODEL} for k in _bbww_keys]
    IMAGE_API_PROVIDERS = _bbww_imgs + IMAGE_API_PROVIDERS

# ---- bbww 出【文本】：走 Gemini 原生协议扩容 LLM 池 ----
# bbww 的分组没开 OpenAI 兼容的 gemini 通道（/v1/chat/completions 报 503），
# 但 gemini 挂在【原生协议】下可用：POST {root}/v1beta/models/{model}:generateContent
# + header x-goog-api-key（实测 gemini-3.1-pro-preview ~3s 出文）。故以 kind=gemini
# 原生方式把这些 key 以【最高优先级】挂进 LLM 池，与 gemini 池一起分摊生文、提吞吐。
# 用 POPOP_BBWW_GEMINI_KEYS（逗号分隔）指定；留空则回退用 _bbww_keys。设
# POPOP_BBWW_GEMINI=0 可关闭。base 取根域（去掉 /v1，原生路径在根下）。
_BBWW_GEMINI_ON = os.environ.get("POPOP_BBWW_GEMINI", "1").strip().lower() not in ("0", "false", "no")
_bbww_gemini_keys = [
    k.strip() for k in os.environ.get("POPOP_BBWW_GEMINI_KEYS", "").split(",") if k.strip()
] or _bbww_keys
_bbww_root = BBWW_BASE[:-3].rstrip("/") if BBWW_BASE.endswith("/v1") else BBWW_BASE.rstrip("/")
if _BBWW_GEMINI_ON and _bbww_gemini_keys and not any(
        p.get("kind") == "gemini" for p in LLM_API_PROVIDERS):
    _bbww_gemini = [{"base": _bbww_root, "key": k, "kind": "gemini", "priority": True}
                    for k in _bbww_gemini_keys]
    LLM_API_PROVIDERS = _bbww_gemini + LLM_API_PROVIDERS
# 向后兼容：保留 BBWW_KEY 名（取第一个），个别地方可能直接引用。
BBWW_KEY = _bbww_keys[0] if _bbww_keys else ""
LLM_MODEL = os.environ.get("POPOP_LLM_MODEL", "gemini-3.1-pro-preview")
CHAT_MODEL = os.environ.get("POPOP_CHAT_MODEL", "gemini-3.5-flash")
IMAGE_MODEL = os.environ.get("POPOP_IMAGE_MODEL", "gpt-image-2")

# 出图「全平摊」round-robin：把全部出图渠道（openai/lk888 同步 + apimart×4/kie 异步）
# 当成一个环均匀轮转分发，而非高优先层先命中、兜底层闲置。默认开；设 0 回退分层优先。
IMAGE_FLAT_ROUND_ROBIN = os.environ.get(
    "POPOP_IMAGE_FLAT_RR", "1").strip().lower() not in ("0", "false", "no")

# ---- 额外同步出图供应商（OpenAI 兼容 /images/generations，返回 b64/url）----
# 通过 POPOP_EXTRA_IMAGE_PROVIDERS（JSON 数组）注入，复用 bbww 同步出图分支
# （kind=bbww），以最高优先级挂进图片池并与其它同步供应商 round-robin 分摊，
# 用于给出图扩容/提速。每个元素：{"base":".../v1","key":"sk-...","image_model":"gpt-image-1"}。
# 例：[{"base":"https://api.openai.com/v1","key":"sk-...","image_model":"gpt-image-1"},
#      {"base":"https://api.lk888.ai/v1","key":"sk-...","image_model":"gpt-image-2"}]
_extra_imgs_raw = os.environ.get("POPOP_EXTRA_IMAGE_PROVIDERS", "").strip()
if _extra_imgs_raw:
    try:
        _extra = json.loads(_extra_imgs_raw)
    except json.JSONDecodeError as e:
        raise ValueError("POPOP_EXTRA_IMAGE_PROVIDERS must be valid JSON") from e
    _extra_providers = []
    for it in (_extra or []):
        if not isinstance(it, dict):
            continue
        base = str(it.get("base", "")).strip().rstrip("/")
        key = str(it.get("key", "")).strip()
        if not (base and key):
            continue
        _extra_providers.append({
            "base": base, "key": key, "kind": "bbww", "priority": True,
            "image_model": str(it.get("image_model") or IMAGE_MODEL).strip(),
        })
    if _extra_providers:
        IMAGE_API_PROVIDERS = _extra_providers + IMAGE_API_PROVIDERS

# ---- Embeddings (火山方舟 Ark，用于灵感库语义检索) ----
# Ark 的向量模型走 /embeddings/multimodal 端点、返回 data.embedding、不支持真批量，
# 与标准 OpenAI /embeddings 不同，api_client.embed 会据 base 自动切换协议。
EMBED_BASE = os.environ.get("POPOP_EMBED_BASE", "https://ark.cn-beijing.volces.com/api/v3")
EMBED_KEY = os.environ.get("POPOP_EMBED_KEY", "")
EMBED_MODEL = os.environ.get("POPOP_EMBED_MODEL", "doubao-embedding-vision-251215")

# Image generation defaults
IMAGE_SIZE_COVER = "3:4"      # portrait cover
IMAGE_RESOLUTION = "2k"
IMAGE_SIZE_POST = "3:4"

# Polling
TASK_POLL_INTERVAL = 5        # seconds between task polls
TASK_POLL_TIMEOUT = 360       # max seconds to wait for one image

# Concurrency for batch operations. bbww 优先链路更快、可承受更高并发，
# 默认拉到 90（可用 POPOP_MAX_WORKERS 覆盖）。
MAX_WORKERS = int(os.environ.get("POPOP_MAX_WORKERS", "90"))

# ---- Paths ----
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
PERSONA_DIR = DATA_DIR / "personas"
POST_DIR = DATA_DIR / "posts"
IMAGE_DIR = DATA_DIR / "images"
LANDING_DIR = DATA_DIR / "landing"
CHAT_DIR = DATA_DIR / "chat"
WEB_DIR = ROOT / "web"

for _d in (UPLOAD_DIR, PERSONA_DIR, POST_DIR, IMAGE_DIR, LANDING_DIR, CHAT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---- Languages ----
LANGUAGES = ["zh", "ja", "ko", "en"]
LANGUAGE_NAMES = {
    "zh": "简体中文",
    "ja": "日本語",
    "ko": "한국어",
    "en": "English",
}

# Per-language native instruction + locale/culture guidance.
# Every downstream prompt for a given character runs ONLY in that language,
# so output reads like it was authored by a native, not translated.
LANGUAGE_NATIVE = {
    "zh": "请全程使用简体中文输出。",
    "ja": "すべて日本語で出力してください。",
    "ko": "모든 내용을 한국어로 출력하세요.",
    "en": "Write everything in English.",
}

LOCALE_GUIDE = {
    "zh": (
        "面向中文简体（中国大陆）受众。用地道的中文网络/生活语感，符合小红书·微博·"
        "豆瓣那种表达；网络用语、语气词、emoji 自然使用。不要翻译腔，不要直译外语句式。"
        "金额用人民币语境，地点/品牌/饮食/作息贴近中国大陆日常。"
    ),
    "ja": (
        "日本語ネイティブ（日本）向け。Instagram・X・Threads の日本のユーザーが書く"
        "ような自然な口語・絵文字・ハッシュタグ感で。翻訳調は禁止。敬語/タメ口は"
        "キャラの口調に合わせる。地名・ブランド・食事・生活リズムは日本の日常に寄せる。"
    ),
    "ko": (
        "한국어 네이티브(대한민국) 대상. 인스타그램·스레드(Threads)·X 한국 유저가 쓰는 "
        "자연스러운 구어체, 신조어, 이모지, 줄임말 감성으로. 번역체 절대 금지. 반말/존댓말은 "
        "캐릭터 말투에 맞춤. 지명·브랜드·음식·생활 리듬은 한국 일상에 맞게. "
        "스레드 특유의 '예쁘게 발광하는' 자조·드립 감성을 살릴 것."
    ),
    "en": (
        "For native English speakers. Natural, casual social-media English like real "
        "Instagram/Threads users — contractions, light slang, emojis where natural. "
        "No translationese, no stiff phrasing. Localize places, brands, food, and daily "
        "rhythms to an English-speaking everyday context."
    ),
}


def lang_name(lang: str) -> str:
    return LANGUAGE_NAMES.get(lang, lang)


def lang_directive(lang: str) -> str:
    """Combined native + locale guidance injected into every prompt."""
    return f"{LANGUAGE_NATIVE.get(lang, '')} {LOCALE_GUIDE.get(lang, '')}".strip()


# ---- arca-i18n 同步配置 ----
ARCA_BASE_URL = os.environ.get("ARCA_BASE_URL", "https://api.popop.dev/").rstrip("/")
ARCA_UID = os.environ.get("ARCA_UID", "608e5d3149e448c2a67e7ae2dfaea4f7") # hello@popop.ai
# token 获取方式：local=用共享密钥本地 PyJWT 自签；endpoint=调内网 /internal/tool/gen_jwt_token
ARCA_JWT_MODE = os.environ.get("ARCA_JWT_MODE", "endpoint")
ARCA_ACCESS_SECRET = os.environ.get("ARCA_ACCESS_SECRET", "")
ARCA_JWT_EXPIRES = int(os.environ.get("ARCA_JWT_EXPIRES", "2592000"))  # 30 天
ARCA_REGION = os.environ.get("ARCA_REGION", "KR")
# 角色语言 → X-Region 映射（两位大写国家码；CN 会被 arca RegionBlock 拒 403，勿用）。
# env 传 JSON 覆盖，如 {"zh":"TW","ja":"JP","ko":"KR","en":"US"}；查不到回退 ARCA_REGION。
_DEFAULT_REGION_BY_LANG = {
    "zh": "TW",        # arca 中文统一 zh-Hant(繁中)，归属 TW/HK 等繁中地区；CN 被拦
    "zh-Hant": "TW",
    "ja": "JP",
    "ko": "KR",
    "en": "US",
}
try:
    ARCA_REGION_BY_LANG = {
        str(k): str(v).upper()
        for k, v in json.loads(os.environ.get("ARCA_REGION_BY_LANG", "") or "{}").items()
    } or _DEFAULT_REGION_BY_LANG
except (json.JSONDecodeError, AttributeError):
    ARCA_REGION_BY_LANG = _DEFAULT_REGION_BY_LANG
ARCA_APP_VERSION = os.environ.get("ARCA_APP_VERSION", "")
ARCA_POST_VISIBILITY = int(os.environ.get("ARCA_POST_VISIBILITY", "0"))  # 0=跟随角色可见性(推荐)；1公开2好友3私密=显式覆盖
ARCA_SYNC_LANDING = os.environ.get("ARCA_SYNC_LANDING", "1") not in ("0", "false", "False", "")
ARCA_TOS_BUCKET_PUBLIC = os.environ.get("ARCA_TOS_BUCKET_PUBLIC", "bucket-popop-i18n-prod")  # 落地页 HTML 用公有桶（留空则用凭证返回的 bucket）
# 调试：打印 arca 每次 HTTP 原始请求/响应（Authorization 与凭证字段自动脱敏）
ARCA_DEBUG = os.environ.get("ARCA_DEBUG", "1") not in ("0", "false", "False", "")
# arca 存储中台（通用 JSONB 集合存储）数据面 API Key（sk_...，在 /admin/storage_hub 开通）。
# 配置后本地 JSON 记录/图片以 arca 为主存、本地为缓存；留空则纯本地存储（历史行为）。
ARCA_STORAGE_KEY = os.environ.get("ARCA_STORAGE_KEY", "sk_5c581cae262b4f54b838246942dd30de3375f9d3f283df24424d9f09502615cb")

