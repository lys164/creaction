"""音色库：按语种分组的 Fish audio 音色。音色ID = 模型ID。

不同语言可选的音色不同（data/voices.json 由数据表生成）。本项目人设是
【按语种各自原生创作】的，所以在每个语言的人设 prompt 里只拼【该语种】的
音色清单，让模型按角色性别从中选一个，voice 字段回填所选音色的【模型ID】。
"""
import json
from pathlib import Path

_VOICES_FILE = Path(__file__).resolve().parent / "data" / "voices.json"
_CACHE = None


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(_VOICES_FILE.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            _CACHE = {}
    return _CACHE


def list_for(lang: str) -> list[dict]:
    """该语种的音色列表 [{name, gender, id}, ...]。"""
    return _load().get(lang, [])


def valid_ids(lang: str) -> set[str]:
    return {v["id"] for v in list_for(lang)}


def prompt_block(lang: str, gender: str | None = None) -> str:
    """渲染成可拼进 prompt 的音色清单文本；gender(男/女) 给定时优先只列该性别。"""
    items = list_for(lang)
    if gender:
        same = [v for v in items if v.get("gender") == gender]
        if same:
            items = same
    return "\n".join(
        f"- {v['id']} ｜ {v['name']}（{v.get('gender', '')}）" for v in items
    )
