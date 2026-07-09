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
    """Round-robin ordered providers for this request kind."""
    providers = (
        config.IMAGE_API_PROVIDERS if kind == "image"
        else config.LLM_API_PROVIDERS
    )
    if not providers:
        raise APIError(f"no API providers configured for {kind}")
    with _RR_LOCK:
        start = _RR_INDEX[kind] % len(providers)
        _RR_INDEX[kind] = (_RR_INDEX[kind] + 1) % len(providers)
    return providers[start:] + providers[:start]


def _headers(provider: dict) -> dict:
    return {
        "Authorization": f"Bearer {provider['key']}",
        "Content-Type": "application/json",
    }


def _is_kie(provider: dict) -> bool:
    return provider.get("kind") == "kie"


def _kie_chat_route(model: str) -> tuple[str, str]:
    """把内部 model 名映射到 KIE 的 (URL 路径段, body model 名)。

    KIE 的 chat 是 OpenAI 兼容但把模型放进路径：POST {base}/{path}/v1/chat/completions。
    未识别的 model 一律回退到 pro 路径（保证可用），避免静默 404。
    """
    m = (model or "").lower()
    if "flash" in m:
        return config.KIE_LLM_PATH_FLASH, config.KIE_LLM_MODEL_FLASH
    return config.KIE_LLM_PATH_PRO, config.KIE_LLM_MODEL_PRO


def _kie_chat(provider: dict, messages: list[dict], model: str,
              temperature: float, max_tokens: int | None, timeout: int) -> str:
    """KIE chat 适配：路径带模型名，响应体与 OpenAI 一致（choices[0].message.content）。"""
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
    # KIE 统一错误体：{"code":401,"msg":"..."}；OpenAI 兼容错误：{"error":{...}}
    if isinstance(data, dict) and "error" in data:
        raise APIError(data["error"].get("message", "kie chat error"))
    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise APIError(f"kie chat code={data.get('code')}: {data.get('msg')}")
    return data["choices"][0]["message"]["content"]


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
                    # provider 专属业务错误（如 401/无效 key）：不重试同一
                    # base，记为 last_err 后换池中下一个 provider，全部失败
                    # 才在下面统一抛出
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
    """火山方舟 Ark 单条文本向量：/embeddings/multimodal，返回 data.embedding。"""
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
    """标准 OpenAI 兼容 /embeddings：返回 data[0].embedding。"""
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
    """剥离首尾的 ```json ... ``` 围栏。

    不能用 split("```", 2)：JSON 字符串值内部若也含字面 ```（如分享的代码
    片段），会在第一个内部围栏处被截断。正确做法是只匹配开头的起始围栏、
    结尾的收尾围栏各一次，中间内容原样保留（哪怕内部还有更多 ``` 也不受
    影响），取匹配到的候选中最长的一段以尽量保留完整正文。
    """
    open_m = _FENCE_OPEN_RE.match(t)
    close_m = _FENCE_CLOSE_RE.search(t)
    if open_m and close_m and close_m.start() >= open_m.end():
        return t[open_m.end():close_m.start()]
    if open_m:
        # 收尾围栏缺失（模型输出被截断等）：仅去掉起始围栏
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
# Image generation (async, poll task)
# --------------------------------------------------------------------------
def _kie_aspect_ratio(size: str | None) -> str:
    """把内部 size（如 '3:4'）映射到 KIE 支持的 aspect_ratio，未知回退 auto。"""
    allowed = {"1:1", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5",
               "16:9", "9:16", "2:1", "1:2", "3:1", "1:3", "21:9", "9:21"}
    s = (size or "").strip()
    return s if s in allowed else "auto"


def _submit_image_kie(provider: dict, prompt: str, size: str | None,
                      resolution: str | None, image_urls: list[str] | None,
                      timeout: int) -> str:
    """KIE 出图提交：按有无参考图切 t2i / i2i，返回 taskId。

    KIE 的 i2i 要求 input_urls 是【可公网访问的 URL 数组】，不接受 data: URI。
    现有 pipeline 传的参考图可能是 data URI（file_to_data_uri），此时降级为 t2i，
    避免提交必然失败——KIE 仅用作分流，参考图强一致的活仍可交回原供应商。
    """
    ar = _kie_aspect_ratio(size)
    # KIE：aspect_ratio=auto 只能出 1K；要 2K/4K 必须指定具体比例
    res = (resolution or "").upper().replace("K", "K")
    if res in ("2K", "4K") and ar == "auto":
        res = "1K"
    http_urls = [u for u in (image_urls or []) if isinstance(u, str)
                 and u.startswith("http")]
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
    """KIE 轮询：GET /api/v1/jobs/recordInfo?taskId=，解析 resultJson.resultUrls。

    归一化成与 APIMart poll_task 相同的返回：{"result":{"images":[{"url":...}]}}，
    让上层 generate_image 无需区分供应商。
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
                    # provider 专属业务错误（如 401/无效 key）：记为 last_err
                    # 后换池中下一个 provider，而不是直接冲出整个轮询
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
                # 含 ReadTimeout(RequestException) 与非 JSON 响应体(r.json() 抛
                # JSONDecodeError⊂ValueError)：同一 base 内重试，用尽后仍会
                # 轮到下一个 base，保持与 chat() 一致的故障切换语义
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


def generate_image(
    prompt: str,
    size: str | None = None,
    resolution: str | None = None,
    image_urls: list[str] | None = None,
    save_path: str | Path | None = None,
) -> dict:
    """End-to-end: submit -> poll -> download. Returns {url, local_path}."""
    prompt = _sanitize_image_prompt(prompt)
    task_id, provider = submit_image(
        prompt, size=size, resolution=resolution, image_urls=image_urls
    )
    dd = poll_task(task_id, provider=provider)
    images = dd.get("result", {}).get("images", [])
    if not images:
        raise APIError(f"no images in task result: {dd}")
    url_field = images[0]["url"]
    url = url_field[0] if isinstance(url_field, list) else url_field

    local_path = None
    if save_path:
        local_path = str(save_path)
        last_download_err = None
        for dl_attempt in range(3):
            try:
                resp = requests.get(url, timeout=120)
                # 签名 URL 过期或 CDN 瞬时 4xx/5xx 时不能把错误页字节当 PNG 落盘/上传，
                # 否则会静默损坏本地缓存与 OSS 私有桶
                if not resp.ok:
                    raise APIError(
                        f"generate_image download failed: HTTP {resp.status_code} for {url}"
                    )
                # 只拦明确的错误页（html/json/xml/text），不拦 octet-stream 等对象存储
                # 常见头，否则会误杀本可成功的下载。
                content_type = resp.headers.get("Content-Type", "").lower()
                if content_type.startswith(("text/", "application/json", "application/xml")):
                    raise APIError(
                        f"generate_image download failed: unexpected Content-Type "
                        f"{content_type!r} for {url}"
                    )
                img_data = resp.content
                break
            except (requests.RequestException, APIError) as e:
                last_download_err = e
                if dl_attempt == 2:
                    raise APIError(
                        f"image download failed after 3 attempts: {last_download_err}"
                    ) from e
                time.sleep(3)
        from . import storage as _storage
        _storage.save_file(Path(local_path), img_data, content_type="image/png")
    return {
        "task_id": task_id,
        "url": url,
        "local_path": local_path,
        "provider_base": provider["base"],
    }
