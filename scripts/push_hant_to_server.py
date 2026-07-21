# -*- coding: utf-8 -*-
"""Push already-translated Traditional Chinese content (in the arca storage hub) back
through the LIVE server so the server's own local cache is refreshed.

Why: the running web service reads its own local `data/` cache first (load_json returns
local hit without going remote). Our earlier fixes updated the remote hub + this machine's
cache, but not the server host's cache. This script uses the server's own write APIs so the
server persists the Traditional version into its cache (and remote).

- Personas: PUT /api/persona  {char_id, persona}   (only if server copy still has simplified)
- IG posts: PUT /api/ig_posts/{char_id}/{post_id}  {content}  (per simplified post)

Name is taken from the remote (already unchanged); we never alter it.
Idempotent: skips entries whose server copy is already Traditional.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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

import requests  # noqa: E402
from app import storage  # noqa: E402
import scripts.fix_residual_simplified_online as fix  # noqa: E402

BASE = os.environ.get("PUSH_BASE", "http://popop-pipeline.internal-app.imaginewithu.com")
_print_lock = threading.Lock()


def log(m):
    with _print_lock:
        print(m, flush=True)


def _simp_count(x) -> int:
    if isinstance(x, dict):
        return sum(_simp_count(v) for k, v in x.items() if k not in ("voice", "visibility"))
    if isinstance(x, list):
        return sum(_simp_count(v) for v in x)
    if isinstance(x, str):
        return sum(1 for ch in x if fix.conv_char(ch) != ch)
    return 0


def has_simp_obj(x) -> bool:
    if isinstance(x, dict):
        return any(has_simp_obj(v) for k, v in x.items() if k not in ("voice", "visibility"))
    if isinstance(x, list):
        return any(has_simp_obj(v) for v in x)
    if isinstance(x, str):
        return any(fix.conv_char(ch) != ch for ch in x)
    return False


def req(method, url, **kw):
    last = None
    for i in range(5):
        try:
            r = requests.request(method, url, timeout=kw.pop("timeout", 60), **kw)
            if r.status_code >= 500 or r.status_code == 429:
                last = f"HTTP {r.status_code}"; time.sleep(3 * (i + 1)); continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = str(e); time.sleep(3 * (i + 1))
    raise RuntimeError(f"{method} {url} failed: {last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--personas-only", action="store_true")
    ap.add_argument("--posts-only", action="store_true")
    args = ap.parse_args()
    dry = not args.apply

    chars = req("GET", f"{BASE}/api/characters").json()
    zh = [c for c in chars if c.get("lang") == "zh"]
    if args.limit:
        zh = zh[: args.limit]
    log(f"server zh characters = {len(zh)} | dry_run={dry} | base={BASE}")

    # Preload remote (Traditional) records once.
    remote_personas = {r.get("key"): (r.get("data") or {})
                       for r in storage.query_all("personas")}
    remote_ig = {}
    for r in storage.query_all("ig_batches"):
        d = r.get("data") or {}
        cid = d.get("char_id") or r.get("key")
        remote_ig[cid] = d

    persona_push = 0
    post_push = 0
    persona_skip = 0
    errors = 0

    def do_char(c):
        nonlocal persona_push, post_push, persona_skip, errors
        cid = c["char_id"]
        out = {"cid": cid, "persona": None, "posts": 0}
        # ---- persona ----
        if not args.posts_only:
            try:
                srv = req("GET", f"{BASE}/api/character/{cid}").json()
                srv_persona = srv.get("persona") or {}
                if has_simp_obj(srv_persona):
                    rem = remote_personas.get(cid) or {}
                    rem_persona = rem.get("persona")
                    # Remote is our latest verified-clean version. Push it as long as it is
                    # strictly better (fewer/equal simplified) than the server copy.
                    if isinstance(rem_persona, dict):
                        srv_name = srv_persona.get("name")
                        rem_name = rem_persona.get("name")
                        # never change the name: keep whatever name is authoritative (they match)
                        if not has_simp_obj(rem_persona) or _simp_count(rem_persona) < _simp_count(srv_persona):
                            if not dry:
                                req("PUT", f"{BASE}/api/persona",
                                    json={"char_id": cid, "persona": rem_persona})
                            out["persona"] = "pushed"
                        else:
                            out["persona"] = "remote_not_better"
                    else:
                        out["persona"] = "no_remote"
                else:
                    out["persona"] = "already_trad"
            except Exception as e:  # noqa: BLE001
                out["persona"] = f"ERR:{e}"
        # ---- posts ----
        if not args.personas_only:
            try:
                srv_ig = req("GET", f"{BASE}/api/ig_posts/{cid}/latest").json()
                srv_posts = srv_ig.get("posts") or [] if isinstance(srv_ig, dict) else []
                rem_posts = {p.get("post_id"): p.get("content")
                             for p in (remote_ig.get(cid, {}).get("posts") or [])
                             if isinstance(p, dict)}
                for p in srv_posts:
                    if not isinstance(p, dict):
                        continue
                    pid = p.get("post_id"); content = p.get("content")
                    if not isinstance(pid, str) or not isinstance(content, str):
                        continue
                    if any(fix.conv_char(ch) != ch for ch in content):
                        rc = rem_posts.get(pid)
                        if isinstance(rc, str) and not any(fix.conv_char(ch) != ch for ch in rc):
                            if not dry:
                                req("PUT", f"{BASE}/api/ig_posts/{cid}/{pid}",
                                    json={"content": rc})
                            out["posts"] += 1
            except Exception as e:  # noqa: BLE001
                out["posts"] = f"ERR:{e}"
        return out

    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as ex:
        futs = [ex.submit(do_char, c) for c in zh]
        for i, fut in enumerate(as_completed(futs), 1):
            o = fut.result()
            pst = o["persona"]; posts = o["posts"]
            if pst == "pushed":
                persona_push += 1
            elif pst == "already_trad":
                persona_skip += 1
            elif isinstance(pst, str) and pst.startswith("ERR"):
                errors += 1
            if isinstance(posts, int):
                post_push += posts
            elif isinstance(posts, str) and posts.startswith("ERR"):
                errors += 1
            if pst == "pushed" or (isinstance(posts, int) and posts) or (isinstance(pst,str) and pst.startswith("ERR")):
                log(f"  [{i}/{len(zh)}] {o['cid']} persona={pst} posts={posts}")

    log(f"DONE. persona_pushed={persona_push} persona_already_trad={persona_skip} "
        f"post_pushed={post_push} errors={errors} dry_run={dry}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
