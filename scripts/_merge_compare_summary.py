import json
import re
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "data" / "compare_youxing"
ORDER = [
    ("A_reasoning_gemini", "当前线上：有 reasoning + gemini"),
    ("B_noreason_gemini", "去掉 reasoning + gemini"),
    ("C_reasoning_gpt5", "有 reasoning + gpt-5"),
    ("D_noreason_gpt5", "去掉 reasoning + gpt-5"),
]


def excl(s: str) -> int:
    return len(re.findall(r"[!！]", s or ""))


lines = ["# 游星 帖子四版对比（zh/real，同一 persona / 同 prompt / 同 API 池）", ""]
lines.append("| 版本 | 说明 | 叹号均值 | 高亢帖(>=3) | 平静帖(0) |")
lines.append("|---|---|---|---|---|")

data = {}
for key, desc in ORDER:
    d = json.loads((D / f"{key}.json").read_text(encoding="utf-8"))
    data[key] = d
    posts = d["posts"]
    n = len(posts) or 1
    e = [excl(p.get("content")) for p in posts]
    lines.append(
        f"| {key} | {desc} | {sum(e)/n:.1f} | "
        f"{sum(1 for x in e if x >= 3)}/{len(posts)} | "
        f"{sum(1 for x in e if x == 0)}/{len(posts)} |"
    )

for key, desc in ORDER:
    d = data[key]
    lines.append(f"\n\n## {key}  —  {desc}  (model={d['model']}, {d['n']} 条)")
    pr = d.get("persona_read")
    if pr:
        lines.append("persona_read:")
        lines.append("```json")
        lines.append(json.dumps(pr, ensure_ascii=False, indent=2))
        lines.append("```")
    for i, p in enumerate(d["posts"], 1):
        tag = f"[{p.get('post_type')}/{p.get('format')}/{p.get('image_type') or '-'}"
        if p.get("photo_kind"):
            tag += f"/{p.get('photo_kind')}"
        tag += "]"
        lines.append(f"{i}. {tag} {p.get('content')}")

(D / "summary.md").write_text("\n".join(lines), encoding="utf-8")
print("written:", D / "summary.md")
