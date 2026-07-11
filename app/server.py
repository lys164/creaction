"""FastAPI backend for the POPOP production pipeline."""
import hashlib
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import arca_sync, chat, config, landing, pipeline, prompts, styles, tasks, storage

app = FastAPI(title="POPOP Pipeline")


@app.exception_handler(ValueError)
def _value_error_handler(request: Request, exc: ValueError):
    """Map domain ValueErrors (e.g. stale/missing post ids) to 404 instead of
    a 500. These are expected client-side conditions, not server faults."""
    msg = str(exc)
    status = 404 if "not found" in msg.lower() else 400
    return JSONResponse(status_code=status, content={"detail": msg})


@app.exception_handler(FileNotFoundError)
def _file_not_found_handler(request: Request, exc: FileNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc) or "not found"})


# ---------- models ----------
class IdentityReq(BaseModel):
    char_id: str


class CoverReq(BaseModel):
    char_id: str
    style_id: str
    # None = auto：写实画风且有源图时自动用 i2i 参考（见 pipeline.generate_cover）
    use_reference: bool | None = None
    mode: str = "fill_missing"
    # True = 以当前封面而非源图为参考重跑封面链路（source=image 远离原图用）
    recook_from_cover: bool = False


class PostsReq(BaseModel):
    char_id: str
    post_type_ids: list[str]
    count_per_type: int = 2
    style_id: str | None = None
    with_images: bool = True
    # 链路：real=真实人设 / light=轻剧情 / flirt=轻剧情+荷尔蒙张力 / adult=成人向；
    # None=沿用角色已存 track
    track: str | None = None


class RerenderImageReq(BaseModel):
    style_id: str | None = None


class IGPostsReq(BaseModel):
    char_id: str
    n: int | None = None
    style_id: str | None = None
    with_images: bool = True
    track: str | None = None


class BatchIGPostsReq(BaseModel):
    char_ids: list[str]
    n: int | None = None
    style_id: str | None = None
    with_images: bool = True
    track: str | None = None


class LandingReq(BaseModel):
    char_id: str
    style_text: str | None = None
    request: str = ""
    current_html: str | None = None
    variant: str | None = None


class BatchLandingReq(BaseModel):
    char_ids: list[str]
    style_text: str | None = None
    request: str = ""
    variant: str | None = None


class PersonaUpdateReq(BaseModel):
    char_id: str
    persona: dict


class PostContentUpdateReq(BaseModel):
    # content 可为字符串或多语言 dict，与生成时形态一致，故用宽松类型
    content: Any = None
    variable: Any = None
    scene: Any = None


class IgPostContentUpdateReq(BaseModel):
    content: Any = None


class LandingHtmlUpdateReq(BaseModel):
    char_id: str
    html: str


class CharIdsReq(BaseModel):
    char_ids: list[str]


class RegenPersonaReq(BaseModel):
    char_ids: list[str]
    # 链路覆盖：None=沿用角色已存 track（默认 real）
    track: str | None = None


class RegenOpeningReq(BaseModel):
    char_id: str
    user_hint: str = ""


class BatchOpeningReq(BaseModel):
    char_ids: list[str]
    user_hint: str = ""


class BatchCoverReq(BaseModel):
    char_ids: list[str]
    style_id: str
    use_reference: bool | None = None
    mode: str = "fill_missing"
    recook_from_cover: bool = False


class ChatReq(BaseModel):
    char_id: str
    message: str
    session_id: str | None = None
    context: dict = Field(default_factory=dict)
    prompt_template: str | None = None
    mode: str = "normal"


class ArcaSyncReq(BaseModel):
    char_ids: list[str]
    force: bool = False
    sync_landing: bool | None = None
    sync_posts: bool = False  # 默认只同步角色本体；帖子入口传 True 才发帖


# ---------- meta ----------
@app.get("/api/languages")
def get_languages():
    return [
        {"id": l, "name": config.lang_name(l)} for l in config.LANGUAGES
    ]


@app.get("/api/post_types")
def get_post_types():
    return prompts.POST_TYPES


@app.get("/api/landing_styles")
def get_landing_styles():
    return landing.landing_styles()


@app.get("/api/landing_variants")
def get_landing_variants():
    return landing.landing_variants()


@app.get("/api/styles")
def get_styles():
    return styles.load_styles()


@app.get("/api/characters")
def get_characters():
    return pipeline.list_characters()


@app.get("/api/tasks/{task_id}")
def get_task_status(task_id: str):
    t = tasks.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@app.get("/api/character/{char_id}")
def get_character(char_id: str):
    try:
        return pipeline.load_character(char_id)
    except FileNotFoundError:
        raise HTTPException(404, "character not found")


# ---------- step 1: upload -> persona ----------
@app.post("/api/personas")
def create_personas(
    files: list[UploadFile] = File(default=[]),
    user_hint: str = Form(""),
    one_per_image: bool = Form(True),
    langs: str = Form("zh,ja,ko,en"),
    with_cover: bool = Form(False),
    cover_style_id: str = Form(""),
    track: str = Form("real"),
    source: str = Form(""),
):
    """Upload images (optional) → one native character per selected language.

    - With images: each image (one_per_image) or the whole set is a character group.
    - Without images: requires user_hint; generates a text-only character group.
    - langs: comma-separated subset of zh,ja,ko,en.
    """
    lang_list = [s.strip() for s in langs.split(",") if s.strip()]
    saved = []
    for f in files:
        if not (f.filename or getattr(f, "size", None)):
            continue  # skip empty multipart placeholder
        ext = Path(f.filename or "img.png").suffix or ".png"
        dest = config.UPLOAD_DIR / \
            f"{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}_{len(saved)}{ext}"
        data = f.file.read()
        storage.save_file(dest, data)  # 本地 + OSS 双写
        saved.append(str(dest))

    # 纯文字模式：没有图片时必须有补充要求，否则无依据可生成。
    if not saved and not user_hint.strip():
        raise HTTPException(400, "请上传图片，或在『创作补充要求』里填写文字用于生成人设")

    if not saved:
        groups = [[]]  # one text-only group
    else:
        groups = [[p] for p in saved] if one_per_image else [saved]
    task_id = tasks.create_task("personas", total=len(groups))

    def _job(tid: str):
        results = []
        group_errors = []
        for group in groups:
            try:
                results.extend(
                    pipeline.create_personas_from_images(
                        group, lang_list, user_hint=user_hint, track=track,
                        source=source
                    )
                )
            except Exception as e:  # noqa: BLE001 单组失败不丢弃其它组已生成的角色
                group_errors.append(str(e))
            tasks.bump(tid)

        cover_errors = {}
        # nonhuman/flirt 链路允许不选画风也自动生成封面（generate_cover 内部会不套画风）
        if with_cover and (cover_style_id or track in ("nonhuman", "flirt")):
            def _cover(rec: dict):
                cid = rec.get("char_id")
                try:
                    pipeline.generate_cover(
                        cid, cover_style_id, use_reference=None,
                        mode="fill_missing",
                    )
                    return cid, None
                except Exception as e:  # noqa: BLE001
                    return cid, str(e)

            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
                for cid, err in ex.map(_cover, results):
                    if err:
                        cover_errors[cid] = err

        return {
            "count": len(results),
            "characters": results,
            "cover_errors": cover_errors,
            "group_errors": group_errors,
        }

    tasks.run(task_id, _job)
    return {"task_id": task_id}


@app.post("/api/personas/import_json")
def import_personas_from_json(
    files: list[UploadFile] = File(...),
    user_hint: str = Form(""),
    langs: str = Form("zh,ja,ko,en"),
    download_image: bool = Form(True),
    with_cover: bool = Form(False),
    cover_style_id: str = Form(""),
    limit: int = Form(0),
    track: str = Form("real"),
    source: str = Form(""),
):
    """Import existing character JSON files. Each source object becomes one
    character group; one native record is created per selected language.

    Accepts a single object, a top-level array, or an object wrapping the list
    under data/characters/items/list/results.
    """
    import json as _json
    import re as _re

    def _loads_lenient(text: str):
        """Parse JSON tolerant of // and /* */ comments and trailing commas,
        which appear in hand-edited / crawled exports."""
        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            pass
        # strip /* */ block comments
        text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.DOTALL)
        # strip // line comments (not inside strings)
        text = _re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*',
                       lambda m: m.group(1) or "", text)
        # remove trailing commas before } or ]
        text = _re.sub(r",(\s*[}\]])", r"\1", text)
        return _json.loads(text)

    lang_list = [s.strip() for s in langs.split(",") if s.strip()]
    sources: list[dict] = []
    for f in files:
        try:
            payload = _loads_lenient(f.file.read().decode("utf-8"))
        except (UnicodeDecodeError, _json.JSONDecodeError) as e:
            raise HTTPException(
                400, f"{f.filename or 'file'} 不是合法 JSON：{e}") from e
        sources.extend(pipeline.extract_source_objects(payload))

    if not sources:
        raise HTTPException(400, "未从上传的 JSON 中解析出任何角色对象")
    if limit and limit > 0:
        sources = sources[:limit]

    task_id = tasks.create_task("import_json", total=len(sources))

    def _job(tid: str):
        results = []
        for obj in sources:
            try:
                results.extend(
                    pipeline.create_personas_from_json_obj(
                        obj, lang_list, user_hint=user_hint,
                        download_image=download_image, track=track,
                        source=source,
                    )
                )
            except Exception as e:  # noqa: BLE001 keep batch resilient
                results.append({"error": str(e)})
            tasks.bump(tid)

        ok = [r for r in results if r.get("char_id")]
        errors = {str(i): r["error"]
                  for i, r in enumerate(results) if r.get("error")}

        cover_errors = {}
        # nonhuman/flirt 链路允许不选画风也自动生成封面（generate_cover 内部会不套画风）
        if with_cover and (cover_style_id or track in ("nonhuman", "flirt")):
            def _cover(rec: dict):
                cid = rec.get("char_id")
                try:
                    pipeline.generate_cover(
                        cid, cover_style_id, use_reference=None,
                        mode="fill_missing",
                    )
                    return cid, None
                except Exception as e:  # noqa: BLE001
                    return cid, str(e)

            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
                for cid, err in ex.map(_cover, ok):
                    if err:
                        cover_errors[cid] = err

        return {
            "count": len(ok),
            "characters": ok,
            "errors": errors,
            "cover_errors": cover_errors,
        }

    tasks.run(task_id, _job)
    return {"task_id": task_id}


@app.put("/api/persona")
def update_persona(req: PersonaUpdateReq):
    with pipeline.char_lock(req.char_id):  # 与后台任务的读改写互斥
        rec = pipeline.load_character(req.char_id)
        rec["persona"] = req.persona
        pipeline.save_character(rec)
    return rec


@app.post("/api/characters/regenerate_persona")
def regenerate_personas(req: RegenPersonaReq):
    """批量重新生成人设：长耗时 LLM 调用改后台任务，返回 task_id 轮询，
    避免同步等待撞反代读超时(502 假报失败而服务端仍在跑)。"""
    tid = tasks.create_task("regen_personas", total=len(req.char_ids))

    def _job(tid: str):
        done, errors = [], {}

        def _one(cid: str):
            try:
                pipeline.regenerate_persona(cid, track=req.track)
                return cid, None
            except Exception as e:  # noqa: BLE001
                return cid, str(e)

        if req.char_ids:
            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
                for cid, err in ex.map(_one, req.char_ids):
                    if err is None:
                        done.append(cid)
                    else:
                        errors[cid] = err
                    tasks.bump(tid)
        return {"regenerated": done, "errors": errors}

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/opening")
def regenerate_opening(req: RegenOpeningReq):
    """单独重写一个角色的开场白（依据其它人设信息），不动其它字段。"""
    return pipeline.regenerate_opening(req.char_id, user_hint=req.user_hint)


@app.post("/api/characters/regenerate_opening")
def batch_regenerate_opening(req: BatchOpeningReq):
    """批量重写开场白：长耗时 LLM 调用改后台任务，返回 task_id 轮询，
    避免同步等待撞反代读超时(502 假报失败而服务端仍在跑)。"""
    tid = tasks.create_task("regen_opening", total=len(req.char_ids))

    def _job(tid: str):
        done, errors = [], {}

        def _one(cid: str):
            try:
                pipeline.regenerate_opening(cid, user_hint=req.user_hint)
                return cid, None
            except Exception as e:  # noqa: BLE001
                return cid, str(e)

        if req.char_ids:
            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
                for cid, err in ex.map(_one, req.char_ids):
                    if err is None:
                        done.append(cid)
                    else:
                        errors[cid] = err
                    tasks.bump(tid)
        return {"regenerated": done, "errors": errors}

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/characters/delete")
def delete_characters(req: CharIdsReq):
    """批量删除角色（后台并发任务）。

    删除会联动远端存储/OSS 清理，单个角色可能耗时数秒；同步串行执行整批时极易
    撞反代读超时(504)，前端误报失败而后台仍在删。改为后台任务：立即返回 task_id，
    前端轮询进度。单个失败(如远端删除失败)不中断其余，错误逐个返回可重试。"""
    tid = tasks.create_task("delete_characters", total=len(req.char_ids))

    def _job(tid: str):
        deleted, errors = [], {}

        def _one(cid: str):
            try:
                pipeline.delete_character(cid)
                return cid, None
            except Exception as e:  # noqa: BLE001 远端删除失败需暴露并允许重试
                return cid, str(e)

        if req.char_ids:
            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
                for cid, err in ex.map(_one, req.char_ids):
                    if err is None:
                        deleted.append(cid)
                    else:
                        errors[cid] = err
                    tasks.bump(tid)
        return {"deleted": deleted, "errors": errors}

    tasks.run(tid, _job)
    return {"task_id": tid}


_EXPORT_TTL = 6 * 3600  # 导出 zip 落盘保留时长（秒），过期自动清理


def _gc_old_exports() -> None:
    """清理过期的导出 zip，避免磁盘无限增长。"""
    try:
        cutoff = time.time() - _EXPORT_TTL
        for p in config.EXPORT_DIR.glob("*.zip"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


@app.post("/api/characters/export")
def export_characters(req: CharIdsReq):
    """批量导出角色为 zip（异步）：立刻返回 task_id，后台并行打包并落盘。

    大批量（几百个）同步生成会超过反代读超时导致 504，故改为后台任务：
    完成后 result 带下载 token，前端凭 token 走 /export/download 拉取 zip。
    """
    if not req.char_ids:
        raise HTTPException(400, "no characters selected")

    char_ids = list(req.char_ids)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex[:16]
    filename = f"characters_export_{stamp}.zip"
    dst = config.EXPORT_DIR / f"{token}.zip"
    _gc_old_exports()

    task_id = tasks.create_task("export", total=len(char_ids))

    def _job(tid: str):
        count = pipeline.export_characters_zip_to_file(
            char_ids, dst, on_done=lambda _cid: tasks.bump(tid))
        return {"token": token, "filename": filename, "count": count,
                "download_url": f"/api/characters/export/download/{token}"}

    tasks.run(task_id, _job)
    return {"task_id": task_id}


@app.get("/api/characters/export/download/{token}")
def download_export(token: str):
    """下载异步导出生成的 zip。token 来自导出任务的 result。"""
    if not re.fullmatch(r"[0-9a-f]{16}", token):
        raise HTTPException(400, "bad token")
    path = config.EXPORT_DIR / f"{token}.zip"
    if not path.is_file():
        raise HTTPException(404, "export not found or expired")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        str(path),
        media_type="application/zip",
        filename=f"characters_export_{stamp}.zip",
    )


# ---------- step 2: identity ----------
@app.post("/api/identity")
def make_identity(req: IdentityReq):
    return pipeline.build_identity(req.char_id)


# ---------- step 3: cover ----------
@app.post("/api/cover")
def make_cover(req: CoverReq):
    """Generate one cover in the background.

    A single cover render can take longer than the reverse-proxy timeout, so the
    endpoint returns a task_id and the client polls /api/tasks/{id}.
    """
    task_id = tasks.create_task("cover", total=1)

    def _job(tid: str):
        page = pipeline.generate_cover(
            req.char_id, req.style_id, req.use_reference, req.mode,
            recook_from_cover=req.recook_from_cover)
        tasks.bump(tid)
        return page

    tasks.run(task_id, _job)
    return {"task_id": task_id}


@app.post("/api/characters/batch_cover")
def make_batch_cover(req: BatchCoverReq):
    """批量为选中的角色生成封面（同一画风），后台并发执行，返回 task_id。"""
    task_id = tasks.create_task("batch_cover", total=len(req.char_ids))

    def _job(tid: str):
        done, errors = [], {}

        def _one(cid: str):
            try:
                pipeline.generate_cover(
                    cid, req.style_id, req.use_reference, req.mode,
                    recook_from_cover=req.recook_from_cover)
                return cid, None
            except Exception as e:  # noqa: BLE001
                return cid, str(e)

        if req.char_ids:
            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
                for cid, err in ex.map(_one, req.char_ids):
                    if err is None:
                        done.append(cid)
                    else:
                        errors[cid] = err
                    tasks.bump(tid)
        return {"covered": done, "errors": errors}

    tasks.run(task_id, _job)
    return {"task_id": task_id}


# ---------- step 4: posts ----------
@app.post("/api/posts")
def make_posts(req: PostsReq):
    """长耗时(LLM+批量生图)改后台任务：立即返回 task_id，前端轮询 /api/tasks/{id}。
    同步等待会撞反代读超时(504)，而生成仍在继续、结果丢失。"""
    tid = tasks.create_task("posts")

    def _job(tid: str):
        return pipeline.generate_posts(
            req.char_id,
            req.post_type_ids,
            count_per_type=req.count_per_type,
            style_id=req.style_id,
            with_images=req.with_images,
            track=req.track,
        )

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.get("/api/posts/{char_id}")
def get_batches(char_id: str):
    return pipeline.list_batches(char_id)


@app.post("/api/posts/{char_id}/{batch_id}/{post_id}/image")
def rerender_post_image(char_id: str, batch_id: str, post_id: str,
                        req: RerenderImageReq):
    return pipeline.rerender_post_image(
        char_id, batch_id, post_id, style_id=req.style_id
    )


@app.put("/api/posts/{char_id}/{batch_id}/{post_id}")
def update_post(char_id: str, batch_id: str, post_id: str,
                req: PostContentUpdateReq):
    """编辑保存一条普通帖子的文本（可选 variable/scene），不重新生成。"""
    return pipeline.update_post_content(
        char_id, batch_id, post_id,
        content=req.content, variable=req.variable, scene=req.scene,
    )


@app.delete("/api/posts/{char_id}/{batch_id}/{post_id}")
def delete_post(char_id: str, batch_id: str, post_id: str):
    return pipeline.delete_post_from_batch(char_id, batch_id, post_id)


@app.post("/api/ig_posts")
def make_ig_posts(req: IGPostsReq):
    """同上：后台任务化，返回 task_id。"""
    tid = tasks.create_task("ig_posts")

    def _job(tid: str):
        return pipeline.generate_instagram_posts(
            req.char_id, n=req.n, style_id=req.style_id,
            with_images=req.with_images, track=req.track
        )

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/ig_posts/batch")
def make_batch_ig_posts(req: BatchIGPostsReq):
    """批量为选中的角色生成 INS 帖子，后台并发执行，返回 task_id。"""
    task_id = tasks.create_task("batch_ig_posts", total=len(req.char_ids))

    def _job(tid: str):
        done, errors = [], {}

        def _one(cid: str):
            try:
                batch = pipeline.generate_instagram_posts(
                    cid, n=req.n, style_id=req.style_id,
                    with_images=req.with_images, track=req.track
                )
                return cid, batch, None
            except Exception as e:  # noqa: BLE001
                return cid, None, str(e)

        if req.char_ids:
            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
                for cid, batch, err in ex.map(_one, req.char_ids):
                    if err is None:
                        done.append({"char_id": cid,
                                     "batch_id": batch.get("batch_id")})
                    else:
                        errors[cid] = err
                    tasks.bump(tid)
        return {"generated": done, "errors": errors}

    tasks.run(task_id, _job)
    return {"task_id": task_id}


@app.get("/api/ig_posts/{char_id}/latest")
def get_latest_ig(char_id: str):
    return pipeline.load_latest_ig(char_id) or {}


@app.post("/api/ig_posts/{char_id}/{post_id}/image")
def rerender_ig_post_image(char_id: str, post_id: str, req: RerenderImageReq):
    return pipeline.rerender_ig_post_image(char_id, post_id, style_id=req.style_id)


@app.put("/api/ig_posts/{char_id}/{post_id}")
def update_ig_post(char_id: str, post_id: str, req: IgPostContentUpdateReq):
    """编辑保存最新 IG 批次里一条帖子的文本，不重新生成。"""
    return pipeline.update_ig_post_content(char_id, post_id, content=req.content)


@app.delete("/api/ig_posts/{char_id}/{post_id}")
def delete_ig_post(char_id: str, post_id: str):
    return pipeline.delete_ig_post(char_id, post_id)


@app.get("/api/posts/{char_id}/{batch_id}")
def get_batch(char_id: str, batch_id: str):
    return pipeline.load_batch(char_id, batch_id)


# ---------- step 5: landing page ----------
@app.post("/api/landing")
def make_landing(req: LandingReq):
    """Generate/iterate a single landing page in the background.

    The LLM call can take well over a minute (a full HTML page at up to 32k
    tokens, with provider failover/retries), which exceeds the reverse-proxy
    read timeout and surfaces as a 502. Returning a task_id immediately keeps
    the request short; the client polls /api/tasks/{id} for the result."""
    task_id = tasks.create_task("landing", total=1)

    def _job(tid: str):
        page = pipeline.generate_landing(
            req.char_id,
            style_text=req.style_text,
            request=req.request,
            current_html=req.current_html,
            variant=req.variant,
        )
        tasks.bump(tid)
        return page

    tasks.run(task_id, _job)
    return {"task_id": task_id}


@app.post("/api/landing/batch")
def make_batch_landing(req: BatchLandingReq):
    """批量为选中的角色生成落地页（同一风格，从零生成），后台并发，返回 task_id。"""
    task_id = tasks.create_task("batch_landing", total=len(req.char_ids))

    def _job(tid: str):
        done, errors = [], {}

        def _one(cid: str):
            try:
                pipeline.generate_landing(
                    cid, style_text=req.style_text, request=req.request,
                    current_html=None, variant=req.variant,
                )
                return cid, None
            except Exception as e:  # noqa: BLE001
                return cid, str(e)

        if req.char_ids:
            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
                for cid, err in ex.map(_one, req.char_ids):
                    if err is None:
                        done.append(cid)
                    else:
                        errors[cid] = err
                    tasks.bump(tid)
        return {"generated": done, "errors": errors}

    tasks.run(task_id, _job)
    return {"task_id": task_id}


@app.put("/api/landing")
def update_landing(req: LandingHtmlUpdateReq):
    """保存平台里手改的落地页 HTML（覆盖最新一份），不重新走 LLM。导出即用这份。"""
    return pipeline.update_landing_html(req.char_id, req.html)


@app.get("/api/landing/{char_id}")
def get_latest_landing(char_id: str):
    return pipeline.load_latest_landing(char_id) or {}


# ---------- character chat ----------
@app.get("/api/chat/{char_id}/latest")
def get_latest_chat(char_id: str, mode: str = "normal"):
    return chat.latest(char_id, mode=mode)


@app.get("/api/chat/{char_id}/sessions")
def list_chat_sessions(char_id: str, mode: str | None = None):
    return chat.list_sessions(char_id, mode=mode)


@app.get("/api/chat/{char_id}/session/{session_id}")
def get_chat_session(char_id: str, session_id: str):
    return chat.get_session(char_id, session_id)


@app.post("/api/chat")
def send_chat(req: ChatReq):
    return chat.send_message(
        req.char_id,
        req.message,
        context=req.context,
        session_id=req.session_id,
        prompt_template=req.prompt_template,
        mode=req.mode,
    )


# ---------- arca sync ----------
@app.post("/api/arca/sync")
def arca_sync_batch(req: ArcaSyncReq):
    tid = tasks.create_task("arca_sync", total=len(req.char_ids))

    def _job(tid: str):
        rows = []
        for cid in req.char_ids:
            try:
                rows.append(arca_sync.sync_character(
                    cid, force=req.force, sync_landing=req.sync_landing,
                    sync_posts=req.sync_posts))
            except Exception as e:  # noqa: BLE001 单角色失败不中断整批
                rows.append({"char_id": cid, "arca_character_id": None,
                             "posts": [], "errors": [str(e)], "skipped": False,
                             "landing_url": None})
            tasks.bump(tid)
        return rows

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/arca/storage/migrate")
def arca_storage_migrate():
    """把本地 data/ 存量全量迁移到 arca 通用存储（JSON→存储中台，图片→OSS）。幂等可重跑。"""
    tid = tasks.create_task("arca_storage_migrate")

    def _job(tid: str):
        return storage.migrate_all(progress=lambda n: tasks.bump(tid, n))

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/arca/delete")
def arca_delete_batch(req: CharIdsReq):
    """删除 arca 上的已同步角色并清空本地映射（本地角色数据不动）。"""
    tid = tasks.create_task("arca_delete", total=len(req.char_ids))

    def _job(tid: str):
        rows = []
        for cid in req.char_ids:
            try:
                rows.append(arca_sync.remove_from_arca(cid))
            except Exception as e:  # noqa: BLE001 单角色失败不中断整批
                rows.append({"char_id": cid, "arca_character_id": None,
                             "deleted": False, "skipped": False, "errors": [str(e)]})
            tasks.bump(tid)
        return rows

    tasks.run(tid, _job)
    return {"task_id": tid}


# ---------- styles management ----------
@app.put("/api/styles")
def replace_styles(new_styles: list[dict]):
    styles.save_styles(new_styles)
    return {"count": len(new_styles), "styles": new_styles}


# ---------- serve generated images ----------
# 内容寻址缓存：图片一旦生成内容基本不变，重绘会换新文件名/mtime，
# 因此可安全地长缓存并用 ETag 做协商，避免每次刷新重下几百 MB 原图。
_THUMB_DIR = config.DATA_DIR / "thumbs"
_THUMB_DIR.mkdir(parents=True, exist_ok=True)
_THUMB_WIDTHS = (200, 400, 800)  # 允许的缩略宽度，防止任意尺寸打爆磁盘


def _file_etag(p: Path) -> str:
    st = p.stat()
    return f'"{int(st.st_mtime)}-{st.st_size}"'


def _cached_file_response(p: Path, request: Request, immutable: bool = True) -> Response:
    """带 ETag 协商 + 长缓存的文件响应；命中则回 304。"""
    etag = _file_etag(p)
    cache = "public, max-age=31536000" + (", immutable" if immutable else "")
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache})
    return FileResponse(str(p), headers={"ETag": etag, "Cache-Control": cache})


def _thumb_path(src: Path, width: int) -> Path:
    key = f"{src.name}-{int(src.stat().st_mtime)}-w{width}"
    digest = hashlib.md5(key.encode()).hexdigest()
    return _THUMB_DIR / f"{digest}.webp"


def _make_thumb(src: Path, width: int) -> Path | None:
    """按需生成 webp 缩略图并落盘缓存；失败返回 None（回退原图）。"""
    dst = _thumb_path(src, width)
    if dst.exists():
        return dst
    try:
        from PIL import Image

        with Image.open(src) as im:
            if im.width <= width:  # 原图already比目标小，不放大
                return None
            im = im.convert("RGB")
            h = round(im.height * width / im.width)
            im = im.resize((width, h), Image.LANCZOS)
            im.save(dst, "WEBP", quality=82, method=4)
        return dst
    except Exception:
        return None


@app.get("/img/{name}")
def serve_image(name: str, request: Request, w: int | None = None):
    p = config.IMAGE_DIR / name
    if not storage.ensure_file(p):  # 本地缺失时从 arca OSS 回源
        raise HTTPException(404, "image not found")
    if w and w in _THUMB_WIDTHS:
        thumb = _make_thumb(p, w)
        if thumb is not None:
            return _cached_file_response(thumb, request)
    return _cached_file_response(p, request)


@app.get("/upload/{name}")
def serve_upload(name: str, request: Request, w: int | None = None):
    p = config.UPLOAD_DIR / name
    if not storage.ensure_file(p):
        raise HTTPException(404, "not found")
    if w and w in _THUMB_WIDTHS:
        thumb = _make_thumb(p, w)
        if thumb is not None:
            return _cached_file_response(thumb, request)
    return _cached_file_response(p, request)


# ---------- static web (no-cache so UI updates show immediately) ----------
class _NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


app.mount("/", _NoCacheStatic(directory=str(config.WEB_DIR), html=True), name="web")
