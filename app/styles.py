"""Art style registry.

The user will provide the real style words later in batch. For now we keep a few
placeholder styles so the pipeline runs end to end. Each style has an id, a display
name (multilingual optional), and a `prompt` snippet appended to image prompts.

To add/replace styles later, just edit STYLES (or load from data/styles.json).
"""
import json
from pathlib import Path

from . import config, storage

_STYLES_FILE = config.DATA_DIR / "styles.json"

DEFAULT_STYLES = [
    {
        "id": "realistic_portrait",
        "name": "Realistic Portrait 写实杂志感",
        "prompt": (
            "Contemporary international fashion magazine portrait and premium beauty "
            "campaign photography. A refined close-up or half-body portrait with natural "
            "soft lighting, clean cinematic color grading, elegant wardrobe styling, and a "
            "calm high-fashion atmosphere. Realistic but tastefully refined facial "
            "proportions, clear skin with subtle natural texture, understated makeup, soft "
            "natural hair, and a polished editorial presence. Borrow ONLY the lighting, "
            "styling and color grading of a high-end fashion editorial / beauty campaign / "
            "music artist promo — render a clean standalone portrait photograph. "
            "NOT illustration, NOT anime, NOT overly glamorous Hollywood red carpet "
            "photography, NOT harsh studio flash, NOT plastic AI skin smoothing, NOT "
            "exaggerated beauty filter, NOT cheap influencer selfie, NOT overly sexualized "
            "posing, NOT a magazine cover layout, NO text, NO headlines, NO masthead, "
            "NO barcode, NO logo, NO watermark."
        ),
    },
    {
        "id": "painterly_anime",
        "name": "Painterly Anime 厚涂幻想插画",
        "prompt": (
            "High-quality 2D painterly fantasy character key art with a rich hand-painted "
            "look. Forms are built primarily through large color masses, layered tones, and "
            "soft value transitions rather than thin line art. The face, hair, costume, and "
            "accessories are shaped by bold painted color blocks, subtle edges, and "
            "controlled shadow layering. Hair is rendered in large flowing sections with "
            "broad ribbon-like highlights, avoiding tiny individual hair strands. Eyes are "
            "elongated and expressive with layered irises and luminous depth. Skin is bright, "
            "stylized, and slightly idealized, with a polished fantasy illustration finish. "
            "NOT realistic photography, NOT 3D render, NOT thin-line cel anime, NOT flat TV "
            "animation coloring, NOT line-art-dominant drawing, NOT sketch, NOT rough concept "
            "art, NOT over-detailed hyperrealism."
        ),
    },
    {
        "id": "comic_portrait",
        "name": "Comic Portrait 暗黑暗恋漫画",
        "prompt": (
            "Moody semi-realistic character illustration with a mature dark romance and indie "
            "graphic novel atmosphere. Face-focused close-up composition with generous "
            "negative space, delicate black linework, refined facial anatomy, long tired "
            "eyes, a distant empty gaze, defined eyelids and lashes, slightly parted lips, a "
            "sharp jawline, and an elegant long neck. Pale low-saturation skin with a matte "
            "surface and subtle highlights on the cheeks, lips, ears, and neck. Tousled hair "
            "with irregular loose strands, soft painterly shading, restrained monochrome, "
            "gray, and muted warm tones. Low-key lighting with a quiet melancholic mood, "
            "subtle red rim light or window backlight, intimate and emotionally restrained "
            "character art. "
            "NOT photorealistic, NOT 3D, NOT cute anime, NOT chibi, NOT bright romance comic "
            "style, NOT flashy fantasy game CG, NOT heavy cel shading, NOT oil painting, NOT "
            "rough sketch, NOT unfinished drawing."
        ),
    },
    {
        "id": "webtoon_lineart",
        "name": "Webtoon Line Art 韩漫线稿",
        "prompt": (
            "Clean serialized webcomic character art with strong black ink linework as the "
            "dominant visual element. Crisp pen lines with clear thickness variation, sharp "
            "silhouettes, and minimal supporting color. Attractive stylized characters with a "
            "pointed chin, narrow sharp eyes, a very simple nose indicated by a dot or short "
            "line, thin lips, and an elegant tall body proportion. Hair is grouped into large "
            "dark shapes with only a few selective strands drawn in line. Coloring is simple "
            "and flat, using base colors with only one clear shadow layer. Skin has minimal "
            "shading, clothing folds are reduced to a few confident lines, and the background "
            "is simple, graphic, or solid-colored. "
            "NOT realistic photography, NOT complex lighting, NOT glossy reflections, NOT glow "
            "effects, NOT airbrushed rendering, NOT cute big-eye anime, NOT 3D CG, NOT "
            "painterly fantasy illustration, NOT highly detailed rendering."
        ),
    },
]


def load_styles() -> list[dict]:
    obj = storage.load_json("styles", "styles", _STYLES_FILE)
    if isinstance(obj, dict) and isinstance(obj.get("styles"), list):
        return obj["styles"]
    if _STYLES_FILE.exists():  # 兼容旧格式：文件是裸数组
        try:
            return json.loads(_STYLES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_STYLES


def save_styles(styles: list[dict]) -> None:
    # 存储中台的 data 须是 JSON 对象，包一层 {styles: [...]}；本地文件同格式
    storage.save_json("styles", "styles", {"styles": styles}, _STYLES_FILE)


def get_style(style_id: str) -> dict | None:
    for s in load_styles():
        if s["id"] == style_id:
            return s
    return None
