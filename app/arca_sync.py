"""编排单个角色的 arca 同步：封面→(落地页)→建角色→发帖，并回写本地 id 实现幂等。"""
import hashlib
import json
from pathlib import Path

from . import arca_client, arca_mapping, config, pipeline


def _latest_posts(char_id: str) -> list[dict]:
    """取该角色最近一批 INS 帖子（有 ig 批次则用，无则返回空、只同步人设）。"""
    ig = pipeline.load_latest_ig(char_id)
    if ig and ig.get("posts"):
        return ig["posts"]
    return []


def _content_for_lang(content, lang: str) -> str:
    if isinstance(content, dict):
        return content.get(lang) or next((v for v in content.values() if v), "")
    return content or ""


def _upload_cover(record: dict, lang: str) -> list[dict]:
    """上传封面并包成 arca 的 UserUploadImage（裸 StorageObject 会被 go-zero 解析拒 400）。

    arca 建角色硬校验必须有一张 is_main_pic=true 的主图（「请设置角色主图」）。
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
    page = pipeline.load_latest_landing(record["char_id"])
    html = (page or {}).get("html_filled")
    if not html:
        return None
    key = f"creaction/{record['char_id']}/landing.html"
    obj = arca_client.tos_upload(html.encode("utf-8"), key, "text/html", lang, public=True)
    return obj.get("url")


def _form_digest(form: dict) -> str:
    return hashlib.md5(
        json.dumps(form, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:8]


def sync_character(char_id: str, *, force: bool = False,
                   sync_landing: bool | None = None,
                   sync_posts: bool = False, progress=None) -> dict:
    """同步一个角色到 arca。

    - 未同步过 → 建角色（封面/落地页一并上传）。
    - 已同步过 → 原地更新（/character/updateBasicInfo，仅 name/gender/species/
      profile/voice_id/opening_prologue/visibility 会生效）；表单指纹没变则跳过。
    - force=True → 忽略已同步状态强制重建：arca 上会【新建一个角色】，旧角色仍保留。
    默认只同步角色本体；sync_posts=True 才把最近一批 INS 帖子发到 arca。
    """
    # 角色级互斥：同一角色的并发同步/删除会导致帖子重复发布与 record 覆盖丢写
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

    # 平台枚举（tags/species/setting_options 对齐用）；拉取失败降级 None 不阻断同步。
    # tags/species 的反查表合并全部语言词表——繁体/日文汉字等跨语言同形词也能命中
    # tag_key（如「非人類」是 zh 的繁体 tag_name）；setting_options 用角色语言（key 本地化）。
    try:
        page_config = dict(arca_client.get_page_config_cached(lang) or {})
        merged_tags, merged_species = [], []
        for other in config.LANGUAGES:
            try:
                c = arca_client.get_page_config_cached(other)
            except Exception:  # noqa: BLE001 单语言失败跳过
                continue
            merged_tags.extend(c.get("character_tags") or [])
            merged_species.extend(c.get("species") or [])
        if merged_tags:
            page_config["character_tags"] = merged_tags
        if merged_species:
            page_config["species"] = merged_species
    except Exception as e:  # noqa: BLE001
        page_config = None
        result["errors"].append(f"page_config 拉取失败(跳过枚举对齐): {e}")

    # 0) 按名匹配（权威判断）：不以本地 character_id 为准——只要 arca 上存在
    #    同名角色且创建者是同一 uid（list_my_characters 即本人自建列表），就在
    #    该角色上原地更新、帖子也挂它；远端无同名则视本地映射过期、走新建。
    #    查询失败时 fail-open 回退本地映射（保持可用性）。
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
                    # 换绑到远端同名角色；旧帖子映射属于其它角色，清空重挂
                    record["arca_character_id"] = matched
                    record.pop("arca_form_digest", None)
                    record.pop("arca_post_ids", None)
                    result["arca_character_id"] = matched
                elif not matched and local_cid:
                    # 远端已无同名（被删/改名）：本地映射过期 → 新建，
                    # 换代避免幂等缓存回放旧角色
                    record.pop("arca_character_id", None)
                    record.pop("arca_form_digest", None)
                    record.pop("arca_post_ids", None)
                    record["arca_rebuild_gen"] = int(record.get("arca_rebuild_gen") or 0) + 1
                    result["arca_character_id"] = None
        except Exception as e:  # noqa: BLE001 按名匹配失败回退本地映射
            result["errors"].append(f"按名匹配失败(回退本地映射): {e}")

    # 1) 建角色 / 更新角色
    need_create = force or not record.get("arca_character_id")
    # 重建代数：删除/强推/自愈都会 +1 并拼进幂等键，避免 arca 幂等缓存
    # 回放「同表单首次创建」的旧响应（旧角色可能已删，回放会假成功）。
    gen = int(record.get("arca_rebuild_gen") or 0)
    if force:
        gen += 1
    if not need_create:
        # 已同步 → 原地更新（不重传封面/落地页，update 不消费它们）。
        # 用核心表单指纹判断是否有变化，避免每次都在 arca 插一个新 version。
        try:
            core_form = arca_mapping.persona_to_character_form(
                record.get("persona", {}), page_config=page_config)
            core_digest = _form_digest(core_form)
            if record.get("arca_form_digest") == core_digest:
                result["skipped"] = True
            else:
                arca_client.update_character_basic_info(
                    record["arca_character_id"], core_form, lang)
                record["arca_form_digest"] = core_digest
                result["updated"] = True
                pipeline.save_character(record)
        except Exception as e:  # noqa: BLE001 更新失败：落 errors，不 raise
            msg = str(e)
            if "角色不存在" in msg or "角色已失效" in msg:
                # arca 上的角色已被删/下架：本地映射失效，自动降级为重建
                stale = record.get("arca_character_id") or ""
                gen += 1
                record.pop("arca_character_id", None)
                record.pop("arca_form_digest", None)
                record.pop("arca_post_ids", None)
                result["arca_character_id"] = None
                result["errors"].append(
                    f"arca 角色已失效({stale[:12]}…)，自动重建")
                need_create = True
            else:
                result["errors"].append(f"更新角色失败: {e}")
    if need_create:
        try:
            images = _upload_cover(record, lang)
            landing_url = None
            if do_landing:
                try:
                    landing_url = _upload_landing(record, lang)
                    result["landing_url"] = landing_url
                except Exception as e:  # noqa: BLE001 落地页可选，失败不阻断建角色
                    result["errors"].append(f"landing 上传失败: {e}")
            form = arca_mapping.persona_to_character_form(
                record.get("persona", {}), images=images, landing_url=landing_url,
                page_config=page_config)
            # arca 建角色前置硬校验（createCharacterParamCheck）：主图 + 音色缺一不可。
            # 提前拦截给出可操作的提示，避免白跑一次异步任务。
            if not images:
                raise arca_client.ArcaError(
                    "缺少封面图：arca 建角色必须有主图，请先为该角色生成封面")
            if not form.get("voice_id"):
                raise arca_client.ArcaError(
                    "缺少音色：persona.voice 为空，arca 建角色必须选择音色")
            # 幂等键 = char_id + 表单指纹 + 重建代数：同表单重放防重；
            # 删除/强推/自愈后代数变化，不会撞上 arca 幂等缓存回放旧(可能已删)角色。
            # 失败尝试盐：arca 异步任务框架按 Idempotency-Key 缓存任务 24h，
            # 失败任务同键重试只会回放缓存的错误——每次失败 +1 换新键才能真正重试。
            attempt = int(record.get("arca_create_attempt") or 0)

            def _create(g: int) -> str:
                salt = (f"-g{g}" if g else "") + (f"-a{attempt}" if attempt else "")
                return arca_client.create_character(
                    form, lang=lang,
                    idempotency_key=f"creaction-{char_id}-{_form_digest(form)}{salt}")

            old_cid = record.get("arca_character_id")
            cid = _create(gen)
            # 兜底：若返回的 cid 已死(幂等缓存回放了已删除角色)，换代重试一次
            try:
                alive = arca_client.character_exists(
                    cid, lang,
                    probe_name=form.get("name") or "同步探针",
                    probe_visibility=form.get("visibility") or "private")
            except Exception:  # noqa: BLE001 校验通道异常时不误判为死角色
                alive = True
            if not alive:
                gen += 1
                result["errors"].append(
                    f"检测到幂等回放已删除角色({cid[:12]}…)，已换代重建")
                cid = _create(gen)
            rebuilt = bool(old_cid) and old_cid != cid
            if rebuilt:
                # force 重建出了新角色：旧帖子映射属于旧角色，必须清空，
                # 否则后续帖子同步会误判「已同步」而跳过，新角色名下没有帖子。
                record.pop("arca_post_ids", None)
            elif force and old_cid == cid:
                # force 但幂等缓存返回了同一个角色（未真重建）：
                # 保留映射并按已同步跳过，避免旧角色名下重复发帖。
                force = False
            record["arca_character_id"] = cid
            record["arca_rebuild_gen"] = gen
            record.pop("arca_create_attempt", None)  # 成功后清盐
            # 存核心表单指纹（不含 images/landing），后续 sync 据此判断是否需要 update
            record["arca_form_digest"] = _form_digest(
                arca_mapping.persona_to_character_form(
                    record.get("persona", {}), page_config=page_config))
            result["arca_character_id"] = cid
            pipeline.save_character(record)
        except Exception as e:  # noqa: BLE001 建角色失败：落 errors，返回结构化结果不 raise
            result["errors"].append(f"建角色失败: {e}")
            # 换盐落盘：避免下次重试因同 Idempotency-Key 回放本次失败任务的缓存错误
            try:
                record["arca_create_attempt"] = int(record.get("arca_create_attempt") or 0) + 1
                pipeline.save_character(record)
            except Exception:  # noqa: BLE001 盐落盘失败不影响错误上报
                pass
            # 建角色失败时绝不能继续发帖：result 里残留的是旧角色 id，
            # force 分支会把帖子重复发到旧角色并覆盖旧映射。
            return result

    cid = result["arca_character_id"]
    if not cid or not sync_posts:
        return result

    # 2) 发帖（仅 sync_posts=True；逐条 resilient；已同步过的 post 跳过）
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
            arca_pid = arca_client.create_post(cid, content, image_objs, lang)
            # 先记映射并立即落盘：后续任何失败（可见性补偿/进程中断）重试时
            # 不会重复 create（/post/create 无幂等键，重复必产生重复帖）。
            synced[pid] = arca_pid
            pipeline.save_character(record)
            result["posts"].append({"post_id": pid, "arca_post_id": arca_pid})
            # 可见性补偿：仅当显式配置覆盖(1/2/3)时执行；0=跟随角色可见性
            # （后端按角色 is_public 派生），避免把私密角色的帖子强制翻公开。
            if config.ARCA_POST_VISIBILITY in (1, 2, 3):
                try:
                    arca_client.set_post_visibility(
                        arca_pid, config.ARCA_POST_VISIBILITY, lang)
                except Exception as e:  # noqa: BLE001 帖子已建，补偿失败仅记录
                    result["errors"].append(f"帖子 {pid} 可见性设置失败: {e}")
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"帖子 {pid} 同步失败: {e}")

    pipeline.save_character(record)
    return result


def remove_from_arca(char_id: str) -> dict:
    """删除该角色在 arca 上的对应角色，并清空本地同步映射。

    arca 返回「角色不存在/已失效」视为已删（幂等）；本地角色数据不动，
    清掉 arca_character_id/arca_form_digest/arca_post_ids 后可重新同步。
    """
    with pipeline.char_lock(char_id):  # 与 sync 互斥，防映射覆盖/复活
        return _remove_from_arca_locked(char_id)


def _remove_from_arca_locked(char_id: str) -> dict:
    record = pipeline.load_character(char_id)
    lang = record.get("lang", config.LANGUAGES[0])
    cid = record.get("arca_character_id")
    result = {"char_id": char_id, "arca_character_id": cid,
              "deleted": False, "skipped": False, "errors": []}
    if not cid:
        result["skipped"] = True  # 从未同步过，无事可做
        return result
    try:
        arca_client.delete_character(cid, lang, reason="creaction 同步端删除")
        result["deleted"] = True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "角色不存在" in msg or "角色已失效" in msg:
            result["deleted"] = True  # arca 上已经没了，视为幂等成功
        else:
            result["errors"].append(f"arca 删除失败: {e}")
            return result  # 删除失败保留本地映射，便于重试
    for k in ("arca_character_id", "arca_form_digest", "arca_post_ids"):
        record.pop(k, None)
    # 换代：下次重新导入用新幂等键，避免 arca 幂等缓存回放刚删除角色的旧响应
    record["arca_rebuild_gen"] = int(record.get("arca_rebuild_gen") or 0) + 1
    pipeline.save_character(record)
    return result
