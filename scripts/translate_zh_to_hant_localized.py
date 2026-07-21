# -*- coding: utf-8 -*-
"""Batch localize existing zh persona/post data from Simplified Chinese to Traditional Chinese.

Scope:
- data/personas/*.json records with lang == "zh": translate record["persona"] text.
- data/posts/<char_id>/*.json batches whose lang == "zh" (or whose persona is zh):
  translate each post["content"] only.

Safety:
- persona.name is never sent as an editable field and is restored exactly.
- Occurrences of the character name inside translated text are protected with a token.
- {user} placeholders, URLs, IDs, opening message type values, voice IDs, and visibility enums are preserved.
- Applies with per-file backups and resumable state.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    """Load ROOT/.env without overriding existing environment variables.

    This avoids relying on shell `source`, which can break JSON-valued env vars.
    Secrets are never printed.
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        os.environ.setdefault(k, v)


_load_dotenv()
from app import api_client, config, storage  # noqa: E402

PERSONA_DIR = config.PERSONA_DIR
POST_DIR = config.POST_DIR
DATA_DIR = config.DATA_DIR
STATE_PATH_DEFAULT = DATA_DIR / "translate_zh_to_hant_state.json"

CHAR_NAME_TOKEN = "⟦CHARACTER_NAME_DO_NOT_TRANSLATE⟧"
USER_TOKEN = "⟦USER_PLACEHOLDER_DO_NOT_TRANSLATE⟧"

_LOCK = threading.Lock()
_PRINT_LOCK = threading.Lock()

SKIP_KEYS = {
    "name",
    "voice",
    "visibility",
}
SKIP_KEY_SUFFIXES = {
    "_id",
    "_url",
    "_path",
    "url",
    "local_path",
}

URL_RE = re.compile(r"https?://[^\s\"'<>]+|/[A-Za-z0-9_./%-]+")
CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class TranslationError(RuntimeError):
    pass


def log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


def load_json(path: Path) -> dict | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    obj = load_json(path)
    if not obj:
        obj = {}
    obj.setdefault("done_personas", [])
    obj.setdefault("done_post_batches", [])
    obj.setdefault("failed", [])
    return obj


def save_state(path: Path, state: dict) -> None:
    with _LOCK:
        atomic_write_json(path, state)


def record_failure(state_path: Path, state: dict, item: str, error: Exception) -> None:
    with _LOCK:
        state.setdefault("failed", []).append({
            "item": item,
            "error": str(error)[:500],
            "ts": int(time.time()),
        })
        atomic_write_json(state_path, state)


def mark_done(state_path: Path, state: dict, bucket: str, key: str) -> None:
    with _LOCK:
        arr = state.setdefault(bucket, [])
        if key not in arr:
            arr.append(key)
        atomic_write_json(state_path, state)


def backup_file(path: Path, backup_root: Path) -> None:
    rel = path.relative_to(ROOT)
    dest = backup_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(path, dest)


def should_skip_key(key: str) -> bool:
    if key in SKIP_KEYS:
        return True
    return any(key.endswith(s) for s in SKIP_KEY_SUFFIXES)


def mask_text(text: str, char_name: str) -> str:
    out = text
    if char_name:
        out = out.replace(char_name, CHAR_NAME_TOKEN)
    out = out.replace("{user}", USER_TOKEN)
    return out


def unmask_text(text: str, char_name: str) -> str:
    out = text.replace(USER_TOKEN, "{user}")
    if char_name:
        out = out.replace(CHAR_NAME_TOKEN, char_name)
    return out


def prepare_payload(value: Any, char_name: str, key: str = "") -> Any:
    """Return a translation payload. Non-translatable fields stay unchanged.

    We still include skipped fields so JSON shape validation remains simple, but the prompt
    also instructs the model to leave IDs/enums/tokens unchanged.
    """
    if isinstance(value, dict):
        return {k: prepare_payload(v, char_name, k) for k, v in value.items()}
    if isinstance(value, list):
        return [prepare_payload(v, char_name, key) for v in value]
    if isinstance(value, str):
        if should_skip_key(key):
            return value
        if key == "type" and value in {"text", "voice", "image_text", "text_only", "selfie", "photo"}:
            return value
        return mask_text(value, char_name)
    return value


def restore_payload(value: Any, char_name: str) -> Any:
    if isinstance(value, dict):
        return {k: restore_payload(v, char_name) for k, v in value.items()}
    if isinstance(value, list):
        return [restore_payload(v, char_name) for v in value]
    if isinstance(value, str):
        return unmask_text(value, char_name)
    return value


def strip_json_text(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = CODE_FENCE_RE.sub("", s).strip()
    if not s.startswith("{"):
        first = s.find("{")
        last = s.rfind("}")
        if first >= 0 and last > first:
            s = s[first:last + 1]
    return s


def parse_json_response(text: str) -> dict:
    s = strip_json_text(text)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        raise TranslationError(f"model returned invalid JSON: {e}: {text[:400]}") from e
    if not isinstance(obj, dict):
        raise TranslationError("model returned non-object JSON")
    return obj


def same_shape(src: Any, dst: Any, path: str = "$", *, strict_list_len: bool = True) -> list[str]:
    errors: list[str] = []
    if isinstance(src, dict):
        if not isinstance(dst, dict):
            return [f"{path}: expected object"]
        if set(src.keys()) != set(dst.keys()):
            missing = sorted(set(src.keys()) - set(dst.keys()))
            extra = sorted(set(dst.keys()) - set(src.keys()))
            errors.append(f"{path}: key mismatch missing={missing} extra={extra}")
            return errors
        for k in src.keys():
            errors.extend(same_shape(src[k], dst[k], f"{path}.{k}", strict_list_len=strict_list_len))
        return errors
    if isinstance(src, list):
        if not isinstance(dst, list):
            return [f"{path}: expected list"]
        if strict_list_len and len(src) != len(dst):
            errors.append(f"{path}: list length mismatch {len(src)} != {len(dst)}")
            return errors
        for i, (a, b) in enumerate(zip(src, dst)):
            errors.extend(same_shape(a, b, f"{path}[{i}]", strict_list_len=strict_list_len))
        return errors
    # Primitives may remain same type. String -> string is required; other primitives unchanged by prompt.
    if isinstance(src, str) and not isinstance(dst, str):
        return [f"{path}: expected string"]
    if not isinstance(src, str) and type(src) is not type(dst):
        return [f"{path}: expected {type(src).__name__}"]
    return errors


def system_prompt(kind: str) -> str:
    return f"""
你是繁體中文（以臺灣讀者為主）的角色產品在地化編輯。你要把既有簡體中文角色資料改寫成自然、有趣、有社群感的繁體中文。

這不是機械簡繁轉換：
- 保留原本角色設定、關係張力、職業階層、世界觀和情緒功能，不要重寫成另一個角色。
- 文字要像臺灣繁中使用者真的會在 IG / Threads / Dcard / LINE 語境看到或說出口；避免中國大陸用語和翻譯腔。
- 現代日常可做自然在地化：例如「私信」→「私訊」、「帖子」→「貼文」、「朋友圈/動態」依語境改成「限動/動態/貼文」，「早高峰」→「通勤尖峰」。
- 若原文是古風、奇幻、韓劇、非人類或特定世界觀，不要硬塞臺灣地名/品牌/新臺幣；只把語氣改成繁中讀起來自然。
- 可以讓語氣更利落、更有梗、更像真人，但不要新增重大經歷、改年齡、改關係、改職業、改物種、改核心秘密。

硬性規則：
1. 只輸出合法 JSON，不能有 Markdown、解釋、前字尾文字。
2. JSON 結構必須和輸入完全一致：key 名稱、key 順序、陣列長度、物件層級都不能改。
3. 不翻譯、不改寫、不刪除任何保護 token：{CHAR_NAME_TOKEN}、{USER_TOKEN}、形如 ⟦URL_0_DO_NOT_TRANSLATE⟧ 的 token。
4. 不要改英文 ID、URL、路徑、voice model、public/private、text/voice 等列舉值。
5. 角色姓名已被保護；你不得猜測或另取名字。若文字裡出現 {CHAR_NAME_TOKEN}，原樣保留。
6. 佔位符 {USER_TOKEN} 必須原樣保留，不要改成「你」或任何名字。
7. 輸出必須是繁體中文；若有必要保留英文品牌/作品/MBTI/emoji，可以保留。

本次資料型別：{kind}
""".strip()


def translate_json(payload: dict, kind: str, model: str, temperature: float, retries: int) -> dict:
    messages = [
        {"role": "system", "content": system_prompt(kind)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            raw = api_client.chat(
                messages,
                model=model,
                temperature=temperature,
                max_retries=3,
                timeout=240,
                max_tokens=24000,
            )
            obj = parse_json_response(raw)
            errors = same_shape(payload, obj)
            if errors:
                raise TranslationError("; ".join(errors[:5]))
            return obj
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(min(4 * (attempt + 1), 20))
    raise TranslationError(f"translation failed after {retries} attempts: {last_err}")


def persona_targets(limit: int = 0) -> list[Path]:
    out: list[Path] = []
    for p in sorted(PERSONA_DIR.glob("*.json")):
        obj = load_json(p)
        if obj and obj.get("lang") == "zh" and isinstance(obj.get("persona"), dict):
            out.append(p)
            if limit and len(out) >= limit:
                break
    return out


def build_persona_lang_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in PERSONA_DIR.glob("*.json"):
        obj = load_json(p)
        if obj:
            cid = obj.get("char_id") or p.stem
            if isinstance(cid, str):
                out[cid] = obj.get("lang") or ""
    return out


def post_targets(persona_lang: dict[str, str], limit: int = 0) -> list[Path]:
    out: list[Path] = []
    if not POST_DIR.exists():
        return out
    for char_dir in sorted(p for p in POST_DIR.iterdir() if p.is_dir()):
        for p in sorted(char_dir.glob("*.json")):
            obj = load_json(p)
            if not obj:
                continue
            cid = obj.get("char_id") or char_dir.name
            lang = obj.get("lang") or persona_lang.get(str(cid), "")
            if lang == "zh" and isinstance(obj.get("posts"), list):
                out.append(p)
                if limit and len(out) >= limit:
                    return out
    return out


def save_persona_record(path: Path, record: dict, local_only: bool) -> None:
    if local_only:
        atomic_write_json(path, record)
        return
    storage.save_json("personas", record["char_id"], record, path)


def save_post_batch(path: Path, batch: dict, local_only: bool) -> None:
    if local_only:
        atomic_write_json(path, batch)
        return
    char_id = batch.get("char_id") or path.parent.name
    if path.name == "ig_latest.json":
        storage.save_json("ig_batches", str(char_id), batch, path)
    else:
        batch_id = batch.get("batch_id") or path.stem
        storage.save_json("post_batches", f"{char_id}__{batch_id}", batch, path)


def translate_persona_file(
    path: Path,
    *,
    model: str,
    temperature: float,
    retries: int,
    dry_run: bool,
    backup_root: Path | None,
    local_only: bool,
) -> dict:
    record = load_json(path)
    if not record or record.get("lang") != "zh" or not isinstance(record.get("persona"), dict):
        return {"path": str(path), "skipped": True}
    persona = record["persona"]
    original_name = persona.get("name")
    if not isinstance(original_name, str):
        raise TranslationError(f"persona.name is not string: {path}")

    editable = copy.deepcopy(persona)
    editable.pop("name", None)
    payload = prepare_payload(editable, original_name)
    translated = translate_json(payload, "persona_json_without_name", model, temperature, retries)
    translated = restore_payload(translated, original_name)
    errors = same_shape(editable, translated)
    if errors:
        raise TranslationError("restored shape mismatch: " + "; ".join(errors[:5]))

    new_persona = copy.deepcopy(translated)
    # Restore protected name exactly and keep it first if it originally existed first enough for readability.
    new_persona["name"] = original_name
    ordered: dict[str, Any] = {}
    for k in persona.keys():
        if k == "name":
            ordered[k] = original_name
        elif k in new_persona:
            ordered[k] = new_persona[k]
    for k, v in new_persona.items():
        if k not in ordered:
            ordered[k] = v

    new_record = copy.deepcopy(record)
    new_record["persona"] = ordered
    if new_record["persona"].get("name") != original_name:
        raise TranslationError(f"name changed for {path}")

    if not dry_run:
        if backup_root:
            backup_file(path, backup_root)
        save_persona_record(path, new_record, local_only)
    return {
        "path": str(path.relative_to(ROOT)),
        "name": original_name,
        "dry_run": dry_run,
        "sample_profile": str(new_record["persona"].get("profile", ""))[:120],
    }


def translate_post_file(
    path: Path,
    *,
    char_name: str,
    model: str,
    temperature: float,
    retries: int,
    dry_run: bool,
    backup_root: Path | None,
    local_only: bool,
) -> dict:
    batch = load_json(path)
    if not batch or not isinstance(batch.get("posts"), list):
        return {"path": str(path), "skipped": True}
    posts_payload = []
    index_by_id: dict[str, dict] = {}
    for post in batch.get("posts", []):
        if not isinstance(post, dict):
            continue
        pid = post.get("post_id")
        content = post.get("content")
        if not isinstance(pid, str) or not isinstance(content, str):
            continue
        index_by_id[pid] = post
        posts_payload.append({"post_id": pid, "content": mask_text(content, char_name)})
    if not posts_payload:
        return {"path": str(path.relative_to(ROOT)), "skipped": True, "reason": "no string content"}

    payload = {"posts": posts_payload}
    translated = translate_json(payload, "post_content_list", model, temperature, retries)
    errors = same_shape(payload, translated)
    if errors:
        raise TranslationError("post response shape mismatch: " + "; ".join(errors[:5]))
    translated_posts = translated.get("posts") or []
    translated_by_id = {p.get("post_id"): p.get("content") for p in translated_posts if isinstance(p, dict)}
    if set(translated_by_id.keys()) != set(index_by_id.keys()):
        raise TranslationError("post_id set changed")

    new_batch = copy.deepcopy(batch)
    for post in new_batch.get("posts", []):
        pid = post.get("post_id") if isinstance(post, dict) else None
        if pid in translated_by_id:
            post["content"] = unmask_text(str(translated_by_id[pid]), char_name)

    if not dry_run:
        if backup_root:
            backup_file(path, backup_root)
        save_post_batch(path, new_batch, local_only)
    sample = ""
    for p in new_batch.get("posts", []):
        if isinstance(p, dict) and isinstance(p.get("content"), str):
            sample = p["content"][:120]
            break
    return {
        "path": str(path.relative_to(ROOT)),
        "posts": len(posts_payload),
        "dry_run": dry_run,
        "sample_content": sample,
    }


def summarize_targets(personas: list[Path], posts: list[Path]) -> dict:
    post_count = 0
    for p in posts:
        obj = load_json(p) or {}
        post_count += sum(1 for it in obj.get("posts", []) if isinstance(it, dict) and isinstance(it.get("content"), str))
    return {"personas": len(personas), "post_batches": len(posts), "post_contents": post_count}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write changes; default is dry-run")
    ap.add_argument("--limit-personas", type=int, default=0)
    ap.add_argument("--limit-post-batches", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--model", default=config.LLM_MODEL)
    ap.add_argument("--temperature", type=float, default=0.35)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--state", default=str(STATE_PATH_DEFAULT))
    ap.add_argument("--force", action="store_true", help="ignore previous done state")
    ap.add_argument("--local-only", action="store_true", help="write local JSON only; do not sync storage hub")
    args = ap.parse_args()

    dry_run = not args.apply
    state_path = Path(args.state)
    state = {"done_personas": [], "done_post_batches": [], "failed": []} if args.force else load_state(state_path)

    persona_lang = build_persona_lang_map()
    personas = persona_targets(args.limit_personas)
    posts = post_targets(persona_lang, args.limit_post_batches)
    summary = summarize_targets(personas, posts)
    log(f"targets: {summary} | dry_run={dry_run} | model={args.model} | concurrency={args.concurrency}")

    if dry_run:
        backup_root = None
    else:
        backup_root = DATA_DIR / f"_backup_zh_hant_{time.strftime('%Y%m%d_%H%M%S')}"
        backup_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(backup_root / "manifest.json", {
            "created": int(time.time()),
            "summary": summary,
            "model": args.model,
            "local_only": args.local_only,
        })
        log(f"backup_root: {backup_root.relative_to(ROOT)}")

    done_personas = set() if args.force else set(state.get("done_personas", []))
    done_posts = set() if args.force else set(state.get("done_post_batches", []))

    todo_personas = [p for p in personas if p.stem not in done_personas]
    todo_posts = [p for p in posts if p.relative_to(ROOT).as_posix() not in done_posts]

    name_by_char: dict[str, str] = {}
    for p in personas:
        obj = load_json(p) or {}
        persona = obj.get("persona") or {}
        name = persona.get("name")
        cid = obj.get("char_id") or p.stem
        if isinstance(cid, str) and isinstance(name, str):
            name_by_char[cid] = name

    errors = 0
    results: list[dict] = []

    def run_persona(p: Path) -> tuple[str, dict | None, Exception | None]:
        try:
            res = translate_persona_file(
                p,
                model=args.model,
                temperature=args.temperature,
                retries=args.retries,
                dry_run=dry_run,
                backup_root=backup_root,
                local_only=args.local_only,
            )
            return (p.stem, res, None)
        except Exception as e:  # noqa: BLE001
            return (p.stem, None, e)

    def run_post(p: Path) -> tuple[str, dict | None, Exception | None]:
        rel = p.relative_to(ROOT).as_posix()
        batch = load_json(p) or {}
        cid = str(batch.get("char_id") or p.parent.name)
        char_name = name_by_char.get(cid, "")
        try:
            res = translate_post_file(
                p,
                char_name=char_name,
                model=args.model,
                temperature=args.temperature,
                retries=args.retries,
                dry_run=dry_run,
                backup_root=backup_root,
                local_only=args.local_only,
            )
            return (rel, res, None)
        except Exception as e:  # noqa: BLE001
            return (rel, None, e)

    log(f"personas to process: {len(todo_personas)}")
    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as ex:
        futs = [ex.submit(run_persona, p) for p in todo_personas]
        for i, fut in enumerate(as_completed(futs), 1):
            key, res, err = fut.result()
            if err:
                errors += 1
                record_failure(state_path, state, f"persona:{key}", err)
                log(f"  ✗ persona {key}: {err}")
                continue
            if not dry_run:
                mark_done(state_path, state, "done_personas", key)
            if res:
                results.append(res)
                log(f"  ✓ persona {i}/{len(todo_personas)} {key} :: {res.get('sample_profile', '')[:70]}")

    log(f"post batches to process: {len(todo_posts)}")
    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as ex:
        futs = [ex.submit(run_post, p) for p in todo_posts]
        for i, fut in enumerate(as_completed(futs), 1):
            key, res, err = fut.result()
            if err:
                errors += 1
                record_failure(state_path, state, f"post:{key}", err)
                log(f"  ✗ post {key}: {err}")
                continue
            if not dry_run:
                mark_done(state_path, state, "done_post_batches", key)
            if res:
                results.append(res)
                log(f"  ✓ post {i}/{len(todo_posts)} {key} :: {res.get('sample_content', '')[:70]}")

    out_path = DATA_DIR / f"translate_zh_to_hant_{'dryrun' if dry_run else 'apply'}_summary.json"
    atomic_write_json(out_path, {
        "dry_run": dry_run,
        "summary": summary,
        "processed_results_sample": results[:20],
        "errors": errors,
        "state": str(state_path),
    })
    log(f"summary written: {out_path.relative_to(ROOT)} | errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
