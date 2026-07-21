# -*- coding: utf-8 -*-
"""Deterministic residual Simplified->Traditional fixer over FULL remote data.

Only converts characters that are unambiguously Simplified-side (has a Traditional
form via s2t AND is itself the simplified variant), skipping a whitelist of shared
variant characters that Taiwan Traditional text legitimately uses (台/里/群/峰/游...).

- Never touches persona.name (protected & restored).
- Never touches keys ending with id/url/path or voice/visibility values.
- Converts persona editable text + ig_batches post contents.
- Writes back to local cache + remote storage hub, with per-record backup.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_envp = ROOT / ".env"
if _envp.exists():
    for _raw in _envp.read_text(encoding="utf-8").splitlines():
        _l = _raw.strip()
        if not _l or _l.startswith("#") or "=" not in _l:
            continue
        _k, _v = _l.split("=", 1)
        _k = _k.strip(); _v = _v.strip()
        if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ("'", '"'):
            _v = _v[1:-1]
        os.environ.setdefault(_k, _v)

from app import config, storage  # noqa: E402
import opencc  # noqa: E402

_s2t = opencc.OpenCC("s2t")
_t2s = opencc.OpenCC("t2s")

# Taiwan-standard overrides where opencc s2t picks a variant Taiwan doesn't use as default.
_TW_OVERRIDE = {
    "\u4e3a": "\u70ba",  # 为 -> 為 (not 爲)
}

# Characters shared/legitimate in Taiwan Traditional that s2t would "upgrade" to a rarer
# variant; we must NOT touch these to avoid over-conversion (台北, 群組, 山峰, 里 as 裡/里, etc.)
SHARED = set("台里群峰游唇床吃伙凶秘斗郁岩托皂征于面松谷丰范准党占雇栗霉熏干扎累恶")

SKIP_KEYS = {"name", "voice", "visibility"}
SKIP_SUFFIXES = ("_id", "_url", "_path", "url", "local_path")

_cache: dict[str, str] = {}


def conv_char(ch: str) -> str:
    if ch in _cache:
        return _cache[ch]
    out = ch
    if "\u4e00" <= ch <= "\u9fff" and ch not in SHARED:
        t = _s2t.convert(ch)
        # ch is simplified-side if it has a different traditional form and ch itself
        # maps to itself under t2s (i.e., ch is not already a traditional-only glyph).
        if t != ch and _t2s.convert(ch) == ch:
            out = _TW_OVERRIDE.get(ch, t)
    _cache[ch] = out
    return out


def fix_text(s: str) -> tuple[str, int]:
    n = 0
    buf = []
    for ch in s:
        c = conv_char(ch)
        if c != ch:
            n += 1
        buf.append(c)
    return "".join(buf), n


def fix_obj(x, changed_counter: list[int]):
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if k in SKIP_KEYS or any(k.endswith(suf) for suf in SKIP_SUFFIXES):
                out[k] = v
            else:
                out[k] = fix_obj(v, changed_counter)
        return out
    if isinstance(x, list):
        return [fix_obj(v, changed_counter) for v in x]
    if isinstance(x, str):
        ns, n = fix_text(x)
        changed_counter[0] += n
        return ns
    return x


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dry = not args.apply
    if not storage.arca_storage.enabled():
        print("ERROR: remote storage not enabled")
        return 2

    print("pulling remote personas / ig_batches ...", flush=True)
    prs = storage.query_all("personas")
    igs = storage.query_all("ig_batches")
    zh_p = [r for r in prs if (r.get("data") or {}).get("lang") == "zh"]
    zh_ig = [r for r in igs if (r.get("data") or {}).get("lang") == "zh"]

    backup = config.DATA_DIR / f"_backup_zh_hant_residualfix_{time.strftime('%Y%m%d_%H%M%S')}"
    if not dry:
        backup.mkdir(parents=True, exist_ok=True)

    def bkp(coll, key, data):
        d = backup / coll
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{key}.json"
        if not f.exists():
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    p_changed = 0; p_chars = 0
    for r in zh_p:
        d = r.get("data") or {}
        cid = d.get("char_id") or r.get("key")
        persona = d.get("persona")
        if not isinstance(persona, dict):
            continue
        name = persona.get("name")
        cnt = [0]
        new_persona = fix_obj(persona, cnt)
        if isinstance(name, str):
            new_persona["name"] = name  # ensure untouched exactly
        if cnt[0] > 0 and new_persona != persona:
            p_changed += 1; p_chars += cnt[0]
            if not dry:
                bkp("personas", cid, d)
                nd = copy.deepcopy(d); nd["persona"] = new_persona
                if isinstance(name, str) and nd["persona"].get("name") != name:
                    print(f"  ! name mismatch guard, skip {cid}"); continue
                storage.save_json("personas", nd.get("char_id") or cid, nd,
                                  config.PERSONA_DIR / f"{cid}.json")

    ig_changed = 0; ig_posts = 0; ig_chars = 0
    for r in zh_ig:
        d = r.get("data") or {}
        cid = d.get("char_id") or r.get("key")
        posts = d.get("posts")
        if not isinstance(posts, list):
            continue
        total = [0]; touched = 0
        new_posts = copy.deepcopy(posts)
        for post in new_posts:
            if isinstance(post, dict) and isinstance(post.get("content"), str):
                ns, n = fix_text(post["content"])
                if n:
                    post["content"] = ns; total[0] += n; touched += 1
        if touched:
            ig_changed += 1; ig_posts += touched; ig_chars += total[0]
            if not dry:
                bkp("ig_batches", cid, d)
                nd = copy.deepcopy(d); nd["posts"] = new_posts
                storage.save_json("ig_batches", str(cid), nd,
                                  config.POST_DIR / str(cid) / "ig_latest.json")

    print("=== residual simplified fix ===")
    print(f"dry_run={dry}")
    print(f"personas changed={p_changed} (chars converted={p_chars}) / {len(zh_p)}")
    print(f"ig_batches changed={ig_changed} posts touched={ig_posts} (chars={ig_chars}) / {len(zh_ig)}")
    if not dry:
        print(f"backup: {backup.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
