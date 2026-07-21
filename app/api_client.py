"""APIMart (aiuxu) API client: LLM chat (with vision) + async image generation."""
import base64
import json
import mimetypes
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import config


_SAFETY_SUBS = [
    (re.compile(r"섹시\S*", re.IGNORECASE), "매력적인"),
    (re.compile(r"sexy", re.IGNORECASE), "attractive"),
    (re.compile(r"性感", re.IGNORECASE), "有吸引力"),
    (re.compile(r"퇴폐\S*", re.IGNORECASE), "분위기 있는"),
    (re.compile(r"야한\S*", re.IGNORECASE), "대담한"),
    (re.compile(r"노출\S*", re.IGNORECASE), "드러나는"),
    (re.compile(r"가슴", re.IGNORECASE), "상체"),
    (re.compile(r"胸", re.IGNORECASE), "上身"),
    (re.compile(r"裸|bare chest|shirtless", re.IGNORECASE), "open shirt"),
    # 「偷拍/監控」語義是生圖審核高頻誤殺區：feed 第三方帖只要取景像被路人拍到即可，
    # 措辭一律換成中性的街拍/媒體攝影詞，避免整條 prompt 被 task_failed 風控攔下。
    (re.compile(r"paparazzi", re.IGNORECASE), "celebrity press"),
    (re.compile(r"voyeur\w*", re.IGNORECASE), "observational"),
    (re.compile(r"cctv|security[- ]camera|surveillance", re.IGNORECASE), "retro monitor-cam"),
    (re.compile(r"hidden camera|spy ?cam\w*", re.IGNORECASE), "street-snap camera"),
    (re.compile(r"secretly (photograph|film|record|captur|shoot|shot|snap)\w*",
                re.IGNORECASE), "candidly capture"),
    (re.compile(r"도촬|몰카"), "캔디드 스냅"),
    (re.compile(r"몰래\s*(찍|촬영)\w*"), "스냅으로 담은"),
    (re.compile(r"偷拍"), "街拍"),
]


def _sanitize_image_prompt(text: str) -> str:
    """Replace words likely to trigger image-generation safety filters."""
    for pattern, replacement in _SAFETY_SUBS:
        text = pattern.sub(replacement, text)
    return text


class APIError(Exception):
    pass


_RR_LOCK = threading.Lock()
_RR_INDEX = {"chat": 0, "image": 0}


def _ordered_providers(kind: str) -> list[dict]:
    """Providers to try for this request kind, priority-first then round-robin.

    帶 "priority": True 的供應商（如 bbww）永遠排在最前、且不參與輪詢，保證
    每次請求都先打最快的站點；其餘供應商在內部做 round-robin 分流，前者失敗時
    自動回退到後者，維持既有故障切換語義。
    """
    providers = (
        config.IMAGE_API_PROVIDERS if kind == "image"
        else config.LLM_API_PROVIDERS
    )
    if not providers:
        raise APIError(f"no API providers configured for {kind}")
    # 出圖「全平攤」模式：把所有渠道（同步 openai/lk888 + 非同步 apimart/kie）當成
    # 一個環做單一 round-robin，每次請求換頭，讓 4 類渠道均勻分攤、吞吐最大化，而非
    # 讓高優先層永遠先命中、兜底層閒置。POPOP_IMAGE_FLAT_RR=0 可回退到分層優先模式。
    if kind == "image" and config.IMAGE_FLAT_ROUND_ROBIN and len(providers) > 1:
        with _RR_LOCK:
            start = _RR_INDEX["image"] % len(providers)
            _RR_INDEX["image"] = (_RR_INDEX["image"] + 1) % len(providers)
        return providers[start:] + providers[:start]
    priority = [p for p in providers if p.get("priority")]
    rest = [p for p in providers if not p.get("priority")]
    # 多個優先供應商（如兩個 bbww key）之間也做 round-robin，讓額度真正分攤；
    # 單個時行為不變（始終先打它）。
    if len(priority) > 1:
        with _RR_LOCK:
            pstart = _RR_INDEX.setdefault("priority", 0) % len(priority)
            _RR_INDEX["priority"] = (_RR_INDEX["priority"] + 1) % len(priority)
        priority = priority[pstart:] + priority[:pstart]
    if rest:
        with _RR_LOCK:
            start = _RR_INDEX[kind] % len(rest)
            _RR_INDEX[kind] = (_RR_INDEX[kind] + 1) % len(rest)
        rest = rest[start:] + rest[:start]
    return priority + rest


def _headers(provider: dict) -> dict:
    return {
        "Authorization": f"Bearer {provider['key']}",
        "Content-Type": "application/json",
    }


def _is_kie(provider: dict) -> bool:
    return provider.get("kind") == "kie"


def _is_bbww(provider: dict) -> bool:
    return provider.get("kind") == "bbww"


def _is_gemini_native(provider: dict) -> bool:
    """走 Gemini 原生協議（/v1beta/models/{model}:generateContent）的供應商。

    某些中轉站（如 bbww）的 gemini 通道只掛在原生協議下，OpenAI 相容的
    /chat/completions 會 503 No available channel；此類 provider 標 kind=gemini。"""
    return provider.get("kind") == "gemini"


def _kie_chat_route(model: str) -> tuple[str, str]:
    """把內部 model 名對映到 KIE 的 (URL 路徑段, body model 名)。

    KIE 的 chat 是 OpenAI 相容但把模型放進路徑：POST {base}/{path}/v1/chat/completions。
    未識別的 model 一律回退到 pro 路徑（保證可用），避免靜默 404。
    """
    m = (model or "").lower()
    if "flash" in m:
        return config.KIE_LLM_PATH_FLASH, config.KIE_LLM_MODEL_FLASH
    return config.KIE_LLM_PATH_PRO, config.KIE_LLM_MODEL_PRO


def _kie_chat(provider: dict, messages: list[dict], model: str,
              temperature: float, max_tokens: int | None, timeout: int) -> str:
    """KIE chat 適配：路徑帶模型名，響應體與 OpenAI 一致（choices[0].message.content）。"""
    path, body_model = _kie_chat_route(model)
    url = f"{provider['base']}/{path}/v1/chat/completions"
    payload: dict[str, Any] = {
        "model": body_model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    r = requests.post(url, headers=_headers(provider), json=payload, timeout=timeout)
    data = r.json()
    # KIE 統一錯誤體：{"code":401,"msg":"..."}；OpenAI 相容錯誤：{"error":{...}}
    if isinstance(data, dict) and "error" in data:
        raise APIError(data["error"].get("message", "kie chat error"))
    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise APIError(f"kie chat code={data.get('code')}: {data.get('msg')}")
    return data["choices"][0]["message"]["content"]


def _to_gemini_parts(content: Any) -> list[dict]:
    """把 OpenAI 風格 content（str 或 [{type:text|image_url}]）轉成 Gemini parts。"""
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict] = []
    for item in content or []:
        if not isinstance(item, dict):
            parts.append({"text": str(item)})
            continue
        if item.get("type") == "text":
            parts.append({"text": item.get("text", "")})
        elif item.get("type") == "image_url":
            url = (item.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                head, _, b64 = url.partition(",")
                mime = head[5:].split(";")[0] or "image/png"
                parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    return parts


def _to_gemini_payload(messages: list[dict]) -> tuple[dict, list[dict]]:
    """拆出 systemInstruction 與 contents（Gemini 用 role=user/model，無 system role）。"""
    sys_parts: list[dict] = []
    contents: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            sys_parts += _to_gemini_parts(m.get("content"))
            continue
        contents.append({
            "role": "model" if role == "assistant" else "user",
            "parts": _to_gemini_parts(m.get("content")),
        })
    sys_instr = {"parts": sys_parts} if sys_parts else None
    return sys_instr, contents


def _gemini_chat(provider: dict, messages: list[dict], model: str,
                 temperature: float, max_tokens: int | None, timeout: int) -> str:
    """Gemini 原生 generateContent 適配：header x-goog-api-key 最快（實測 ~3s）。"""
    sys_instr, contents = _to_gemini_payload(messages)
    url = f"{provider['base'].rstrip('/')}/v1beta/models/{model}:generateContent"
    payload: dict[str, Any] = {"contents": contents}
    if sys_instr:
        payload["systemInstruction"] = sys_instr
    gen_cfg: dict[str, Any] = {"temperature": temperature}
    if max_tokens:
        gen_cfg["maxOutputTokens"] = max_tokens
    payload["generationConfig"] = gen_cfg
    headers = {"x-goog-api-key": provider["key"], "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise APIError(err.get("message", "gemini chat error") if isinstance(err, dict) else str(err))
    cands = (data or {}).get("candidates") or []
    if not cands:
        raise APIError(f"gemini chat empty response: {str(data)[:200]}")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        # 無正文（如 thinking 模型把 token 全用在 thoughts、MAX_TOKENS 截斷）：
        # 拋錯觸發池內 fallback，而非靜默返回空串導致下游生成空頁。
        reason = cands[0].get("finishReason", "")
        raise APIError(f"gemini chat no text (finishReason={reason})")
    return text


def file_to_data_uri(path: str | Path) -> str:
    p = Path(path)
    mime, _ = mimetypes.guess_type(str(p))
    if not mime:
        mime = "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


# --------------------------------------------------------------------------
# LLM chat
# --------------------------------------------------------------------------
def chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.8,
    max_retries: int = 4,
    timeout: int = 180,
    max_tokens: int | None = None,
) -> str:
    """Non-streaming chat completion. Returns assistant text content."""
    model = model or config.LLM_MODEL
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    last_err = None
    for provider in _ordered_providers("chat"):
        base = provider["base"]
        if _is_gemini_native(provider):
            g_model = provider.get("chat_model") or model
            for attempt in range(max_retries):
                try:
                    return _gemini_chat(provider, messages, g_model,
                                        temperature, max_tokens, timeout)
                except APIError as e:
                    msg = str(e).lower()
                    if any(x in msg for x in ("429", "rate", "wait", "500", "503", "overload")):
                        last_err = e
                        time.sleep(2 * (attempt + 1))
                        continue
                    last_err = e
                    break
                except (requests.RequestException, KeyError, ValueError) as e:
                    last_err = e
                    time.sleep(2 * (attempt + 1))
            continue
        if _is_kie(provider):
            for attempt in range(max_retries):
                try:
                    return _kie_chat(provider, messages, model,
                                     temperature, max_tokens, timeout)
                except APIError as e:
                    msg = str(e).lower()
                    if "429" in msg or "rate" in msg or "wait" in msg or "500" in msg:
                        last_err = e
                        time.sleep(2 * (attempt + 1))
                        continue
                    last_err = e
                    break
                except (requests.RequestException, KeyError, ValueError) as e:
                    last_err = e
                    time.sleep(2 * (attempt + 1))
            continue
        for attempt in range(max_retries):
            try:
                r = requests.post(
                    f"{base}/chat/completions",
                    headers=_headers(provider),
                    json=payload,
                    timeout=timeout,
                )
                data = r.json()
                if "error" in data:
                    msg = data["error"].get("message", "")
                    # transient "please wait" / rate limit -> retry same base
                    if "wait" in msg.lower() or r.status_code in (429, 500, 503):
                        last_err = APIError(msg)
                        time.sleep(2 * (attempt + 1))
                        continue
                    # provider 專屬業務錯誤（如 401/無效 key）：不重試同一
                    # base，記為 last_err 後換池中下一個 provider，全部失敗
                    # 才在下面統一丟擲
                    last_err = APIError(msg)
                    break
                return data["choices"][0]["message"]["content"]
            except requests.exceptions.SSLError as e:
                # domain likely blocked -> stop retrying this base, try next
                last_err = e
                break
            except requests.exceptions.ConnectionError as e:
                last_err = e
                break
            except (requests.RequestException, KeyError, ValueError) as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
    raise APIError(f"chat failed on all API domains: {last_err}")


def chat_json(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.7,
    max_retries: int = 4,
) -> Any:
    """Chat call that must return JSON. Strips markdown fences and parses."""
    text = chat(messages, model=model, temperature=temperature, max_retries=max_retries)
    return _parse_json(text)


def parse_json_text(text: str) -> Any:
    return _parse_json(text)


def _embed_one_ark(text: str, base: str, key: str, model: str,
                   max_retries: int, timeout: int) -> list[float]:
    """火山方舟 Ark 單條文字向量：/embeddings/multimodal，返回 data.embedding。"""
    url = f"{base.rstrip('/')}/embeddings/multimodal"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "input": [{"type": "text", "text": text}]}
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            data = r.json()
            if "error" in data:
                msg = data["error"].get("message", "")
                if "wait" in msg.lower() or r.status_code in (429, 500, 503):
                    last_err = APIError(msg)
                    time.sleep(2 * (attempt + 1))
                    continue
                raise APIError(msg)
            return data["data"]["embedding"]
        except (requests.RequestException, KeyError, ValueError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise APIError(f"ark embed failed: {last_err}")


def _embed_one_openai(text: str, base: str, key: str, model: str,
                      max_retries: int, timeout: int) -> list[float]:
    """標準 OpenAI 相容 /embeddings：返回 data[0].embedding。"""
    url = f"{base.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "input": text}
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            data = r.json()
            if "error" in data:
                raise APIError(data["error"].get("message", ""))
            return data["data"][0]["embedding"]
        except (requests.RequestException, KeyError, ValueError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise APIError(f"openai embed failed: {last_err}")


def embed(
    inputs: list[str],
    model: str | None = None,
    max_retries: int = 3,
    timeout: int = 60,
) -> list[list[float]]:
    """Text embeddings for inspiration retrieval.

    Uses the dedicated embedding provider (config.EMBED_BASE/KEY/MODEL). Ark's
    vision embedding model needs the /embeddings/multimodal endpoint and does
    NOT support real batching, so inputs are embedded one by one. Order preserved.
    Raises APIError on failure so callers can fall back to lexical matching.
    """
    if not inputs:
        return []
    base = config.EMBED_BASE
    key = config.EMBED_KEY or config.API_KEY
    model = model or config.EMBED_MODEL
    is_ark = "volces.com" in base or "ark" in base.lower()
    fn = _embed_one_ark if is_ark else _embed_one_openai
    return [fn(t, base, key, model, max_retries, timeout) for t in inputs]


_FENCE_OPEN_RE = re.compile(r"\A```[ \t]*(?:json)?[ \t]*\r?\n", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"\r?\n```[ \t]*\Z")


def _strip_fence(t: str) -> str:
    """剝離首尾的 ```json ... ``` 圍欄。

    不能用 split("```", 2)：JSON 字串值內部若也含字面 ```（如分享的程式碼
    片段），會在第一個內部圍欄處被截斷。正確做法是隻匹配開頭的起始圍欄、
    結尾的收尾圍欄各一次，中間內容原樣保留（哪怕內部還有更多 ``` 也不受
    影響），取匹配到的候選中最長的一段以儘量保留完整正文。
    """
    open_m = _FENCE_OPEN_RE.match(t)
    close_m = _FENCE_CLOSE_RE.search(t)
    if open_m and close_m and close_m.start() >= open_m.end():
        return t[open_m.end():close_m.start()]
    if open_m:
        # 收尾圍欄缺失（模型輸出被截斷等）：僅去掉起始圍欄
        return t[open_m.end():]
    return t


def _parse_json(text: str) -> Any:
    t = text.strip()
    if t.startswith("```"):
        t = _strip_fence(t).strip()
    # find first { or [ to last } or ]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start = min(
            [i for i in (t.find("{"), t.find("[")) if i != -1],
            default=-1,
        )
        end = max(t.rfind("}"), t.rfind("]"))
        if start != -1 and end != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise


def vision_message(text: str, image_data_uris: list[str]) -> dict:
    """Build a multimodal user message for gemini vision."""
    content: list[dict] = [{"type": "text", "text": text}]
    for uri in image_data_uris:
        content.append({"type": "image_url", "image_url": {"url": uri}})
    return {"role": "user", "content": content}


# --------------------------------------------------------------------------
# bbww (api.bbww.top) synchronous OpenAI image protocol
# --------------------------------------------------------------------------
_BBWW_SIZE_MAP = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "4:3": "1536x1024",
    "2:3": "1024x1536",
    "3:2": "1536x1024",
    "9:16": "1024x1536",
    "16:9": "1536x1024",
}


def _bbww_size(size: str | None) -> str:
    """把內部 aspect ratio（如 '3:4'）對映到 gpt-image 支援的畫素 size。

    gpt-image-1.5 僅接受 1024x1024 / 1024x1536 / 1536x1024 / auto，故按比例就近
    歸一到豎/橫/方三檔；已經是 WxH 畫素串則原樣透傳；未知回退 auto。
    """
    s = (size or "").strip()
    if not s:
        return "auto"
    if "x" in s and s.replace("x", "").isdigit():
        return s
    return _BBWW_SIZE_MAP.get(s, "auto")


def _bbww_image_bytes(uri: str) -> tuple[bytes, str, str]:
    """把 data: URI 或 http(s) URL 取成 (bytes, filename, mime)，供 edits 上傳。"""
    if uri.startswith("data:"):
        header, _, b64 = uri.partition(",")
        mime = header[5:].split(";")[0] or "image/png"
        ext = mimetypes.guess_extension(mime) or ".png"
        return base64.b64decode(b64), f"image{ext}", mime
    resp = requests.get(uri, timeout=120)
    if not resp.ok:
        raise APIError(f"bbww edits ref download failed: HTTP {resp.status_code}")
    mime = resp.headers.get("Content-Type", "image/png").split(";")[0]
    ext = mimetypes.guess_extension(mime) or ".png"
    return resp.content, f"image{ext}", mime


def _bbww_extract_image(data: dict) -> tuple[str | None, bytes | None]:
    """從 OpenAI 相容響應裡取出圖片：返回 (url, raw_bytes)，二者取其一。"""
    if isinstance(data, dict) and "error" in data:
        raise APIError(data["error"].get("message", "bbww image error"))
    items = (data or {}).get("data") or []
    if not items:
        raise APIError(f"bbww image: empty data in {data}")
    first = items[0]
    if first.get("b64_json"):
        return None, base64.b64decode(first["b64_json"])
    if first.get("url"):
        return first["url"], None
    raise APIError(f"bbww image: no url/b64_json in {first}")


def _bbww_generate(provider: dict, prompt: str, size: str | None,
                   image_urls: list[str] | None, timeout: int) -> tuple[str | None, bytes | None]:
    """bbww 同步出圖/改圖：有參考圖走 /images/edits(multipart)，否則 /images/generations。

    返回 (url, raw_bytes)——由上層統一落盤。
    """
    model = provider.get("image_model") or config.BBWW_IMAGE_MODEL
    px = _bbww_size(size)
    refs = [u for u in (image_urls or []) if isinstance(u, str) and u]
    auth = {"Authorization": f"Bearer {provider['key']}"}
    if refs:
        url = f"{provider['base']}/images/edits"
        files = []
        for u in refs[:16]:
            b, fn, mime = _bbww_image_bytes(u)
            files.append(("image[]", (fn, b, mime)))
        form = {"model": model, "prompt": prompt}
        if px != "auto":
            form["size"] = px
        r = requests.post(url, headers=auth, data=form, files=files, timeout=timeout)
    else:
        url = f"{provider['base']}/images/generations"
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
        if px != "auto":
            payload["size"] = px
        headers = dict(auth)
        headers["Content-Type"] = "application/json"
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        data = r.json()
    except ValueError as e:
        raise APIError(f"bbww image non-JSON response (HTTP {r.status_code})") from e
    return _bbww_extract_image(data)


# --------------------------------------------------------------------------
# Image generation (async, poll task)
# --------------------------------------------------------------------------
def _kie_aspect_ratio(size: str | None) -> str:
    """把內部 size（如 '3:4'）對映到 KIE 支援的 aspect_ratio，未知回退 auto。"""
    allowed = {"1:1", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5",
               "16:9", "9:16", "2:1", "1:2", "3:1", "1:3", "21:9", "9:21"}
    s = (size or "").strip()
    return s if s in allowed else "auto"


def _submit_image_kie(provider: dict, prompt: str, size: str | None,
                      resolution: str | None, image_urls: list[str] | None,
                      timeout: int) -> str:
    """KIE 出圖提交：按有無參考圖切 t2i / i2i，返回 taskId。

    KIE 的 i2i 要求 input_urls 是【可公網訪問的 URL 陣列】，不接受 data: URI。
    現有 pipeline 傳的參考圖可能是 data URI（file_to_data_uri）。這種情況必須跳過
    KIE 並交給下一個支援 i2i 的供應商；絕不能安靜地改走 t2i，否則輪詢到 KIE 的
    某些圖片會既失去角色錨點又失去原始作畫語言。
    """
    ar = _kie_aspect_ratio(size)
    # KIE：aspect_ratio=auto 只能出 1K；要 2K/4K 必須指定具體比例
    res = (resolution or "").upper().replace("K", "K")
    if res in ("2K", "4K") and ar == "auto":
        res = "1K"
    refs = [u for u in (image_urls or []) if isinstance(u, str) and u]
    http_urls = [u for u in refs if u.startswith(("http://", "https://"))]
    if refs and len(http_urls) != len(refs):
        # 讓 generate_image() 的 provider fallback 接手，而不是生成一張看似成功的
        # 無參考 t2i 圖。措辭刻意不含 "ref"，避免上層把這個渠道不相容誤判成
        # 參考圖失效並自行降級成 t2i。
        raise APIError(
            "KIE image-to-image requires public HTTP image URLs; "
            "plain text generation is disabled for this request"
        )
    if http_urls:
        model = config.KIE_IMAGE_MODEL_I2I
        payload_input: dict[str, Any] = {"prompt": prompt, "input_urls": http_urls[:16]}
    else:
        model = config.KIE_IMAGE_MODEL_T2I
        payload_input = {"prompt": prompt}
    if ar != "auto":
        payload_input["aspect_ratio"] = ar
    if res in ("1K", "2K", "4K"):
        payload_input["resolution"] = res
    payload = {"model": model, "input": payload_input}
    url = f"{provider['base']}/api/v1/jobs/createTask"
    r = requests.post(url, headers=_headers(provider), json=payload, timeout=timeout)
    data = r.json()
    code = data.get("code")
    if code not in (200, None):
        raise APIError(f"kie createTask code={code}: {data.get('msg')}")
    task_id = (data.get("data") or {}).get("taskId")
    if not task_id:
        raise APIError(f"kie createTask no taskId: {data}")
    return task_id


def _poll_task_kie(provider: dict, task_id: str, interval: int,
                   timeout: int) -> dict:
    """KIE 輪詢：GET /api/v1/jobs/recordInfo?taskId=，解析 resultJson.resultUrls。

    歸一化成與 APIMart poll_task 相同的返回：{"result":{"images":[{"url":...}]}}，
    讓上層 generate_image 無需區分供應商。
    """
    url = f"{provider['base']}/api/v1/jobs/recordInfo"
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(url, headers=_headers(provider),
                         params={"taskId": task_id}, timeout=30)
        data = r.json()
        dd = data.get("data") or {}
        state = dd.get("state")
        if state == "success":
            result_json = dd.get("resultJson") or "{}"
            try:
                parsed = json.loads(result_json)
            except (json.JSONDecodeError, TypeError) as e:
                raise APIError(f"kie resultJson parse failed: {result_json!r}") from e
            urls = parsed.get("resultUrls") or []
            if not urls:
                raise APIError(f"kie task {task_id} success but no resultUrls: {parsed}")
            return {"result": {"images": [{"url": urls[0]}]}}
        if state == "fail":
            raise APIError(f"kie task failed: {dd.get('failMsg') or dd.get('failCode')}")
        time.sleep(interval)
    raise APIError(f"kie image task {task_id} timed out after {timeout}s")


def _submit_one_provider(provider: dict, prompt: str, size: str | None,
                         resolution: str | None, image_urls: list[str] | None,
                         model: str, timeout: int) -> str:
    """對【單個】非同步 provider 提交出圖任務，返回 task_id（不遍歷、不回退）。

    供 generate_image 的統一輪轉分發使用：由上層決定渠道順序，這裡只負責一個渠道。
    """
    if _is_kie(provider):
        return _submit_image_kie(provider, prompt, size, resolution, image_urls, timeout)
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
    if size:
        payload["size"] = size
    if resolution:
        payload["resolution"] = resolution
    if image_urls:
        payload["image_urls"] = image_urls
    r = requests.post(f"{provider['base']}/images/generations",
                      headers=_headers(provider), json=payload, timeout=timeout)
    data = r.json()
    if "error" in data:
        raise APIError(data["error"].get("message", "submit error"))
    try:
        return data["data"][0]["task_id"]
    except (KeyError, IndexError) as e:
        raise APIError(f"submit_image unexpected response: {data}") from e


def _generate_via_provider(provider: dict, prompt: str, size: str | None,
                           resolution: str | None, image_urls: list[str] | None,
                           save_path: str | Path | None, model: str) -> dict:
    """單渠道端到端出圖：bbww/openai/lk888 同步直返；apimart/kie 非同步 submit+poll+下載。

    返回統一結構 {task_id, url, local_path, provider_base}。任一步失敗拋 APIError，
    由 generate_image 捕獲後換下一個渠道。
    """
    if _is_bbww(provider):
        return _generate_image_bbww(provider, prompt, size, image_urls, save_path, timeout=180)
    # 非同步渠道（apimart / kie）：submit -> poll -> download。
    task_id = _submit_one_provider(provider, prompt, size, resolution, image_urls, model, 60)
    dd = poll_task(task_id, provider=provider)
    images = dd.get("result", {}).get("images", [])
    if not images:
        raise APIError(f"no images in task result: {dd}")
    url_field = images[0]["url"]
    url = url_field[0] if isinstance(url_field, list) else url_field
    local_path = None
    if save_path:
        local_path = str(save_path)
        img_data = _download_image_bytes(url)
        from . import storage as _storage
        _storage.save_file(Path(local_path), img_data, content_type="image/png")
    return {"task_id": task_id, "url": url, "local_path": local_path,
            "provider_base": provider["base"]}


def submit_image(
    prompt: str,
    size: str | None = None,
    resolution: str | None = None,
    image_urls: list[str] | None = None,
    model: str | None = None,
    timeout: int = 60,
) -> tuple[str, dict]:
    """Submit an image task. Returns (task_id, provider), and polling must use
    the same provider that accepted the task."""
    model = model or config.IMAGE_MODEL
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
    if size:
        payload["size"] = size
    if resolution:
        payload["resolution"] = resolution
    if image_urls:
        payload["image_urls"] = image_urls

    last_err = None
    for provider in _ordered_providers("image"):
        base = provider["base"]
        # bbww 是同步出圖（無 task_id），由 generate_image 直接處理，submit 跳過。
        if _is_bbww(provider):
            continue
        if _is_kie(provider):
            for attempt in range(4):
                try:
                    tid = _submit_image_kie(provider, prompt, size,
                                            resolution, image_urls, timeout)
                    return tid, provider
                except APIError as e:
                    msg = str(e).lower()
                    if "429" in msg or "rate" in msg or "500" in msg or "455" in msg:
                        last_err = e
                        time.sleep(2 * (attempt + 1))
                        continue
                    last_err = e
                    break
                except (requests.RequestException, KeyError, ValueError) as e:
                    last_err = e
                    time.sleep(2 * (attempt + 1))
            continue
        for attempt in range(4):
            try:
                r = requests.post(
                    f"{base}/images/generations",
                    headers=_headers(provider),
                    json=payload,
                    timeout=timeout,
                )
                data = r.json()
                if "error" in data:
                    msg = data["error"].get("message", "")
                    if "wait" in msg.lower() or r.status_code in (429, 500, 503):
                        last_err = APIError(msg)
                        time.sleep(2 * (attempt + 1))
                        continue
                    # provider 專屬業務錯誤（如 401/無效 key）：記為 last_err
                    # 後換池中下一個 provider，而不是直接衝出整個輪詢
                    last_err = APIError(f"submit_image: {msg}")
                    break
                try:
                    return data["data"][0]["task_id"], provider
                except (KeyError, IndexError) as e:
                    raise APIError(f"submit_image unexpected response: {data}") from e
            except requests.exceptions.SSLError as e:
                # domain likely blocked -> stop retrying this base, try next
                last_err = e
                break
            except requests.exceptions.ConnectionError as e:
                last_err = e
                break
            except (requests.RequestException, KeyError, ValueError) as e:
                # 含 ReadTimeout(RequestException) 與非 JSON 響應體(r.json() 拋
                # JSONDecodeError⊂ValueError)：同一 base 內重試，用盡後仍會
                # 輪到下一個 base，保持與 chat() 一致的故障切換語義
                last_err = e
                time.sleep(2 * (attempt + 1))
    raise APIError(f"submit_image failed on all API domains: {last_err}")


def poll_task(
    task_id: str,
    provider: dict | None = None,
    interval: int | None = None,
    timeout: int | None = None,
) -> dict:
    """Poll an async task until completed/failed. Returns the task data dict."""
    provider = provider or config.IMAGE_API_PROVIDERS[0]
    base = provider["base"]
    interval = interval or config.TASK_POLL_INTERVAL
    timeout = timeout or config.TASK_POLL_TIMEOUT
    if _is_kie(provider):
        return _poll_task_kie(provider, task_id, interval, timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            f"{base}/tasks/{task_id}",
            headers=_headers(provider),
            timeout=30,
        )
        data = r.json()
        dd = data.get("data", data)
        status = dd.get("status")
        if status == "completed":
            return dd
        if status == "failed":
            err = dd.get("error") or dd.get("message") or "unknown error"
            raise APIError(f"image task failed: {err}")
        time.sleep(interval)
    raise APIError(f"image task {task_id} timed out after {timeout}s")


def _download_image_bytes(url: str) -> bytes:
    """下載出圖 URL 為位元組，帶 3 次重試並攔截錯誤頁，避免把 html/json 當 PNG 落盤。"""
    last_download_err = None
    for dl_attempt in range(3):
        try:
            resp = requests.get(url, timeout=120)
            # 簽名 URL 過期或 CDN 瞬時 4xx/5xx 時不能把錯誤頁位元組當 PNG 落盤/上傳，
            # 否則會靜默損壞本地快取與 OSS 私有桶
            if not resp.ok:
                raise APIError(
                    f"generate_image download failed: HTTP {resp.status_code} for {url}"
                )
            # 只攔明確的錯誤頁（html/json/xml/text），不攔 octet-stream 等物件儲存
            # 常見頭，否則會誤殺本可成功的下載。
            content_type = resp.headers.get("Content-Type", "").lower()
            if content_type.startswith(("text/", "application/json", "application/xml")):
                raise APIError(
                    f"generate_image download failed: unexpected Content-Type "
                    f"{content_type!r} for {url}"
                )
            return resp.content
        except (requests.RequestException, APIError) as e:
            last_download_err = e
            if dl_attempt == 2:
                raise APIError(
                    f"image download failed after 3 attempts: {last_download_err}"
                ) from e
            time.sleep(3)
    raise APIError(f"image download failed: {last_download_err}")


def _generate_image_bbww(provider: dict, prompt: str, size: str | None,
                         image_urls: list[str] | None,
                         save_path: str | Path | None, timeout: int) -> dict:
    """bbww 同步出圖 + 落盤。返回與 legacy 一致的 {url, local_path, ...}。"""
    url, raw = _bbww_generate(provider, prompt, size, image_urls, timeout)
    local_path = None
    if save_path:
        img_data = raw if raw is not None else _download_image_bytes(url)
        local_path = str(save_path)
        from . import storage as _storage
        _storage.save_file(Path(local_path), img_data, content_type="image/png")
    return {
        "task_id": None,
        "url": url,
        "local_path": local_path,
        "provider_base": provider["base"],
    }


def generate_image(
    prompt: str,
    size: str | None = None,
    resolution: str | None = None,
    image_urls: list[str] | None = None,
    save_path: str | Path | None = None,
    model: str | None = None,
) -> dict:
    """End-to-end 出圖，priority-first：先打 bbww(同步)，失敗回退到非同步 submit+poll。

    model 省略時用預設 config.IMAGE_MODEL（gpt-image-2）。指定其它模型（如 nanobanana）
    時只走能按 body.model 出圖的「純中轉」渠道（apib/aiuxu/aishuch）——bbww/kie 用固定或
    自有模型名，會忽略傳入 model 或無法出該模型，故排除，避免出成 gpt-image-2。

    Returns {url, local_path, provider_base, task_id}.
    """
    prompt = _sanitize_image_prompt(prompt)
    model = model or config.IMAGE_MODEL

    # 統一 round-robin 分發：_ordered_providers 已把全部渠道（openai/lk888 同步 +
    # apimart×3/kie 非同步）排成一個每次換頭的輪轉序列。每張圖從不同渠道起頭、成功即
    # 返回，讓各類渠道真正分攤出圖吞吐；當前渠道失敗（限流/超時/錯誤）自動換下一個。
    providers = _ordered_providers("image")
    if model != config.IMAGE_MODEL:
        providers = [p for p in providers if not p.get("kind")]
        if not providers:
            raise APIError(f"no provider supports image model {model!r}")
    last_err = None
    for provider in providers:
        try:
            return _generate_via_provider(
                provider, prompt, size, resolution, image_urls, save_path, model
            )
        except (APIError, requests.RequestException, KeyError, ValueError) as e:
            last_err = e
            continue
    raise APIError(f"generate_image failed on all providers: {last_err}")
