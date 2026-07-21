"""Direct import: 外星人小歪 TSV → arca 平臺建角色（跳過圖片→LLM 生成鏈路）。

TSV 本身就是 arca 的角色匯出，欄位與 /character/create 幾乎一一對應，因此
這裡直接把最新版本(v3)那一行對映成 character_create_form 建角色，方便在平臺測聊天。

用法:
  PYTHONPATH=. python3 scripts/import_xiaowai_direct.py [/path/to/外星人小歪.tsv]
"""
import csv
import json
import sys
from pathlib import Path

from app import arca_client, config

TSV_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / "外星人小歪.tsv"
LANG = "zh-Hant"          # 匯出即繁中；X-Region 會被解析成 TW（CN 會被平臺 403）
COVER_URL = ("https://cdn-prod-i18n-public.popop.ai/popop-fe-user-upload/"
             "images/1783481709562-df45a92f-11fe-4b90-ba0f-1c073c7ca917.jpg")


def _jload(text: str, default):
    """TSV 裡的 JSON 列可能帶外層雙引號轉義（""）。寬鬆解析，失敗回預設值。"""
    text = (text or "").strip()
    if not text:
        return default
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].replace('""', '"')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def parse_latest_row(path: Path) -> dict:
    """取 version 最大的那一行（v3）並按列位對映出建角色所需欄位。"""
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines(), delimiter="\t"))
    rows = [r for r in rows if len(r) >= 22 and r[2].strip()]
    if not rows:
        raise SystemExit(f"未從 {path} 解析出任何角色行")
    row = max(rows, key=lambda r: int(r[15] or 0))  # 第16列=version
    return {
        "name": row[2].strip(),
        "gender": row[4].strip(),
        "species": row[5].strip(),
        "voice_id": row[6].strip(),
        "profile": row[7].strip(),
        "tags": _jload(row[18], []),
        "customized_settings_raw": _jload(row[19], {}),
        "opening_prologue": _jload(row[20], []),
        "disposition": row[21].strip(),
    }


def build_customized_settings(raw, page_config) -> list[dict]:
    """把匯出的 customized_settings（v3 為 {tag_key: value} 字典）對齊平臺 setting_options。"""
    options = {s.get("tag_key"): s for s in page_config.get("setting_options") or []}
    items = []
    pairs = raw.items() if isinstance(raw, dict) else [
        (d.get("tag_key"), d.get("tag_value")) for d in raw if isinstance(d, dict)]
    for key, value in pairs:
        opt = options.get(key)
        if not opt or not value:
            continue  # 平臺無此設定項則丟棄，避免整單被拒
        items.append({
            "tag_key": key,
            "tag_name": opt.get("tag_name") or key,
            "tag_icon": opt.get("tag_icon") or "",
            "tag_value": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            "index": len(items),
        })
    return items


def main():
    data = parse_latest_row(TSV_PATH)
    print(f"角色: {data['name']}  species={data['species']}  voice={data['voice_id']}")

    mine = arca_client.list_my_characters(LANG)
    dup = [c for c in mine if c["name"] == data["name"]]
    if dup:
        print(f"平臺已存在同名角色，跳過建角色: {dup}")
        return

    pc = arca_client.get_page_config(LANG)

    print(f"上傳封面圖 …")
    import requests
    img = requests.get(COVER_URL, timeout=120).content
    media = arca_client.tos_upload(img, "creaction/xiaowai/cover.jpg", "image/jpeg", LANG)

    form = {
        "name": data["name"],
        "profile": data["profile"],
        "gender": data["gender"],
        "species": data["species"],
        "voice_id": data["voice_id"],
        "tags": data["tags"],
        "disposition": data["disposition"],
        "opening_prologue": data["opening_prologue"],
        "customized_settings": build_customized_settings(
            data["customized_settings_raw"], pc),
        "visibility": "public",
        "images": [{"image_type": "aigc", "is_main_pic": True, "media": media}],
    }
    print("customized_settings:", [s["tag_key"] for s in form["customized_settings"]])
    print("opening_prologue 條數:", len(form["opening_prologue"]))

    cid = arca_client.create_character(
        form, lang=LANG, idempotency_key=f"import-xiaowai-{data['voice_id'][:8]}")
    print(f"\n建角色成功 character_id = {cid}")
    print("現在可在平臺上找到「外星人小歪」並測試聊天。")


if __name__ == "__main__":
    main()
