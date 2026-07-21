"""Global configuration for the POPOP production pipeline."""
import json
import os
from pathlib import Path

# ---- API (aiuxu / apib / aishuch，APIMart 相容中轉) ----
# 國內可用域名（原 apimart.ai 海外 IP 在國內可能被牆，已移除，統一走這三個相容域名）。
# 第一個為預設；client 會在連線失敗時按順序故障切換到後續域名。
API_BASES = os.environ.get(
    "POPOP_API_BASES",
    "https://api.apib.ai/v1,https://api.aiuxu.com/v1,https://api.aishuch.com/v1",
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
            # 透傳 base/key 以外的欄位（如 "kind":"kie"），供 api_client 分派協議
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

# ---- KIE (kie.ai) 供應商：與 APIMart 協議不同（chat 把模型名放進 URL 路徑；
# 圖片走 /api/v1/jobs/createTask + /recordInfo 非同步輪詢）。api_client 依據
# provider 的 "kind":"kie" 標記自動切換協議。設定 POPOP_KIE_KEY 即把 KIE
# 同時掛進 LLM 池與圖片池做分流；也可用 POPOP_*_API_PROVIDERS JSON 手動加帶
# "kind":"kie" 的條目。
KIE_BASE = os.environ.get("POPOP_KIE_BASE", "https://api.kie.ai").rstrip("/")
KIE_KEY = os.environ.get("POPOP_KIE_KEY", "").strip()
# KIE 上對應現有 gemini-3.1-pro-preview / gemini-3.5-flash 的模型名（型號齊全）。
# KIE 的 chat 把模型名放進 URL 路徑，且路徑段與 body 的 model 可能不同
# （如 flash：路徑 gemini-3-5-flash-openai、body gemini-3-5-flash），故分開配。
KIE_LLM_MODEL = os.environ.get("POPOP_KIE_LLM_MODEL", "gemini-3.1-pro")
KIE_LLM_PATH_PRO = os.environ.get("POPOP_KIE_LLM_PATH_PRO", "gemini-3.1-pro")
KIE_LLM_MODEL_PRO = os.environ.get("POPOP_KIE_LLM_MODEL_PRO", "gemini-3.1-pro")
KIE_LLM_PATH_FLASH = os.environ.get("POPOP_KIE_LLM_PATH_FLASH", "gemini-3-5-flash-openai")
KIE_LLM_MODEL_FLASH = os.environ.get("POPOP_KIE_LLM_MODEL_FLASH", "gemini-3-5-flash")
# 出圖與現有 gpt-image-2 同源；按有無參考圖切 t2i / i2i（見 api_client）。
KIE_IMAGE_MODEL_T2I = os.environ.get("POPOP_KIE_IMAGE_MODEL_T2I", "gpt-image-2-text-to-image")
KIE_IMAGE_MODEL_I2I = os.environ.get("POPOP_KIE_IMAGE_MODEL_I2I", "gpt-image-2-image-to-image")

# KIE 是否進【圖片池】：預設進；設 POPOP_KIE_IMAGE=0 關閉（如 kie 出圖餘額不足/不可用時）。
_KIE_IN_IMAGE = os.environ.get("POPOP_KIE_IMAGE", "1").strip().lower() not in ("0", "false", "no")
if KIE_KEY:
    _kie_provider = {"base": KIE_BASE, "key": KIE_KEY, "kind": "kie"}
    if not any(p.get("kind") == "kie" for p in LLM_API_PROVIDERS):
        LLM_API_PROVIDERS = LLM_API_PROVIDERS + [_kie_provider]
    if _KIE_IN_IMAGE and not any(p.get("kind") == "kie" for p in IMAGE_API_PROVIDERS):
        IMAGE_API_PROVIDERS = IMAGE_API_PROVIDERS + [_kie_provider]

# ---- bbww (api.bbww.top) 優先供應商 ----
# 全 OpenAI 相容：chat 走 /v1/chat/completions（與現有鏈路一致）；出圖走同步
# /v1/images/generations、改圖走 /v1/images/edits（區別於 APIMart 的非同步 submit+
# poll，見 api_client 的 "kind":"bbww" 分支）。設定 POPOP_BBWW_KEY 即把 bbww
# 以【最高優先順序】掛進 LLM 池與圖片池：每次請求先打 bbww（更快），失敗再回退
# 到其餘供應商。圖片模型預設 gpt-image-2（bbww 上支援、最高 4K）；注意該模型需
# token 所在【分組】已開通對應通道，否則 New API 會返回 "No available channel for
# model ... under group ..."（分組未配通道，而非模型不存在），此時會自動回退到池中
# 其它供應商。可用 POPOP_BBWW_IMAGE_MODEL 覆蓋為該分組實際可達的型號。
BBWW_BASE = os.environ.get("POPOP_BBWW_BASE", "https://api.bbww.top/v1").rstrip("/")
BBWW_IMAGE_MODEL = os.environ.get("POPOP_BBWW_IMAGE_MODEL", "gpt-image-2")
# 支援多個 bbww key 分攤限流：POPOP_BBWW_KEYS（逗號分隔）優先，其次單個 POPOP_BBWW_KEY。
# 多個 key 都以最高優先順序掛入，且在優先層內部 round-robin（見 api_client._ordered_providers），
# 讓請求把額度分攤到不同 key 上，減少 429。
_bbww_keys = [
    k.strip() for k in
    (os.environ.get("POPOP_BBWW_KEYS", "") or os.environ.get("POPOP_BBWW_KEY", "")).split(",")
    if k.strip()
]
# bbww 是否掛進 LLM 池：預設開；設 POPOP_BBWW_LLM=0 關閉。
# 當 bbww 分組沒有開通 gemini 通道時，它對每次 LLM 呼叫都返回 503（約 2.5s/次），
# 排在最高優先順序反而使每次 chat 白白多等數秒 → 關掉可顯著加速文字/視覺呼叫。
_BBWW_IN_LLM = os.environ.get("POPOP_BBWW_LLM", "1").strip().lower() not in ("0", "false", "no")
if _BBWW_IN_LLM and _bbww_keys and not any(p.get("kind") == "bbww" for p in LLM_API_PROVIDERS):
    _bbww_llms = [{"base": BBWW_BASE, "key": k, "kind": "bbww", "priority": True}
                  for k in _bbww_keys]
    LLM_API_PROVIDERS = _bbww_llms + LLM_API_PROVIDERS
# bbww 是否參與【出圖】：預設關（設 POPOP_BBWW_IMAGE=1 才掛進圖片池）。
# 出圖改由 OpenAI 直連 + lk888（見 POPOP_EXTRA_IMAGE_PROVIDERS）承擔，bbww 只出文字。
_BBWW_IN_IMAGE = os.environ.get("POPOP_BBWW_IMAGE", "0").strip().lower() not in ("0", "false", "no")
if _BBWW_IN_IMAGE and _bbww_keys and not any(p.get("kind") == "bbww" for p in IMAGE_API_PROVIDERS):
    _bbww_imgs = [{"base": BBWW_BASE, "key": k, "kind": "bbww", "priority": True,
                   "image_model": BBWW_IMAGE_MODEL} for k in _bbww_keys]
    IMAGE_API_PROVIDERS = _bbww_imgs + IMAGE_API_PROVIDERS

# ---- bbww 出【文字】：走 Gemini 原生協議擴容 LLM 池 ----
# bbww 的分組沒開 OpenAI 相容的 gemini 通道（/v1/chat/completions 報 503），
# 但 gemini 掛在【原生協議】下可用：POST {root}/v1beta/models/{model}:generateContent
# + header x-goog-api-key（實測 gemini-3.1-pro-preview ~3s 出文）。故以 kind=gemini
# 原生方式把這些 key 以【最高優先順序】掛進 LLM 池，與 gemini 池一起分攤生文、提吞吐。
# 用 POPOP_BBWW_GEMINI_KEYS（逗號分隔）指定；留空則回退用 _bbww_keys。設
# POPOP_BBWW_GEMINI=0 可關閉。base 取根域（去掉 /v1，原生路徑在根下）。
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
# 向後相容：保留 BBWW_KEY 名（取第一個），個別地方可能直接引用。
BBWW_KEY = _bbww_keys[0] if _bbww_keys else ""
LLM_MODEL = os.environ.get("POPOP_LLM_MODEL", "gemini-3.1-pro-preview")
CHAT_MODEL = os.environ.get("POPOP_CHAT_MODEL", "gemini-3.5-flash")
IMAGE_MODEL = os.environ.get("POPOP_IMAGE_MODEL", "gpt-image-2")
# nanobanana（Google Gemini Flash Image）出圖模型名：走 apib/aiuxu/aishuch 等
# APIMart 相容中轉的非同步 /images/generations + 輪詢鏈路（同 gpt-image-2 的提交協議）。
# 可用 POPOP_BANANA_IMAGE_MODEL 覆蓋為中轉實際可達的型號（如 nano-banana-2-ext /
# gemini-3.1-flash-image / nano-banana-ext 等）。
BANANA_IMAGE_MODEL = os.environ.get("POPOP_BANANA_IMAGE_MODEL", "nano-banana")
# 前端「生圖模型」下拉的選項值 → 實際模型名 的對映（image-2=既有 gpt-image-2；
# banana=nanobanana）。api_client / feed_posts 依此把介面選擇翻成真模型名。
IMAGE_MODEL_CHOICES = {
    "image-2": IMAGE_MODEL,
    "banana": BANANA_IMAGE_MODEL,
}
DEFAULT_IMAGE_MODEL_CHOICE = os.environ.get("POPOP_DEFAULT_IMAGE_MODEL_CHOICE", "image-2")


def resolve_image_model(choice: str | None) -> str:
    """把介面生圖模型選項（image-2/banana）翻成實際模型名；未知回退預設 IMAGE_MODEL。"""
    if not choice:
        return IMAGE_MODEL
    return IMAGE_MODEL_CHOICES.get(choice, IMAGE_MODEL)


# 出圖「全平攤」round-robin：把全部出圖渠道（openai/lk888 同步 + apimart×4/kie 非同步）
# 當成一個環均勻輪轉分發，而非高優先層先命中、兜底層閒置。預設開；設 0 回退分層優先。
IMAGE_FLAT_ROUND_ROBIN = os.environ.get(
    "POPOP_IMAGE_FLAT_RR", "1").strip().lower() not in ("0", "false", "no")

# ---- 額外同步出圖供應商（OpenAI 相容 /images/generations，返回 b64/url）----
# 透過 POPOP_EXTRA_IMAGE_PROVIDERS（JSON 陣列）注入，複用 bbww 同步出圖分支
# （kind=bbww），以最高優先順序掛進圖片池並與其它同步供應商 round-robin 分攤，
# 用於給出圖擴容/提速。每個元素：{"base":".../v1","key":"sk-...","image_model":"gpt-image-1"}。
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

# ---- Embeddings (火山方舟 Ark，用於靈感庫語義檢索) ----
# Ark 的向量模型走 /embeddings/multimodal 端點、返回 data.embedding、不支援真批次，
# 與標準 OpenAI /embeddings 不同，api_client.embed 會據 base 自動切換協議。
EMBED_BASE = os.environ.get("POPOP_EMBED_BASE", "https://ark.cn-beijing.volces.com/api/v3")
EMBED_KEY = os.environ.get("POPOP_EMBED_KEY", "")
EMBED_MODEL = os.environ.get("POPOP_EMBED_MODEL", "doubao-embedding-vision-251215")

# Image generation defaults
IMAGE_SIZE_COVER = "3:4"      # portrait cover
IMAGE_RESOLUTION = "2k"
IMAGE_SIZE_POST = "3:4"

# Polling
# 出圖輪詢間隔：原為 5s，單張圖哪怕 11~12s 就緒也要等到第 15s 才被取回，尾延遲明顯。
# 降到 2s 削單圖尾延遲（一批幾十張圖累計省下可觀時間），對渠道壓力增加有限。
# 可用 POPOP_TASK_POLL_INTERVAL 覆蓋。
TASK_POLL_INTERVAL = int(os.environ.get("POPOP_TASK_POLL_INTERVAL", "2"))  # seconds
TASK_POLL_TIMEOUT = int(os.environ.get("POPOP_TASK_POLL_TIMEOUT", "360"))  # max wait/image

# Concurrency for batch operations. bbww 優先鏈路更快、可承受更高併發，
# 預設拉到 90（可用 POPOP_MAX_WORKERS 覆蓋）。
MAX_WORKERS = int(os.environ.get("POPOP_MAX_WORKERS", "90"))
# 批次匯出角色時的【角色級】併發上限（每個角色內部還會為帖圖另開執行緒池，
# 故此處適度封頂，避免 N×M 連線打爆 OSS/TOS）。可用 POPOP_EXPORT_CONCURRENCY 覆蓋。
EXPORT_CONCURRENCY = int(os.environ.get("POPOP_EXPORT_CONCURRENCY", "16"))
# OSS 直傳（put_object）的連線/讀超時（秒）。預設無超時會讓網路卡死永久掛住
# 匯出執行緒，導致進度停滯。可用 POPOP_OSS_PUT_TIMEOUT 覆蓋。
OSS_PUT_TIMEOUT = int(os.environ.get("POPOP_OSS_PUT_TIMEOUT", "60"))

# ---- Paths ----
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
PERSONA_DIR = DATA_DIR / "personas"
POST_DIR = DATA_DIR / "posts"
IMAGE_DIR = DATA_DIR / "images"
LANDING_DIR = DATA_DIR / "landing"
CHAT_DIR = DATA_DIR / "chat"
EXPORT_DIR = DATA_DIR / "exports"  # 非同步批次匯出生成的 zip 落盤目錄（臨時檔案）
WEB_DIR = ROOT / "web"

for _d in (UPLOAD_DIR, PERSONA_DIR, POST_DIR, IMAGE_DIR, LANDING_DIR, CHAT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---- Languages ----
LANGUAGES = ["zh", "ja", "ko", "en"]
LANGUAGE_NAMES = {
    "zh": "簡體中文",
    "zh-Hant": "繁體中文",
    "ja": "日本語",
    "ko": "한국어",
    "en": "English",
}

# Per-language native instruction + locale/culture guidance.
# Every downstream prompt for a given character runs ONLY in that language,
# so output reads like it was authored by a native, not translated.
LANGUAGE_NATIVE = {
    "zh": "請全程使用簡體中文輸出。",
    "ja": "すべて日本語で出力してください。",
    "ko": "모든 내용을 한국어로 출력하세요.",
    "en": "Write everything in English.",
}

LOCALE_GUIDE = {
    "zh": (
        "面向中文簡體（中國大陸）受眾。用地道的中文網路/生活語感，符合小紅書·微博·"
        "豆瓣那種表達；網路用語、語氣詞、emoji 自然使用。不要翻譯腔，不要直譯外語句式。"
        "金額用人民幣語境，地點/品牌/飲食/作息貼近中國大陸日常。"
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
# token 獲取方式：local=用共享金鑰本地 PyJWT 自籤；endpoint=調內網 /internal/tool/gen_jwt_token
ARCA_JWT_MODE = os.environ.get("ARCA_JWT_MODE", "endpoint")
ARCA_ACCESS_SECRET = os.environ.get("ARCA_ACCESS_SECRET", "")
ARCA_JWT_EXPIRES = int(os.environ.get("ARCA_JWT_EXPIRES", "2592000"))  # 30 天
ARCA_REGION = os.environ.get("ARCA_REGION", "KR")
# 角色語言 → X-Region 對映（兩位大寫國家碼；CN 會被 arca RegionBlock 拒 403，勿用）。
# env 傳 JSON 覆蓋，如 {"zh":"TW","ja":"JP","ko":"KR","en":"US"}；查不到回退 ARCA_REGION。
_DEFAULT_REGION_BY_LANG = {
    "zh": "TW",        # arca 中文統一 zh-Hant(繁中)，歸屬 TW/HK 等繁中地區；CN 被攔
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
ARCA_POST_VISIBILITY = int(os.environ.get("ARCA_POST_VISIBILITY", "0"))  # 0=跟隨角色可見性(推薦)；1公開2好友3私密=顯式覆蓋
ARCA_SYNC_LANDING = os.environ.get("ARCA_SYNC_LANDING", "1") not in ("0", "false", "False", "")
# exporting ImportCharacterReq：同 provider + external_character_id 是匯入幂等鍵。
# 有來源標記的角色優先使用 record.source，否則使用這個穩定的本地生產方標識。
ARCA_IMPORT_PROVIDER = os.environ.get("ARCA_IMPORT_PROVIDER", "popop")
ARCA_TOS_BUCKET_PUBLIC = os.environ.get("ARCA_TOS_BUCKET_PUBLIC", "bucket-popop-i18n-prod")  # 落地頁 HTML 用公有桶（留空則用憑證返回的 bucket）
# 落地頁 html_filled 裡相對的 /img/、/upload/ 資源改寫成帶域名的絕對 URL 時用的站點根地址，
# 便於把落地頁脫離平臺單獨託管/預覽。留空則保持相對路徑（歷史行為）。
PUBLIC_BASE_URL = os.environ.get(
    "POPOP_PUBLIC_BASE_URL",
    "http://popop-pipeline.internal-app.imaginewithu.com",
).rstrip("/")
# 除錯：列印 arca 每次 HTTP 原始請求/響應（Authorization 與憑證欄位自動脫敏）
ARCA_DEBUG = os.environ.get("ARCA_DEBUG", "1") not in ("0", "false", "False", "")
# arca 儲存中臺（通用 JSONB 集合儲存）資料面 API Key（sk_...，在 /admin/storage_hub 開通）。
# 配置後本地 JSON 記錄/圖片以 arca 為主存、本地為快取；留空則純本地儲存（歷史行為）。
ARCA_STORAGE_KEY = os.environ.get("ARCA_STORAGE_KEY", "sk_5c581cae262b4f54b838246942dd30de3375f9d3f283df24424d9f09502615cb")
