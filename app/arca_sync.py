"""編排單個角色的 arca 同步：封面→(落地頁)→建角色→發帖，並回寫本地 id 實現冪等。"""
import hashlib
import json
from pathlib import Path

from . import arca_client, arca_mapping, config, pipeline


def _latest_posts(char_id: str) -> list[dict]:
    """取該角色最近一批 INS 帖子（有 ig 批次則用，無則返回空、只同步人設）。"""
    ig = pipeline.load_latest_ig(char_id)
    if ig and ig.get("posts"):
        return ig["posts"]
    return []


def _content_for_lang(content, lang: str) -> str:
    if isinstance(content, dict):
        return content.get(lang) or next((v for v in content.values() if v), "")
    return content or ""


def _post_visibility(record: dict) -> int:
    """帖子可見性（/post/create 只接受 1公開/2好友/3私密，不再接受 0）。

    顯式配置 ARCA_POST_VISIBILITY=1/2/3 時透傳；否則（0=跟隨角色）按角色
    visibility 派生：public→1、private→3，預設按公開。
    """
    cfg = config.ARCA_POST_VISIBILITY
    if cfg in (1, 2, 3):
        return cfg
    vis = ((record.get("persona") or {}).get("visibility") or "").strip().lower()
    return 3 if vis == "private" else 1


def _upload_cover(record: dict, lang: str) -> list[dict]:
    """上傳封面幷包成 arca 的 UserUploadImage（裸 StorageObject 會被 go-zero 解析拒 400）。

    arca 建角色硬校驗必須有一張 is_main_pic=true 的主圖（「請設定角色主圖」）。
    """
    cover = record.get("cover") or {}
    lp = cover.get("local_path")
    if lp and Path(lp).exists():
        data = Path(lp).read_bytes()
        key = f"creaction/{record['char_id']}/cover.png"
        obj = arca_client.tos_upload(data, key, "image/png", lang)
        return [{"image_type": "aigc", "is_main_pic": True, "media": obj}]
    return []


def _upload_landing(record: dict, lang: str) -> str | None:
    char_id = record["char_id"]
    page = pipeline.load_latest_landing(char_id)
    html = (page or {}).get("html")
    if not html:
        html = (page or {}).get("html_filled")
    if not html:
        return None
    # 落地頁 img 指向【公網 TOS URL】，不內聯 base64：封面/帖圖先傳公有桶拿直鏈。
    cover_bytes = pipeline._image_bytes(record.get("cover"))
    html = pipeline._landing_html_with_public_urls(
        html, char_id, lang,
        (page or {}).get("cover_url"), cover_bytes,
        (page or {}).get("post_urls"))
    key = f"creaction/{char_id}/landing.html"
    obj = arca_client.tos_upload(html.encode("utf-8"), key, "text/html", lang, public=True)
    return obj.get("url")


def _form_digest(form: dict) -> str:
    return hashlib.md5(
        json.dumps(form, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:8]


def sync_character(char_id: str, *, force: bool = False,
                   sync_landing: bool | None = None,
                   sync_posts: bool = False, progress=None) -> dict:
    """同步一個角色到 arca。

    - 未同步過 → 建角色（封面/落地頁一併上傳）。
    - 已同步過 → 原地更新（/character/updateBasicInfo，僅 name/gender/species/
      profile/voice_id/opening_prologue/visibility 會生效）；表單指紋沒變則跳過。
    - force=True → 忽略已同步狀態強制重建：arca 上會【新建一個角色】，舊角色仍保留。
    預設只同步角色本體；sync_posts=True 才把最近一批 INS 帖子發到 arca。
    """
    # 角色級互斥：同一角色的併發同步/刪除會導致帖子重複釋出與 record 覆蓋丟寫
    with pipeline.char_lock(char_id):
        return _sync_character_locked(char_id, force=force,
                                      sync_landing=sync_landing,
                                      sync_posts=sync_posts, progress=progress)


def _sync_character_locked(char_id: str, *, force: bool,
                           sync_landing: bool | None,
                           sync_posts: bool, progress=None) -> dict:
    record = pipeline.load_character(char_id)
    lang = record.get("lang", config.LANGUAGES[0])
    result = {"char_id": char_id, "arca_character_id": record.get("arca_character_id"),
              "posts": [], "landing_url": None, "skipped": False,
              "updated": False, "errors": []}

    do_landing = config.ARCA_SYNC_LANDING if sync_landing is None else sync_landing

    # 平臺列舉（tags/species/setting_options 對齊用）；拉取失敗降級 None 不阻斷同步。
    # tags/species 的反查表合併全部語言詞表——繁體/日文漢字等跨語言同形詞也能命中
    # tag_key（如「非人類」是 zh 的繁體 tag_name）；setting_options 用角色語言（key 本地化）。
    try:
        page_config = dict(arca_client.get_page_config_cached(lang) or {})
        merged_tags, merged_species = [], []
        for other in config.LANGUAGES:
            try:
                c = arca_client.get_page_config_cached(other)
            except Exception:  # noqa: BLE001 單語言失敗跳過
                continue
            merged_tags.extend(c.get("character_tags") or [])
            merged_species.extend(c.get("species") or [])
        if merged_tags:
            page_config["character_tags"] = merged_tags
        if merged_species:
            page_config["species"] = merged_species
    except Exception as e:  # noqa: BLE001
        page_config = None
        result["errors"].append(f"page_config 拉取失敗(跳過列舉對齊): {e}")

    # 0) 按名匹配（權威判斷）：不以本地 character_id 為準——只要 arca 上存在
    #    同名角色且建立者是同一 uid（list_my_characters 即本人自建列表），就在
    #    該角色上原地更新、帖子也掛它；遠端無同名則視本地對映過期、走新建。
    #    查詢失敗時 fail-open 回退本地對映（保持可用性）。
    if not force:
        try:
            pname = (record.get("persona") or {}).get("name") or ""
            pname = pname.strip() if isinstance(pname, str) else ""
            if pname:
                mine = arca_client.list_my_characters(lang)
                matched = next((c["character_id"] for c in mine
                                if c.get("name") == pname), None)
                local_cid = record.get("arca_character_id")
                if matched and matched != local_cid:
                    # 換綁到遠端同名角色；舊帖子對映屬於其它角色，清空重掛
                    record["arca_character_id"] = matched
                    record.pop("arca_form_digest", None)
                    record.pop("arca_post_ids", None)
                    result["arca_character_id"] = matched
                elif not matched and local_cid:
                    # 遠端已無同名（被刪/改名）：本地對映過期 → 新建，
                    # 換代避免冪等快取回放舊角色
                    record.pop("arca_character_id", None)
                    record.pop("arca_form_digest", None)
                    record.pop("arca_post_ids", None)
                    record["arca_rebuild_gen"] = int(record.get("arca_rebuild_gen") or 0) + 1
                    result["arca_character_id"] = None
        except Exception as e:  # noqa: BLE001 按名匹配失敗回退本地對映
            result["errors"].append(f"按名匹配失敗(回退本地對映): {e}")

    # 1) 建角色 / 更新角色
    need_create = force or not record.get("arca_character_id")
    # 重建代數：刪除/強推/自愈都會 +1 並拼進冪等鍵，避免 arca 冪等快取
    # 回放「同表單首次建立」的舊響應（舊角色可能已刪，回放會假成功）。
    gen = int(record.get("arca_rebuild_gen") or 0)
    if force:
        gen += 1
    if not need_create:
        # 已同步 → 原地更新（不重傳封面/落地頁，update 不消費它們）。
        # 用核心表單指紋判斷是否有變化，避免每次都在 arca 插一個新 version。
        try:
            core_form = arca_mapping.persona_to_character_form(
                record.get("persona", {}), page_config=page_config, lang=lang)
            core_digest = _form_digest(core_form)
            if record.get("arca_form_digest") == core_digest:
                result["skipped"] = True
            else:
                arca_client.update_character_basic_info(
                    record["arca_character_id"], core_form, lang)
                record["arca_form_digest"] = core_digest
                result["updated"] = True
                pipeline.save_character(record)
        except Exception as e:  # noqa: BLE001 更新失敗：落 errors，不 raise
            msg = str(e)
            if "角色不存在" in msg or "角色已失效" in msg:
                # arca 上的角色已被刪/下架：本地對映失效，自動降級為重建
                stale = record.get("arca_character_id") or ""
                gen += 1
                record.pop("arca_character_id", None)
                record.pop("arca_form_digest", None)
                record.pop("arca_post_ids", None)
                result["arca_character_id"] = None
                result["errors"].append(
                    f"arca 角色已失效({stale[:12]}…)，自動重建")
                need_create = True
            else:
                result["errors"].append(f"更新角色失敗: {e}")
    if need_create:
        try:
            images = _upload_cover(record, lang)
            landing_url = None
            if do_landing:
                try:
                    landing_url = _upload_landing(record, lang)
                    result["landing_url"] = landing_url
                except Exception as e:  # noqa: BLE001 落地頁可選，失敗不阻斷建角色
                    result["errors"].append(f"landing 上傳失敗: {e}")
            form = arca_mapping.persona_to_character_form(
                record.get("persona", {}), images=images, landing_url=landing_url,
                page_config=page_config, lang=lang)
            # arca 建角色前置硬校驗（createCharacterParamCheck）：主圖 + 音色缺一不可。
            # 提前攔截給出可操作的提示，避免白跑一次非同步任務。
            if not images:
                raise arca_client.ArcaError(
                    "缺少封面圖：arca 建角色必須有主圖，請先為該角色生成封面")
            if not form.get("voice_id"):
                raise arca_client.ArcaError(
                    "缺少音色：persona.voice 為空，arca 建角色必須選擇音色")
            # 冪等鍵 = char_id + 表單指紋 + 重建代數：同表單重放防重；
            # 刪除/強推/自愈後代數變化，不會撞上 arca 冪等快取回放舊(可能已刪)角色。
            # 失敗嘗試鹽：arca 非同步任務框架按 Idempotency-Key 快取任務 24h，
            # 失敗任務同鍵重試只會回放快取的錯誤——每次失敗 +1 換新鍵才能真正重試。
            attempt = int(record.get("arca_create_attempt") or 0)

            def _create(g: int) -> str:
                salt = (f"-g{g}" if g else "") + (f"-a{attempt}" if attempt else "")
                return arca_client.create_character(
                    form, lang=lang,
                    idempotency_key=f"creaction-{char_id}-{_form_digest(form)}{salt}")

            old_cid = record.get("arca_character_id")
            cid = _create(gen)
            # 兜底：若返回的 cid 已死(冪等快取回放了已刪除角色)，換代重試一次
            try:
                alive = arca_client.character_exists(
                    cid, lang,
                    probe_name=form.get("name") or "同步探針",
                    probe_visibility=form.get("visibility") or "private")
            except Exception:  # noqa: BLE001 校驗通道異常時不誤判為死角色
                alive = True
            if not alive:
                gen += 1
                result["errors"].append(
                    f"檢測到冪等回放已刪除角色({cid[:12]}…)，已換代重建")
                cid = _create(gen)
            rebuilt = bool(old_cid) and old_cid != cid
            if rebuilt:
                # force 重建出了新角色：舊帖子對映屬於舊角色，必須清空，
                # 否則後續帖子同步會誤判「已同步」而跳過，新角色名下沒有帖子。
                record.pop("arca_post_ids", None)
            elif force and old_cid == cid:
                # force 但冪等快取返回了同一個角色（未真重建）：
                # 保留對映並按已同步跳過，避免舊角色名下重複發帖。
                force = False
            record["arca_character_id"] = cid
            record["arca_rebuild_gen"] = gen
            record.pop("arca_create_attempt", None)  # 成功後清鹽
            # 存核心表單指紋（不含 images/landing），後續 sync 據此判斷是否需要 update
            record["arca_form_digest"] = _form_digest(
                arca_mapping.persona_to_character_form(
                    record.get("persona", {}), page_config=page_config, lang=lang))
            result["arca_character_id"] = cid
            pipeline.save_character(record)
        except Exception as e:  # noqa: BLE001 建角色失敗：落 errors，返回結構化結果不 raise
            result["errors"].append(f"建角色失敗: {e}")
            # 換鹽落盤：避免下次重試因同 Idempotency-Key 回放本次失敗任務的快取錯誤
            try:
                record["arca_create_attempt"] = int(record.get("arca_create_attempt") or 0) + 1
                pipeline.save_character(record)
            except Exception:  # noqa: BLE001 鹽落盤失敗不影響錯誤上報
                pass
            # 建角色失敗時絕不能繼續發帖：result 裡殘留的是舊角色 id，
            # force 分支會把帖子重複發到舊角色並覆蓋舊對映。
            return result

    cid = result["arca_character_id"]
    if not cid or not sync_posts:
        return result

    # 2) 發帖（僅 sync_posts=True；逐條 resilient；已同步過的 post 跳過）
    post_vis = _post_visibility(record)
    synced = record.setdefault("arca_post_ids", {})
    for post in _latest_posts(char_id):
        pid = post.get("post_id")
        if not pid or (pid in synced and not force):
            if pid in synced:
                result["posts"].append({"post_id": pid, "arca_post_id": synced[pid]})
            continue
        try:
            content = _content_for_lang(post.get("content"), lang)
            img = post.get("image") or {}
            image_objs = []
            lp = img.get("local_path")
            if lp and Path(lp).exists():
                obj = arca_client.tos_upload(
                    Path(lp).read_bytes(),
                    f"creaction/{char_id}/post_{pid}.png", "image/png", lang)
                image_objs = [obj]
            arca_pid = arca_client.create_post(
                cid, content, image_objs, lang, visibility=post_vis)
            # 先記對映並立即落盤：後續任何失敗（可見性補償/程式中斷）重試時
            # 不會重複 create（/post/create 無冪等鍵，重複必產生重複帖）。
            synced[pid] = arca_pid
            pipeline.save_character(record)
            result["posts"].append({"post_id": pid, "arca_post_id": arca_pid})
            # 可見性補償：僅當顯式配置覆蓋(1/2/3)時執行；0=跟隨角色可見性
            # （後端按角色 is_public 派生），避免把私密角色的帖子強制翻公開。
            if config.ARCA_POST_VISIBILITY in (1, 2, 3):
                try:
                    arca_client.set_post_visibility(
                        arca_pid, config.ARCA_POST_VISIBILITY, lang)
                except Exception as e:  # noqa: BLE001 帖子已建，補償失敗僅記錄
                    result["errors"].append(f"帖子 {pid} 可見性設定失敗: {e}")
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"帖子 {pid} 同步失敗: {e}")

    pipeline.save_character(record)
    return result


def remove_from_arca(char_id: str) -> dict:
    """刪除該角色在 arca 上的對應角色，並清空本地同步對映。

    arca 返回「角色不存在/已失效」視為已刪（冪等）；本地角色資料不動，
    清掉 arca_character_id/arca_form_digest/arca_post_ids 後可重新同步。
    """
    with pipeline.char_lock(char_id):  # 與 sync 互斥，防對映覆蓋/復活
        return _remove_from_arca_locked(char_id)


def _remove_from_arca_locked(char_id: str) -> dict:
    record = pipeline.load_character(char_id)
    lang = record.get("lang", config.LANGUAGES[0])
    cid = record.get("arca_character_id")
    result = {"char_id": char_id, "arca_character_id": cid,
              "deleted": False, "skipped": False, "errors": []}
    if not cid:
        result["skipped"] = True  # 從未同步過，無事可做
        return result
    try:
        arca_client.delete_character(cid, lang, reason="creaction 同步端刪除")
        result["deleted"] = True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "角色不存在" in msg or "角色已失效" in msg:
            result["deleted"] = True  # arca 上已經沒了，視為冪等成功
        else:
            result["errors"].append(f"arca 刪除失敗: {e}")
            return result  # 刪除失敗保留本地對映，便於重試
    for k in ("arca_character_id", "arca_form_digest", "arca_post_ids"):
        record.pop(k, None)
    # 換代：下次重新匯入用新冪等鍵，避免 arca 冪等快取回放剛刪除角色的舊響應
    record["arca_rebuild_gen"] = int(record.get("arca_rebuild_gen") or 0) + 1
    pipeline.save_character(record)
    return result
