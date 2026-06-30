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
API_KEY = os.environ.get("POPOP_API_KEY", "")
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
            out.append({"base": base, "key": key})
    return out or fallback


# OpenAI/APIMart-compatible provider pools. Tasks are round-robin distributed
# across these providers; image polling stays on the provider that accepted
# the task. KIE uses a different image task API, so it should not be added here
# unless wrapped to this same protocol.
API_PROVIDERS = _load_provider_pool("POPOP_API_PROVIDERS", _DEFAULT_API_PROVIDERS)
LLM_API_PROVIDERS = _load_provider_pool("POPOP_LLM_API_PROVIDERS", API_PROVIDERS)
IMAGE_API_PROVIDERS = _load_provider_pool("POPOP_IMAGE_API_PROVIDERS", API_PROVIDERS)
LLM_MODEL = os.environ.get("POPOP_LLM_MODEL", "gemini-3.1-pro-preview")
IMAGE_MODEL = os.environ.get("POPOP_IMAGE_MODEL", "gpt-image-2")

# Image generation defaults
IMAGE_SIZE_COVER = "3:4"      # portrait cover
IMAGE_RESOLUTION = "2k"
IMAGE_SIZE_POST = "3:4"

# Polling
TASK_POLL_INTERVAL = 5        # seconds between task polls
TASK_POLL_TIMEOUT = 360       # max seconds to wait for one image

# Concurrency for batch operations
MAX_WORKERS = int(os.environ.get("POPOP_MAX_WORKERS", "4"))

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

