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


# 按角色的进程内互斥锁：所有对同一 record 的读-改-写都应持锁，
# 防止后台任务与前台请求并发时整文件覆盖丢写。
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
    """按首参 char_id 持角色锁执行：所有对同一 record/批次的读-改-写互斥，
    防止后台批量任务与前台请求并发时整文件覆盖丢写。"""
    import functools

    @functools.wraps(fn)
    def wrapper(char_id, *args, **kwargs):
        with char_lock(char_id):
            return fn(char_id, *args, **kwargs)
    return wrapper


COVER_SPEC_VERSION = 3
_RECENTLY_USED_VOICES: list[str] = []
_VOICE_LOCK = _threading.Lock()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}"


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
    records = []
    for p in config.PERSONA_DIR.glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if rec.get("lang") != lang or rec.get("char_id") == exclude_char_id:
            continue
        records.append(rec)
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
    # 只把真正扎堆的 tag 列入避让（≥3 次且覆盖 ≥20% 的近期角色）
    threshold = max(3, len(records) // 5)
    overused = [t for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])
                if c >= threshold]
    # 去重保序
    names = list(dict.fromkeys(names))
    jobs = list(dict.fromkeys(jobs))
    return names, jobs, overused[:8]


def _postprocess_persona(persona: dict) -> dict:
    """Strip production-only scratch keys that must never land in the persona.

    behavior_patterns / online_chat_style 现在【所有角色都写】（不同情绪下的反应差异），
    不按"是否普通人"剥离；personality/inner_structure 的写法也交给 PERSONALITY_RULES
    引导，本函数只清理与人设内容无关的生产台账字段。
    """
    if not isinstance(persona, dict):
        return persona
    # used_seeds 是灵感手牌的生产台账（real track），不属于人设内容；
    # 正常路径在生成处已取走，这里防御性剥离，保证任何路径都不落进 persona。
    persona.pop("used_seeds", None)
    # _reasoning 是"先推理出这个人是谁"的思考区（real track 人设 prompt 要求作为第一个
    # 字段输出），只用于引导模型先想清人再写卖点，不属于人设内容，剥离。
    persona.pop("_reasoning", None)
    return persona


def _charm_audit(persona: dict, lang: str) -> dict:
    """real track 冷读验收（转述测试）。API 异常时返回 skip，绝不阻塞生产。"""
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
    """把冷读验收的失败意见拼回创作补充要求，用于打回重写。"""
    feedback = (
        "# 上一版未通过冷读验收（转述测试）\n"
        f"陌生读者只能转述出：「{audit.get('retell', '')}」；判定理由：{audit.get('reason', '')}。\n"
        "重写时修正：给出更具体、带画面、只属于这个人的行为与反差，"
        "让粉丝能用一句独一无二的话转述 TA；禁止退回泛用形容词。"
    )
    base = user_hint.strip()
    return f"{base}\n\n{feedback}" if base else feedback


def _generate_persona_real(uris: list[str], lang: str, user_hint: str,
                           diversity_block: str,
                           track: str) -> tuple[dict, list[str], object]:
    """一次人设生成调用：返回 (persona, used_seeds, reasoning)。

    reasoning 是模型在 _reasoning 字段里做的"先推理出这个人"的思考，从 persona 里
    剥出来单独保存（不进下游 prompt），仅用于在前端"完整人设 JSON"里展示。
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
                            track: str = "real", source: str = "") -> dict:
    """Create a single-language character: persona authored natively in `lang`."""
    uris = [api_client.file_to_data_uri(p) for p in image_paths]
    names, jobs, tags = _recent_persona_traits(lang)
    diversity_block = prompts.build_persona_diversity_block(
        lang, avoid_names=names, recent_jobs=jobs, overused_tags=tags, track=track)
    # real track：发一手灵感牌（性格/职业库，冷却过滤后随机），
    # 模型自主决定用不用，用了哪条通过 used_seeds 回报，编排层据此销账。
    # 只有 real 发牌：light 保留自己的关系引擎、kdrama 走韩剧主角推理，随机职业/性格卡
    # 都会干扰。light 虽已对齐 real 的魅力方法论，但用户明确不加职业/性格灵感（只留冷读验收）。
    if track == "real":
        hand = library.checkout()
        hand_block = library.hand_block(hand, lang)
        if hand_block:
            diversity_block = f"{diversity_block}\n\n{hand_block}"

    persona, used_seeds, reasoning = _generate_persona_real(
        uris, lang, user_hint, diversity_block, track)

    # real/light/nonhuman track：冷读验收（转述测试），fail 则带意见打回重写一次。
    # nonhuman = real 方法论（要求"一句能转述这只 TA"），故同样验收；但它【不发灵感手牌】
    # （职业/性格库是人向的，会把角色拽回"人"），手牌仍只发给 real（见上）。
    # kdrama 不走此验收：它是 real track 的"粉丝转述测试"，retry 意见也是 real 方法论
    # （让粉丝能一句转述 TA），与韩剧主角/对手戏方法论冲突——kdrama 同 adult 不发牌不验收。
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
                                source: str = "") -> list[dict]:
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
            track=track, source=source
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
    """只重新生成人设 schema：复用同一来源（源图 或 导入的原始 JSON）、同语言、同补充要求，
    原地覆盖 persona。

    不改图、不动 identity / cover / 帖子。用于批量重刷人设。
    """
    record = load_character(char_id)
    lang = record.get("lang", config.LANGUAGES[0])
    # 界面可临时覆盖链路；覆盖后持久化到 record，后续发帖沿用新 track
    if track:
        record["track"] = track
    track = record.get("track", "real")
    user_hint = record.get("user_hint", "")
    # 从原始 JSON 导入的角色：用同一份 import_source 重新扩写，保持忠实保留。
    if record.get("import_source") is not None:
        messages = prompts.build_persona_from_json_messages(
            record["import_source"], lang, user_hint=user_hint, track=track)
        persona = api_client.chat_json(messages, temperature=0.85)
        record["persona"] = _postprocess_persona(persona)
        _randomize_voice(record["persona"], lang)
    else:
        # 原图优先；原图不在则回退用封面图推理，绝不无图凭空随机生成。
        ref_images = _regen_reference_images(record)
        if not ref_images:
            raise ValueError(
                f"{char_id} 没有可用的原图或封面图，跳过重新生成（拒绝无图随机生成）")
        uris = [api_client.file_to_data_uri(p) for p in ref_images]
        names, jobs, tags = _recent_persona_traits(
            lang, exclude_char_id=char_id)
        diversity_block = prompts.build_persona_diversity_block(
            lang, avoid_names=names, recent_jobs=jobs, overused_tags=tags, track=track)
        # 只有 real 发灵感手牌；light/nonhuman 走冷读验收但不发牌（见 create 路径注释）。
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
        # 早期"角色类型骰子"已移除；这里剥掉旧存档里遗留的 archetype 键（迁移清理），
        # 新记录本就不含该键。
        record.pop("archetype", None)
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
                                      source: str = "") -> dict:
    """Create a single-language character from an existing source JSON object.

    The original object is stored under `import_source` so the persona can be
    re-expanded later. `source_image` (a local path, already downloaded) is
    shared across all language variants of the same source object.
    """
    messages = prompts.build_persona_from_json_messages(
        source_obj, lang, user_hint=user_hint, track=track)
    persona = api_client.chat_json(messages, temperature=0.85)
    persona = _postprocess_persona(persona)
    _randomize_voice(persona, lang)

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
                                  source: str = "") -> list[dict]:
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
            source_image=source_image, track=track, source=source)

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
    """只重写角色【开场白】(persona.opening)：依据其它人设信息生成新的 note + messages。

    不改图、不动其它人设字段、不动 identity / cover / 帖子。用于单独/批量刷开场白。
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


# 上传图引用快照缓存：批量删除时几乎每个角色都要判断“共享上传图是否仍被引用”，
# 原本每次都全量扫本地 1500+ persona 文件 + 远端 query_all 整集合，N 个角色 = N 次
# 全量扫描，慢到离谱。这里按短 TTL 缓存一次“所有被引用的上传路径”快照，整批复用。
# 快照可能滞后（刚删的角色仍在快照里）→ 只会让判断更保守（跳过删共享图，留下孤儿
# 上传件），绝不会误删仍被引用的图；孤儿件可由存量迁移/清理另行回收。
_REF_SNAPSHOT: dict = {"paths": None, "exp": 0.0}
_REF_SNAPSHOT_TTL = 20  # 秒
_REF_SNAPSHOT_LOCK = _threading.Lock()


def _referenced_upload_paths() -> set[str] | None:
    """所有 persona 仍引用的 source_images 路径集合。

    返回 None 表示启用了远端存储但远端查询失败——调用方据此 fail-safe 拒删。
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
            except Exception:  # noqa: BLE001 远端不可达 → 拒删（不缓存失败态）
                return None
            for row in rows:
                rec = row.get("data") or {}
                paths.update(rec.get("source_images", []))
        _REF_SNAPSHOT["paths"] = paths
        _REF_SNAPSHOT["exp"] = now + _REF_SNAPSHOT_TTL
        return paths


def _source_image_is_referenced(path: str) -> bool:
    """Return True if any remaining character record still references upload path.

    删除性守卫必须 fail-safe：启用远端存储时若远端查询失败，保守返回 True
    （视为仍被引用、跳过删除），绝不能在只看到本地残缺视图时误删共享上传图。
    """
    paths = _referenced_upload_paths()
    if paths is None:  # 远端不可达 → 拒删
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
            # (connect, read) 超时：避免坏链/慢源永久挂住导出线程。
            resp = requests.get(url, timeout=(10, 60))
            if resp.ok and resp.content:
                return resp.content
        except requests.RequestException:
            return None
    return None


# 导出兜底：真实社交平台名一律替换成 Popop（prompt 层已要求生成时就用 Popop，
# 这里只是最后一道保险）。长词在前，避免"인스타그램"被"인스타"截断替换。
_BRAND_TOKEN_REPLACEMENTS = [
    "Instagram", "instagram", "INSTAGRAM",
    "인스타그램", "인스타", "インスタグラム", "インスタ",
    "小红书", "Threads", "threads", "스레드", "スレッズ",
    "Twitter", "twitter", "推特", "트위터", "ツイッター",
    "TikTok", "tiktok", "틱톡", "抖音",
    # KakaoTalk：长词在前，避免"카카오톡"被"카카오"截断替换。
    "KakaoTalk", "kakaotalk", "KAKAOTALK", "Kakao", "kakao",
    "카카오톡", "카톡", "カカオトーク",
]
# 短缩写只在无字母上下文时替换（"发个ins"命中，"insert/<ins>"不命中）。
_BRAND_SHORT_RE = re.compile(r"(?<![A-Za-z</])(?:ins|IG)(?![A-Za-z>])")
# LINE 单独处理：英文"line"是常见普通词（online / a line），直接替换会误伤。
# 只命中全大写 LINE（无字母上下文）、韩语 라인、日语 ライン。
_BRAND_LINE_RE = re.compile(r"(?<![A-Za-z</])LINE(?![A-Za-z>])|라인|ライン")


def _apply_brand_replacements(text: str) -> str:
    """Replace real social-platform names with Popop in exported text."""
    for token in _BRAND_TOKEN_REPLACEMENTS:
        if token in text:
            text = text.replace(token, "Popop")
    text = _BRAND_LINE_RE.sub("Popop", text)
    return _BRAND_SHORT_RE.sub("Popop", text)


# 已上传公有桶的图片直链缓存：key = object_key + 内容 md5，value = 公网 URL。
# 导出/同步会为封面/帖图反复传同样的图，缓存后重复导出可直接命中、跳过上传。
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

    落地页 img 需指向公网 URL（不内联 base64），封面/帖图先传公有桶拿直链。
    相同内容+对象键命中缓存则直接返回，跳过重复上传（大幅加速重复导出）。
    失败返回 None，调用方自行降级。"""
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
    except Exception:  # noqa: BLE001 上传失败降级，不阻断导出/同步
        return None


def _landing_html_with_public_urls(
    html: str, char_id: str, lang: str,
    cover_url: str | None, cover_bytes: bytes | None,
    post_urls: list[str] | None,
) -> str:
    """把落地页里的封面/帖图替换成【公网 TOS URL】（img 指向 url，不用 base64）。

    复用 inject_cover / inject_post_images：只是注入的是公网 URL 而非 /img/ 相对路径，
    同时把落地页里残留的 /img/ 引用替换成公网 URL。上传失败的图保持原样（不内联）。
    """
    # 1) 封面 → 公有桶
    if cover_bytes:
        pub = _tos_public_url(
            cover_bytes, f"creaction/{char_id}/landing/cover.png", lang)
        if pub:
            if cover_url:
                # html_filled 里封面可能是相对 /img/… 也可能已被改写成带域名的
                # 绝对 URL（config.PUBLIC_BASE_URL），两种形态都替换成公有桶直链。
                # 先替换更长的绝对形态，再替换相对形态：否则相对串是绝对串的
                # 子串，先换相对会把绝对 URL 拦腰截断，拼出 http://域名https://直链。
                abs_cover = landing.absolutize_urls(
                    f'src="{cover_url}"', config.PUBLIC_BASE_URL)[len('src="'):-1]
                if abs_cover != cover_url:
                    html = html.replace(abs_cover, pub)
                html = html.replace(cover_url, pub)
            html = landing.inject_cover(html, pub)

    # 2) 帖图 → 公有桶（逐个上传、逐个替换 /img/ 引用并填空槽位）
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
        # 同封面：先替换绝对形态再替换相对形态，避免子串截断拼坏 URL。
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


def _build_character_files(char_id: str) -> tuple[str, list[tuple[str, bytes | str]], dict]:
    """Do all the heavy per-character work (OSS 下载 + TOS 上传 + 文案处理)，
    返回 (folder_base, files, report)。files 是 [(相对路径, 内容)] 列表，
    内容为 str(文本) 或 bytes(二进制)。不碰 zip、不加文件夹去重后缀 ——
    这些留给串行的写入阶段，以便整段重活可安全并行。"""
    record = load_character(char_id)
    persona = record.get("persona", {})
    lang = record.get("lang", "zh")

    name = _localized_text(persona.get("name"), lang) or char_id
    folder_base = _safe_name(name, fallback=char_id)

    ig = load_latest_ig(char_id) or {}
    posts = ig.get("posts", []) if isinstance(ig, dict) else []

    files: list[tuple[str, bytes | str]] = []
    report = {"char_id": char_id, "folder": folder_base, "images": 0, "missing": []}

    # 图片一律【上传公有桶取公网直链】写进 character.json，不再把二进制打进 zip：
    # 避免几百个角色的封面/帖图（每张 4-5MB）撑成数 GB 大包、下载超时/404。
    # 2) 封面 → 公网 URL
    cover_bytes = _image_bytes(record.get("cover"))
    cover_public = None
    if cover_bytes:
        # 复用与落地页导出一致的对象键，命中内容哈希缓存、避免重复上传同一张封面。
        cover_public = _tos_public_url(
            cover_bytes, f"creaction/{char_id}/landing/cover.png", lang)
        if cover_public:
            report["images"] += 1
        else:
            report["missing"].append("cover_upload_failed")
    else:
        report["missing"].append("cover")

    # 3) 帖图 → 公网 URL（此角色内部并行上传），把 image_url 注入每条 post
    export_posts = [dict(p) for p in posts]  # 浅拷贝，避免污染内存态

    def _upload_post(i_post: tuple[int, dict]) -> tuple[int, str | None]:
        i, post = i_post
        img = post.get("image")
        data = _image_bytes(img)
        if not data:
            return i, None
        # 用图片文件名做对象键（与落地页帖图键一致），命中缓存复用同一张已传图。
        nm = None
        if isinstance(img, dict):
            lp = img.get("local_path") or img.get("url")
            if lp:
                nm = Path(lp).name
        key = f"creaction/{char_id}/landing/{nm or f'post_{i + 1}.png'}"
        return i, _tos_public_url(data, key, lang)

    if export_posts:
        with ThreadPoolExecutor(max_workers=min(len(export_posts), config.MAX_WORKERS)) as ex:
            for i, url in ex.map(_upload_post, enumerate(export_posts)):
                if url:
                    export_posts[i]["image_url"] = url
                    report["images"] += 1

    # 交付 schema：每条 post 只保留 post_id / content / image_url，
    # 其余中间字段（post_type、format、image、photo_* 等）不出包。
    export_posts = [
        {
            "post_id": p.get("post_id"),
            "content": p.get("content"),
            "image_url": p.get("image_url"),
        }
        for p in export_posts
    ]

    # 1) character.json — persona fields + posts + 封面/帖图公网 URL（无二进制）
    from .persona_export import to_export_schema
    bundle = {
        "char_id": char_id,
        "lang": lang,
        "name": name,
        "source": record.get("source", ""),
        "track": record.get("track", "real"),
        "cover_url": cover_public,
        "persona": to_export_schema(persona, lang),
        "posts": export_posts,
    }
    bundle_json = _apply_brand_replacements(
        json.dumps(bundle, ensure_ascii=False, indent=2))
    files.append(("character.json", bundle_json))

    # 4) landing.html — 封面/帖图指向【公网 TOS URL】（img 用 url，不内联 base64）。
    landing_page = load_latest_landing(char_id) or {}
    landing_html = landing_page.get("html_filled") or landing_page.get("html")
    if landing_html:
        landing_html = _apply_brand_replacements(_landing_html_with_public_urls(
            landing_html, char_id, lang,
            landing_page.get("cover_url"), cover_bytes,
            landing_page.get("post_urls")))
        files.append(("landing.html", landing_html))
    else:
        report["missing"].append("landing")

    # mark the character record as exported
    record["exported"] = True
    record["exported_at"] = int(time.time())
    save_character(record)

    return folder_base, files, report


def export_characters_zip(char_ids: list[str]) -> bytes:
    """Bundle the given characters into a zip (one folder per character).

    Each folder contains character.json (persona + posts + 封面/帖图公网 URL) and
    landing.html (standalone landing page) when one has been generated. 图片不
    再打包二进制，全部以公网 TOS/CDN URL 引用，导出包因此很小、下载稳定。

    每个角色的重活（OSS 下载 + TOS 上传 + 文案处理）跨角色并行；zip 写入
    （非线程安全）在主线程串行完成。保序输出，保证结果与串行版一致。

    注意：同步版把整份 zip 囤在内存并阻塞请求，仅适合小批量。大批量走
    export_characters_zip_to_file + 异步任务，避免反代读超时（504）。
    """
    results = _build_all_character_files(char_ids)
    buf = io.BytesIO()
    _write_zip(buf, char_ids, results)
    buf.seek(0)
    return buf.getvalue()


def _write_zip(dst, char_ids: list[str], results: dict[str, tuple]) -> None:
    """把已构建好的角色文件写进 zip（dst 为文件对象或路径）。串行、保序。"""
    used_folders: set = set()
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for cid in char_ids:  # 保持请求顺序
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
    """并行构建所有角色的文件（重活）。on_done(cid) 每完成一个回调一次，
    供任务进度上报。返回 {cid: (folder_base, files, report)}。"""
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
    """异步导出用：并行构建后把 zip 直接写到磁盘文件（不在内存里囤 160MB+）。
    返回成功打包的角色数。"""
    results = _build_all_character_files(char_ids, on_done=on_done)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dst_path, "wb") as f:
        _write_zip(f, char_ids, results)
    return len(results)


@_locked
@_locked
def delete_character(char_id: str) -> bool:
    """删除一个角色及所有以角色 id 归属的数据。

    持角色锁执行：与后台同步/生成互斥，避免删除中途被并发写“复活”。
    """
    p = _char_path(char_id)
    record = None
    deleted_any = False

    if p.exists():
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            record = None

    # 远端存储联动删除：persona 记录 + 该角色的批次/ig/landing/chat 记录。
    # 注意顺序：远端删除成功后才删本地文件——否则远端失败重试时 record 已丢，
    # 共享上传图清理会被跳过。
    # 远端删除失败必须上抛（否则残留记录会被 list/load 回源"复活"），由上层报错重试。
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
        # OSS 图片/目录级联清理（尽力而为）：不清会永久残留并被 /img 回源复活。
        # 合并成单次调用：只取一次 STS 凭证 + 批量删，避免逐前缀重复取凭证/逐个删。
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

    ref_override: 显式指定封面规划参考图（本地路径）。用于"以现有封面为参考重跑
    封面链路"的场景（source=image 的角色希望远离原图但保留气质）。传入时优先于源图。
    """
    record = load_character(char_id)
    if not record.get("identity"):
        build_identity(char_id)
        record = load_character(char_id)

    track = record.get("track", "real")
    cover_ref = ref_override or _first_source_image(record)
    lang = record.get("lang", "ko")

    # 封面规划【喂原图】：real/light（按用户要求，封面锚定原图，让封面像上传的这个人）
    # 及 adult/nonhuman 都把原图作为参考传入。只有 kdrama 例外——它是"这部剧的第一帧剧照"，
    # 从人设重新设计场景，喂原图会把镜头拉回自拍快照，故仍纯文本规划。
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
    style_id: str,
    use_reference: bool | None = None,
    mode: str = "fill_missing",
    recook_from_cover: bool = False,
) -> dict:
    """Generate a cover image.

    use_reference:
    - None (default): auto — use the source image as i2i reference whenever the
      style is photographic and a source image exists. The cover anchors every
      later selfie i2i, and covers drawn without a reference are visibly worse.
    - True/False: explicit override.

    mode:
    - "fill_missing": keep existing identity/spec, generate only missing data.
    - "full": regenerate identity and cover_spec before rendering image.
    - "image_only": render image only; fail if identity or cover_spec is missing.

    recook_from_cover: 以角色【当前封面】而非源图作为参考重跑封面链路。用于 source=image
    的角色——原封面太像上传原图，希望保留气质但漂离原图。开启时：cover_spec 用当前封面
    做规划参考，i2i 渲染也用当前封面而非源图（源图不再进入任何一步）。会强制重建 cover_spec。
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
    # 非人物（nonhuman）链路：不套画风、不拼画风词，纯按 identity + 原图生成。
    # flirt 链路且未指定画风时同样不拼画风词（写实底座 + 情欲向视觉层 + i2i 参考图）。
    if track == "nonhuman" or (track == "flirt" and not style_id):
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
        # 拼入原图做 i2i——原图只锁"神似"（脸/发型/气质），场景/距离/构图/入口由 cover_spec
        # 文本重新设计（compose_selfie_prompt 里的 I2I_OVERRIDE_GUARD 明确要求不要复刻原图
        # 构图）。写实画风且有原图时就用；非写实画风不拼原图。nonhuman 无画风=写实底座，会用原图。
        use_reference = prompts.is_photographic_style(style_prompt)
    image_urls = None
    if use_reference:
        # recook_from_cover：i2i 参考用当前封面（recook_ref），源图不再进入渲染，
        # 让新封面漂离上传原图但保留气质。否则维持原行为（源图做参考）。
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


def _render_post_image(record: dict, post: dict, style: dict | None) -> dict:
    """Render or re-render one regular post image in-place.

    style 可为 None（nonhuman 非人物链路不套画风），此时 style_prompt=None，
    compose_image_prompt 不拼画风词。
    """
    char_id = record["char_id"]
    identity = record["identity"]
    style_prompt = style["prompt"] if style else None
    prompt = prompts.compose_image_prompt(
        identity, post["variable"], post["scene"], style_prompt,
        track=record.get("track", "real"),
    )
    save_path = config.IMAGE_DIR / f"{char_id}_{post['post_id']}.png"
    image_urls = None
    if prompts.is_photographic_style(style_prompt):
        # 统一取参：real track 走封面锚（绝不直连原图，避免形似泄漏），
        # 其他 track 维持原行为（封面优先、原图兜底）。
        ref = _ref_image_uri_for_selfie(record)
        if ref:
            image_urls = [ref]
    res = api_client.generate_image(
        prompt,
        size=config.IMAGE_SIZE_POST,
        resolution=config.IMAGE_RESOLUTION,
        image_urls=image_urls,
        save_path=save_path,
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
        record["track"] = track  # 界面临时覆盖链路并持久化
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
        style = None  # 非人物链路：不套画风、不拼画风词

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

    # 2) render images (parallel) using identity + variable + scene + style
    # nonhuman 无画风也要出图（style=None，_render_post_image 会不拼画风词）。
    if with_images and (style is not None or track == "nonhuman"):
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
        style = None  # 非人物链路：不套画风
    elif not style:
        raise ValueError("style is required to re-render post image")
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
    """就地更新一条普通帖子的文本（content），可选 variable/scene。不动图片。

    content 保持与生成时相同的形态（str 或 dict）；variable/scene 传 None 表示不改。
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
    storage.save_json("post_batches", f"{char_id}__{batch_id}", batch,
                      _posts_dir(char_id) / f"{batch_id}.json")
    return {"deleted": post_id, "batch": batch}


# --------------------------------------------------------------------------
# Step 5: landing page (角色主页/展示页) — character -> single-screen HTML
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
    if cover_lp and storage.ensure_file(Path(cover_lp)):  # 冷缓存从 OSS 回源
        content.append(
            {"type": "image_url",
             "image_url": {"url": api_client.file_to_data_uri(cover_lp)}}
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
    raw = api_client.chat(messages, temperature=0.85, max_tokens=32000)
    html = landing.clean_html(raw)
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
    # 单角色只保留最新一份，重新生成直接覆盖
    # html 本体不进 storage_hub(单条 256KB 上限)，剥离到 OSS，hub 只存元数据
    storage.save_json("landings", char_id, page,
                      _landing_dir(char_id) / "landing_latest.json",
                      oss_fields=["html", "html_filled"])
    return page


@_locked
def update_landing_html(char_id: str, html: str) -> dict:
    """用平台里手改的 HTML 覆盖保存该角色的落地页，不重新走 LLM 生成。

    存的是"原始 html"（封面槽位留空，读/导出时再注入实际封面 URL/内联图），
    与 generate_landing 一致，保证导出得到的是这份最新手改内容。
    """
    page = storage.load_json("landings", char_id,
                             _landing_dir(char_id) / "landing_latest.json") or {}
    record = load_character(char_id)
    cover_url = _cover_url_for_landing(record)
    cleaned = landing.clean_html(html)
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
    """该角色的普通帖子批次（新到旧）。

    远端键是 "char__batch"，本地文件名是 "batch.json"：query 按 char_id 过滤
    （避免拉整集合/跨角色污染），远端键剥前缀映射回本地名（避免同批次重复出现）。
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

    real/kdrama/light track 不做原图兜底：封面已与真人原图解耦（形似只能来自封面），
    没封面时宁可无参考生成，也不把真人的脸直接带进帖子图。
    adult/flirt 仍保留原图兜底：flirt 的参考图本身自带氛围/张力，允许直接引用。
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
    use_reference = prompts.is_photographic_style(style_prompt)
    selfie_ref = _ref_image_uri_for_selfie(record) if use_reference else None

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
    """收集【同 group 其它角色】最新 IG 批次里用过的 photo_kind，随机抽 sample_k 个。

    随机抽样让每个角色看到的"避开列表"都不同，避免大家被推向同一批替代形式。
    """
    group_id = record.get("group_id")
    self_id = record.get("char_id")
    if not group_id:
        return []
    used: set[str] = set()
    for p in config.PERSONA_DIR.glob("*.json"):
        try:
            sib = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
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
        record["track"] = track  # 界面临时覆盖链路并持久化
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
        style_prompt = None  # 非人物链路：不套画风、不拼画风词

    # 1) infer the feed (single LLM call, native language)
    avoid_kinds = _sibling_used_photo_kinds(record)
    # 注：vibe 氛围灵感已下线（SELFIE_SCHEMA 的 filter/shot_size/angle/framing 本就覆盖
    # 色调·景深·构图，且给的是发散选项清单，再塞检索到的具体调子反而收敛）。
    # 拼贴范例（collage）仍在 prompts 内注入。若要重开氛围灵感，见 app/inspiration.py。
    feed = api_client.chat_json(
        prompts.build_ig_feed_messages(
            persona, record.get("lang", config.LANGUAGES[0]), n=n,
            avoid_kinds=avoid_kinds, track=record.get("track", "real"),
        ),
        temperature=0.95,
    )
    # real 链路先输出 {persona_read, posts}：persona_read 是"这个人凭什么有意思"的
    # 自我判断（reasoning），posts 才是帖子。其它链路仍直接返回数组。
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
    # 单角色只保留最新一份，重新生成直接覆盖
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
        style_prompt = None  # 非人物链路：不套画风
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
    """就地更新最新 IG 批次里一条帖子的文本（content）。不动图片。"""
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
    storage.save_json("ig_batches", char_id, batch,
                      _posts_dir(char_id) / "ig_latest.json")
    return {"deleted": post_id, "batch": batch}
