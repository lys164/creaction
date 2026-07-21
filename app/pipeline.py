"""Pipeline orchestration: persona extraction, identity reverse, cover, posts.

Persists a per-character record under data/personas/<char_id>.json and post
batches under data/posts/<char_id>/<batch_id>.json. Images saved under data/images.
"""
import hashlib
import io
import json
import random
import re
import shutil
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from . import api_client, config, landing, library, prompts, storage, styles, voices


# 按角色的程式內互斥鎖：所有對同一 record 的讀-改-寫都應持鎖，
# 防止後臺任務與前臺請求併發時整檔案覆蓋丟寫。
import threading as _threading
_CHAR_LOCKS: dict[str, _threading.RLock] = {}
_CHAR_LOCKS_GUARD = _threading.Lock()


def char_lock(char_id: str) -> _threading.RLock:
    with _CHAR_LOCKS_GUARD:
        lock = _CHAR_LOCKS.get(char_id)
        if lock is None:
            lock = _CHAR_LOCKS[char_id] = _threading.RLock()
        return lock


def _locked(fn):
    """按首參 char_id 持角色鎖執行：所有對同一 record/批次的讀-改-寫互斥，
    防止後臺批次任務與前臺請求併發時整檔案覆蓋丟寫。"""
    import functools

    @functools.wraps(fn)
    def wrapper(char_id, *args, **kwargs):
        with char_lock(char_id):
            return fn(char_id, *args, **kwargs)
    return wrapper


COVER_SPEC_VERSION = 3
_RECENTLY_USED_VOICES: list[str] = []
_VOICE_LOCK = _threading.Lock()
PRODUCTION_STYLES = frozenset({"fantasy", "cute", "real"})

# 全量 persona 快照快取：建人設的多樣性避讓(_recent_persona_traits)與發帖的同組
# 兄弟掃描(_sibling_used_photo_kinds)原本每次呼叫都 glob+解析本地 1766+ 個 persona
# 檔案。批次跑 N 組 × 4 語言時 = 數十萬次讀盤解析（O(N²)，資料越多越慢）。這裡按短
# TTL 快取一次「所有 persona 的輕量記錄」，整批覆用；快取滯後最多讓避讓/避重看到
# 略舊的近況（無正確性影響，最壞是多樣性避讓稍欠），到期自動重掃。
_PERSONA_SNAPSHOT: dict = {"records": None, "exp": 0.0}
_PERSONA_SNAPSHOT_TTL = 30  # 秒
_PERSONA_SNAPSHOT_LOCK = _threading.Lock()


def _persona_snapshot() -> list[dict]:
    """所有本地 persona 記錄的輕量快照（短 TTL 快取，批次複用）。

    只保留下游避讓/避重需要的欄位（char_id/group_id/lang/created/persona），避免
    每次呼叫都全量 glob+json.loads。快取過期或未建立時重掃一次。
    """
    now = time.time()
    snap = _PERSONA_SNAPSHOT["records"]
    if snap is not None and now < _PERSONA_SNAPSHOT["exp"]:
        return snap
    with _PERSONA_SNAPSHOT_LOCK:
        snap = _PERSONA_SNAPSHOT["records"]
        if snap is not None and now < _PERSONA_SNAPSHOT["exp"]:
            return snap
        records: list[dict] = []
        for p in config.PERSONA_DIR.glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            persona = rec.get("persona") or {}
            records.append({
                "char_id": rec.get("char_id"),
                "group_id": rec.get("group_id"),
                "lang": rec.get("lang"),
                "created": rec.get("created", 0),
                "persona": persona,
            })
        _PERSONA_SNAPSHOT["records"] = records
        _PERSONA_SNAPSHOT["exp"] = now + _PERSONA_SNAPSHOT_TTL
        return records


def _new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def normalize_production_style(value: str | None) -> str:
    """Validate the business style chosen when a character is produced."""
    style = (value or "").strip().lower()
    if style not in PRODUCTION_STYLES:
        raise ValueError("style must be one of: fantasy, cute, real")
    return style


def _gender_for_voice(persona: dict) -> str | None:
    raw = str(persona.get("gender", "")).lower()
    if any(m in raw for m in ("男", "남", "male", "man", "男性")):
        return "男"
    if any(m in raw for m in ("女", "여", "female", "woman", "女性")):
        return "女"
    return None


def _randomize_voice(persona: dict, lang: str) -> dict:
    """Override model-chosen voice with a random gender-matched pick.

    The LLM tends to overuse the same voice IDs. Keeping a small recent-memory
    rotation makes batches across languages/characters sound more diverse.
    """
    if not isinstance(persona, dict):
        return persona
    all_voices = voices.list_for(lang)
    if not all_voices:
        return persona
    gender = _gender_for_voice(persona)
    pool = [v for v in all_voices if v.get(
        "gender") == gender] if gender else []
    if not pool:
        pool = all_voices

    with _VOICE_LOCK:
        available = [v for v in pool if v.get(
            "id") not in _RECENTLY_USED_VOICES]
        if not available:
            available = pool
        chosen = random.choice(available).get("id")
        if not chosen:
            return persona
        persona["voice"] = chosen
        _RECENTLY_USED_VOICES.append(chosen)
        max_memory = max(len(pool) - 1, 1)
        while len(_RECENTLY_USED_VOICES) > max_memory:
            _RECENTLY_USED_VOICES.pop(0)
    return persona


def _char_path(char_id: str) -> Path:
    return config.PERSONA_DIR / f"{char_id}.json"


def _existing_source_images(record: dict) -> list[str]:
    """Source image paths that exist locally or can be restored from OSS."""
    return [p for p in record.get("source_images", [])
            if p and storage.ensure_file(Path(p))]


def _first_source_image(record: dict) -> str | None:
    """First source image that still exists, or None if all are missing."""
    imgs = _existing_source_images(record)
    return imgs[0] if imgs else None


def _cover_as_local_image(record: dict) -> str | None:
    """Materialize the character's cover into a local file for use as a
    persona-inference reference. Prefer an existing local cover; otherwise
    download the cover URL into UPLOAD_DIR. Returns a local path or None."""
    cover = record.get("cover") or {}
    lp = cover.get("local_path")
    if lp and storage.ensure_file(Path(lp)):
        return lp
    url = cover.get("url")
    if isinstance(url, str) and url.lower().startswith("http"):
        return _download_image(url)
    return None


def _regen_reference_images(record: dict) -> list[str]:
    """Reference images to feed persona regeneration, in priority order:
    existing source images first, else fall back to the cover image.
    Never returns empty just to allow image-free generation — callers must
    skip regeneration when this is empty."""
    imgs = _existing_source_images(record)
    if imgs:
        return imgs
    cover_local = _cover_as_local_image(record)
    return [cover_local] if cover_local else []


def load_character(char_id: str) -> dict:
    record = storage.load_json("personas", char_id, _char_path(char_id))
    if record is None:
        raise FileNotFoundError(f"character {char_id} not found")
    return record


def save_character(record: dict) -> None:
    storage.save_json("personas", record["char_id"], record,
                      _char_path(record["char_id"]))


def _served_image_url(image: dict | None) -> str | None:
    """Prefer this service's /img route over provider URLs that may expire/403."""
    if not isinstance(image, dict):
        return None
    local_path = image.get("local_path")
    if local_path:
        name = Path(str(local_path)).name
        if name:
            return f"/img/{name}"
    return image.get("url")


def list_characters() -> list[dict]:
    out = []
    records = storage.list_json("personas", config.PERSONA_DIR)
    for _key, r in sorted(records.items()):
        name = r.get("persona", {}).get("name", "")
        if isinstance(name, dict):  # legacy multilingual record
            name = name.get("zh") or next(iter(name.values()), "")
        out.append({
            "char_id": r.get("char_id"),
            "name": name,
            "lang": r.get("lang"),
            "lang_name": config.lang_name(r.get("lang")) if r.get("lang") else None,
            "group_id": r.get("group_id"),
            "cover_url": _served_image_url(r.get("cover")),
            "has_identity": bool(r.get("identity")),
            "exported": bool(r.get("exported")),
            "arca_synced": bool(r.get("arca_character_id")),
            "source": r.get("source") or "",
            "style": r.get("style") or "",
            "created": r.get("created"),
        })
    return out


# --------------------------------------------------------------------------
# Step 1: image -> persona  (one separate character PER language)
# --------------------------------------------------------------------------


def _job_snippet(social_status) -> str:
    """First clause of social_status, used for the cross-character avoid list."""
    if not isinstance(social_status, str):
        return ""
    s = social_status.strip()
    for sep in ("。", ". ", "；", ";", "\n"):
        idx = s.find(sep)
        if idx > 0:
            s = s[:idx]
            break
    return s[:24]


def _recent_persona_traits(lang: str, exclude_char_id: str | None = None,
                           max_records: int = 40) -> tuple[list[str], list[str], list[str]]:
    """Collect (names, job snippets, overused tags) from recent same-language
    characters, for the diversity avoid lists injected into persona prompts."""
    records = [rec for rec in _persona_snapshot()
               if rec.get("lang") == lang and rec.get("char_id") != exclude_char_id]
    records.sort(key=lambda r: r.get("created", 0), reverse=True)
    records = records[:max_records]

    names: list[str] = []
    jobs: list[str] = []
    tag_counts: dict[str, int] = {}
    for rec in records:
        persona = rec.get("persona") or {}
        name = persona.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
        job = _job_snippet(persona.get("identity") or persona.get("social_status"))
        if job:
            jobs.append(job)
        for t in persona.get("tags") or []:
            if isinstance(t, str) and t.strip():
                tag_counts[t.strip()] = tag_counts.get(t.strip(), 0) + 1
    # 只把真正扎堆的 tag 列入避讓（≥3 次且覆蓋 ≥20% 的近期角色）
    threshold = max(3, len(records) // 5)
    overused = [t for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])
                if c >= threshold]
    # 去重保序
    names = list(dict.fromkeys(names))
    jobs = list(dict.fromkeys(jobs))
    return names, jobs, overused[:8]


def _postprocess_persona(persona: dict) -> dict:
    """Strip production-only scratch keys that must never land in the persona.

    behavior_patterns / online_chat_style 現在【所有角色都寫】（不同情緒下的反應差異），
    不按"是否普通人"剝離；personality/inner_structure 的寫法也交給 PERSONALITY_RULES
    引導，本函式只清理與人設內容無關的生產臺賬欄位。
    """
    if not isinstance(persona, dict):
        return persona
    # used_seeds 是靈感手牌的生產臺賬（real track），不屬於人設內容；
    # 正常路徑在生成處已取走，這裡防禦性剝離，保證任何路徑都不落進 persona。
    persona.pop("used_seeds", None)
    # _reasoning 是"先推理出這個人是誰"的思考區（real track 人設 prompt 要求作為第一個
    # 欄位輸出），只用於引導模型先想清人再寫賣點，不屬於人設內容，剝離。
    persona.pop("_reasoning", None)
    return persona


def _charm_audit(persona: dict, lang: str) -> dict:
    """real track 冷讀驗收（轉述測試）。API 異常時返回 skip，絕不阻塞生產。"""
    try:
        messages = prompts.build_persona_audit_messages(persona, lang)
        res = api_client.chat_json(messages, temperature=0.2)
        if isinstance(res, dict) and res.get("verdict") in ("pass", "fail"):
            return {
                "verdict": res["verdict"],
                "retell": str(res.get("retell", "")),
                "reason": str(res.get("reason", "")),
            }
    except Exception:
        pass
    return {"verdict": "skip", "retell": "", "reason": "audit unavailable"}


def _audit_retry_hint(user_hint: str, audit: dict) -> str:
    """把冷讀驗收的失敗意見拼回創作補充要求，用於打回重寫。"""
    feedback = (
        "# 上一版未透過冷讀驗收（轉述測試）\n"
        f"陌生讀者只能轉述出：「{audit.get('retell', '')}」；判定理由：{audit.get('reason', '')}。\n"
        "重寫時修正：給出更具體、帶畫面、只屬於這個人的行為與反差，"
        "讓粉絲能用一句獨一無二的話轉述 TA；禁止退回泛用形容詞。"
    )
    base = user_hint.strip()
    return f"{base}\n\n{feedback}" if base else feedback


def _generate_persona_real(uris: list[str], lang: str, user_hint: str,
                           diversity_block: str,
                           track: str) -> tuple[dict, list[str], object]:
    """一次人設生成呼叫：返回 (persona, used_seeds, reasoning)。

    reasoning 是模型在 _reasoning 欄位裡做的"先推理出這個人"的思考，從 persona 裡
    剝出來單獨儲存（不進下游 prompt），僅用於在前端"完整人設 JSON"裡展示。
    """
    messages = prompts.build_persona_messages(
        uris, lang, user_hint=user_hint, diversity_block=diversity_block,
        track=track)
    persona = api_client.chat_json(messages, temperature=0.85)
    used: list = []
    reasoning = None
    if isinstance(persona, dict):
        raw = persona.pop("used_seeds", [])
        if isinstance(raw, list):
            used = raw
        reasoning = persona.get("_reasoning")
    return _postprocess_persona(persona), used, reasoning


def create_persona_one_lang(image_paths: list[str], lang: str,
                            user_hint: str = "", group_id: str | None = None,
                            track: str = "real", source: str = "",
                            style: str = "real") -> dict:
    """Create a single-language character: persona authored natively in `lang`."""
    style = normalize_production_style(style)
    uris = [api_client.file_to_data_uri(p) for p in image_paths]
    names, jobs, tags = _recent_persona_traits(lang)
    diversity_block = prompts.build_persona_diversity_block(
        lang, avoid_names=names, recent_jobs=jobs, overused_tags=tags, track=track)
    # real track：發一手靈感牌（性格/職業庫，冷卻過濾後隨機），
    # 模型自主決定用不用，用了哪條透過 used_seeds 回報，編排層據此銷賬。
    # 只有 real 發牌：light 保留自己的關係引擎、kdrama 走韓劇主角推理，隨機職業/性格卡
    # 都會干擾。light 雖已對齊 real 的魅力方法論，但使用者明確不加職業/性格靈感（只留冷讀驗收）。
    if track == "real":
        hand = library.checkout()
        hand_block = library.hand_block(hand, lang)
        if hand_block:
            diversity_block = f"{diversity_block}\n\n{hand_block}"

    persona, used_seeds, reasoning = _generate_persona_real(
        uris, lang, user_hint, diversity_block, track)

    # real/light/nonhuman track：冷讀驗收（轉述測試），fail 則帶意見打回重寫一次。
    # nonhuman = real 方法論（要求"一句能轉述這隻 TA"），故同樣驗收；但它【不發靈感手牌】
    # （職業/性格庫是人向的，會把角色拽回"人"），手牌仍只發給 real（見上）。
    # kdrama 不走此驗收：它是 real track 的"粉絲轉述測試"，retry 意見也是 real 方法論
    # （讓粉絲能一句轉述 TA），與韓劇主角/對手戲方法論衝突——kdrama 同 adult 不發牌不驗收。
    audits: list[dict] = []
    if track in ("real", "light", "flirt", "nonhuman"):
        audit = _charm_audit(persona, lang)
        audits.append(audit)
        if audit["verdict"] == "fail":
            persona, used_seeds, reasoning = _generate_persona_real(
                uris, lang, _audit_retry_hint(user_hint, audit),
                diversity_block, track)
            audits.append(_charm_audit(persona, lang))
        used_seeds = library.commit(used_seeds)
    else:
        used_seeds = []
    _randomize_voice(persona, lang)
    # This is an operational character category selected by the producer, not
    # an image-generation prompt or a source label.
    persona["style"] = style

    char_id = _new_id("char")
    record = {
        "char_id": char_id,
        "lang": lang,
        "group_id": group_id or char_id,
        "created": int(time.time()),
        "source_images": image_paths,
        "user_hint": user_hint,
        "track": track,
        "source": source,
        "style": style,
        "used_seeds": used_seeds,
        "charm_audit": audits or None,
        "reasoning": reasoning,
        "persona": persona,
        "identity": None,
        "cover": None,
        "style_id": None,
    }
    save_character(record)
    return record


def create_personas_from_images(image_paths: list[str], langs: list[str],
                                user_hint: str = "",
                                track: str = "real",
                                source: str = "", style: str = "real") -> list[dict]:
    """For each selected language, create an independent native character record.

    All records from the same upload share a `group_id`.
    """
    langs = [l for l in langs if l in config.LANGUAGES] or [
        config.LANGUAGES[0]]
    group_id = _new_id("grp")
    records = []

    def _one(lang: str) -> dict:
        return create_persona_one_lang(
            image_paths, lang, user_hint=user_hint, group_id=group_id,
            track=track, source=source, style=style
        )

    with ThreadPoolExecutor(max_workers=min(len(langs), config.MAX_WORKERS)) as ex:
        futures = {ex.submit(_one, l): l for l in langs}
        for fut in as_completed(futures):
            records.append(fut.result())
    # keep stable order matching langs
    order = {l: i for i, l in enumerate(langs)}
    records.sort(key=lambda r: order.get(r["lang"], 99))
    return records


@_locked
def regenerate_persona(char_id: str, track: str | None = None) -> dict:
    """只重新生成人設 schema：複用同一來源（源圖 或 匯入的原始 JSON）、同語言、同補充要求，
    原地覆蓋 persona。

    不改圖、不動 identity / cover / 帖子。用於批次重刷人設。
    """
    record = load_character(char_id)
    lang = record.get("lang", config.LANGUAGES[0])
    # 介面可臨時覆蓋鏈路；覆蓋後持久化到 record，後續發帖沿用新 track
    if track:
        record["track"] = track
    track = record.get("track", "real")
    user_hint = record.get("user_hint", "")
    # 從原始 JSON 匯入的角色：用同一份 import_source 重新擴寫，保持忠實保留。
    if record.get("import_source") is not None:
        messages = prompts.build_persona_from_json_messages(
            record["import_source"], lang, user_hint=user_hint, track=track)
        persona = api_client.chat_json(messages, temperature=0.85)
        record["persona"] = _postprocess_persona(persona)
        _randomize_voice(record["persona"], lang)
    else:
        # 原圖優先；原圖不在則回退用封面圖推理，絕不無圖憑空隨機生成。
        ref_images = _regen_reference_images(record)
        if not ref_images:
            raise ValueError(
                f"{char_id} 沒有可用的原圖或封面圖，跳過重新生成（拒絕無圖隨機生成）")
        uris = [api_client.file_to_data_uri(p) for p in ref_images]
        names, jobs, tags = _recent_persona_traits(
            lang, exclude_char_id=char_id)
        diversity_block = prompts.build_persona_diversity_block(
            lang, avoid_names=names, recent_jobs=jobs, overused_tags=tags, track=track)
        # 只有 real 發靈感手牌；light/nonhuman 走冷讀驗收但不發牌（見 create 路徑註釋）。
        if track == "real":
            hand = library.checkout()
            hand_block = library.hand_block(hand, lang)
            if hand_block:
                diversity_block = f"{diversity_block}\n\n{hand_block}"
        persona, used_seeds, reasoning = _generate_persona_real(
            uris, lang, user_hint, diversity_block, track)
        audits: list[dict] = []
        if track in ("real", "light", "flirt", "nonhuman"):
            audit = _charm_audit(persona, lang)
            audits.append(audit)
            if audit["verdict"] == "fail":
                persona, used_seeds, reasoning = _generate_persona_real(
                    uris, lang, _audit_retry_hint(user_hint, audit),
                    diversity_block, track)
                audits.append(_charm_audit(persona, lang))
            record["used_seeds"] = library.commit(used_seeds)
            record["charm_audit"] = audits
        record["persona"] = persona
        record["reasoning"] = reasoning
        _randomize_voice(record["persona"], lang)
        # 早期"角色型別骰子"已移除；這裡剝掉舊存檔裡遺留的 archetype 鍵（遷移清理），
        # 新記錄本就不含該鍵。
        record.pop("archetype", None)
    # Regenerating the LLM-authored persona must not erase the producer's
    # business style selection.
    if record.get("style") in PRODUCTION_STYLES:
        record["persona"]["style"] = record["style"]
    record.pop("cover_spec", None)
    save_character(record)
    return record


# --------------------------------------------------------------------------
# Step 1 (alt): existing character JSON -> persona  (one record PER language)
# --------------------------------------------------------------------------
def _download_image(url: str) -> str | None:
    """Download a remote image into UPLOAD_DIR. Returns local path or None."""
    if not url or not isinstance(url, str) or not url.lower().startswith("http"):
        return None
    try:
        resp = requests.get(url, timeout=120)
        if not resp.ok or not resp.content:
            return None
    except requests.RequestException:
        return None
    ctype = (resp.headers.get("Content-Type") or "").lower()
    ext = ".png"
    for k, v in {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png",
                 "webp": ".webp", "gif": ".gif"}.items():
        if k in ctype:
            ext = v
            break
    dest = config.UPLOAD_DIR / \
        f"import_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}{ext}"
    try:
        dest.write_bytes(resp.content)
    except OSError:
        return None
    return str(dest)


def _extract_image_url(source_obj: dict) -> str | None:
    """Best-effort: find an image URL in an arbitrary source object."""
    if not isinstance(source_obj, dict):
        return None
    for key in ("image_url", "imageUrl", "image", "avatar", "cover", "cover_url",
                "photo", "picture", "img", "thumbnail"):
        val = source_obj.get(key)
        if isinstance(val, str) and val.lower().startswith("http"):
            return val
        if isinstance(val, list) and val and isinstance(val[0], str) \
                and val[0].lower().startswith("http"):
            return val[0]
    return None


def create_persona_from_json_one_lang(source_obj: dict, lang: str,
                                      user_hint: str = "",
                                      group_id: str | None = None,
                                      source_image: str | None = None,
                                      track: str = "real",
                                      source: str = "", style: str = "real") -> dict:
    """Create a single-language character from an existing source JSON object.

    The original object is stored under `import_source` so the persona can be
    re-expanded later. `source_image` (a local path, already downloaded) is
    shared across all language variants of the same source object.
    """
    style = normalize_production_style(style)
    messages = prompts.build_persona_from_json_messages(
        source_obj, lang, user_hint=user_hint, track=track)
    persona = api_client.chat_json(messages, temperature=0.85)
    persona = _postprocess_persona(persona)
    _randomize_voice(persona, lang)
    persona["style"] = style

    char_id = _new_id("char")
    record = {
        "char_id": char_id,
        "lang": lang,
        "group_id": group_id or char_id,
        "created": int(time.time()),
        "source_images": [source_image] if source_image else [],
        "user_hint": user_hint,
        "track": track,
        "source": source,
        "style": style,
        "import_source": source_obj,
        "persona": persona,
        "identity": None,
        "cover": None,
        "style_id": None,
    }
    save_character(record)
    return record


def create_personas_from_json_obj(source_obj: dict, langs: list[str],
                                  user_hint: str = "",
                                  download_image: bool = True,
                                  track: str = "real",
                                  source: str = "", style: str = "real") -> list[dict]:
    """For one source object, create an independent native character per language.

    All records from the same source share a `group_id` and (if available) the
    same downloaded source image.
    """
    langs = [l for l in langs if l in config.LANGUAGES] or [
        config.LANGUAGES[0]]
    group_id = _new_id("grp")
    source_image = None
    if download_image:
        source_image = _download_image(_extract_image_url(source_obj) or "")

    def _one(lang: str) -> dict:
        return create_persona_from_json_one_lang(
            source_obj, lang, user_hint=user_hint, group_id=group_id,
            source_image=source_image, track=track, source=source, style=style)

    records = []
    with ThreadPoolExecutor(max_workers=min(len(langs), config.MAX_WORKERS)) as ex:
        futures = {ex.submit(_one, l): l for l in langs}
        for fut in as_completed(futures):
            records.append(fut.result())
    order = {l: i for i, l in enumerate(langs)}
    records.sort(key=lambda r: order.get(r["lang"], 99))
    return records


def extract_source_objects(payload) -> list[dict]:
    """Normalize an uploaded JSON payload into a flat list of character objects.

    Accepts: a single object, a top-level array, or an object wrapping the list
    under a common key ("data", "characters", "items", "list", "results").
    """
    if isinstance(payload, list):
        return [o for o in payload if isinstance(o, dict)]
    if isinstance(payload, dict):
        for key in ("data", "characters", "items", "list", "results"):
            val = payload.get(key)
            if isinstance(val, list):
                return [o for o in val if isinstance(o, dict)]
        return [payload]
    return []


@_locked
def regenerate_opening(char_id: str, user_hint: str = "") -> dict:
    """只重寫角色【開場白】(persona.opening)：依據其它人設資訊生成新的 note + messages。

    不改圖、不動其它人設欄位、不動 identity / cover / 帖子。用於單獨/批次刷開場白。
    """
    record = load_character(char_id)
    persona = record.get("persona", {})
    messages = prompts.build_opening_messages(
        persona, record.get("lang", config.LANGUAGES[0]), user_hint=user_hint,
        track=record.get("track", "real"),
    )
    result = api_client.chat_json(messages, temperature=0.9)
    opening = result.get("opening", result) if isinstance(result, dict) else {}
    persona["opening"] = {
        "note": opening.get("note", ""),
        "messages": opening.get("messages", []),
    }
    record["persona"] = persona
    save_character(record)
    return record


# 上傳圖引用快照快取：批次刪除時幾乎每個角色都要判斷“共享上傳圖是否仍被引用”，
# 原本每次都全量掃本地 1500+ persona 檔案 + 遠端 query_all 整集合，N 個角色 = N 次
# 全量掃描，慢到離譜。這裡按短 TTL 快取一次“所有被引用的上傳路徑”快照，整批覆用。
# 快照可能滯後（剛刪的角色仍在快照裡）→ 只會讓判斷更保守（跳過刪共享圖，留下孤兒
# 上傳件），絕不會誤刪仍被引用的圖；孤兒件可由存量遷移/清理另行回收。
_REF_SNAPSHOT: dict = {"paths": None, "exp": 0.0}
_REF_SNAPSHOT_TTL = 20  # 秒
_REF_SNAPSHOT_LOCK = _threading.Lock()


def _referenced_upload_paths() -> set[str] | None:
    """所有 persona 仍引用的 source_images 路徑集合。

    返回 None 表示啟用了遠端儲存但遠端查詢失敗——呼叫方據此 fail-safe 拒刪。
    """
    import time as _time
    now = _time.time()
    snap = _REF_SNAPSHOT["paths"]
    if snap is not None and now < _REF_SNAPSHOT["exp"]:
        return snap
    with _REF_SNAPSHOT_LOCK:
        snap = _REF_SNAPSHOT["paths"]
        if snap is not None and now < _REF_SNAPSHOT["exp"]:
            return snap
        paths: set[str] = set()
        for p in config.PERSONA_DIR.glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            paths.update(rec.get("source_images", []))
        from . import arca_storage
        if arca_storage.enabled():
            try:
                rows = storage.query_all("personas")
            except Exception:  # noqa: BLE001 遠端不可達 → 拒刪（不快取失敗態）
                return None
            for row in rows:
                rec = row.get("data") or {}
                paths.update(rec.get("source_images", []))
        _REF_SNAPSHOT["paths"] = paths
        _REF_SNAPSHOT["exp"] = now + _REF_SNAPSHOT_TTL
        return paths


def _source_image_is_referenced(path: str) -> bool:
    """Return True if any remaining character record still references upload path.

    刪除性守衛必須 fail-safe：啟用遠端儲存時若遠端查詢失敗，保守返回 True
    （視為仍被引用、跳過刪除），絕不能在只看到本地殘缺檢視時誤刪共享上傳圖。
    """
    paths = _referenced_upload_paths()
    if paths is None:  # 遠端不可達 → 拒刪
        return True
    return path in paths


# --------------------------------------------------------------------------
# Export: bundle selected characters into a single zip (one folder each)
# --------------------------------------------------------------------------
def _safe_name(text: str, fallback: str = "untitled", limit: int = 60) -> str:
    """Make a string safe for use as a file/folder name across OSes."""
    if not isinstance(text, str):
        text = str(text or "")
    text = text.replace("\n", " ").replace("\r", " ").strip()
    # strip characters illegal on common filesystems
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > limit:
        text = text[:limit].strip()
    return text or fallback


def _localized_text(value, lang: str = "zh") -> str:
    """Persona fields may be plain string or {lang: str}; pick a readable one."""
    if isinstance(value, dict):
        return value.get(lang) or value.get("zh") or next(
            (v for v in value.values() if isinstance(v, str)), "")
    return value if isinstance(value, str) else ""


def _image_bytes(image: dict | None) -> bytes | None:
    """Get image bytes: prefer the local file on disk, else download the url."""
    if not isinstance(image, dict):
        return None
    local = image.get("local_path")
    if local and storage.ensure_file(Path(local)):
        try:
            return Path(local).read_bytes()
        except OSError:
            pass
    url = image.get("url")
    if url:
        try:
            # (connect, read) 超時：避免壞鏈/慢源永久掛住匯出執行緒。
            resp = requests.get(url, timeout=(10, 60))
            if resp.ok and resp.content:
                return resp.content
        except requests.RequestException:
            return None
    return None


# 匯出兜底：真實社交平臺名一律替換成 Popop（prompt 層已要求生成時就用 Popop，
# 這裡只是最後一道保險）。長詞在前，避免"인스타그램"被"인스타"截斷替換。
_BRAND_TOKEN_REPLACEMENTS = [
    "Instagram", "instagram", "INSTAGRAM",
    "인스타그램", "인스타", "インスタグラム", "インスタ",
    "小紅書", "Threads", "threads", "스레드", "スレッズ",
    "Twitter", "twitter", "推特", "트위터", "ツイッター",
    "TikTok", "tiktok", "틱톡", "抖音",
    # KakaoTalk：長詞在前，避免"카카오톡"被"카카오"截斷替換。
    "KakaoTalk", "kakaotalk", "KAKAOTALK", "Kakao", "kakao",
    "카카오톡", "카톡", "カカオトーク",
]
# 短縮寫只在無字母上下文時替換（"發個ins"命中，"insert/<ins>"不命中）。
_BRAND_SHORT_RE = re.compile(r"(?<![A-Za-z</])(?:ins|IG)(?![A-Za-z>])")
# LINE 單獨處理：英文"line"是常見普通詞（online / a line），直接替換會誤傷。
# 只命中全大寫 LINE（無字母上下文）、韓語 라인、日語 ライン。
_BRAND_LINE_RE = re.compile(r"(?<![A-Za-z</])LINE(?![A-Za-z>])|라인|ライン")


def _apply_brand_replacements(text: str) -> str:
    """Replace real social-platform names with Popop in exported text."""
    for token in _BRAND_TOKEN_REPLACEMENTS:
        if token in text:
            text = text.replace(token, "Popop")
    text = _BRAND_LINE_RE.sub("Popop", text)
    return _BRAND_SHORT_RE.sub("Popop", text)


# 已上傳公有桶的圖片直鏈快取：key = object_key + 內容 md5，value = 公網 URL。
# 匯出/同步會為封面/帖圖反覆傳同樣的圖，快取後重復匯出可直接命中、跳過上傳。
_TOS_URL_CACHE_PATH = config.DATA_DIR / "tos_public_url_cache.json"
_TOS_URL_CACHE_LOCK = _threading.Lock()
_TOS_URL_CACHE: dict[str, str] | None = None


def _tos_cache() -> dict[str, str]:
    global _TOS_URL_CACHE
    if _TOS_URL_CACHE is None:
        try:
            _TOS_URL_CACHE = json.loads(
                _TOS_URL_CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(_TOS_URL_CACHE, dict):
                _TOS_URL_CACHE = {}
        except (OSError, ValueError):
            _TOS_URL_CACHE = {}
    return _TOS_URL_CACHE


def _tos_cache_put(key: str, url: str) -> None:
    with _TOS_URL_CACHE_LOCK:
        cache = _tos_cache()
        cache[key] = url
        try:
            tmp = _TOS_URL_CACHE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_TOS_URL_CACHE_PATH)
        except OSError:
            pass


def _tos_public_url(data: bytes, object_key: str, lang: str) -> str | None:
    """Upload bytes to the public TOS bucket, return the public https URL.

    落地頁 img 需指向公網 URL（不內聯 base64），封面/帖圖先傳公有桶拿直鏈。
    相同內容+物件鍵命中快取則直接返回，跳過重複上傳（大幅加速重複匯出）。
    失敗返回 None，呼叫方自行降級。"""
    if not data:
        return None
    cache_key = object_key + ":" + hashlib.md5(data).hexdigest()
    cached = _tos_cache().get(cache_key)
    if cached:
        return cached
    from . import arca_client
    try:
        obj = arca_client.tos_upload(data, object_key, "image/png", lang,
                                     public=True)
        url = obj.get("url")
        if url:
            _tos_cache_put(cache_key, url)
        return url
    except Exception:  # noqa: BLE001 上傳失敗降級，不阻斷匯出/同步
        return None


def _landing_html_with_public_urls(
    html: str, char_id: str, lang: str,
    cover_url: str | None, cover_bytes: bytes | None,
    post_urls: list[str] | None,
) -> str:
    """把落地頁裡的封面/帖圖替換成【公網 TOS URL】（img 指向 url，不用 base64）。

    複用 inject_cover / inject_post_images：只是注入的是公網 URL 而非 /img/ 相對路徑，
    同時把落地頁裡殘留的 /img/ 引用替換成公網 URL。上傳失敗的圖保持原樣（不內聯）。
    """
    # 1) 封面 → 公有桶
    if cover_bytes:
        pub = _tos_public_url(
            cover_bytes, f"creaction/{char_id}/landing/cover.png", lang)
        if pub:
            if cover_url:
                # html_filled 裡封面可能是相對 /img/… 也可能已被改寫成帶域名的
                # 絕對 URL（config.PUBLIC_BASE_URL），兩種形態都替換成公有桶直鏈。
                # 先替換更長的絕對形態，再替換相對形態：否則相對串是絕對串的
                # 子串，先換相對會把絕對 URL 攔腰截斷，拼出 http://域名https://直鏈。
                abs_cover = landing.absolutize_urls(
                    f'src="{cover_url}"', config.PUBLIC_BASE_URL)[len('src="'):-1]
                if abs_cover != cover_url:
                    html = html.replace(abs_cover, pub)
                html = html.replace(cover_url, pub)
            html = landing.inject_cover(html, pub)

    # 2) 帖圖 → 公有桶（逐個上傳、逐個替換 /img/ 引用並填空槽位）
    for idx, url in enumerate(post_urls or [], start=1):
        if not url:
            continue
        name = Path(url).name
        p = config.IMAGE_DIR / name
        if not storage.ensure_file(p):
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        pub = _tos_public_url(
            data, f"creaction/{char_id}/landing/{name}", lang)
        if not pub:
            continue
        # 同封面：先替換絕對形態再替換相對形態，避免子串截斷拼壞 URL。
        abs_url = landing.absolutize_urls(
            f'src="{url}"', config.PUBLIC_BASE_URL)[len('src="'):-1]
        if abs_url != url:
            html = html.replace(abs_url, pub)
        html = html.replace(url, pub)
        html = landing.inject_post_images(html, _slot_fill(idx, pub))
    return html


def _slot_fill(idx: int, url: str) -> list[str]:
    """Build a sparse post_urls list that only sets slot `idx` (1-based)."""
    return [""] * (idx - 1) + [url]


def _post_image_desc(post: dict) -> str:
    """交付用的「生圖 prompt」：取原始生成意圖，不含畫風/收尾等後綴。

    - photo / composite：直接用模型產出的 photo_prompt。
    - selfie：photo_prompt 為 null，把 selfie 的 variable/shooting/scene 拍平成一段描述。
    - text_only 或無圖：空字串。
    """
    if post.get("format") == "text_only":
        return ""
    raw = post.get("photo_prompt")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    selfie = post.get("selfie")
    if isinstance(selfie, dict):
        parts: list[str] = []
        for section in ("variable", "shooting", "scene"):
            block = selfie.get(section)
            if isinstance(block, dict):
                parts.extend(str(v).strip() for v in block.values() if str(v).strip())
            elif isinstance(block, str) and block.strip():
                parts.append(block.strip())
        return "；".join(parts)
    return ""


def _import_main_image(char_id: str, lang: str, provider: str,
                       cover_bytes: bytes | None) -> list[dict]:
    """Upload the required import main image to the private bucket.

    ImportCharacterReq consumes a StorageObject and resolves its primary image
    by bucket/object_key.  It is intentionally separate from the public landing
    asset upload: character images may stay private, while landing assets must
    be public.
    """
    if not cover_bytes:
        return []
    from . import arca_client
    obj = arca_client.tos_upload(
        cover_bytes,
        f"character_import/{provider}/{char_id}/main.png",
        "image/png",
        lang,
        public=False,
    )
    return [{"image_type": "aigc", "is_main_pic": True, "media": obj}]


def _build_import_landing(
    *, char_id: str, lang: str, provider: str, cover_bytes: bytes | None,
    public_asset_hosts: set[str],
) -> tuple[str | None, str | None]:
    """Prepare and upload a contract-compliant public landing page, if any."""
    landing_page = load_latest_landing(char_id) or {}
    landing_html = landing_page.get("html_filled") or landing_page.get("html")
    if not landing_html:
        return None, None

    from . import arca_client
    from .arca_import_export import (
        ContractViolation,
        ImportCharacterContractError,
        validate_landing_html,
    )

    landing_html = _apply_brand_replacements(_landing_html_with_public_urls(
        landing_html, char_id, lang,
        landing_page.get("cover_url"), cover_bytes,
        landing_page.get("post_urls")))
    issues = validate_landing_html(landing_html, public_asset_hosts)
    if issues:
        raise ImportCharacterContractError(issues)
    object_key = f"landing_import/{provider}/{char_id}/index.html"
    landing = arca_client.tos_upload(
        landing_html.encode("utf-8"), object_key,
        "text/html; charset=utf-8", lang, public=True)
    landing_url = landing.get("url")
    if not landing_url:
        raise ImportCharacterContractError([
            ContractViolation("landing.html", "public upload did not return a URL")])
    return landing_url, landing_html


def _build_character_files(char_id: str) -> tuple[str, list[tuple[str, bytes | str]], dict]:
    """Build exactly one importing-arca-character delivery JSON.

    Persona generation is deliberately left unchanged.  This export boundary
    fetches the live enum configuration, uploads contract assets, maps the
    stored persona to ImportCharacterReq, then rejects the entire file on any
    contract violation.
    """
    record = load_character(char_id)
    persona = record.get("persona", {})
    lang = record.get("lang", "zh")

    name = _localized_text(persona.get("name"), lang) or char_id
    folder_base = _safe_name(name, fallback=char_id)
    report = {"char_id": char_id, "folder": folder_base, "images": 0, "missing": []}

    from . import arca_client
    from .arca_import_export import (
        ContractViolation,
        ImportCharacterContractError,
        assert_valid_import_character_req,
        build_import_character_req,
    )

    # Enums (especially voice_id) are runtime API data.  Do not fall back to a
    # static list or silently produce a JSON that will later be rejected.
    page_config = arca_client.get_page_config_cached(lang)
    provider = str(record.get("source") or config.ARCA_IMPORT_PROVIDER).strip()
    if not provider:
        raise ImportCharacterContractError([
            ContractViolation("request.provider", "must be configured")])

    cover_bytes = _image_bytes(record.get("cover"))
    if not cover_bytes:
        raise ImportCharacterContractError([
            ContractViolation(
                "request.character_create_form.images",
                "a source cover is required to export the contract main image")])

    # Validate persona content and runtime enum values before creating durable
    # OSS objects.  The temporary image is structurally valid and lets the
    # common validator inspect every non-asset part of the final request.
    preflight = build_import_character_req(
        record,
        page_config=page_config,
        images=[{
            "image_type": "upload",
            "is_main_pic": True,
            "media": {"object_key": "preflight"},
        }],
        landing_page_url=None,
        provider=provider,
    )
    assert_valid_import_character_req(
        preflight, page_config=page_config)

    # Obtain the public bucket/CDN identity only after persona preflight.  The
    # host set is required to prove landing assets are final public URLs.
    public_asset_hosts = arca_client.public_tos_hosts(lang)
    images = _import_main_image(char_id, lang, provider, cover_bytes)
    if images:
        report["images"] += 1

    landing_url, landing_html = _build_import_landing(
        char_id=char_id, lang=lang, provider=provider, cover_bytes=cover_bytes,
        public_asset_hosts=public_asset_hosts)
    request = build_import_character_req(
        record, page_config=page_config, images=images,
        landing_page_url=landing_url, provider=provider)
    assert_valid_import_character_req(
        request, page_config=page_config, public_asset_hosts=public_asset_hosts)

    files: list[tuple[str, bytes | str]] = [(
        "character.json", json.dumps(request, ensure_ascii=False, indent=2))]
    if landing_html:
        files.append(("landing.html", landing_html))

    # mark the character record as exported
    record["exported"] = True
    record["exported_at"] = int(time.time())
    save_character(record)

    return folder_base, files, report


def export_characters_zip(char_ids: list[str]) -> bytes:
    """Bundle the given characters into a zip (one folder per character).

    Each ``character.json`` is a complete, directly importable
    ImportCharacterReq.  ``landing.html`` is included only as an optional
    source copy; the JSON points at the final public-bucket HTML URL.  Images
    are never bundled as binaries.

    每個角色的重活（OSS 下載 + TOS 上傳 + 文案處理）跨角色並行；zip 寫入
    （非執行緒安全）在主執行緒序列完成。保序輸出，保證結果與序列版一致。

    注意：同步版把整份 zip 囤在記憶體並阻塞請求，僅適合小批次。大批次走
    export_characters_zip_to_file + 非同步任務，避免反代讀超時（504）。
    """
    results = _build_all_character_files(char_ids)
    buf = io.BytesIO()
    _write_zip(buf, char_ids, results)
    buf.seek(0)
    return buf.getvalue()


def _write_zip(dst, char_ids: list[str], results: dict[str, tuple]) -> None:
    """把已構建好的角色檔案寫進 zip（dst 為檔案物件或路徑）。序列、保序。"""
    used_folders: set = set()
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for cid in char_ids:  # 保持請求順序
            built = results.get(cid)
            if built is None:
                continue
            folder_base, files, _ = built
            folder, idx = folder_base, 2
            while folder in used_folders:
                folder = f"{folder_base} ({idx})"
                idx += 1
            used_folders.add(folder)
            for rel, content in files:
                zf.writestr(f"{folder}/{rel}", content)


def _build_all_character_files(char_ids: list[str],
                               on_done=None) -> dict[str, tuple]:
    """並行構建所有角色的檔案（重活）。on_done(cid) 每完成一個回撥一次，
    供任務進度上報。返回 {cid: (folder_base, files, report)}。"""
    def _one(cid: str):
        try:
            return cid, _build_character_files(cid)
        except FileNotFoundError:
            return cid, None

    results: dict[str, tuple] = {}
    if char_ids:
        workers = min(len(char_ids), config.EXPORT_CONCURRENCY)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for cid, built in ex.map(_one, char_ids):
                if built is not None:
                    results[cid] = built
                if on_done:
                    on_done(cid)
    return results


def export_characters_zip_to_file(char_ids: list[str], dst_path: Path,
                                  on_done=None) -> int:
    """非同步匯出用：並行構建後把 zip 直接寫到磁碟檔案（不在記憶體裡囤 160MB+）。
    返回成功打包的角色數。"""
    results = _build_all_character_files(char_ids, on_done=on_done)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dst_path, "wb") as f:
        _write_zip(f, char_ids, results)
    return len(results)


@_locked
@_locked
def delete_character(char_id: str) -> bool:
    """刪除一個角色及所有以角色 id 歸屬的資料。

    持角色鎖執行：與後臺同步/生成互斥，避免刪除中途被併發寫“復活”。
    """
    p = _char_path(char_id)
    record = None
    deleted_any = False

    if p.exists():
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            record = None

    # 遠端儲存聯動刪除：persona 記錄 + 該角色的批次/ig/landing/chat 記錄。
    # 注意順序：遠端刪除成功後才刪本地檔案——否則遠端失敗重試時 record 已丟，
    # 共享上傳圖清理會被跳過。
    # 遠端刪除失敗必須上拋（否則殘留記錄會被 list/load 回源"復活"），由上層報錯重試。
    from . import arca_storage
    if arca_storage.enabled():
        arca_storage.delete_record("personas", char_id)
        for coll in ("ig_batches", "landings"):
            arca_storage.delete_record(coll, char_id)
        for coll in ("post_batches", "chats"):
            for row in arca_storage.query_records(
                    coll, match={"char_id": char_id}, limit=500):
                if row.get("key"):
                    arca_storage.delete_record(coll, row["key"])
        deleted_any = True
        # OSS 圖片/目錄級聯清理（盡力而為）：不清會永久殘留並被 /img 回源復活。
        # 合併成單次呼叫：只取一次 STS 憑證 + 批次刪，避免逐字首重複取憑證/逐個刪。
        storage.delete_oss_prefixes(
            [f"images/{char_id}_", f"posts/{char_id}/",
             f"landing/{char_id}/", f"chat/{char_id}/"])

    if p.exists():
        p.unlink()
        deleted_any = True

    for img in config.IMAGE_DIR.glob(f"{char_id}_*.png"):
        img.unlink(missing_ok=True)
        deleted_any = True

    post_dir = config.POST_DIR / char_id
    if post_dir.exists():
        shutil.rmtree(post_dir, ignore_errors=True)
        deleted_any = True

    landing_dir = config.LANDING_DIR / char_id
    if landing_dir.exists():
        shutil.rmtree(landing_dir, ignore_errors=True)
        deleted_any = True

    chat_dir = config.CHAT_DIR / char_id
    if chat_dir.exists():
        shutil.rmtree(chat_dir, ignore_errors=True)
        deleted_any = True

    # Upload files can be shared by multiple language variants. Delete only when
    # no remaining persona still points at the same uploaded source image.
    if record:
        for src in record.get("source_images", []):
            src_path = Path(src)
            try:
                if (
                    src_path.exists()
                    and src_path.parent == config.UPLOAD_DIR
                    and not _source_image_is_referenced(src)
                ):
                    src_path.unlink(missing_ok=True)
                    storage.delete_oss_file(src_path)
                    deleted_any = True
            except OSError:
                pass

    return deleted_any


# --------------------------------------------------------------------------
# Step 2: persona -> identity (appearance DNA)
# --------------------------------------------------------------------------
@_locked
def build_identity(char_id: str) -> dict:
    """Build stable appearance DNA from the persona and one main visual anchor.

    Persona generation can use multiple images, but identity should not average
    across multiple faces or photo styles. Use the first available source image
    as the character's main visual reference.
    """
    record = load_character(char_id)
    identity_ref = _first_source_image(record)
    uris = [api_client.file_to_data_uri(identity_ref)] if identity_ref else []
    messages = prompts.build_identity_messages(record["persona"], uris)
    identity = api_client.chat_json(messages, temperature=0.5)
    record["identity"] = identity
    if identity_ref:
        record["identity_reference_image"] = identity_ref
    save_character(record)
    return record


# --------------------------------------------------------------------------
# Step 3: cover image (identity + cover variable/scene + chosen style)
# --------------------------------------------------------------------------
def build_cover_spec(char_id: str, ref_override: str | None = None) -> dict:
    """Generate the cover-specific variable + scene block.

    Persona generation can use multiple source images, and identity can use one
    main visual anchor. Cover planning stays text-only so the shooting/filter
    schema comes from the character rather than copying the uploaded photo.

    ref_override: 顯式指定封面規劃參考圖（本地路徑）。用於"以現有封面為參考重跑
    封面鏈路"的場景（source=image 的角色希望遠離原圖但保留氣質）。傳入時優先於源圖。
    """
    record = load_character(char_id)
    if not record.get("identity"):
        build_identity(char_id)
        record = load_character(char_id)

    track = record.get("track", "real")
    cover_ref = ref_override or _first_source_image(record)
    lang = record.get("lang", "ko")

    # 封面規劃【喂原圖】：real/light（按使用者要求，封面錨定原圖，讓封面像上傳的這個人）
    # 及 adult/nonhuman 都把原圖作為參考傳入。只有 kdrama 例外——它是"這部劇的第一幀劇照"，
    # 從人設重新設計場景，喂原圖會把鏡頭拉回自拍快照，故仍純文字規劃。
    if track == "kdrama":
        cover_ref_uri = None
    else:
        cover_ref_uri = api_client.file_to_data_uri(
            cover_ref) if cover_ref else None
    messages = prompts.build_cover_spec_messages(
        record["persona"], record["identity"], cover_ref_uri, track=track,
        lang=lang)
    spec = api_client.chat_json(messages, temperature=0.65)
    record["cover_spec"] = {
        "variable": spec.get("variable", {}),
        "shooting": spec.get("shooting", {}),
        "scene": spec.get("scene", {}),
        "version": COVER_SPEC_VERSION,
    }
    if cover_ref_uri and cover_ref:
        record["cover_spec"]["reference_image"] = cover_ref
    save_character(record)
    return record


@_locked
def generate_cover(
    char_id: str,
    style_id: str | None,
    use_reference: bool | None = None,
    mode: str = "fill_missing",
    recook_from_cover: bool = False,
) -> dict:
    """Generate a cover image.

    use_reference:
    - None (default): auto — use the source image as an i2i reference whenever it
      exists. This works both for identity consistency and for preserving a supplied
      illustration's original visual language without inventing a style prompt.
    - True/False: explicit override.

    mode:
    - "fill_missing": keep existing identity/spec, generate only missing data.
    - "full": regenerate identity and cover_spec before rendering image.
    - "image_only": render image only; fail if identity or cover_spec is missing.

    recook_from_cover: 以角色【當前封面】而非源圖作為參考重跑封面鏈路。用於 source=image
    的角色——原封面太像上傳原圖，希望保留氣質但漂離原圖。開啟時：cover_spec 用當前封面
    做規劃參考，i2i 渲染也用當前封面而非源圖（源圖不再進入任何一步）。會強制重建 cover_spec。
    """
    record = load_character(char_id)
    if mode not in {"fill_missing", "full", "image_only"}:
        raise ValueError(f"unknown cover generation mode {mode}")

    recook_ref: str | None = None
    if recook_from_cover:
        recook_ref = _cover_as_local_image(record)
        if not recook_ref:
            raise ValueError("recook_from_cover requires an existing cover image")

    if recook_from_cover:
        if not record.get("identity"):
            build_identity(char_id)
        build_cover_spec(char_id, ref_override=recook_ref)
        record = load_character(char_id)
    elif mode == "full":
        build_identity(char_id)
        build_cover_spec(char_id)
        record = load_character(char_id)
    elif mode == "image_only":
        if not record.get("identity"):
            raise ValueError("image_only mode requires existing identity")
        if not record.get("cover_spec"):
            raise ValueError("image_only mode requires existing cover_spec")
    elif not record.get("identity"):
        build_identity(char_id)
        record = load_character(char_id)

    cover_spec = record.get("cover_spec") or {}
    if not recook_from_cover and mode == "fill_missing" and (
        not cover_spec or cover_spec.get("version") != COVER_SPEC_VERSION
    ):
        build_cover_spec(char_id)
        record = load_character(char_id)

    track = record.get("track", "real")
    # 沒有明確選擇 style_id 時，一律不猜測/不拼畫風詞；由圖生圖參考保持原始視覺。
    # 顯式選擇的 style_id 才會注入使用者畫風庫中的 prompt。
    if not style_id:
        style = None
        eff_style_id = None
    else:
        style = styles.get_style(style_id)
        if not style:
            raise ValueError(f"unknown style {style_id}")
        eff_style_id = style_id
    style_prompt = style["prompt"] if style else None

    identity = record["identity"]
    cover_spec = record.get("cover_spec", {})
    mood = identity.get("persona_mood", "")
    prompt = prompts.cover_image_prompt(
        identity,
        style_prompt,
        persona_mood=mood,
        variable=cover_spec.get("variable"),
        scene=cover_spec.get("scene"),
        shooting=cover_spec.get("shooting"),
        track=track,
    )
    if use_reference is None:
        # 不論是否顯式選畫風，都傳原圖作 i2i 參考：它鎖角色設計與原始作畫語言；
        # 場景/距離/構圖仍由 cover_spec 重新設計。
        use_reference = True
    image_urls = None
    if use_reference:
        # recook_from_cover：i2i 參考用當前封面（recook_ref），源圖不再進入渲染，
        # 讓新封面漂離上傳原圖但保留氣質。否則維持原行為（源圖做參考）。
        src = recook_ref or _first_source_image(record)
        if src:
            image_urls = [api_client.file_to_data_uri(src)]

    save_path = config.IMAGE_DIR / \
        f"{char_id}_cover_{eff_style_id or 'nostyle'}.png"
    result = api_client.generate_image(
        prompt,
        size=config.IMAGE_SIZE_COVER,
        resolution=config.IMAGE_RESOLUTION,
        image_urls=image_urls,
        save_path=save_path,
    )
    record["style_id"] = eff_style_id
    record["cover"] = {
        "style_id": eff_style_id,
        "url": result["url"],
        "local_path": result["local_path"],
        "prompt": prompt,
        "spec": cover_spec,
    }
    save_character(record)
    return record


# --------------------------------------------------------------------------
# Step 4: batch posts (text 4-lang + variable + scene), then images
# --------------------------------------------------------------------------
def _posts_dir(char_id: str) -> Path:
    d = config.POST_DIR / char_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _render_post_image(record: dict, post: dict, style: dict | None,
                       drop_identity_if_ref: bool = False,
                       image_model: str | None = None) -> dict:
    """Render or re-render one regular post image in-place.

    style 可為 None（nonhuman 非人物鏈路不套畫風），此時 style_prompt=None，
    compose_image_prompt 不拼畫風詞。
    drop_identity_if_ref=True 且確實傳了參考圖時，省略 [IDENTITY] 靜態長相段
    （臉交給 i2i 錨圖；如 feed 路透 candid）。無參考圖時仍保留 identity 兜底。
    """
    char_id = record["char_id"]
    identity = record["identity"]
    style_prompt = style["prompt"] if style else None
    save_path = config.IMAGE_DIR / f"{char_id}_{post['post_id']}.png"
    image_urls = None
    # 不論有無 style prompt，都以封面作同一個角色的視覺錨。這裡不推斷或追加畫風詞。
    ref = _ref_image_uri_for_selfie(record)
    if ref:
        image_urls = [ref]
    prompt = prompts.compose_image_prompt(
        identity, post["variable"], post["scene"], style_prompt,
        track=record.get("track", "real"),
        include_identity=not (drop_identity_if_ref and bool(image_urls)),
    )
    res = api_client.generate_image(
        prompt,
        size=config.IMAGE_SIZE_POST,
        resolution=config.IMAGE_RESOLUTION,
        image_urls=image_urls,
        save_path=save_path,
        model=image_model,
    )
    post["image"] = {
        "url": res["url"],
        "local_path": res["local_path"],
        "prompt": prompt,
        "used_reference": bool(image_urls),
    }
    return post


@_locked
def generate_posts(
    char_id: str,
    post_type_ids: list[str],
    count_per_type: int = 2,
    style_id: str | None = None,
    with_images: bool = True,
    track: str | None = None,
) -> dict:
    """Generate posts for each selected type, then optionally render images."""
    record = load_character(char_id)
    if track and record.get("track") != track:
        record["track"] = track  # 介面臨時覆蓋鏈路並持久化
        save_character(record)
    if not record.get("identity"):
        build_identity(char_id)
        record = load_character(char_id)

    persona = record["persona"]
    identity = record["identity"]
    track = record.get("track", "real")
    style_id = style_id or record.get("style_id")
    style = styles.get_style(style_id) if style_id else None
    if track == "nonhuman":
        style = None  # 非人物鏈路：不套畫風、不拼畫風詞

    # 1) generate all post text + variable/scene (parallel across types)
    posts: list[dict] = []

    def _gen_type(pt_id: str) -> list[dict]:
        pt = prompts.POST_TYPE_BY_ID.get(pt_id)
        if not pt:
            return []
        msgs = prompts.build_post_messages(
            persona, identity, pt, record.get("lang", config.LANGUAGES[0]),
            count=count_per_type, track=record.get("track", "real"),
        )
        items = api_client.chat_json(msgs, temperature=0.9)
        if isinstance(items, dict):
            items = [items]
        out = []
        for it in items:
            out.append({
                "post_id": _new_id("post"),
                "type_id": pt_id,
                "type_name": pt["name"],
                "content": it.get("content", {}),
                "variable": it.get("variable", {}),
                "scene": it.get("scene", {}),
                "image": None,
            })
        return out

    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        futures = {ex.submit(_gen_type, pid): pid for pid in post_type_ids}
        for fut in as_completed(futures):
            posts.extend(fut.result())

    # 2) render images (parallel) using identity + variable + scene + optional style.
    # style=None 仍可走圖生圖參考，且不會憑空追加任何畫風描述。
    if with_images:
        def _render(post: dict) -> dict:
            try:
                _render_post_image(record, post, style)
            except Exception as e:  # noqa: BLE001 keep batch resilient
                post["image"] = {"error": str(e)}
            return post

        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
            list(ex.map(_render, posts))

    batch_id = _new_id("batch")
    batch = {
        "batch_id": batch_id,
        "char_id": char_id,
        "lang": record.get("lang"),
        "created": int(time.time()),
        "style_id": style_id,
        "post_type_ids": post_type_ids,
        "count_per_type": count_per_type,
        "with_images": with_images,
        "posts": posts,
    }
    storage.save_json("post_batches", f"{char_id}__{batch_id}", batch,
                      _posts_dir(char_id) / f"{batch_id}.json")
    return batch


@_locked
def rerender_post_image(char_id: str, batch_id: str, post_id: str,
                        style_id: str | None = None) -> dict:
    """Re-render one image in a regular post batch without regenerating text/spec."""
    record = load_character(char_id)
    if not record.get("identity"):
        build_identity(char_id)
        record = load_character(char_id)
    batch = load_batch(char_id, batch_id)
    style_id = style_id or batch.get("style_id") or record.get("style_id")
    style = styles.get_style(style_id) if style_id else None
    if record.get("track") == "nonhuman":
        style = None  # 非人物鏈路：不套畫風
    for post in batch.get("posts", []):
        if post.get("post_id") == post_id:
            _render_post_image(record, post, style)
            batch["style_id"] = style_id
            storage.save_json("post_batches", f"{char_id}__{batch_id}", batch,
                              _posts_dir(char_id) / f"{batch_id}.json")
            return {"post": post, "batch": batch}
    raise ValueError(f"post {post_id} not found in batch {batch_id}")


def _delete_post_image(post: dict) -> None:
    image = post.get("image") or {}
    local_path = image.get("local_path")
    if local_path:
        try:
            p = Path(local_path)
            if p.exists() and p.parent == config.IMAGE_DIR:
                p.unlink(missing_ok=True)
        except OSError:
            pass


@_locked
def update_post_content(char_id: str, batch_id: str, post_id: str,
                        content, variable=None, scene=None) -> dict:
    """就地更新一條普通帖子的文字（content），可選 variable/scene。不動圖片。

    content 保持與生成時相同的形態（str 或 dict）；variable/scene 傳 None 表示不改。
    """
    batch = load_batch(char_id, batch_id)
    for post in batch.get("posts", []):
        if post.get("post_id") == post_id:
            post["content"] = content
            if variable is not None:
                post["variable"] = variable
            if scene is not None:
                post["scene"] = scene
            storage.save_json("post_batches", f"{char_id}__{batch_id}", batch,
                              _posts_dir(char_id) / f"{batch_id}.json")
            return {"post": post, "batch": batch}
    raise ValueError(f"post {post_id} not found in batch {batch_id}")


@_locked
def delete_post_from_batch(char_id: str, batch_id: str, post_id: str) -> dict:
    """Delete one regular post from a saved batch."""
    batch = load_batch(char_id, batch_id)
    posts = batch.get("posts", [])
    kept = []
    deleted = None
    for post in posts:
        if post.get("post_id") == post_id:
            deleted = post
        else:
            kept.append(post)
    if deleted is None:
        raise ValueError(f"post {post_id} not found in batch {batch_id}")
    _delete_post_image(deleted)
    batch["posts"] = kept
    # 刪除類寫入走遠端嚴格模式（同 delete_ig_post）：遠端失敗即拋錯供前端重試，
    # 不留「本地刪了、遠端沒刪」的隱患。
    storage.save_json("post_batches", f"{char_id}__{batch_id}", batch,
                      _posts_dir(char_id) / f"{batch_id}.json",
                      strict_remote=True)
    return {"deleted": post_id, "batch": batch}


# --------------------------------------------------------------------------
# Step 5: landing page (角色主頁/展示頁) — character -> single-screen HTML
# --------------------------------------------------------------------------
def _landing_dir(char_id: str) -> Path:
    d = config.LANDING_DIR / char_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cover_url_for_landing(record: dict) -> str | None:
    """Public URL the iframe/standalone page can load for the cover slot."""
    cover = record.get("cover") or {}
    lp = cover.get("local_path")
    if lp and storage.ensure_file(Path(lp)):
        return f"/img/{Path(lp).name}"
    return cover.get("url")


@_locked
def generate_landing(
    char_id: str,
    style_text: str | None = None,
    request: str = "",
    current_html: str | None = None,
    variant: str | None = None,
) -> dict:
    """Generate (or edit) a single-screen HTML landing page for a character.

    Uses the persona (flattened to a profile block) + the redrawn cover as the
    design source. `style_text` is a preset name from landing.landing_styles()
    or any free-form style description. Pass `current_html` to iterate.
    """
    record = load_character(char_id)
    persona = record.get("persona", {})
    cover_url = _cover_url_for_landing(record)

    lang = record.get("lang", config.LANGUAGES[0])
    system_prompt = landing.build_system_prompt(
        style_text, lang=lang, variant=variant)
    user_text = landing.build_user_message(
        persona,
        lang,
        has_cover=bool(cover_url),
        request=request,
        style_text=style_text,
        current_html=current_html,
        variant=variant,
    )

    # Multimodal reference: only show the generated cover image. Landing page
    # generation no longer uses recent post images as visual/material inputs.
    content: list[dict] = [{"type": "text", "text": user_text}]
    cover_lp = record.get("cover", {}).get(
        "local_path") if record.get("cover") else None
    if cover_lp and storage.ensure_file(Path(cover_lp)):  # 冷快取從 OSS 回源
        content.append(
            {"type": "image_url",
             "image_url": {"url": api_client.file_to_data_uri(cover_lp)}}
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
    raw = api_client.chat(messages, temperature=0.85, max_tokens=32000)
    html = landing.replace_user_placeholder(landing.clean_html(raw), lang)
    saved_html = landing.absolutize_urls(
        landing.inject_cover(html, cover_url), config.PUBLIC_BASE_URL)

    page_id = _new_id("page")
    page = {
        "page_id": page_id,
        "char_id": char_id,
        "lang": record.get("lang"),
        "created": int(time.time()),
        "style_text": style_text,
        "variant": variant or "default",
        "request": request,
        "cover_url": cover_url,
        # raw (slots empty) — preview injects client-side
        "html": html,
        "html_filled": saved_html,  # cover URL baked in for standalone use
    }
    # 單角色只保留最新一份，重新生成直接覆蓋
    # html 本體不進 storage_hub(單條 256KB 上限)，剝離到 OSS，hub 只存後設資料
    storage.save_json("landings", char_id, page,
                      _landing_dir(char_id) / "landing_latest.json",
                      oss_fields=["html", "html_filled"])
    return page


@_locked
def update_landing_html(char_id: str, html: str) -> dict:
    """用平臺裡手改的 HTML 覆蓋儲存該角色的落地頁，不重新走 LLM 生成。

    存的是"原始 html"（封面槽位留空，讀/匯出時再注入實際封面 URL/內聯圖），
    與 generate_landing 一致，保證匯出得到的是這份最新手改內容。
    """
    page = storage.load_json("landings", char_id,
                             _landing_dir(char_id) / "landing_latest.json") or {}
    record = load_character(char_id)
    cover_url = _cover_url_for_landing(record)
    cleaned = landing.replace_user_placeholder(
        landing.clean_html(html), record.get("lang"))
    page.update({
        "page_id": page.get("page_id") or _new_id("page"),
        "char_id": char_id,
        "lang": record.get("lang"),
        "edited": int(time.time()),
        "cover_url": cover_url,
        "html": cleaned,
        "html_filled": landing.absolutize_urls(
            landing.inject_cover(cleaned, cover_url), config.PUBLIC_BASE_URL),
    })
    page.setdefault("created", page.get("edited"))
    storage.save_json("landings", char_id, page,
                      _landing_dir(char_id) / "landing_latest.json",
                      oss_fields=["html", "html_filled"])
    return page


def load_latest_landing(char_id: str) -> dict | None:
    page = storage.load_json("landings", char_id,
                             _landing_dir(char_id) / "landing_latest.json")
    if page is None:
        return None
    # Re-inject the cover on read so pages saved before the injector handled
    # background-image divs (and pages whose cover changed) still display it.
    raw = page.get("html")
    cover_url = page.get("cover_url")
    if raw:
        raw = landing.replace_user_placeholder(raw, page.get("lang"))
        page["html"] = raw
    if raw and cover_url:
        page["html_filled"] = landing.absolutize_urls(
            landing.inject_cover(raw, cover_url), config.PUBLIC_BASE_URL)
    return page


def load_batch(char_id: str, batch_id: str) -> dict:
    batch = storage.load_json("post_batches", f"{char_id}__{batch_id}",
                              _posts_dir(char_id) / f"{batch_id}.json")
    if batch is None:
        raise FileNotFoundError(f"batch {batch_id} not found for {char_id}")
    return batch


def list_batches(char_id: str) -> list[dict]:
    """該角色的普通帖子批次（新到舊）。

    遠端鍵是 "char__batch"，本地檔名是 "batch.json"：query 按 char_id 過濾
    （避免拉整集合/跨角色汙染），遠端鍵剝字首對映回本地名（避免同批次重複出現）。
    """
    prefix = f"{char_id}__"
    batches = storage.list_json(
        "post_batches", _posts_dir(char_id), pattern="batch_*.json",
        match={"char_id": char_id},
        remote_key_to_local=lambda k: k[len(prefix):] if k.startswith(prefix) else None)
    out = [b for b in batches.values()
           if isinstance(b, dict) and b.get("char_id") == char_id]
    out.sort(key=lambda b: b.get("created", 0), reverse=True)
    return out


# --------------------------------------------------------------------------
# Instagram feed: infer recent N posts, render selfie(i2i)/photo(t2i) images
# --------------------------------------------------------------------------
def _ref_image_uri_for_selfie(record: dict) -> str | None:
    """Prefer the redrawn cover as the i2i reference; fall back to source image.

    real/kdrama/light track 不做原圖兜底：封面已與真人原圖解耦（形似只能來自封面），
    沒封面時寧可無參考生成，也不把真人的臉直接帶進帖子圖。
    adult/flirt 仍保留原圖兜底：flirt 的參考圖本身自帶氛圍/張力，允許直接引用。
    """
    cover = record.get("cover") or {}
    if cover.get("local_path") and storage.ensure_file(Path(cover["local_path"])):
        return api_client.file_to_data_uri(cover["local_path"])
    if cover.get("url"):
        return cover["url"]
    if record.get("track", "real") in ("real", "kdrama", "light"):
        return None
    src = _first_source_image(record)
    if src:
        return api_client.file_to_data_uri(src)
    return None


def _render_ig_post_image(record: dict, post: dict, identity: dict,
                          style_prompt: str | None, with_images: bool = True) -> dict:
    """Render or re-render one Instagram post image in-place."""
    if not with_images or post.get("format") == "text_only":
        return post

    char_id = record["char_id"]
    itype = post.get("image_type")
    save_path = config.IMAGE_DIR / f"{char_id}_{post['post_id']}.png"
    # 封面是角色的純視覺錨；不需要也不應從 source.style 猜一段畫風 prompt。
    selfie_ref = _ref_image_uri_for_selfie(record)

    if itype == "selfie":
        prompt = prompts.compose_selfie_prompt(
            identity, post.get("selfie") or {}, style_prompt,
            track=record.get("track", "real"),
        )
        image_urls = [selfie_ref] if selfie_ref else None
        res = api_client.generate_image(
            prompt, size=config.IMAGE_SIZE_POST,
            resolution=config.IMAGE_RESOLUTION,
            image_urls=image_urls, save_path=save_path,
        )
        post["image"] = {
            "type": "selfie", "url": res["url"],
            "local_path": res["local_path"], "prompt": prompt,
            "used_reference": bool(image_urls),
        }
    elif itype in {"photo", "composite"}:
        prompt = prompts.compose_photo_prompt(
            post.get("photo_prompt") or "", style_prompt,
            photo_kind=post.get("photo_kind") or "photo",
            photo_schema=post.get("photo_schema"),
        )
        image_urls = [
            selfie_ref] if itype == "composite" and selfie_ref else None
        res = api_client.generate_image(
            prompt, size=config.IMAGE_SIZE_POST,
            resolution=config.IMAGE_RESOLUTION,
            image_urls=image_urls, save_path=save_path,
        )
        post["image"] = {
            "type": itype, "url": res["url"],
            "local_path": res["local_path"], "prompt": prompt,
            "photo_kind": post.get("photo_kind") or "photo",
            "photo_schema": post.get("photo_schema"),
            "used_reference": bool(image_urls),
        }
    return post


def _sibling_used_photo_kinds(record: dict, sample_k: int = 4) -> list[str]:
    """收集【同 group 其它角色】最新 IG 批次裡用過的 photo_kind，隨機抽 sample_k 個。

    隨機抽樣讓每個角色看到的"避開列表"都不同，避免大家被推向同一批替代形式。
    """
    group_id = record.get("group_id")
    self_id = record.get("char_id")
    if not group_id:
        return []
    used: set[str] = set()
    for sib in _persona_snapshot():
        if sib.get("group_id") != group_id or sib.get("char_id") == self_id:
            continue
        ig = load_latest_ig(sib.get("char_id")) or {}
        for post in ig.get("posts", []):
            k = post.get("photo_kind")
            if k:
                used.add(k)
    kinds = list(used)
    if len(kinds) > sample_k:
        kinds = random.sample(kinds, sample_k)
    return kinds


@_locked
def generate_instagram_posts(
    char_id: str,
    n: int | None = None,
    style_id: str | None = None,
    with_images: bool = True,
    track: str | None = None,
) -> dict:
    """Infer N recent IG posts, then render images:
    - selfie  -> image-to-image using the redrawn cover as reference
    - photo   -> text-to-image (no person reference)
    - text_only -> no image
    """
    record = load_character(char_id)
    if track and record.get("track") != track:
        record["track"] = track  # 介面臨時覆蓋鏈路並持久化
        save_character(record)
    if not record.get("identity"):
        build_identity(char_id)
        record = load_character(char_id)

    persona = record["persona"]
    identity = record["identity"]
    style_id = style_id or record.get("style_id")
    style = styles.get_style(style_id) if style_id else None
    style_prompt = style["prompt"] if style else None
    if record.get("track") == "nonhuman":
        style_prompt = None  # 非人物鏈路：不套畫風、不拼畫風詞

    # 1) infer the feed (single LLM call, native language)
    avoid_kinds = _sibling_used_photo_kinds(record)
    # 注：vibe 氛圍靈感已下線（SELFIE_SCHEMA 的 filter/shot_size/angle/framing 本就覆蓋
    # 色調·景深·構圖，且給的是發散選項清單，再塞檢索到的具體調子反而收斂）。
    # 拼貼範例（collage）仍在 prompts 內注入。若要重開氛圍靈感，見 app/inspiration.py。
    feed = api_client.chat_json(
        prompts.build_ig_feed_messages(
            persona, record.get("lang", config.LANGUAGES[0]), n=n,
            avoid_kinds=avoid_kinds, track=record.get("track", "real"),
        ),
        temperature=0.95,
    )
    # real 鏈路先輸出 {persona_read, posts}：persona_read 是"這個人憑什麼有意思"的
    # 自我判斷（reasoning），posts 才是帖子。其它鏈路仍直接返回陣列。
    persona_read = None
    if isinstance(feed, dict):
        if isinstance(feed.get("posts"), list):
            persona_read = feed.get("persona_read")
            feed = feed["posts"]
        else:
            feed = [feed]
    max_posts = n if n else 9

    posts = []
    for item in feed[:max_posts]:
        posts.append({
            "post_id": _new_id("ig"),
            "content": item.get("content", {}),
            "mood": item.get("mood", ""),
            "post_time": item.get("post_time", ""),
            "post_type": item.get("post_type"),
            "post_type_name": prompts.POST_TYPE_BY_ID.get(
                item.get("post_type"), {}
            ).get("name"),
            "format": item.get("format", "image_text"),
            "image_type": item.get("image_type"),
            "selfie": item.get("selfie"),
            "photo_kind": item.get("photo_kind"),
            "photo_schema": item.get("photo_schema"),
            "photo_prompt": item.get("photo_prompt"),
            "topic_seed": item.get("topic_seed"),
            "image": None,
        })

    def _render(post: dict) -> dict:
        if not with_images or post.get("format") == "text_only":
            return post
        try:
            _render_ig_post_image(record, post, identity,
                                  style_prompt, with_images)
        except Exception as e:  # noqa: BLE001 keep batch resilient
            post["image"] = {"error": str(e)}
        return post

    if with_images:
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
            list(ex.map(_render, posts))

    batch_id = _new_id("igbatch")
    batch = {
        "batch_id": batch_id,
        "char_id": char_id,
        "lang": record.get("lang"),
        "created": int(time.time()),
        "kind": "instagram_feed",
        "style_id": style_id,
        "requested_n": n,
        "n": len(posts),
        "with_images": with_images,
        "persona_read": persona_read,
        "posts": posts,
    }
    # 單角色只保留最新一份，重新生成直接覆蓋
    storage.save_json("ig_batches", char_id, batch,
                      _posts_dir(char_id) / "ig_latest.json")
    return batch


def load_latest_ig(char_id: str) -> dict | None:
    return storage.load_json("ig_batches", char_id,
                             _posts_dir(char_id) / "ig_latest.json")


@_locked
def rerender_ig_post_image(char_id: str, post_id: str,
                           style_id: str | None = None) -> dict:
    """Re-render one image in the latest Instagram batch."""
    record = load_character(char_id)
    if not record.get("identity"):
        build_identity(char_id)
        record = load_character(char_id)
    batch = load_latest_ig(char_id)
    if not batch:
        raise ValueError("no saved Instagram posts for this character")
    style_id = style_id or batch.get("style_id") or record.get("style_id")
    style = styles.get_style(style_id) if style_id else None
    style_prompt = style["prompt"] if style else None
    if record.get("track") == "nonhuman":
        style_prompt = None  # 非人物鏈路：不套畫風
    for post in batch.get("posts", []):
        if post.get("post_id") == post_id:
            _render_ig_post_image(
                record, post, record["identity"], style_prompt, True)
            batch["style_id"] = style_id
            storage.save_json("ig_batches", char_id, batch,
                              _posts_dir(char_id) / "ig_latest.json")
            return {"post": post, "batch": batch}
    raise ValueError(f"Instagram post {post_id} not found")


@_locked
def update_ig_post_content(char_id: str, post_id: str, content) -> dict:
    """就地更新最新 IG 批次裡一條帖子的文字（content）。不動圖片。"""
    batch = load_latest_ig(char_id)
    if not batch:
        raise ValueError("no saved Instagram posts for this character")
    for post in batch.get("posts", []):
        if post.get("post_id") == post_id:
            post["content"] = content
            storage.save_json("ig_batches", char_id, batch,
                              _posts_dir(char_id) / "ig_latest.json")
            return {"post": post, "batch": batch}
    raise ValueError(f"Instagram post {post_id} not found")


@_locked
def delete_ig_post(char_id: str, post_id: str) -> dict:
    """Delete one post from the latest Instagram batch."""
    batch = load_latest_ig(char_id)
    if not batch:
        raise ValueError("no saved Instagram posts for this character")
    kept = []
    deleted = None
    for post in batch.get("posts", []):
        if post.get("post_id") == post_id:
            deleted = post
        else:
            kept.append(post)
    if deleted is None:
        raise ValueError(f"Instagram post {post_id} not found")
    _delete_post_image(deleted)
    batch["posts"] = kept
    batch["n"] = len(kept)
    # 刪除類寫入走遠端嚴格模式：遠端同步失敗即拋錯，避免本地已刪而遠端仍存舊帖，
    # 導致匯出/換機回源時把刪掉的帖子「復活」。
    storage.save_json("ig_batches", char_id, batch,
                      _posts_dir(char_id) / "ig_latest.json",
                      strict_remote=True)
    return {"deleted": post_id, "batch": batch}
