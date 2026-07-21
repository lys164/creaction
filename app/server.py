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

from . import (arca_storage, arca_sync, chat, config, daily, feed_posts,
               landing, phone_check, phone_check_gen, pipeline, prompts,
               phone_runtime, schedule_pipeline, styles, tasks, storage)

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


@app.exception_handler(arca_storage.StorageError)
def _storage_error_handler(request: Request, exc: arca_storage.StorageError):
    """遠端儲存同步失敗（如刪帖時 put 遠端失敗）：返回 503 讓前端提示可重試。
    這類失敗下本地可能已改、遠端未同步，客戶端應重試直至兩端一致。"""
    return JSONResponse(
        status_code=503,
        content={"detail": f"雲端同步失敗，請重試：{exc}"},
    )


# ---------- models ----------
class IdentityReq(BaseModel):
    char_id: str


class CoverReq(BaseModel):
    char_id: str
    style_id: str
    # None = auto：寫實畫風且有源圖時自動用 i2i 參考（見 pipeline.generate_cover）
    use_reference: bool | None = None
    mode: str = "fill_missing"
    # True = 以當前封面而非源圖為參考重跑封面鏈路（source=image 遠離原圖用）
    recook_from_cover: bool = False


class PostsReq(BaseModel):
    char_id: str
    post_type_ids: list[str]
    count_per_type: int = 2
    style_id: str | None = None
    with_images: bool = True
    # 鏈路：real=真實人設 / light=輕劇情 / flirt=輕劇情+荷爾蒙張力 / adult=成人向；
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
    # content 可為字串或多語言 dict，與生成時形態一致，故用寬鬆型別
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
    # 鏈路覆蓋：None=沿用角色已存 track（預設 real）
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


class FeedPostReq(BaseModel):
    char_id: str
    kind: str  # t1=平臺媒體號論壇體宣傳帖 / t2=角色綁定號帖子
    subtype: str = "auto"  # t2：witness/couple/…/auto；t1：T1_GENRES 鍵名，auto=服務端輪換指派
    user_name: str = ""
    hint: str = ""
    session_id: str | None = None  # 僅 t2：指定聊天會話作素材，預設取最近一次
    schedule_text: str = ""  # 僅 t2：貼入日程素材(手帳工坊 JSON/文字)；空=自動生成當日摘要
    image_model: str | None = None  # 生圖模型：image-2(gpt-image-2)/banana(nanobanana)


class DailyRunReq(BaseModel):
    char_id: str
    user_name: str = ""
    weather: str = ""
    season: str = ""
    city: str = ""
    hint: str = ""
    t2_subtype: str = "auto"       # 第三方帖子體裁（複用 T2 引擎）
    session_id: str | None = None  # 指定聊天會話作 echo/消息素材，預設取最近一次
    with_images: bool = True       # 限動 selfie + 帖子配圖走正式生圖鏈路
    with_t2_post: bool = True      # 是否伴生第三方帖子（落 feed 存檔，發現流可見）


class ScheduleMonthReq(BaseModel):
    char_id: str
    season: str = ""
    city: str = ""
    month_start_date: str = ""
    weather: str = ""
    dialogue: str = ""
    continue_month: bool = False


class ScheduleWeekReq(BaseModel):
    char_id: str
    week_no: int = Field(ge=1, le=4)


class ScheduleDaysReq(BaseModel):
    char_id: str
    week_no: int = Field(ge=1, le=4)
    days: list[str] = Field(min_length=1, max_length=7)


class ScheduleWorkspaceReq(BaseModel):
    workspace: dict


class PhoneEnterReq(BaseModel):
    char_id: str
    session_id: str = ""


class PhoneEventReq(BaseModel):
    char_id: str
    event: str
    detail: str = ""


class PhoneUnlockReq(BaseModel):
    char_id: str
    app_id: str


class FeedReplyReq(BaseModel):
    comment_index: int          # -1=發新主評論；否則為主評論索引
    text: str
    reply_to: str = ""          # 被回覆人暱稱（顯示「回覆 @xxx」用）
    user_name: str = ""


class FeedEventReq(BaseModel):
    char_id: str
    hint: str = ""
    user_name: str = ""
    session_id: str | None = None
    with_images: bool = True   # True=事件內每帖必配圖，同 T1/T2；False=純文
    schedule_text: str = ""    # 日程素材；空=自動生成當日日程摘要作事實底座
    image_model: str | None = None  # 生圖模型：image-2(gpt-image-2)/banana(nanobanana)


class ArcaSyncReq(BaseModel):
    char_ids: list[str]
    force: bool = False
    sync_landing: bool | None = None
    sync_posts: bool = False  # 預設只同步角色本體；帖子入口傳 True 才發帖


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
    style: str = Form("real"),
):
    """Upload images (optional) → one native character per selected language.

    - With images: each image (one_per_image) or the whole set is a character group.
    - Without images: requires user_hint; generates a text-only character group.
    - langs: comma-separated subset of zh,ja,ko,en.
    """
    lang_list = [s.strip() for s in langs.split(",") if s.strip()]
    try:
        style = pipeline.normalize_production_style(style)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    saved = []
    for f in files:
        if not (f.filename or getattr(f, "size", None)):
            continue  # skip empty multipart placeholder
        ext = Path(f.filename or "img.png").suffix or ".png"
        dest = config.UPLOAD_DIR / \
            f"{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}_{len(saved)}{ext}"
        data = f.file.read()
        storage.save_file(dest, data)  # 本地 + OSS 雙寫
        saved.append(str(dest))

    # 純文字模式：沒有圖片時必須有補充要求，否則無依據可生成。
    if not saved and not user_hint.strip():
        raise HTTPException(400, "請上傳圖片，或在『創作補充要求』裡填寫文字用於生成人設")

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
                        source=source, style=style
                    )
                )
            except Exception as e:  # noqa: BLE001 單組失敗不丟棄其它組已生成的角色
                group_errors.append(str(e))
            tasks.bump(tid)

        cover_errors = {}
        # nonhuman/flirt 鏈路允許不選畫風也自動生成封面（generate_cover 內部會不套畫風）
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
    style: str = Form("real"),
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
    try:
        style = pipeline.normalize_production_style(style)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    sources: list[dict] = []
    for f in files:
        try:
            payload = _loads_lenient(f.file.read().decode("utf-8"))
        except (UnicodeDecodeError, _json.JSONDecodeError) as e:
            raise HTTPException(
                400, f"{f.filename or 'file'} 不是合法 JSON：{e}") from e
        sources.extend(pipeline.extract_source_objects(payload))

    if not sources:
        raise HTTPException(400, "未從上傳的 JSON 中解析出任何角色物件")
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
                        source=source, style=style,
                    )
                )
            except Exception as e:  # noqa: BLE001 keep batch resilient
                results.append({"error": str(e)})
            tasks.bump(tid)

        ok = [r for r in results if r.get("char_id")]
        errors = {str(i): r["error"]
                  for i, r in enumerate(results) if r.get("error")}

        cover_errors = {}
        # nonhuman/flirt 鏈路允許不選畫風也自動生成封面（generate_cover 內部會不套畫風）
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
    with pipeline.char_lock(req.char_id):  # 與後臺任務的讀改寫互斥
        rec = pipeline.load_character(req.char_id)
        rec["persona"] = req.persona
        pipeline.save_character(rec)
    return rec


@app.post("/api/characters/regenerate_persona")
def regenerate_personas(req: RegenPersonaReq):
    """批次重新生成人設：長耗時 LLM 呼叫改後臺任務，返回 task_id 輪詢，
    避免同步等待撞反代讀超時(502 假報失敗而服務端仍在跑)。"""
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
    """單獨重寫一個角色的開場白（依據其它人設資訊），不動其它欄位。"""
    return pipeline.regenerate_opening(req.char_id, user_hint=req.user_hint)


@app.post("/api/characters/regenerate_opening")
def batch_regenerate_opening(req: BatchOpeningReq):
    """批次重寫開場白：長耗時 LLM 呼叫改後臺任務，返回 task_id 輪詢，
    避免同步等待撞反代讀超時(502 假報失敗而服務端仍在跑)。"""
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
    """批次刪除角色（後臺併發任務）。

    刪除會聯動遠端儲存/OSS 清理，單個角色可能耗時數秒；同步序列執行整批時極易
    撞反代讀超時(504)，前端誤報失敗而後臺仍在刪。改為後臺任務：立即返回 task_id，
    前端輪詢進度。單個失敗(如遠端刪除失敗)不中斷其餘，錯誤逐個返回可重試。"""
    tid = tasks.create_task("delete_characters", total=len(req.char_ids))

    def _job(tid: str):
        deleted, errors = [], {}

        def _one(cid: str):
            try:
                pipeline.delete_character(cid)
                return cid, None
            except Exception as e:  # noqa: BLE001 遠端刪除失敗需暴露並允許重試
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


_EXPORT_TTL = 6 * 3600  # 匯出 zip 落盤保留時長（秒），過期自動清理


def _gc_old_exports() -> None:
    """清理過期的匯出 zip，避免磁碟無限增長。"""
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
    """批次匯出角色為 zip（非同步）：立刻返回 task_id，後臺並行打包並落盤。

    大批次（幾百個）同步生成會超過反代讀超時導致 504，故改為後臺任務：
    完成後 result 帶下載 token，前端憑 token 走 /export/download 拉取 zip。
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
    """下載非同步匯出生成的 zip。token 來自匯出任務的 result。"""
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
    """批次為選中的角色生成封面（同一畫風），後臺併發執行，返回 task_id。"""
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
    """長耗時(LLM+批次生圖)改後臺任務：立即返回 task_id，前端輪詢 /api/tasks/{id}。
    同步等待會撞反代讀超時(504)，而生成仍在繼續、結果丟失。"""
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
    """編輯儲存一條普通帖子的文字（可選 variable/scene），不重新生成。"""
    return pipeline.update_post_content(
        char_id, batch_id, post_id,
        content=req.content, variable=req.variable, scene=req.scene,
    )


@app.delete("/api/posts/{char_id}/{batch_id}/{post_id}")
def delete_post(char_id: str, batch_id: str, post_id: str):
    return pipeline.delete_post_from_batch(char_id, batch_id, post_id)


@app.post("/api/ig_posts")
def make_ig_posts(req: IGPostsReq):
    """同上：後臺任務化，返回 task_id。"""
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
    """批次為選中的角色生成 INS 帖子，後臺併發執行，返回 task_id。"""
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
    """編輯儲存最新 IG 批次裡一條帖子的文字，不重新生成。"""
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
    """批次為選中的角色生成落地頁（同一風格，從零生成），後臺併發，返回 task_id。"""
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
    """儲存平臺裡手改的落地頁 HTML（覆蓋最新一份），不重新走 LLM。匯出即用這份。"""
    return pipeline.update_landing_html(req.char_id, req.html)


@app.get("/api/landing/{char_id}")
def get_latest_landing(char_id: str):
    return pipeline.load_latest_landing(char_id) or {}


# ---------- third-party feed posts (demo) ----------
@app.post("/api/feed_posts")
def make_feed_post(req: FeedPostReq):
    """生成一條第三方視角帖子（T1 論壇體宣傳 / T2 角色綁定帖）。
    單次 LLM 呼叫可能超過反代讀超時，走後臺任務 + 輪詢。"""
    if req.kind not in ("t1", "t2"):
        raise HTTPException(400, "kind must be t1 or t2")
    tid = tasks.create_task("feed_post", total=1)

    def _job(tid: str):
        post = feed_posts.generate_feed_post(
            req.char_id, req.kind, subtype=req.subtype,
            user_name=req.user_name, hint=req.hint,
            session_id=req.session_id,
            schedule_text=req.schedule_text,
            image_model=req.image_model,
        )
        tasks.bump(tid)
        return post

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/feed_events")
def make_feed_event(req: FeedEventReq):
    """生成一場熱搜事件（詞條+多方帖子+角色轉發私信），走後臺任務+輪詢。"""
    tid = tasks.create_task("feed_event", total=1)

    def _job(tid: str):
        try:
            event = feed_posts.generate_feed_event(
                req.char_id, hint=req.hint, user_name=req.user_name,
                session_id=req.session_id, with_images=req.with_images,
                schedule_text=req.schedule_text, image_model=req.image_model)
        except feed_posts.EventAbstain as e:
            tasks.bump(tid)
            return {"abstain": True, "reason": str(e)}
        tasks.bump(tid)
        return event

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.delete("/api/feed_events/{char_id}/{event_id}")
def delete_feed_event(char_id: str, event_id: str):
    return feed_posts.delete_feed_event(char_id, event_id)


@app.post("/api/feed_posts/{char_id}/{post_id}/reply")
def reply_feed_comment(char_id: str, post_id: str, req: FeedReplyReq):
    """使用者回覆評論 → NPC 續寫接話（LLM 一次小呼叫，走後臺任務+輪詢）。"""
    tid = tasks.create_task("feed_reply", total=1)

    def _job(tid: str):
        post = feed_posts.continue_comment_thread(
            char_id, post_id, req.comment_index, req.text,
            reply_to=req.reply_to, user_name=req.user_name)
        tasks.bump(tid)
        return post

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.get("/api/feed_characters")
def get_feed_characters():
    """Feed demo 的策展角色列表（每語種 5 個），不返回全量角色。"""
    return feed_posts.list_feed_characters()


# ---------- daily run（完整日產：日程+主動消息+生活動態+第三方帖子） ----------
@app.post("/api/daily_runs")
def make_daily_run(req: DailyRunReq):
    """一次生產角色完整的一天。LLM + 多張生圖較慢，走後臺任務 + 輪詢。"""
    tid = tasks.create_task("daily_run", total=1)

    def _job(tid: str):
        run = daily.generate_daily_run(
            req.char_id, user_name=req.user_name,
            weather=req.weather, season=req.season, city=req.city,
            hint=req.hint, t2_subtype=req.t2_subtype,
            session_id=req.session_id,
            with_images=req.with_images, with_t2_post=req.with_t2_post,
        )
        tasks.bump(tid)
        return run

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.get("/api/daily_runs/{char_id}")
def get_daily_runs(char_id: str):
    return daily.list_daily_runs(char_id)


@app.delete("/api/daily_runs/{char_id}/{run_id}")
def delete_daily_run(char_id: str, run_id: str):
    return daily.delete_daily_run(char_id, run_id)


# ---------- schedule pipeline (month → week → day → notebook) ----------
@app.get("/api/schedule_pipeline/{char_id}")
def get_schedule_pipeline(char_id: str):
    # Validate character early so a stale URL does not create an orphan plan.
    pipeline.load_character(char_id)
    return schedule_pipeline.load_workspace(char_id)


@app.put("/api/schedule_pipeline/{char_id}")
def save_schedule_pipeline(char_id: str, req: ScheduleWorkspaceReq):
    pipeline.load_character(char_id)
    workspace = dict(req.workspace or {})
    workspace["char_id"] = char_id
    return schedule_pipeline.save_workspace(workspace)


@app.post("/api/schedule_pipeline/month")
def make_schedule_month(req: ScheduleMonthReq):
    tid = tasks.create_task("schedule_month", total=1)

    def _job(tid: str):
        result = schedule_pipeline.generate_month(
            req.char_id,
            {"season": req.season, "city": req.city,
             "month_start_date": req.month_start_date, "weather": req.weather,
             "dialogue": req.dialogue},
            continue_month=req.continue_month,
        )
        tasks.bump(tid)
        return result

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/schedule_pipeline/week")
def make_schedule_week(req: ScheduleWeekReq):
    tid = tasks.create_task("schedule_week", total=1)

    def _job(tid: str):
        result = schedule_pipeline.generate_week(req.char_id, req.week_no)
        tasks.bump(tid)
        return result

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/schedule_pipeline/days")
def make_schedule_days(req: ScheduleDaysReq):
    tid = tasks.create_task("schedule_days", total=len(req.days))

    def _job(tid: str):
        result = schedule_pipeline.generate_days(req.char_id, req.week_no, req.days)
        tasks.bump(tid, len(req.days))
        return result

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.get("/api/feed_stream")
def get_feed_stream(limit: int = 60, offset: int = 0, kind: str | None = None):
    """發現流：聚合所有角色的第三方帖子，消費者視角混排展示。"""
    return feed_posts.list_feed_stream(limit=limit, offset=offset, kind=kind)


@app.get("/api/feed_posts/{char_id}")
def get_feed_posts(char_id: str):
    return feed_posts.list_feed_posts(char_id)


@app.get("/api/phone_check/enrich")
def get_phone_check_enrich():
    """查手機 Demo 的真實素材補給：真封面 + 最近聊天 + feed 帖子摘要。
    read-only，缺資料時對應欄位為空，前端保留自帶 mock。"""
    return phone_check.enrich()


@app.get("/api/phone_check/content")
def get_phone_check_content():
    """查手機 Demo 的 LLM 生成內容（17 功能 × 角色），讀快取。
    未生成時 chars 為空，前端保留自帶 mock。"""
    return phone_check_gen.load_all()


@app.get("/api/phone_check/runtime/{char_id}")
def get_phone_runtime(char_id: str):
    pipeline.load_character(char_id)
    return phone_runtime.runtime(char_id)


@app.post("/api/phone_check/enter")
def enter_phone(req: PhoneEnterReq):
    pipeline.load_character(req.char_id)
    return phone_runtime.enter(req.char_id, req.session_id)


@app.post("/api/phone_check/event")
def phone_event(req: PhoneEventReq):
    pipeline.load_character(req.char_id)
    return phone_runtime.record_event(req.char_id, req.event, req.detail)


@app.get("/api/phone_check/tiers")
def get_phone_check_tiers():
    """前端用：哪些 App 免費、哪些付費鎖。"""
    return phone_check_gen.tiers()


@app.post("/api/phone_check/generate")
def post_phone_check_generate(demo_id: str | None = None, layer: str = "all"):
    """生成整支手機內容（走後臺任務）。
    layer=all（免費+付費，demo 自動觸發用）/ free（只免費層）/ paid（只付費層）。
    demo_id 省略＝全部角色。單次 LLM 呼叫較久，回 task_id 供輪詢。"""
    tid = tasks.create_task("phone_check_gen", total=1)

    def _one(demo: str):
        real = phone_check_gen.DEMO_CHAR_MAP.get(demo)
        if not real:
            raise ValueError(f"unknown demo_id: {demo}")
        if layer == "free":
            return phone_check_gen.generate_free(demo, real)
        if layer == "paid":
            return phone_check_gen.generate_paid(demo, real, None)
        return phone_check_gen.generate_one(demo, real)

    def _job(tid: str):
        if demo_id:
            out = _one(demo_id)
        elif layer == "all":
            out = phone_check_gen.generate_all()
        else:
            out = {"results": [{"demo_id": d, "ok": True, "result": _one(d)}
                               for d in phone_check_gen.DEMO_CHAR_MAP]}
        tasks.bump(tid)
        return out

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/phone_check/unlock")
def post_phone_check_unlock(req: PhoneUnlockReq):
    """付費解鎖某個 App：若尚未生成，用中心秘密單次生成該 App 並落盤。
    app_id 可為前端 app（會映射到付費生成鍵，sns 小號用 sns_alt）。同步回傳生成內容。"""
    pipeline.load_character(req.char_id)
    demo_id = None
    for d, real in phone_check_gen.DEMO_CHAR_MAP.items():
        if real == req.char_id:
            demo_id = d
            break
    if not demo_id:
        return {"ok": False, "error": "unknown char"}
    gen_key = "sns_alt" if req.app_id in ("sns", "sns_alt") else req.app_id
    if gen_key not in phone_check_gen.PAID_APP_IDS:
        return {"ok": False, "error": f"not a paid app: {req.app_id}"}
    existing = phone_check_gen.load_one(demo_id) or {}
    if gen_key in (existing.get("paid_apps") or {}):
        return {"ok": True, "cached": True, "result": existing}
    result = phone_check_gen.generate_paid(demo_id, req.char_id, [gen_key])
    return {"ok": True, "cached": False, "result": result}


@app.post("/api/phone_check/apply_schedule/{char_id}")
def post_phone_check_apply_schedule(char_id: str):
    """把最新日程的 phone_update 併進免費層 dossier（chat/動線/推薦 + 紅點）。"""
    pipeline.load_character(char_id)
    return phone_runtime.apply_schedule_to_dossier(char_id)


@app.delete("/api/feed_posts/{char_id}/{post_id}")
def delete_feed_post(char_id: str, post_id: str):
    return feed_posts.delete_feed_post(char_id, post_id)


@app.post("/api/feed_posts/{char_id}/{post_id}/image")
def rerender_feed_post_image(char_id: str, post_id: str,
                             image_model: str | None = None):
    """按帖子已有的 image spec 重出配圖（生圖較慢，走後臺任務）。
    image_model：可選，image-2/banana；不傳則沿用該帖生成時的選擇。"""
    tid = tasks.create_task("feed_post_image", total=1)

    def _job(tid: str):
        post = feed_posts.rerender_feed_post_image(
            char_id, post_id, image_model=image_model)
        tasks.bump(tid)
        return post

    tasks.run(tid, _job)
    return {"task_id": tid}


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
            except Exception as e:  # noqa: BLE001 單角色失敗不中斷整批
                rows.append({"char_id": cid, "arca_character_id": None,
                             "posts": [], "errors": [str(e)], "skipped": False,
                             "landing_url": None})
            tasks.bump(tid)
        return rows

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/arca/storage/migrate")
def arca_storage_migrate():
    """把本地 data/ 存量全量遷移到 arca 通用儲存（JSON→儲存中臺，圖片→OSS）。冪等可重跑。"""
    tid = tasks.create_task("arca_storage_migrate")

    def _job(tid: str):
        return storage.migrate_all(progress=lambda n: tasks.bump(tid, n))

    tasks.run(tid, _job)
    return {"task_id": tid}


@app.post("/api/arca/delete")
def arca_delete_batch(req: CharIdsReq):
    """刪除 arca 上的已同步角色並清空本地對映（本地角色資料不動）。"""
    tid = tasks.create_task("arca_delete", total=len(req.char_ids))

    def _job(tid: str):
        rows = []
        for cid in req.char_ids:
            try:
                rows.append(arca_sync.remove_from_arca(cid))
            except Exception as e:  # noqa: BLE001 單角色失敗不中斷整批
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
# 內容定址快取：圖片一旦生成內容基本不變，重繪會換新檔名/mtime，
# 因此可安全地長快取並用 ETag 做協商，避免每次重新整理重下幾百 MB 原圖。
_THUMB_DIR = config.DATA_DIR / "thumbs"
_THUMB_DIR.mkdir(parents=True, exist_ok=True)
_THUMB_WIDTHS = (200, 400, 800)  # 允許的縮略寬度，防止任意尺寸打爆磁碟


def _file_etag(p: Path) -> str:
    st = p.stat()
    return f'"{int(st.st_mtime)}-{st.st_size}"'


def _cached_file_response(p: Path, request: Request, immutable: bool = True) -> Response:
    """帶 ETag 協商的檔案響應；命中則回 304。

    immutable=True 用於檔名唯一、內容永不變的資源（如帶時間戳的上傳原圖），
    可長快取 + immutable，瀏覽器不再回源校驗。
    immutable=False 用於會"原地重繪"的資源（封面/帖圖等檔名固定但內容會被覆蓋），
    必須用 no-cache 強制每次帶 ETag 回源協商：內容沒變回 304（幾乎零開銷），
    內容變了則返回新圖——否則瀏覽器會一直顯示快取舊圖，表現為"重新生成點了沒用"。
    """
    etag = _file_etag(p)
    cache = ("public, max-age=31536000, immutable" if immutable
             else "no-cache")
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache})
    return FileResponse(str(p), headers={"ETag": etag, "Cache-Control": cache})


def _thumb_path(src: Path, width: int) -> Path:
    key = f"{src.name}-{int(src.stat().st_mtime)}-w{width}"
    digest = hashlib.md5(key.encode()).hexdigest()
    return _THUMB_DIR / f"{digest}.webp"


def _make_thumb(src: Path, width: int) -> Path | None:
    """按需生成 webp 縮圖並落盤快取；失敗返回 None（回退原圖）。"""
    dst = _thumb_path(src, width)
    if dst.exists():
        return dst
    try:
        from PIL import Image

        with Image.open(src) as im:
            if im.width <= width:  # 原圖already比目標小，不放大
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
    # 生成類圖片（封面/帖圖等）檔名固定但會被"原地重繪"覆蓋，不能用 immutable，
    # 否則重繪後瀏覽器仍顯示快取舊圖。統一走 no-cache + ETag 協商。
    p = config.IMAGE_DIR / name
    if not storage.ensure_file(p):  # 本地缺失時從 arca OSS 回源
        raise HTTPException(404, "image not found")
    if w and w in _THUMB_WIDTHS:
        thumb = _make_thumb(p, w)
        if thumb is not None:
            return _cached_file_response(thumb, request, immutable=False)
    return _cached_file_response(p, request, immutable=False)


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
