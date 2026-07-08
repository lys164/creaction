"""把 137MB 的 decompositions.json 预处理成精简灵感库。

- 去掉冗余的 raw 字段（它只是 parsed 的字符串副本）
- 每个 frame 拍平成一条灵感：vibe/event/mood 作检索键，
  action/expression/makeup/clothes/framing/visual_style/shooting_style 作生图灵感
- 保留 char/file 溯源
无第三方依赖，可反复运行、完全从源库派生。
"""
import json
import sys
from pathlib import Path

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / "decompositions.json"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "app" / "data" / "inspiration_library.json"


def _flatten(framing):
    if not isinstance(framing, dict):
        return framing or None
    parts = []
    if framing.get("shooting_method"):
        parts.append(str(framing["shooting_method"]))
    if framing.get("composition"):
        parts.append(str(framing["composition"]))
    return " / ".join(parts) or None


def main():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    items = []
    for _, v in data.items():
        p = v.get("parsed") or {}
        prof = p.get("post_profile") or {}
        vibe = [x for x in (prof.get("vibe") or []) if x]
        char = v.get("char")
        file = v.get("file")
        for post in (p.get("posts") or []):
            event = [x for x in (post.get("event") or []) if x]
            mood = [x for x in (post.get("mood") or []) if x]
            for fr in (post.get("frames") or []):
                subj = (fr.get("subject") or [{}])
                subj = subj[0] if subj else {}
                item = {
                    "vibe": vibe,
                    "event": event,
                    "mood": mood,
                    "action": subj.get("action"),
                    "expression": subj.get("expression"),
                    "makeup": subj.get("makeup"),
                    "clothes": subj.get("clothes"),
                    "framing": _flatten(fr.get("framing")),
                    "visual_style": fr.get("visual_style"),
                    "shooting_style": fr.get("shooting_style"),
                    "src": f"{char}/{file}",
                }
                # 至少要有一个生图灵感字段才留
                if any(item[k] for k in ("action", "clothes", "framing", "visual_style")):
                    items.append(item)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    size_mb = OUT.stat().st_size / 1e6
    print(f"wrote {len(items)} items -> {OUT} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
