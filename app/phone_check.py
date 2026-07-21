"""查手機（폰 몰래보기）Demo 的後端資料補給。

前端 web/phone_check_kr.html 是「固定功能 UI + 按角色注入內容」的殼：
每個功能的版式寫死在前端，內容由角色資料注入。這裡負責把前端寫死的
mock 角色，跟平台裡真實存在的第三方帖子角色對齊——拉出真實的封面圖、
最近一次聊天記錄、feed 帖子摘要，讓 demo 的「나 ME 對話」「線索」等
內容有真實素材可依。

read-only：只讀，不寫任何檔案，不呼叫 LLM。缺資料時降級（回空），
前端保留自己的 mock 兜底。
"""
from __future__ import annotations

from . import chat, config, feed_posts, pipeline

# 前端 demo 角色 id -> 平台真實 char_id（素材最豐富的三個第三方帖子角色）
DEMO_CHAR_MAP: dict[str, str] = {
    "yuwi": "char_1783597290_f0d265",   # 游嶼 / 深夜便利店音效師（治癒·纯爱）
    "shen": "char_1784089818_de4be8",   # 203號沈先生 / 旅行攝影師（懸疑·危險鄰居）
    "haesu": "char_1783634537_a54d49",  # 都海樹 / 望遠洞高冷藥師（日常·反差）
}


def _served_url_ok(url: str | None) -> bool:
    """本地 /img/<name> 圖是否真的存在（避免給前端一個 404 的封面）。
    外部 http(s) URL 一律當作可用（交給瀏覽器判斷）。"""
    if not url:
        return False
    if url.startswith("/img/"):
        return (config.IMAGE_DIR / url[len("/img/"):]).exists()
    return url.startswith("http")


def _persona_name(record: dict) -> str:
    name = (record.get("persona") or {}).get("name")
    if isinstance(name, dict):
        return name.get("zh") or name.get("ko") or next(iter(name.values()), "")
    return str(name or "")


def _chat_lines(char_id: str, char_name: str, max_lines: int = 40) -> list[dict]:
    """最近一次 normal 會話的逐條訊息（who/text），供前端注入『나 ME』對話。"""
    session = chat._latest_session(char_id, mode="normal")
    if session is None:
        return []
    out: list[dict] = []
    for m in session.get("messages", []):
        role = m.get("role")
        if role == "user":
            text = (m.get("content") or "").strip()
            if text:
                out.append({"who": "me", "text": text[:200]})
        elif role == "assistant":
            for item in (m.get("items") or []):
                text = feed_posts._item_text(item).strip()
                if text:
                    out.append({"who": "them", "text": text[:200]})
    return out[-max_lines:]


def _feed_snippets(char_id: str, limit: int = 6) -> list[dict]:
    """該角色的第三方帖子正文首行 + 配圖 URL，供前端做 SNS/線索素材。"""
    feed = feed_posts.list_feed_posts(char_id)
    out: list[dict] = []
    for post in (feed.get("posts") or [])[:limit]:
        data = post.get("data") or {}
        content = data.get("content")
        if isinstance(content, dict):
            content = content.get("zh") or content.get("ko") or ""
        img = post.get("image") or {}
        out.append({
            "kind": post.get("kind"),
            "subtype": post.get("subtype"),
            "first_line": str(content or "").strip().split("\n")[0][:120],
            "image_url": pipeline._served_image_url(img) if isinstance(img, dict) else None,
        })
    return out


def enrich_one(demo_id: str, real_id: str) -> dict:
    """單角色的真實素材包；角色缺失/無資料時對應欄位為空，前端保留 mock。"""
    result: dict = {"demo_id": demo_id, "char_id": real_id,
                    "found": False, "name": None, "cover_url": None,
                    "chat_lines": [], "feed": []}
    try:
        record = pipeline.load_character(real_id)
    except Exception:  # noqa: BLE001 角色不存在/讀取失敗時降級，前端保留 mock
        return result
    result["found"] = True
    result["name"] = _persona_name(record)
    try:
        result["feed"] = _feed_snippets(real_id)
    except Exception:  # noqa: BLE001 feed 缺失不阻斷
        pass
    # 封面優先用 persona cover；本地缺圖時退回第一張存在的 feed 配圖，
    # 避免前端拿到一個 404 的 /img 路徑（demo 可用性優先）。
    cover = pipeline._served_image_url(record.get("cover"))
    if not _served_url_ok(cover):
        cover = next((f["image_url"] for f in result["feed"]
                      if _served_url_ok(f.get("image_url"))), None)
    result["cover_url"] = cover
    try:
        result["chat_lines"] = _chat_lines(real_id, result["name"] or real_id)
    except Exception:  # noqa: BLE001 聊天缺失不阻斷
        pass
    return result


def enrich() -> dict:
    """全部 demo 角色的真實素材補給包（前端一次 fetch）。"""
    return {"chars": [enrich_one(d, r) for d, r in DEMO_CHAR_MAP.items()]}
