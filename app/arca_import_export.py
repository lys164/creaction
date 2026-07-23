"""Build and validate the importing-arca-character delivery contract.

This module deliberately sits at the export boundary.  The production persona
prompt remains free to optimise character quality; this adapter turns its
stored output into the exact ``ImportCharacterReq`` needed by
``POST /internal/import/character``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from .arca_mapping import normalize_gender
from .persona_export import build_personality_text, to_export_schema


_log = logging.getLogger(__name__)

CONTRACT_NAME = "importing-arca-character"
SPECIES = frozenset({"人类", "精灵", "兽人", "动物", "其他"})
GENDERS = frozenset({"male", "female", "other"})
VISIBILITIES = frozenset({"public", "private"})
OPENING_OUTPUT_TYPES = frozenset({"tts", "text", "system"})
IMAGE_TYPES = frozenset({"aigc", "upload"})
LANDING_STYLES = frozenset({"cutesy", "y2k", "indie_zine", "cyber_future"})

# The names are storage keys, not translated labels.  They are defined by the
# importing Skill and must also be present in /character/page_config.
SETTING_KEYS = frozenset({
    "hometown", "residence", "occupation", "appearance", "speech_style",
    "fashion_style", "relationship_mode", "love_style", "values",
    "life_details", "likes", "dislike", "backstory", "social_network",
    "worldview", "wishlist", "identity", "relationship_with_user",
    "behavior_patterns", "inner_structure",
})

_PERSONA_SETTING_KEYS = {
    # The production schema's compact identity card (age/height/body type,
    # etc.) has no separate import field.  Appearance is the contract field
    # that preserves it without changing the generation prompt.
    "value": "appearance",
    "hometown": "hometown",
    "residence": "residence",
    # social_status/fears/premise are accepted only because the export adapter
    # also supports records written before the current persona schema.
    "social_status": "occupation",
    "occupation": "occupation",
    "appearance": "appearance",
    "speech_style": "speech_style",
    "fashion_style": "fashion_style",
    "relationship_mode": "relationship_mode",
    "love_style": "love_style",
    "values": "values",
    "life_details": "life_details",
    "likes": "likes",
    "dislikes": "dislike",
    "fears": "dislike",
    "backstory": "backstory",
    "social_network": "social_network",
    "family": "social_network",
    "worldview": "worldview",
    "premise": "worldview",
    "wishlist": "wishlist",
    "identity": "identity",
    "relationship_with_user": "relationship_with_user",
    "behavior_patterns": "behavior_patterns",
    "inner_structure": "inner_structure",
}

_SPECIES_ALIASES = {
    "human": "人类", "人类": "人类", "人類": "人类",
    "elf": "精灵", "精灵": "精灵", "精靈": "精灵",
    "orc": "兽人", "兽人": "兽人", "獸人": "兽人",
    "animal": "动物", "动物": "动物", "動物": "动物",
    "other": "其他", "其它": "其他", "其他": "其他",
}
_SINGLE_USER_PLACEHOLDER = re.compile(r"(?<!\{)\{user\}(?!\})")
# 双花括号占位符（规范形态）：tts 行合成时后端会剔除它，构建时同步剔除以对齐存库文本。
_USER_PLACEHOLDER = re.compile(r"\{\{\s*user\s*\}\}")


@dataclass(frozen=True)
class ContractViolation:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class ImportCharacterContractError(ValueError):
    def __init__(self, violations: list[ContractViolation]):
        self.violations = tuple(violations)
        detail = "\n".join(f"- {issue}" for issue in violations)
        super().__init__(f"{CONTRACT_NAME} validation failed:\n{detail}")


def build_import_character_req(
    record: dict,
    *,
    page_config: dict,
    images: list[dict],
    landing_page_url: str | None,
    provider: str,
) -> dict:
    """Convert one persisted POPOP record into a complete ImportCharacterReq.

    Structural conversions (old schema field names, gender and species storage
    values, display tag names) are safe to do here.  No content is invented:
    unknown tags or an unavailable voice remain visible for the validator to
    reject with a precise path.
    """
    record = record or {}
    raw_persona = record.get("persona") or {}
    lang = record.get("lang") or "zh"
    persona = to_export_schema(raw_persona, lang)

    form: dict[str, Any] = {
        "name": _text(persona.get("name")),
        "gender": normalize_gender(_text(persona.get("gender"))),
        "species": _canonical_species(persona.get("species"), page_config),
        "tags": _normalise_tags(persona.get("tags"), page_config),
        "voice_id": _text(persona.get("voice")),
        "profile": _text(persona.get("profile")),
        "disposition": _disposition(persona, lang),
        "visibility": _visibility(persona.get("visibility")),
        "images": list(images or []),
    }

    anonymous_tags = _string_list(persona.get("anonymous_identities"))
    if anonymous_tags:
        form["anonymous_tags"] = anonymous_tags

    creators_note = _text((persona.get("opening") or {}).get("note")) \
        if isinstance(persona.get("opening"), dict) else ""
    if creators_note:
        form["creators_note"] = creators_note
    opening = _opening_prologue(persona.get("opening"))
    if opening:
        form["opening_prologue"] = opening

    settings = _customized_settings(persona)
    if settings:
        form["customized_settings"] = settings
    if landing_page_url:
        form["landing_page_url"] = landing_page_url

    return {
        "external_character_id": _text(record.get("char_id")),
        "provider": _text(provider),
        "character_create_form": form,
    }


def validate_import_character_req(
    request: Any, *, page_config: dict | None,
    public_asset_hosts: set[str] | None = None,
) -> list[ContractViolation]:
    """Return all ImportCharacterReq violations; never silently repair them."""
    issues: list[ContractViolation] = []
    if not isinstance(request, dict):
        return [ContractViolation("request", "must be an object")]
    for key in sorted(set(request) - {
        "external_character_id", "provider", "character_create_form"}):
        issues.append(ContractViolation(f"request.{key}", "is not allowed"))
    for key in ("external_character_id", "provider"):
        if not _text(request.get(key)):
            issues.append(ContractViolation(f"request.{key}", "must be a non-empty string"))

    form = request.get("character_create_form")
    if not isinstance(form, dict):
        issues.append(ContractViolation("request.character_create_form", "must be an object"))
        return issues
    prefix = "request.character_create_form"
    for key in ("name", "profile", "disposition", "voice_id"):
        if not _text(form.get(key)):
            issues.append(ContractViolation(f"{prefix}.{key}", "must be a non-empty string"))
    if form.get("gender") not in GENDERS:
        issues.append(ContractViolation(f"{prefix}.gender", "must be male, female, or other"))
    if form.get("species") not in SPECIES:
        issues.append(ContractViolation(
            f"{prefix}.species", "must be one of 人类/精灵/兽人/动物/其他"))
    if form.get("visibility") not in VISIBILITIES:
        issues.append(ContractViolation(f"{prefix}.visibility", "must be public or private"))

    _validate_page_config(issues, form, page_config, prefix)
    _validate_opening(issues, form.get("opening_prologue"), f"{prefix}.opening_prologue")
    _validate_images(issues, form.get("images"), f"{prefix}.images")
    _validate_settings(issues, form.get("customized_settings"), page_config,
                       f"{prefix}.customized_settings")
    _validate_landing_url(issues, form.get("landing_page_url"), public_asset_hosts,
                          f"{prefix}.landing_page_url")

    style = form.get("landing_page_style")
    if style is not None:
        if not isinstance(style, dict) or style.get("style_key") not in LANDING_STYLES:
            issues.append(ContractViolation(
                f"{prefix}.landing_page_style.style_key",
                "must be cutesy, y2k, indie_zine, or cyber_future"))
    return issues


def assert_valid_import_character_req(
    request: dict, *, page_config: dict | None,
    public_asset_hosts: set[str] | None = None,
) -> None:
    issues = validate_import_character_req(
        request, page_config=page_config, public_asset_hosts=public_asset_hosts)
    if issues:
        raise ImportCharacterContractError(issues)


def validate_landing_html(html: str, public_asset_hosts: set[str]) -> list[ContractViolation]:
    """Validate the Skill's public-bucket/no-inline landing asset rules."""
    issues: list[ContractViolation] = []
    if len((html or "").encode("utf-8")) > 16 * 1024 * 1024:
        issues.append(ContractViolation("landing.html", "must be no larger than 16 MB"))
    if re.search(r"\bdata:\s*", html or "", flags=re.IGNORECASE):
        issues.append(ContractViolation("landing.html", "must not contain data: base64 assets"))
    parser = _AssetParser()
    try:
        parser.feed(html or "")
    except Exception as exc:  # HTMLParser is forgiving; retain a useful guard.
        issues.append(ContractViolation("landing.html", f"cannot parse asset references: {exc}"))
        return issues
    for location, src in parser.sources + _css_urls(html or ""):
        url = urlparse(src)
        if url.scheme != "https" or not url.hostname:
            issues.append(ContractViolation(
                f"landing.html {location}", "must be an absolute https public-bucket URL"))
        elif url.hostname not in public_asset_hosts:
            issues.append(ContractViolation(
                f"landing.html {location}", "must point to the public OSS bucket"))
    if parser.audio_sources and (
        "<button" not in (html or "").lower()
        or not re.search(r"\.play\s*\(", html or "")
    ):
        issues.append(ContractViolation(
            "landing.html audio", "must have a clickable playback button and script"))
    return issues


def _validate_page_config(issues: list[ContractViolation], form: dict,
                          page_config: dict | None, prefix: str) -> None:
    if not isinstance(page_config, dict):
        issues.append(ContractViolation("page_config", "is required to validate tags and voice_id"))
        return
    tag_keys = _config_keys(page_config, "character_tags", "tag_key")
    voice_ids = _config_keys(page_config, "voices", "voice_id")
    if not tag_keys:
        issues.append(ContractViolation("page_config.character_tags", "is unavailable"))
    tags = form.get("tags")
    if not isinstance(tags, list) or not tags:
        issues.append(ContractViolation(f"{prefix}.tags", "must be a non-empty array"))
    elif tag_keys:
        # SKILL 口径：tag 属软校验（不在集合内只记日志、不拒导入），后端本身也
        # 只对 tag 做软校验。集合外的值没有 i18n 翻译，这里记 warning 供事后核对，
        # 但不再作为硬违约拦截导入。
        off_config = [tag for tag in tags if tag not in tag_keys]
        if off_config:
            _log.warning(
                "%s: %d tag(s) not in page_config character_tags (soft check, "
                "imported as-is, no i18n translation): %s",
                prefix, len(off_config), ", ".join(map(str, off_config)))
    if not voice_ids:
        issues.append(ContractViolation("page_config.voices", "is unavailable"))
    elif form.get("voice_id") not in voice_ids:
        issues.append(ContractViolation(
            f"{prefix}.voice_id", "is not a page_config voice_id"))


def _validate_opening(issues: list[ContractViolation], opening: Any, path: str) -> None:
    if opening is None:
        return
    if not isinstance(opening, list):
        issues.append(ContractViolation(path, "must be an array"))
        return
    for index, item in enumerate(opening):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            issues.append(ContractViolation(item_path, "must be an object"))
            continue
        if not _text(item.get("text")):
            issues.append(ContractViolation(f"{item_path}.text", "must be a non-empty string"))
        output_type = item.get("output_type")
        if output_type not in OPENING_OUTPUT_TYPES:
            issues.append(ContractViolation(
                f"{item_path}.output_type", "must be tts, text, or system"))
        if output_type == "tts":
            resource_id = item.get("tts_resource_id", "")
            if resource_id is not None and not isinstance(resource_id, str):
                issues.append(ContractViolation(
                    f"{item_path}.tts_resource_id", "must be empty or a platform resource id"))
            # SKILL 口径：tts 行的 {{user}} 由后端合成时剔除、存库文本同步改写，属正常
            # 兼容行为而非违约。构建阶段（_opening_prologue）已剔除，故此处不再硬拒。


def _validate_images(issues: list[ContractViolation], images: Any, path: str) -> None:
    if not isinstance(images, list):
        issues.append(ContractViolation(path, "must be an array"))
        return
    main_count = 0
    for index, image in enumerate(images):
        item_path = f"{path}[{index}]"
        if not isinstance(image, dict):
            issues.append(ContractViolation(item_path, "must be an object"))
            continue
        if image.get("image_type") not in IMAGE_TYPES:
            issues.append(ContractViolation(
                f"{item_path}.image_type", "must be aigc or upload"))
        if image.get("is_main_pic") is True:
            main_count += 1
        media = image.get("media")
        if not isinstance(media, dict):
            issues.append(ContractViolation(f"{item_path}.media", "must be an object"))
            continue
        has_key = bool(_text(media.get("object_key")))
        url = _text(media.get("url"))
        if not has_key and not _is_absolute_http_url(url):
            issues.append(ContractViolation(
                f"{item_path}.media", "needs object_key or an absolute http(s) url"))
    if main_count != 1:
        issues.append(ContractViolation(path, "must contain exactly one is_main_pic=true image"))


def _validate_settings(issues: list[ContractViolation], settings: Any,
                       page_config: dict | None, path: str) -> None:
    if settings is None:
        return
    if not isinstance(settings, list):
        issues.append(ContractViolation(path, "must be an array"))
        return
    config_keys = _config_keys(page_config or {}, "setting_options", "tag_key")
    if settings and not config_keys:
        issues.append(ContractViolation("page_config.setting_options", "is unavailable"))
    for index, item in enumerate(settings):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            issues.append(ContractViolation(item_path, "must be an object"))
            continue
        key = item.get("tag_key")
        if key not in SETTING_KEYS:
            issues.append(ContractViolation(f"{item_path}.tag_key", "is not a contract setting key"))
        elif config_keys and key not in config_keys:
            issues.append(ContractViolation(
                f"{item_path}.tag_key", "is not in page_config.setting_options"))
        if not _text(item.get("tag_value")):
            issues.append(ContractViolation(f"{item_path}.tag_value", "must be a non-empty string"))


def _validate_landing_url(issues: list[ContractViolation], url: Any,
                          public_asset_hosts: set[str] | None, path: str) -> None:
    if url is None:
        return
    parsed = urlparse(_text(url))
    if parsed.scheme != "https" or not parsed.netloc:
        issues.append(ContractViolation(path, "must be an absolute https URL"))
        return
    if public_asset_hosts:
        if parsed.hostname not in public_asset_hosts:
            issues.append(ContractViolation(path, "must point to the public OSS bucket"))


def _canonical_species(value: Any, page_config: dict | None = None) -> str:
    """把 species 反解成 page_config 下发的规范 tag_key（5 个中文 key）。

    优先用 page_config 的 species 枚举做反解：tag_key 原样保留、本地化 tag_name
    （如韩语「인간」、日语「その他」、繁中「人類」）映射回对应 tag_key，无需维护
    多语言别名表。page_config 缺失或查不到时，退回内建别名表（覆盖英文导出包等
    page_config 不下发的形态）兜底。"""
    raw = _text(value)
    if isinstance(page_config, dict):
        lookup: dict[str, str] = {}
        for item in page_config.get("species") or []:
            key = _text((item or {}).get("tag_key"))
            name = _text((item or {}).get("tag_name"))
            if key:
                lookup[key] = key
                if name:
                    lookup[name] = key
        if raw in lookup:
            return lookup[raw]
    return _SPECIES_ALIASES.get(raw.lower(), raw)


def _normalise_tags(value: Any, page_config: dict) -> list[str]:
    lookup: dict[str, str] = {}
    for item in page_config.get("character_tags") or []:
        key = _text((item or {}).get("tag_key"))
        name = _text((item or {}).get("tag_name"))
        if key:
            lookup[key] = key
            if name:
                lookup[name] = key
    tags: list[str] = []
    for item in _string_list(value):
        key = lookup.get(item, item)
        if key not in tags:
            tags.append(key)
    return tags


def _disposition(persona: dict, lang: str) -> str:
    personality = persona.get("personality")
    if isinstance(personality, dict):
        personality = build_personality_text(
            personality, lang, normalize_gender(_text(persona.get("gender"))))
    parts = [_text(personality), _text(persona.get("inner_structure"))]
    return "\n".join(part for part in parts if part)


def _visibility(value: Any) -> str:
    value = _text(value).lower()
    # The API defaults a missing value to private.  Emit it explicitly so an
    # export remains stable if the server default ever changes; public is never
    # inferred and must come from the stored persona.
    return value if value in VISIBILITIES else "private"


def _opening_prologue(opening: Any) -> list[dict]:
    if not isinstance(opening, dict):
        return []
    messages = opening.get("messages")
    if not isinstance(messages, list):
        return []
    output: list[dict] = []
    for message in messages:
        if isinstance(message, str):
            text, output_type = message, "text"
        elif isinstance(message, dict):
            data = message.get("data") if isinstance(message.get("data"), dict) else {}
            text = data.get("content") or message.get("content") or ""
            output_type = "tts" if message.get("type") == "voice" else "text"
        else:
            continue
        text = _SINGLE_USER_PLACEHOLDER.sub("{{user}}", _text(text))
        if output_type == "tts":
            # SKILL 口径：tts 行念不出占位符，后端合成时会剔除 {{user}} 且存库文本
            # 同步改写为剔除后的版本。这里在构建时就对齐这一最终形态，避免把动态
            # 称呼残留进语音行；想保留 {{user}} 动态称呼应改用 output_type=text 行。
            text = _USER_PLACEHOLDER.sub("", text).strip()
        if not text:
            continue
        item = {"text": text, "output_type": output_type}
        if output_type == "tts":
            item["tts_resource_id"] = ""
        output.append(item)
    return output


def _customized_settings(persona: dict) -> list[dict]:
    values: dict[str, str] = {}
    for source_key, target_key in _PERSONA_SETTING_KEYS.items():
        value = persona.get(source_key)
        if value in (None, "", [], {}):
            continue
        text = _naturalize(value)
        if text:
            # A stored persona can have both ``value`` and ``appearance``;
            # they map to the same contract setting and both describe useful
            # character information, so retain rather than overwrite either.
            existing = values.get(target_key)
            values[target_key] = (
                existing if existing == text else f"{existing}\n{text}"
            ) if existing else text
    # online_chat_style has no independent ImportCharacterReq setting.  It is a
    # subtype of speech style, so preserve it in the platform's speech_style
    # setting rather than discarding it or emitting an invalid custom key.
    online_chat = _naturalize(persona.get("online_chat_style"))
    if online_chat:
        existing = values.get("speech_style", "")
        values["speech_style"] = "\n".join(
            part for part in (existing, f"线上聊天习惯：{online_chat}") if part)
    return [
        {"tag_key": key, "tag_value": value}
        for key, value in values.items()
    ]


def _config_keys(page_config: dict, field: str, key_field: str) -> set[str]:
    return {
        _text((item or {}).get(key_field))
        for item in page_config.get(field) or []
        if _text((item or {}).get(key_field))
    }


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_text(value)] if _text(value) else []
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    return []


def _naturalize(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(part for part in (_naturalize(item) for item in value) if part)
    if isinstance(value, dict):
        return "；".join(
            f"{key}: {text}" for key, item in value.items()
            if (text := _naturalize(item)))
    return ""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_absolute_http_url(value: Any) -> bool:
    parsed = urlparse(_text(value))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class _AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources: list[tuple[str, str]] = []
        self.audio_sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag not in {"img", "audio", "source", "video", "script", "iframe", "embed", "object", "link"}:
            return
        attr_map = dict(attrs)
        for attribute in ("src", "href", "poster", "data"):
            src = attr_map.get(attribute)
            if not src:
                continue
            self.sources.append((f"<{tag} {attribute}>", src))
            if tag in {"audio", "source"} and attribute == "src":
                self.audio_sources.append(src)
        if tag in {"img", "source"} and attr_map.get("srcset"):
            for candidate in attr_map["srcset"].split(","):
                src = candidate.strip().split(maxsplit=1)[0] if candidate.strip() else ""
                if src:
                    self.sources.append((f"<{tag} srcset>", src))


_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)


def _css_urls(html: str) -> list[tuple[str, str]]:
    """Return CSS url(...) asset references, including inline style blocks."""
    urls: list[tuple[str, str]] = []
    for match in _CSS_URL_RE.finditer(html):
        value = match.group(2).strip()
        if value and not value.startswith("#"):
            urls.append(("CSS url()", value))
    return urls
