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
