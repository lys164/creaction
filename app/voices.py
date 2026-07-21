"""音色庫：按語種分組的 Fish audio 音色。音色ID = 模型ID。

不同語言可選的音色不同（data/voices.json 由資料表生成）。本專案人設是
【按語種各自原生創作】的，所以在每個語言的人設 prompt 裡只拼【該語種】的
音色清單，讓模型按角色性別從中選一個，voice 欄位回填所選音色的【模型ID】。
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
    """該語種的音色列表 [{name, gender, id}, ...]。"""
    return _load().get(lang, [])


def valid_ids(lang: str) -> set[str]:
    return {v["id"] for v in list_for(lang)}


def prompt_block(lang: str, gender: str | None = None) -> str:
    """渲染成可拼進 prompt 的音色清單文字；gender(男/女) 給定時優先只列該性別。"""
    items = list_for(lang)
    if gender:
        same = [v for v in items if v.get("gender") == gender]
        if same:
            items = same
    return "\n".join(
        f"- {v['id']} ｜ {v['name']}（{v.get('gender', '')}）" for v in items
    )
