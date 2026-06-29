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

from . import api_client, config, landing, prompts, styles


def _new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def _char_path(char_id: str) -> Path:
    return config.PERSONA_DIR / f"{char_id}.json"


def _existing_source_images(record: dict) -> list[str]:
    """Source image paths that still exist on disk (uploads may be cleaned up)."""
    return [p for p in record.get("source_images", []) if p and Path(p).exists()]


def _first_source_image(record: dict) -> str | None:
    """First source image that still exists, or None if all are missing."""
    imgs = _existing_source_images(record)
    return imgs[0] if imgs else None


def load_character(char_id: str) -> dict:
    p = _char_path(char_id)
    if not p.exists():
        raise FileNotFoundError(f"character {char_id} not found")
    return json.loads(p.read_text(encoding="utf-8"))


def save_character(record: dict) -> None:
    _char_path(record["char_id"]).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_characters() -> list[dict]:
    out = []
    for p in sorted(config.PERSONA_DIR.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        name = r.get("persona", {}).get("name", "")
        if isinstance(name, dict):  # legacy multilingual record
            name = name.get("zh") or next(iter(name.values()), "")
        out.append({
            "char_id": r.get("char_id"),
            "name": name,
            "lang": r.get("lang"),
            "lang_name": config.lang_name(r.get("lang")) if r.get("lang") else None,
            "group_id": r.get("group_id"),
            "cover_url": r.get("cover", {}).get("url") if r.get("cover") else None,
            "has_identity": bool(r.get("identity")),
            "exported": bool(r.get("exported")),
            "created": r.get("created"),
        })
    return out


# --------------------------------------------------------------------------
# Step 1: image -> persona  (one separate character PER language)
# --------------------------------------------------------------------------
def create_persona_one_lang(image_paths: list[str], lang: str,
                            user_hint: str = "", group_id: str | None = None) -> dict:
    """Create a single-language character: persona authored natively in `lang`."""
    uris = [api_client.file_to_data_uri(p) for p in image_paths]
    messages = prompts.build_persona_messages(uris, lang, user_hint=user_hint)
    persona = api_client.chat_json(messages, temperature=0.85)

    char_id = _new_id("char")
    record = {
        "char_id": char_id,
        "lang": lang,
        "group_id": group_id or char_id,
        "created": int(time.time()),
        "source_images": image_paths,
        "user_hint": user_hint,
        "persona": persona,
        "identity": None,
        "cover": None,
        "style_id": None,
    }
    save_character(record)
    return record


def create_personas_from_images(image_paths: list[str], langs: list[str],
                                user_hint: str = "") -> list[dict]:
    """For each selected language, create an independent native character record.

    All records from the same upload share a `group_id`.
    """
    langs = [l for l in langs if l in config.LANGUAGES] or [
        config.LANGUAGES[0]]
    group_id = _new_id("grp")
    records = []

    def _one(lang: str) -> dict:
        return create_persona_one_lang(
            image_paths, lang, user_hint=user_hint, group_id=group_id
        )

    with ThreadPoolExecutor(max_workers=min(len(langs), config.MAX_WORKERS)) as ex:
        futures = {ex.submit(_one, l): l for l in langs}
        for fut in as_completed(futures):
            records.append(fut.result())
    # keep stable order matching langs
    order = {l: i for i, l in enumerate(langs)}
    records.sort(key=lambda r: order.get(r["lang"], 99))
    return records


def regenerate_persona(char_id: str) -> dict:
    """只重新生成人设 schema：复用同一来源（源图 或 导入的原始 JSON）、同语言、同补充要求，
    原地覆盖 persona。

    不改图、不动 identity / cover / 帖子。用于批量重刷人设。
    """
    record = load_character(char_id)
    lang = record.get("lang", config.LANGUAGES[0])
    user_hint = record.get("user_hint", "")
    # 从原始 JSON 导入的角色：用同一份 import_source 重新扩写，保持忠实保留。
    if record.get("import_source") is not None:
        messages = prompts.build_persona_from_json_messages(
            record["import_source"], lang, user_hint=user_hint)
    else:
        uris = [api_client.file_to_data_uri(p)
                for p in _existing_source_images(record)]
        messages = prompts.build_persona_messages(uris, lang, user_hint=user_hint)
    record["persona"] = api_client.chat_json(messages, temperature=0.85)
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
                                      source_image: str | None = None) -> dict:
    """Create a single-language character from an existing source JSON object.

    The original object is stored under `import_source` so the persona can be
    re-expanded later. `source_image` (a local path, already downloaded) is
    shared across all language variants of the same source object.
    """
    messages = prompts.build_persona_from_json_messages(
        source_obj, lang, user_hint=user_hint)
    persona = api_client.chat_json(messages, temperature=0.85)

    char_id = _new_id("char")
    record = {
        "char_id": char_id,
        "lang": lang,
        "group_id": group_id or char_id,
        "created": int(time.time()),
        "source_images": [source_image] if source_image else [],
        "user_hint": user_hint,
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
                                  download_image: bool = True) -> list[dict]:
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
            source_image=source_image)

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
    """Return True if any remaining character record still references upload path."""
    for persona_path in config.PERSONA_DIR.glob("*.json"):
        try:
            rec = json.loads(persona_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
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
    if local and Path(local).exists():
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


# Export-time brand substitution: replace real messenger/app brand names with
# our own brand. Applied ONLY to the exported text, never to the stored source
# data. Longer terms come first so that e.g. "단톡방" is handled before "톡".
_BRAND_REPLACEMENTS = [
    ("KakaoTalk", "Popop"),
    ("Kakaotalk", "Popop"),
    ("kakaotalk", "Popop"),
    ("카카오톡", "Popop"),
    ("보이스톡", "Popop 음성통화"),
    ("단톡방", "Popop 단체방"),
    ("단톡", "Popop 단체"),
    ("카톡", "Popop"),
    # other real messengers/apps -> Popop
    ("WhatsApp", "Popop"),
    ("Whatsapp", "Popop"),
    ("WeChat", "Popop"),
    ("微信", "Popop"),
    ("Telegram", "Popop"),
    ("텔레그램", "Popop"),
    ("라인", "Popop"),
    ("连我", "Popop"),
    ("LINE", "Popop"),
]


def _apply_brand_replacements(text: str) -> str:
    for old, new in _BRAND_REPLACEMENTS:
        text = text.replace(old, new)
    return text


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
        if not p.exists():
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
                    deleted_any = True
            except OSError:
                pass

    return deleted_any


# --------------------------------------------------------------------------
# Step 2: persona -> identity (appearance DNA)
# --------------------------------------------------------------------------
def build_identity(char_id: str) -> dict:
    record = load_character(char_id)
    uris = [api_client.file_to_data_uri(p)
            for p in _existing_source_images(record)]
    messages = prompts.build_identity_messages(record["persona"], uris)
    identity = api_client.chat_json(messages, temperature=0.5)
    record["identity"] = identity
    save_character(record)
    return record


# --------------------------------------------------------------------------
# Step 3: cover image (identity + cover variable/scene + chosen style)
# --------------------------------------------------------------------------
def build_cover_spec(char_id: str) -> dict:
    """Generate the cover-specific variable + scene block."""
    record = load_character(char_id)
    if not record.get("identity"):
        build_identity(char_id)
        record = load_character(char_id)

    messages = prompts.build_cover_spec_messages(
        record["persona"], record["identity"])
    spec = api_client.chat_json(messages, temperature=0.65)
    record["cover_spec"] = {
        "variable": spec.get("variable", {}),
        "scene": spec.get("scene", {}),
    }
    save_character(record)
    return record


def generate_cover(
    char_id: str,
    style_id: str,
    use_reference: bool = False,
    mode: str = "fill_missing",
) -> dict:
    """Generate a cover image.

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

    if mode == "fill_missing" and not record.get("cover_spec"):
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
    )

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
        src = _first_source_image(record)
        if src:
            image_urls = [api_client.file_to_data_uri(src)]
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


def generate_posts(
    char_id: str,
    post_type_ids: list[str],
    count_per_type: int = 2,
    style_id: str | None = None,
    with_images: bool = True,
) -> dict:
    """Generate posts for each selected type, then optionally render images."""
    record = load_character(char_id)
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
            count=count_per_type,
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
    (_posts_dir(char_id) / f"{batch_id}.json").write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return batch


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
            (_posts_dir(char_id) / f"{batch_id}.json").write_text(
                json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
            )
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
    (_posts_dir(char_id) / f"{batch_id}.json").write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
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
    if lp and Path(lp).exists():
        return f"/img/{Path(lp).name}"
    return cover.get("url")


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

    # 收集最近一批 IG 帖子里【已生成且文件存在】的图，连同其文案，
    # 既作为多模态设计参考，也作为页面里真实展示的相册素材（oc-post-N 槽位）。
    ig = load_latest_ig(char_id) or {}
    post_imgs: list[dict] = []  # {local_path, url, caption}
    for post in (ig.get("posts", []) if isinstance(ig, dict) else []):
        if len(post_imgs) >= 6:
            break
        lp = (post.get("image") or {}).get("local_path")
        if lp and Path(lp).exists():
            post_imgs.append({
                "local_path": lp,
                "url": f"/img/{Path(lp).name}",
                "caption": _localized_text(
                    post.get("content"), record.get("lang", config.LANGUAGES[0])),
            })

    system_prompt = landing.build_system_prompt(style_text)
    user_text = landing.build_user_message(
        persona,
        record.get("lang", config.LANGUAGES[0]),
        has_cover=bool(cover_url),
        request=request,
        style_text=style_text,
        current_html=current_html,
        post_images=post_imgs,
    )

    # multimodal: show the cover + the post images so the model can match
    # colors / mood and lay out a real photo album. 落地页只参考【生成的封面图】
    # 和【已有帖子图】，都缺失就纯文字生成，绝不回退到上传源图。
    content: list[dict] = [{"type": "text", "text": user_text}]
    ref_paths: list[str] = []
    cover_lp = record.get("cover", {}).get(
        "local_path") if record.get("cover") else None
    if cover_lp and Path(cover_lp).exists():
        ref_paths.append(cover_lp)
    ref_paths.extend(pi["local_path"] for pi in post_imgs)
    for p in ref_paths:
        content.append(
            {"type": "image_url",
             "image_url": {"url": api_client.file_to_data_uri(p)}}
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
    raw = api_client.chat(messages, temperature=0.85, max_tokens=32000)
    html = landing.clean_html(raw)
    post_urls = [pi["url"] for pi in post_imgs]
    saved_html = landing.inject_cover(html, cover_url)
    saved_html = landing.inject_post_images(saved_html, post_urls)

    page_id = _new_id("page")
    page = {
        "page_id": page_id,
        "char_id": char_id,
        "lang": record.get("lang"),
        "created": int(time.time()),
        "style_text": style_text,
        "request": request,
        "cover_url": cover_url,
        "post_urls": post_urls,
        # raw (slots empty) — preview injects client-side
        "html": html,
        "html_filled": saved_html,  # cover + post URLs baked in for standalone use
    }
    # 单角色只保留最新一份，重新生成直接覆盖
    (_landing_dir(char_id) / "landing_latest.json").write_text(
        json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return page


def load_latest_landing(char_id: str) -> dict | None:
    p = _landing_dir(char_id) / "landing_latest.json"
    if not p.exists():
        return None
    try:
        page = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # Re-inject the cover on read so pages saved before the injector handled
    # background-image divs (and pages whose cover changed) still display it.
    raw = page.get("html")
    cover_url = page.get("cover_url")
    if raw and cover_url:
        page["html_filled"] = landing.inject_cover(raw, cover_url)
    if raw and page.get("post_urls"):
        base = page.get("html_filled") or raw
        page["html_filled"] = landing.inject_post_images(base, page["post_urls"])
    return page


def load_batch(char_id: str, batch_id: str) -> dict:
    p = _posts_dir(char_id) / f"{batch_id}.json"
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Instagram feed: infer recent N posts, render selfie(i2i)/photo(t2i) images
# --------------------------------------------------------------------------
def _ref_image_uri_for_selfie(record: dict) -> str | None:
    """Prefer the redrawn cover as the i2i reference; fall back to source image."""
    cover = record.get("cover") or {}
    if cover.get("local_path") and Path(cover["local_path"]).exists():
        return api_client.file_to_data_uri(cover["local_path"])
    if cover.get("url"):
        return cover["url"]
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


def generate_instagram_posts(
    char_id: str,
    n: int | None = None,
    style_id: str | None = None,
    with_images: bool = True,
) -> dict:
    """Infer N recent IG posts, then render images:
    - selfie  -> image-to-image using the redrawn cover as reference
    - photo   -> text-to-image (no person reference)
    - text_only -> no image
    """
    record = load_character(char_id)
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
            avoid_kinds=avoid_kinds,
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
    (_posts_dir(char_id) / "ig_latest.json").write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return batch


def load_latest_ig(char_id: str) -> dict | None:
    p = _posts_dir(char_id) / "ig_latest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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
            (_posts_dir(char_id) / "ig_latest.json").write_text(
                json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return {"post": post, "batch": batch}
    raise ValueError(f"Instagram post {post_id} not found")


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
    (_posts_dir(char_id) / "ig_latest.json").write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"deleted": post_id, "batch": batch}
