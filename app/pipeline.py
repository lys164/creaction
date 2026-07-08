"""Pipeline orchestration: persona extraction, identity reverse, cover, posts.

Persists a per-character record under data/personas/<char_id>.json and post
batches under data/posts/<char_id>/<batch_id>.json. Images saved under data/images.
"""
import io
import json
import base64
import random
import re
import shutil
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from . import api_client, config, inspiration, landing, library, prompts, storage, styles, voices


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
    pool = [v for v in all_voices if v.get("gender") == gender] if gender else []
    if not pool:
        pool = all_voices

    with _VOICE_LOCK:
        available = [v for v in pool if v.get("id") not in _RECENTLY_USED_VOICES]
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
_PERSONALITY_CHAIN_FIELDS = (
    "decisive_event", "response", "cost",
    "desire_outer", "desire_inner", "desire_bottom_line", "healing",
)
_HUMAN_SPECIES_MARKERS = ("人类", "인간", "human", "人間")


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
        job = _job_snippet(persona.get("social_status"))
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


def _is_plain_human(persona: dict) -> bool:
    """True when the character is an ordinary human with no special premise."""
    species = persona.get("species")
    if isinstance(species, str) and species.strip():
        s = species.strip().lower()
        if not any(m in s for m in _HUMAN_SPECIES_MARKERS):
            return False
    premise = persona.get("premise")
    return not (isinstance(premise, str) and premise.strip())


def _postprocess_persona(persona: dict, archetype: str | None = None) -> dict:
    """Enforce conditional-field rules the model tends to ignore.

    - situational_reactions is only for non-human / special-premise characters;
      strip it from ordinary humans (observed 61/62 filled despite the rule).
    - "plain" archetype characters must not carry a personality causal chain or
      a hidden_side — blank them even if the model wrote them anyway.
    """
    if not isinstance(persona, dict):
        return persona
    # used_seeds 是灵感手牌的生产台账（real track），不属于人设内容；
    # 正常路径在生成处已取走，这里防御性剥离，保证任何路径都不落进 persona。
    persona.pop("used_seeds", None)
    # _reasoning 是"先推理出这个人是谁"的强制思考区（real track 人设 prompt 要求
    # 作为第一个字段输出），只用于引导模型先想清人再写卖点，不属于人设内容，剥离。
    persona.pop("_reasoning", None)
    if _is_plain_human(persona):
        persona.pop("situational_reactions", None)
    if archetype == "plain":
        personality = persona.get("personality")
        if isinstance(personality, dict):
            for key in _PERSONALITY_CHAIN_FIELDS:
                if personality.get(key):
                    personality[key] = ""
        if persona.get("hidden_side"):
            persona["hidden_side"] = ""
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
                           diversity_block: str, archetype: str,
                           track: str) -> tuple[dict, list[str]]:
    """一次人设生成调用：返回 (persona, 模型回报的 used_seeds)。"""
    messages = prompts.build_persona_messages(
        uris, lang, user_hint=user_hint, diversity_block=diversity_block,
        track=track)
    persona = api_client.chat_json(messages, temperature=0.85)
    used: list = []
    if isinstance(persona, dict):
        raw = persona.pop("used_seeds", [])
        if isinstance(raw, list):
            used = raw
    return _postprocess_persona(persona, archetype), used


def create_persona_one_lang(image_paths: list[str], lang: str,
                            user_hint: str = "", group_id: str | None = None,
                            track: str = "real", source: str = "") -> dict:
    """Create a single-language character: persona authored natively in `lang`."""
    uris = [api_client.file_to_data_uri(p) for p in image_paths]
    archetype = prompts.sample_archetype()
    names, jobs, tags = _recent_persona_traits(lang)
    diversity_block = prompts.build_persona_diversity_block(
        lang, archetype, avoid_names=names, recent_jobs=jobs, overused_tags=tags)
    # real track：发一手灵感牌（性格/职业库，冷却过滤后随机），
    # 模型自主决定用不用，用了哪条通过 used_seeds 回报，编排层据此销账。
    if track == "real":
        hand = library.checkout()
        hand_block = library.hand_block(hand, lang)
        if hand_block:
            diversity_block = f"{diversity_block}\n\n{hand_block}"

    persona, used_seeds = _generate_persona_real(
        uris, lang, user_hint, diversity_block, archetype, track)

    # real track：冷读验收（转述测试），fail 则带意见打回重写一次。
    audits: list[dict] = []
    if track == "real":
        audit = _charm_audit(persona, lang)
        audits.append(audit)
        if audit["verdict"] == "fail":
            persona, used_seeds = _generate_persona_real(
                uris, lang, _audit_retry_hint(user_hint, audit),
                diversity_block, archetype, track)
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
        "archetype": archetype,
        "track": track,
        "source": source,
        "used_seeds": used_seeds,
        "charm_audit": audits or None,
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
        # 重刷时重新掷骰子：换角色类型/种子，避免刷回同一种模式。
        archetype = prompts.sample_archetype()
        names, jobs, tags = _recent_persona_traits(lang, exclude_char_id=char_id)
        diversity_block = prompts.build_persona_diversity_block(
            lang, archetype, avoid_names=names, recent_jobs=jobs,
            overused_tags=tags)
        # real track：发灵感手牌 + 冷读验收，与 create_persona_one_lang 同构。
        if track == "real":
            hand = library.checkout()
            hand_block = library.hand_block(hand, lang)
            if hand_block:
                diversity_block = f"{diversity_block}\n\n{hand_block}"
        persona, used_seeds = _generate_persona_real(
            uris, lang, user_hint, diversity_block, archetype, track)
        audits: list[dict] = []
        if track == "real":
            audit = _charm_audit(persona, lang)
            audits.append(audit)
            if audit["verdict"] == "fail":
                persona, used_seeds = _generate_persona_real(
                    uris, lang, _audit_retry_hint(user_hint, audit),
                    diversity_block, archetype, track)
                audits.append(_charm_audit(persona, lang))
            record["used_seeds"] = library.commit(used_seeds)
            record["charm_audit"] = audits
        record["persona"] = persona
        _randomize_voice(record["persona"], lang)
        record["archetype"] = archetype
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
    dest = config.UPLOAD_DIR / f"import_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}{ext}"
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
    langs = [l for l in langs if l in config.LANGUAGES] or [config.LANGUAGES[0]]
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


def _source_image_is_referenced(path: str) -> bool:
    """Return True if any remaining character record still references upload path.

    删除性守卫必须 fail-safe：启用远端存储时若远端查询失败，保守返回 True
    （视为仍被引用、跳过删除），绝不能在只看到本地残缺视图时误删共享上传图。
    """
    for p in config.PERSONA_DIR.glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if path in rec.get("source_images", []):
            return True
    from . import arca_storage
    if arca_storage.enabled():
        try:
            rows = storage.query_all("personas")
        except Exception:  # noqa: BLE001 远端不可达 → 拒删
            return True
        for row in rows:
            rec = row.get("data") or {}
            if path in rec.get("source_images", []):
                return True
    return False


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
            resp = requests.get(url, timeout=120)
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
]
# 短缩写只在无字母上下文时替换（"发个ins"命中，"insert/<ins>"不命中）。
_BRAND_SHORT_RE = re.compile(r"(?<![A-Za-z</])(?:ins|IG)(?![A-Za-z>])")


def _apply_brand_replacements(text: str) -> str:
    """Replace real social-platform names with Popop in exported text."""
    for token in _BRAND_TOKEN_REPLACEMENTS:
        if token in text:
            text = text.replace(token, "Popop")
    return _BRAND_SHORT_RE.sub("Popop", text)


def _inline_landing_cover(html: str, cover_url: str | None,
                          cover_bytes: bytes | None) -> str:
    """Make a landing page self-contained by inlining the cover as a data URI.

    Handles every common template form: an explicit cover_url string, <img>
    tags, css url(), and empty oc-cover / oc-img-1 divs shown via background.
    """
    if not cover_bytes:
        return html
    data_uri = "data:image/png;base64," + base64.b64encode(cover_bytes).decode()
    if cover_url:
        html = html.replace(cover_url, data_uri)
    html = _fill_empty_cover_divs(html, data_uri)
    return html


def _inline_landing_posts(html: str, post_urls: list[str] | None) -> str:
    """Inline each post image (referenced by its /img/<name> URL) as a data URI
    and fill empty oc-post-N slots, so the exported page is self-contained."""
    if not post_urls:
        return html
    for idx, url in enumerate(post_urls, start=1):
        if not url:
            continue
        name = Path(url).name
        p = config.IMAGE_DIR / name
        if not storage.ensure_file(p):  # 冷缓存从 OSS 回源，避免导出页槽位静默为空
            continue
        try:
            b = p.read_bytes()
        except OSError:
            continue
        data_uri = "data:image/png;base64," + base64.b64encode(b).decode()
        # 1) replace any live /img/ reference the injector already baked in
        html = html.replace(url, data_uri)
        # 2) fill still-empty oc-post-N slots directly
        html = landing.inject_post_images(html, _slot_fill(idx, data_uri))
    return html


def _slot_fill(idx: int, data_uri: str) -> list[str]:
    """Build a sparse post_urls list that only sets slot `idx` (1-based)."""
    return [""] * (idx - 1) + [data_uri]


def _fill_empty_cover_divs(html: str, data_uri: str) -> str:
    """Inject background-image into empty <div class=oc-cover/oc-img-1> slots."""
    def _repl(m: "re.Match") -> str:
        tag = m.group(0)
        if "background-image" in tag.lower():
            return tag
        style = f"background-image:url('{data_uri}');"
        sm = re.search(r'style\s*=\s*"([^"]*)"', tag)
        if sm:
            return tag[:sm.start(1)] + sm.group(1) + ";" + style + tag[sm.end(1):]
        return tag[:-1] + f' style="{style}">'

    return re.sub(
        r'<div\b[^>]*\bclass=["\'][^"\']*oc-(?:cover|img-1)[^"\']*["\'][^>]*>',
        _repl, html)


def _export_one_character(zf: zipfile.ZipFile, char_id: str, used_folders: set) -> dict:
    """Write one character's folder into the open zip. Returns a small report."""
    record = load_character(char_id)
    persona = record.get("persona", {})
    lang = record.get("lang", "zh")

    name = _localized_text(persona.get("name"), lang) or char_id
    folder = _safe_name(name, fallback=char_id)
    # de-duplicate folder names across characters with the same name
    base_folder, idx = folder, 2
    while folder in used_folders:
        folder = f"{base_folder} ({idx})"
        idx += 1
    used_folders.add(folder)

    ig = load_latest_ig(char_id) or {}
    posts = ig.get("posts", []) if isinstance(ig, dict) else []

    # 1) character.json — persona fields + posts (text data, no binary)
    bundle = {
        "char_id": char_id,
        "lang": lang,
        "name": name,
        "source": record.get("source", ""),
        "track": record.get("track", "real"),
        "persona": persona,
        "posts": posts,
    }
    bundle_json = json.dumps(bundle, ensure_ascii=False, indent=2)
    bundle_json = _apply_brand_replacements(bundle_json)
    zf.writestr(f"{folder}/character.json", bundle_json)

    report = {"char_id": char_id, "folder": folder, "images": 0, "missing": []}

    # 2) cover.png
    cover_bytes = _image_bytes(record.get("cover"))
    if cover_bytes:
        zf.writestr(f"{folder}/cover.png", cover_bytes)
        report["images"] += 1
    else:
        report["missing"].append("cover")
    # 3) posts/<content>.png — name each image by the post content
    used_post_names: set = set()
    for i, post in enumerate(posts):
        img = post.get("image")
        data = _image_bytes(img)
        if not data:
            continue
        label = _safe_name(_apply_brand_replacements(post.get("content", "")), fallback=f"post_{i + 1}")
        fname, n = label, 2
        while fname in used_post_names:
            fname = f"{label} ({n})"
            n += 1
        used_post_names.add(fname)
        zf.writestr(f"{folder}/posts/{fname}.png", data)
        report["images"] += 1

    # 4) landing.html — standalone single-screen page. The cover is inlined as
    # a data URI so the file opens correctly outside the server regardless of
    # how the template references it (img src, css url(), or an empty
    # oc-cover/oc-img-1 div filled via background-image).
    landing_page = load_latest_landing(char_id) or {}
    landing_html = landing_page.get("html_filled") or landing_page.get("html")
    if landing_html:
        landing_html = _inline_landing_cover(
            landing_html, landing_page.get("cover_url"), cover_bytes)
        landing_html = _inline_landing_posts(
            landing_html, landing_page.get("post_urls"))
        landing_html = _apply_brand_replacements(landing_html)
        zf.writestr(f"{folder}/landing.html", landing_html)
    else:
        report["missing"].append("landing")

    # mark the character record as exported
    record["exported"] = True
    record["exported_at"] = int(time.time())
    save_character(record)

    return report


def export_characters_zip(char_ids: list[str]) -> bytes:
    """Bundle the given characters into a zip (one folder per character).

    Each folder contains character.json (persona + posts), cover.png,
    posts/<content>.png for every post that has a generated image, and
    landing.html (standalone landing page) when one has been generated.
    """
    buf = io.BytesIO()
    used_folders: set = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for cid in char_ids:
            try:
                _export_one_character(zf, cid, used_folders)
            except FileNotFoundError:
                continue
    buf.seek(0)
    return buf.getvalue()


@_locked
def delete_character(char_id: str) -> bool:
    """删除一个角色及所有以角色 id 归属的数据。"""
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
        # OSS 图片/目录级联清理（尽力而为）：不清会永久残留并被 /img 回源复活
        storage.delete_oss_prefix(f"images/{char_id}_")
        for sub in ("posts", "landing", "chat"):
            storage.delete_oss_prefix(f"{sub}/{char_id}/")

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
def build_cover_spec(char_id: str) -> dict:
    """Generate the cover-specific variable + scene block.

    Persona generation can use multiple source images, and identity can use one
    main visual anchor. Cover planning stays text-only so the shooting/filter
    schema comes from the character rather than copying the uploaded photo.
    """
    record = load_character(char_id)
    if not record.get("identity"):
        build_identity(char_id)
        record = load_character(char_id)

    # real track：规划封面时【不喂原图】。实测原图信号太强，会把封面场景/构图/道具
    # 拉回原始快照（如"在洗手间刷牙"），压过 prompt 里"从人设重新设计签名瞬间"的要求。
    # "神似"已由 identity 文本承载（identity 照旧从原图提取），封面渲染阶段 real track
    # 也不拼原图（use_reference=False），所以规划阶段完全靠人设设计签名瞬间即可。
    # light/adult 仍按原逻辑把原图作为气质参考传入。
    track = record.get("track", "real")
    cover_ref = _first_source_image(record)
    if track == "real":
        cover_ref_uri = None
    else:
        cover_ref_uri = api_client.file_to_data_uri(cover_ref) if cover_ref else None
    messages = prompts.build_cover_spec_messages(
        record["persona"], record["identity"], cover_ref_uri, track=track,
        lang=record.get("lang", "ko"))
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
    """
    record = load_character(char_id)
    if mode not in {"fill_missing", "full", "image_only"}:
        raise ValueError(f"unknown cover generation mode {mode}")

    if mode == "full":
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
    if mode == "fill_missing" and (
        not cover_spec or cover_spec.get("version") != COVER_SPEC_VERSION
    ):
        build_cover_spec(char_id)
        record = load_character(char_id)

    style = styles.get_style(style_id)
    if not style:
        raise ValueError(f"unknown style {style_id}")

    identity = record["identity"]
    cover_spec = record.get("cover_spec", {})
    mood = identity.get("persona_mood", "")
    prompt = prompts.cover_image_prompt(
        identity,
        style["prompt"],
        persona_mood=mood,
        variable=cover_spec.get("variable"),
        scene=cover_spec.get("scene"),
        shooting=cover_spec.get("shooting"),
    )

    if use_reference is None:
        # real track：封面不拼原图——断开与真人原图的"形似"（肖像风险），
        # 也让封面真正服从 cover_spec 设计的签名瞬间而不被参考图构图拉回。
        # "神似"仍由 identity 文本承载（identity 照旧从原图提取）。
        # 后续帖子 i2i 锚定的是封面，脸部一致性链不受影响。
        # 显式传 use_reference=True 可覆盖（用于 A/B 对比）。
        if record.get("track", "real") == "real":
            use_reference = False
        else:
            use_reference = prompts.is_photographic_style(style["prompt"])
    image_urls = None
    if use_reference:
        src = _first_source_image(record)
        if src:
            image_urls = [api_client.file_to_data_uri(src)]

    save_path = config.IMAGE_DIR / f"{char_id}_cover_{style_id}.png"
    result = api_client.generate_image(
        prompt,
        size=config.IMAGE_SIZE_COVER,
        resolution=config.IMAGE_RESOLUTION,
        image_urls=image_urls,
        save_path=save_path,
    )
    record["style_id"] = style_id
    record["cover"] = {
        "style_id": style_id,
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


def _render_post_image(record: dict, post: dict, style: dict) -> dict:
    """Render or re-render one regular post image in-place."""
    char_id = record["char_id"]
    identity = record["identity"]
    prompt = prompts.compose_image_prompt(
        identity, post["variable"], post["scene"], style["prompt"]
    )
    save_path = config.IMAGE_DIR / f"{char_id}_{post['post_id']}.png"
    image_urls = None
    if prompts.is_photographic_style(style["prompt"]):
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
    style_id = style_id or record.get("style_id")
    style = styles.get_style(style_id) if style_id else None

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
    if with_images and style:
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
    if not style:
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
    system_prompt = landing.build_system_prompt(style_text, lang=lang)
    user_text = landing.build_user_message(
        persona,
        lang,
        has_cover=bool(cover_url),
        request=request,
        style_text=style_text,
        current_html=current_html,
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
    saved_html = landing.inject_cover(html, cover_url)

    page_id = _new_id("page")
    page = {
        "page_id": page_id,
        "char_id": char_id,
        "lang": record.get("lang"),
        "created": int(time.time()),
        "style_text": style_text,
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
        page["html_filled"] = landing.inject_cover(raw, cover_url)
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

    real track 不做原图兜底：封面已与真人原图解耦（形似只能来自封面），
    没封面时宁可无参考生成，也不把真人的脸直接带进帖子图。
    """
    cover = record.get("cover") or {}
    if cover.get("local_path") and storage.ensure_file(Path(cover["local_path"])):
        return api_client.file_to_data_uri(cover["local_path"])
    if cover.get("url"):
        return cover["url"]
    if record.get("track", "real") == "real":
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
            identity, post.get("selfie") or {}, style_prompt
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


def _refine_ig_image_specs(persona: dict, posts: list, lang: str) -> None:
    """real 链路阶段2：检索真实拍摄范例作灵感，重生成图文帖的图像 spec（就地更新）。

    整段尽量隔离：库缺失、检索为空或 LLM 失败都静默跳过，保留阶段1 的草稿 spec，
    绝不影响帖子 content 或阻断出图。
    """
    if not inspiration.available():
        return
    image_posts = [p for p in posts if p.get("format") != "text_only"]
    if not image_posts:
        return
    vibe = persona.get("personality") or persona.get("vibe") or []
    refs_by_index = {}
    for i, p in enumerate(posts):
        if p.get("format") == "text_only":
            continue
        items = inspiration.retrieve(vibe, content=p.get("content") or "", k=3)
        refs = inspiration.format_refs(items)
        if refs:
            refs_by_index[i] = refs
    if not refs_by_index:
        return
    try:
        specs = api_client.chat_json(
            prompts.build_ig_image_spec_messages_real(persona, posts, lang, refs_by_index),
            temperature=0.9,
        )
    except Exception:  # noqa: BLE001 阶段2 失败不影响主流程
        return
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, list):
        return
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        idx = spec.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(posts)):
            continue
        post = posts[idx]
        if post.get("format") == "text_only":
            continue
        for key in ("image_type", "selfie", "photo_kind", "photo_prompt", "photo_schema"):
            if key in spec and spec.get(key) is not None:
                post[key] = spec[key]


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

    # 1) infer the feed (single LLM call, native language)
    avoid_kinds = _sibling_used_photo_kinds(record)
    feed = api_client.chat_json(
        prompts.build_ig_feed_messages(
            persona, record.get("lang", config.LANGUAGES[0]), n=n,
            avoid_kinds=avoid_kinds, track=record.get("track", "real"),
        ),
        temperature=0.95,
    )
    if isinstance(feed, dict):
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

    # 1.5) real 链路：content 已生成，用真实拍摄范例检索灵感，再重跑图像 spec
    if record.get("track", "real") == "real":
        _refine_ig_image_specs(persona, posts, record.get("lang", config.LANGUAGES[0]))

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
