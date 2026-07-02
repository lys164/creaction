# 全项目正确性审查报告（2026-07-03）

多 agent 审查：6 维度并行查找 + 每候选独立对抗验证（默认误报立场）。
候选 35 → **确认 33**（证伪 2）。0 critical / 25 major / 8 minor。

## [1] MAJOR — force 重建失败后帖子被重复发到旧角色，且旧 arca_post_ids 映射被覆盖丢失
**位置**: `app/arca_sync.py:194`（维度: arca-sync）

**缺陷与触发路径**: 触发路径：角色已同步且已发过帖（record 有 arca_character_id 和 arca_post_ids），调用 sync_character(force=True, sync_posts=True)，而本次重建失败（如封面文件被清理导致「缺少封面图」，或 create_character 网络/业务失败）。此时 need_create 分支只把错误落入 result["errors"]，但 result["arca_character_id"] 仍是第 67 行初始化的旧角色 id；第 186 行 cid 取到旧角色后进入发帖循环，第 194 行 `pid in synced and not force` 因 force=True 不跳过 → 对每条已同步过的帖子再次 create_post（后端 /post/create 无幂等键，必产生新帖）→ 同一帖子在旧角色名下出现两份；且 `synced[pid] = arca_pid` 覆盖旧映射并在第 214 行持久化，旧 arca_post_id 永久丢失，重复帖无法再被识别/清理。force 语义应是发到重建出的新角色，建角色失败时应跳过发帖。

**验证证据**: 证实。触发链完整：角色已同步（record 有 arca_character_id 和 arca_post_ids），封面文件缺失后调 sync_character(force=True, sync_posts=True)。(1) arca_sync.py:95 force 强制 need_create；:147 因 _upload_cover 返回空主动抛「缺少封面图」，:183 except 只落 errors，result["arca_character_id"] 仍是 :67 初始化的旧角色 id；(2) :186 cid 取到旧角色，sync_posts=True 进入发帖循环；(3) :194 `pid in synced and not force` 因 force=True 不跳过已同步帖子；(4) arca_client.py:300-319 create_post 无任何幂等键且 /post/create 即发布，必在旧角色名下产生重复公开帖；(5) :209 `synced[pid]=arca_pid` 覆盖旧 arca_post_id（synced 即 record["arca_post_ids"] 本体），:214 save_character 持久化，旧映射永久丢失，重复帖无法再识别/清理。对比 force 成功路径（:171-174 pop 旧映射、发到新角色）可确认失败路径违背 force 语义，是真实逻辑错误+数据损坏。另有同源路径：幂等回放返回相同 cid 时 :171 条件不成立、映射不清，:194 同样重复发帖。

## [2] MAJOR — character_exists 对已软删角色返回 True，arca_sync 的幂等回放防护（换代重建兜底）实际失效
**位置**: `app/arca_client.py:224`（维度: arca-sync）

**缺陷与触发路径**: character_exists 用 POST /character/detail 判活并声称「软删/失效返回 False」，但已对照 Go 后端核实：GetCharacterDetail 走 getCharacterByID（characterdao.GetByCharacterID 明确注释「不过滤 is_deleted」），buildCharacterDetail 只校验 Status；而用户自删（delete_type=1）只把 is_deleted 置 2、不改 Status → detail 对已删角色返回成功，character_exists 返回 True。触发路径：角色首次创建成功但本地保存失败/进程中断（record 无 arca_character_id、gen 未变），之后该角色在 arca 端被删除；再次 sync 时用与首次相同的幂等键 create → arca 幂等缓存回放已删角色的 cid → arca_sync.py 第 163 行 alive 检查因本缺陷判定「存活」→ 不换代重建，本地绑定死角色并报同步成功；随后 sync_posts 的 /post/create 同样不过滤 is_deleted，帖子会「成功」发到已删除角色名下（对外不可见，等于静默丢数据），直到下次 updateBasicInfo 报「角色不存在」才自愈。第 161-170 行专为该场景设计的兜底成为死代码。

**验证证据**: 证实。触发链：(1) 首次 sync_character 建角色成功但在 arca_sync.py:182 pipeline.save_character 前进程中断/保存失败（:183 except 吞掉），磁盘 record 无 arca_character_id、gen=0；(2) 该角色在 arca 端被用户自删——Go 侧 character_delete.go 只执行 UpdateByID {"is_deleted": 2}，Status 不变；(3) 24h 内重跑 sync：arca_sync.py:158 幂等键与首次相同（gen=0 无 salt），arca 两层幂等缓存（async_task.go:452-468 asyncTaskTTL=24h 回放旧 task_id；character_create_core.go:78-110 结果缓存 24h）回放已删角色 cid；(4) arca_sync.py:163 调 character_exists → POST /character/detail → GetCharacterDetail(character_query.go:251) 用 getCharacterByID → character_repository.go:102 GetByCharacterID 仅 Where("character_id=?") 不过滤 is_deleted（对比 :116 GetActiveByCharacterID 才有 is_deleted=0），buildCharacterDetail(character_query.go:588) 只校验 Status→detail 对软删角色返回成功，character_exists 返回 True，与其 docstring「软删返回 False」相悖；(5) alive=True → arca_sync.py:166-170 专为幂等回放死角色设计的换代重建兜底不触发，本地绑定死角色并报同步成功；(6) sync_posts 时 post/user.go:1683 getCharacterOwnerUserId 同样用不过滤 is_deleted 的 GetByCharacterID，帖子成功发到已删除（对外不可见）角色名下，静默丢数据，直到 updateBasicInfo 报错才自愈。缺陷根因在 character_exists 用 /character/detail 判活无法识别 is_deleted=2 的用户自删角色。

## [3] MAJOR — create_post 的 visibility 参数（含 ARCA_POST_VISIBILITY 配置）被后端静默忽略，私密/好友可见配置不生效
**位置**: `app/arca_client.py:310`（维度: arca-sync）

**缺陷与触发路径**: 触发路径：设置 ARCA_POST_VISIBILITY=3（私密）或调用 create_post(..., visibility=3) 后同步帖子 → 帖子仍按角色的 is_public 公开发布。已对照 Go 后端核实：internal/impl/post/user.go 的 CreatePost 中 `visibility := visibilityForCharacter(author.IsPublic)`，源码注释明示「请求里的 visibility 字段保留但忽略（兼容老客户端）」。客户端与 config 注释（1公开2好友3私密）给出了一个完全不生效的开关，期望发私密帖的内容会被公开发布，属静默错误行为。

**验证证据**: 证实。客户端 app/arca_client.py:310 将 visibility（默认取 config.py:160 的 ARCA_POST_VISIBILITY，注释宣称"1公开2好友3私密"）发给 /post/create，但 Go 后端 internal/impl/post/user.go:65-66 明确忽略请求中的 visibility，改用 visibilityForCharacter(author.IsPublic)（post.go:62-69：公开角色→1，私有→3）。触发链：角色按默认 visibility="public" 同步（arca_mapping.py:165-167）→ 用户设 ARCA_POST_VISIBILITY=3 或显式传 visibility=3 → 帖子仍以 visibility=1 公开落库并可对外分享（landing.go:16-17），无任何报错。creaction 全仓库也未调用后端存在的 /post/update_visibility（user.go:300）做补偿。期望私密的内容被静默公开发布，属真实正确性缺陷；因默认配置路径下结果恰好一致、需显式改配置才触发，定级 major 而非 critical。

## [4] MAJOR — 同一角色并发同步无任何互斥：帖子重复发布 + record 整文件读改写互相覆盖
**位置**: `app/arca_sync.py:54`（维度: arca-sync）

**缺陷与触发路径**: 触发路径：server.py 的 /api/arca/sync 每次调用都把批次 job 丢进 tasks.py 的 ThreadPoolExecutor(max_workers=4) 并行执行；用户连点两次「同步」（或两个批次都包含同一 char_id，或 sync 与 /api/arca/delete 并发）→ 两个线程同时 load_character 拿到相同快照：(a) 都看到 pid 不在 synced → 各自 create_post（无幂等键）→ 同一帖子在 arca 上重复两份；(b) 结束时各自 save_character 整记录覆盖写，后写者抹掉先写者回写的 arca_post_ids/arca_character_id/arca_rebuild_gen（delete 并发时可复活刚被清掉的映射，指向已删角色）。sync_character/remove_from_arca 内无按 char_id 的锁或去重。

**验证证据**: 证实。触发链：(1) server.py:631-650 每次 POST /api/arca/sync 都经 tasks.run→ThreadPoolExecutor(max_workers=4).submit 并行执行，HTTP 立即返回；tasks.py 的 _LOCK 只护任务状态字典。(2) arca_sync.py/pipeline.py:42-51/storage.py:32-64 全链路无按 char_id 的互斥，save_character 是整记录 JSON 覆盖写。(3) web/app.js 中 #btnArcaSync(561)、#btnArcaSyncPosts(632)、卡片删除按钮(607) 三个按钮互不禁用，UI 即可制造同 char_id 并发。具体场景：角色 X 已同步、帖子 p1 未同步，两个带 sync_posts 的请求并发 → 两线程各自 load_character 拿到相同快照，都在 arca_sync.py:194 看到 p1 不在 synced；帖子循环含 tos_upload+create_post 网络调用(60s 超时)，竞态窗口数秒宽 → 双方各调 arca_client.create_post(arca_client.py:300-315，_headers 不带 Idempotency-Key，对比 create_character:144 有带)，/post/create 即发布 → 同一帖子在 arca 重复发布两份；随后两线程先后 save_character(:214) 整文件覆盖，先写者的 arca_post_id 映射被抹掉，重复帖永久失联无法清理。sync∥delete 分支同样成立：remove_from_arca(:242-246) 删角色并清映射+gen+1 后，持旧快照的 sync 线程 save_character 把已删角色的 arca_character_id 与旧 gen 整体写回（该分支下次 sync 经"角色不存在"自愈，但重复帖分支无自愈）。角色创建本身因幂等键(char_id+digest+gen 相同)不会重复，帖子重复与映射覆盖是真实损害。

## [5] MAJOR — list_json 本地 key(文件名 stem) 与远端复合 key 不一致，post_batches 列表重复且经 migrate_all 无限繁殖脏记录
**位置**: `app/storage.py:85`（维度: storage）

**缺陷与触发路径**: save_json 写 post_batches 时远端 key 是 f"{char_id}__{batch_id}"（pipeline.py:823），本地文件是 posts/<char_id>/batch_x.json；list_batches(pipeline.py:1016) 调 list_json 时本地取 p.stem="batch_x"，远端行 key="charA__batch_x"，第 85 行 `key in out` 去重永远不命中 → 同一批次在返回 dict 里出现两份，前端批次列表每个批次显示两次（都通过 char_id 过滤）。更严重的连锁：第 90 行把远端行按 key 回写为 posts/charA/charA__batch_x.json；migrate_all(storage.py:164-168) 遍历该目录所有 *.json 时会把它再以 key="charA__charA__batch_x" 迁到远端，下一轮 list 又回写 charA__charA__batch_x.json……每跑一轮 list+migrate 前缀翻倍，远端脏记录和本地缓存文件无限膨胀，列表出现三份、四份重复。触发路径：配置 ARCA_STORAGE_KEY → 生成一个帖子批次 → 调 GET 批次列表接口即出现重复；再执行 /api/arca/storage/migrate 后重复数继续增加。

**验证证据**: 证实。触发链：(1) pipeline.py:823 save_json 远端 key=f"{char_id}__{batch_id}" 但本地文件为 posts/<char_id>/<batch_id>.json；(2) 配置 ARCA_STORAGE_KEY 后生成一个批次，GET 批次列表 → list_batches(pipeline.py:1016) 调 list_json(storage.py:67-94)：本地 glob 得 key="batch_yyy"，远端 query_records（无 match 过滤）返回 key="char_xxx__batch_yyy"，storage.py:85 的 `key in out` 永不命中，同一批次以两个 key 进入返回 dict，两份 data.char_id 均匹配，pipeline.py:1018 过滤不掉 → 列表每个批次重复两次；(3) storage.py:90 将远端行回写为 posts/char_xxx/char_xxx__batch_yyy.json，migrate_all(storage.py:161-168) 对目录 glob *.json 时对该文件生成 key="char_xxx__char_xxx__batch_yyy" 推到远端，下一轮 list 再回写、再 migrate 再加前缀 → 脏记录/缓存文件每轮递增，列表重复数递增为 3、4 份。另有同根因跨角色污染：list_json 远端查询不按 char 过滤，charA 的行被缓存进 charB 目录，migrate 生成 charB__charA__batch_yyy。

## [6] MAJOR — list_json 远端 query 不带 match 过滤且 limit=500 无分页：跨角色数据写入他人目录、超 500 条静默丢数据
**位置**: `app/storage.py:79`（维度: storage）

**缺陷与触发路径**: list_batches 按单角色目录调 list_json，但第 79 行 query_records(collection, limit=500) 拉的是整个 post_batches 集合（所有角色），第 88-91 行把远端独有行全部回写进当前角色的 posts/<char_id>/ 目录——charB 的批次文件被写进 charA 的目录（文件名 charB__batch_y.json），随后被 migrate_all 以错误 key 再迁移（见上条），本地缓存目录被跨角色污染。同时 limit=500 无 offset 翻页：任何集合远端记录超过 500 条时（如 personas 冷启动换机恢复、多角色批次累计），第 501 条起的远端独有记录被静默截断，list 结果不完整且永远不会回写恢复——用户在新机器上看到部分角色"丢失"。触发路径：两个及以上角色各生成过批次 → 删除 charA 本地 posts 目录（或换机）→ 调 charA 批次列表 → charB 的批次 json 落入 charA 目录；或远端 personas>500 条 → 角色列表缺失。

**验证证据**: 证实。触发链：pipeline.py:1016 list_batches(charA) → storage.py:79 query_records("post_batches", limit=500) 不带 match，拉回全部角色的行；远端 key 为 "{char_id}__{batch_id}"（pipeline.py:823）而本地 key 是 p.stem="batch_xxx"（storage.py:73），故 storage.py:85 的 "key in out" 去重永不命中——(a) charA 自己的已上云批次以两个 key 同时进入 out 且都通过 char_id 过滤，arca 启用后批次列表每条重复两次；(b) charB 的行经 88-91 行回写为 posts/charA/charB__batch_y.json，跨角色污染本地目录；(c) migrate_all（storage.py:164-168）再把污染文件以 "charA__charB__batch_y" 为 key put 上云，形成级联的错误 key 脏记录，每轮 list+migrate 前缀再叠一层；(d) limit=500 无 offset 翻页（后端合同 limit≤500，arca_storage.py:7），集合超 500 条时远端独有记录静默截断且永不回写恢复。仅需配置 ARCA_STORAGE_KEY + 两角色各有批次 + 调批次列表即触发。

## [7] MAJOR — 删除链路远端失败仅 warn/被吞且无补偿机制，已删数据被 list/load 回源"复活"
**位置**: `app/storage.py:102`（维度: storage）

**缺陷与触发路径**: delete_json 第 101-104 行远端 delete_record 失败只 log.warning；pipeline.delete_character(pipeline.py:532-544) 更是整段 except: pass 吞掉——若删 personas 远端记录时网络瞬断（或删到一半抛异常，后续集合全部跳过），本地文件已删但远端记录仍在。而 load_json/list_json 的回源逻辑会无条件把远端记录回写本地缓存（storage.py:57-63, 88-93），migrate_all 只推不删，没有任何路径修复该不一致。触发路径：配置远端 → 删除角色时远端请求失败（仅 warn/静默）→ 下次打开角色列表 list_json("personas") 从远端拉回该记录并回写 personas/<id>.json → 用户已删除的角色重新出现，且永久复活（每次删本地、远端仍在、再复活）。

**验证证据**: 证实。触发链：(1) server.py:399 delete_characters → pipeline.delete_character；pipeline.py:527 先 unlink 本地 personas/<cid>.json。(2) pipeline.py:533-544 远端 delete_record("personas") 网络瞬断抛异常时被 `except Exception: pass` 整段吞掉，且首条失败会跳过 ig_batches/landings/post_batches/chats 的所有远端删除；函数仍返回 True，API 报告删除成功。(3) 下次角色列表 server.py:147 → pipeline.py:56 list_json("personas")：本地已删、远端仍在，storage.py:83-93 无条件把远端独有记录并入并回写 personas/<cid>.json——已删角色在 UI 复活且本地文件重建；pipeline.py:1016 list_json("post_batches") 同样复活残留批次。(4) 无补偿：migrate_all 只推不删（storage.py:130-209），delete_json 远端失败只 warn（storage.py:101-104），无 tombstone/重试。候选唯一夸大处是"永久复活"——用户网络恢复后再删一次远端会成功，可自愈；但"报成功却静默复活+部分失败留孤儿记录"是真实数据一致性缺陷。

## [8] MAJOR — rerender_ig_post_image / delete_ig_post 直接 write_text 绕过 storage.save_json，远端 ig_batches 停留旧版本
**位置**: `app/pipeline.py:1228`（维度: storage）

**缺陷与触发路径**: IG 批次的其余写路径都走 storage.save_json("ig_batches", char_id, ...)（pipeline.py:1200）双写本地+远端，但 rerender_ig_post_image(1228-1230) 和 delete_ig_post(1252-1254) 只 write_text 本地 ig_latest.json，远端记录不更新。触发路径：配置远端 → 生成 IG 批次（远端已存）→ 删除其中一条 IG 帖（仅本地更新）→ 本地缓存丢失/换机后 load_latest_ig 回源 → 已删除的帖子连同旧图重新出现；重绘的图片信息同理回退到旧版本。与模块声明的"本地为缓存、远端为主"语义矛盾，属于读写路径不一致导致的数据回退。

**验证证据**: 证实。触发链：配置 ARCA_STORAGE_KEY（enabled()=True）→ 生成 IG 批次时 pipeline.py:1200 storage.save_json 双写本地+远端 ig_batches → 调 delete_ig_post（1252-1254）或 rerender_ig_post_image（1228-1230）时只裸 write_text 本地 ig_latest.json，远端记录仍是旧版本 → 本地缓存丢失/换机后 load_latest_ig（1205-1207）走 storage.load_json，storage.py:50-64 本地 miss 时从 arca_storage.get_record 回源并回写本地缓存 → 已删除的帖子复活（旧图经 OSS ensure_file 仍可完整显示）、重绘图片回退旧版，且脏数据被回写固化到本地。同文件对照证明这是遗漏而非设计：普通帖子的等价操作 rerender（844）和 delete_post_from_batch（877）都正确走 storage.save_json 双写。与 storage.py docstring "arca 为主、本地为缓存"语义直接矛盾。

## [9] MAJOR — save_json 非原子写 + load_json 读到半截文件时用远端旧版本回写覆盖，线程池并发下本地数据回退
**位置**: `app/storage.py:60`（维度: storage）

**缺陷与触发路径**: save_json 用 write_text 直接截断重写（无临时文件+rename），tasks.py 线程池里的批次生成任务会对同一 batch json 渐进多次保存（pipeline.py:823/844/877），同时 HTTP 请求线程可并发 load_json 同一文件：读到写了一半的 JSON → JSONDecodeError 被吞（第 48 行）→ 走远端 get 拿到上一版数据 → 第 60 行回写本地。若该回写落在任务线程最终一次 write_text 之后（且晚于其远端 put 前的取数），本地文件被覆盖回旧版本，直到下次保存前 load_batch/前端轮询读到的都是回退数据。触发路径：远端启用 + 批次生成任务保存的同时前端轮询批次详情，命中写入窗口即数据回退。

**验证证据**: 证实。触发链全部可在代码定位：(1) server.py 端点均为同步 def（AnyIO 线程池并发），DELETE /api/posts/{c}/{b}/{p}（→pipeline.py:877 save_json）或 POST .../image（→pipeline.py:844）可与 GET /api/posts/{c}/{b}（server.py:547→load_batch→load_json）对同一 batch 文件真并发。(2) storage.py:34 write_text 以 'w' 打开即截断、无 tmp+rename，存在读到空/半截 JSON 的窗口。(3) storage.py:47-49 本地解析失败被 pass 吞掉，当作缓存缺失走远端 get_record。(4) save_json 顺序是先本地写 V2 再 put_record(V2)，故读者在写窗口内发起的 get_record 必然拿到远端旧 V1，且其回写（storage.py:60）晚一个网络 RTT 落盘，必然覆盖写者已完成的本地 V2 → 本地=V1、远端=V2。(5) load_json 本地命中即返回（45-47 行）、list_json 本地 key 存在跳过远端同 key（85 行），回退黏性持续到该 batch 下次保存。具体后果：删帖场景下 pipeline.py:875 已物理 unlink 图片文件，帖子却在本地"复活"成坏图；重渲染场景下新图信息丢失回退。候选描述中"pipeline.py:823 对同一文件渐进多次保存"细节不准确（823 是新 batch 单次保存），但 844/877 重存 vs GET 轮询的主触发链成立。限定条件：需 ARCA_STORAGE_KEY 启用远端 + 命中毫秒级写窗口，故非 critical。

## [10] MAJOR — list_batches 本地/远端 key 命名空间不一致：批次重复出现且跨角色缓存污染
**位置**: `app/pipeline.py:1016`（维度: pipeline）

**缺陷与触发路径**: generate_posts 用远端 key `{char_id}__{batch_id}` 保存，但本地文件名是 `{batch_id}.json`。storage.list_json 以本地文件 stem(`batch_x`) 和远端 key(`char__batch_x`) 去重，两者永不相等 → 同一批次在返回 dict 里出现两次，且都通过 `b.get("char_id")==char_id` 过滤 → 列表页每个批次显示两条。触发路径：配置了 ARCA_STORAGE_KEY(config.py 默认已配) → 生成一个帖子批次 → 调 GET /api/posts/{char_id}/batches → 重复。次生破坏：list_json 对『远端独有』的行回写缓存，会把【所有角色】的批次写进当前角色目录 data/posts/<charA>/<charB>__<batch>.json；之后 storage.migrate_all 会把这些污染文件以 `charA__charB__batch` 的垃圾 key 再推到远端，垃圾记录 data 里 char_id=charB，又会出现在 charB 的 list_batches 里，重复条目持续放大。

**验证证据**: 证实。写入不对称：pipeline.py:823 远端 key=`{char_id}__{batch_id}`，本地文件=`{batch_id}.json`（batch_id 形如 batch_<ts>_<hex>，pipeline.py:23）。storage.list_json（storage.py:67-94）以本地 stem(`batch_x`) 和远端 key(`charA__batch_x`) 在第85行 `key in out` 判重，永不相等 → 同一批次两个条目；query_records 无 match 过滤返回全集合所有角色记录。pipeline.py:1018 只按 data 内 char_id 过滤，本地/远端两份 data 相同都通过 → GET /api/posts/{char_id}（server.py:474）每个批次显示两条。默认触发：config.py:136/167 的 ARCA_BASE_URL/ARCA_STORAGE_KEY 均有非空默认值，enabled()=True。次生污染确认：storage.py:88-93 把所有"远端独有"行（含其他角色的）回写当前角色目录 data/posts/<charA>/<charB>__batch_x.json；migrate_all（storage.py:161-168）对角色目录 glob *.json，把污染文件以 `charA__charB__batch_x` 垃圾 key 推远端，data 中 char_id=charB 使其再进入 charB 的 list_batches，重复持续放大。触发链：默认配置 → 生成一个帖子批次 → GET /api/posts/{char_id} → 该批次重复出现两次；执行迁移后进一步放大并跨角色扩散。

## [11] MAJOR — rerender_ig_post_image / delete_ig_post 直接 write_text 写本地，绕过 storage.save_json，远端 ig_batches 永不更新
**位置**: `app/pipeline.py:1228`（维度: pipeline）

**缺陷与触发路径**: generate_instagram_posts 走 storage.save_json("ig_batches", char_id, ...) 双写，但 rerender_ig_post_image(第1228-1230行) 和 delete_ig_post(第1252-1254行) 修改批次后只 `(_posts_dir/ig_latest.json).write_text(...)`，远端记录保持旧内容。触发路径：删除一条 IG 帖(或重渲一张图) → 本地已更新；随后换机、或本地 data/posts 缓存被清理 → load_latest_ig 本地未命中回源远端 → 拉回旧批次并回写本地缓存 → 已删除的帖子『复活』、重渲的图片回退为旧图；且被删帖的图片本地已被 _delete_post_image 删掉而 OSS 仍在，/img 路由 ensure_file 还能把它拉回来展示。

**验证证据**: 证实。触发链完整：(1) app/config.py:167 ARCA_STORAGE_KEY 有默认值，arca_storage.enabled() 默认为 True，storage 层处于"远端为主、本地为缓存"模式（app/storage.py:1-9 文档明确 load_json"缺失时从远端拉并回写本地缓存"）。(2) generate_instagram_posts (pipeline.py:1200-1201) 用 storage.save_json("ig_batches", char_id, ...) 双写，远端记录含帖子 [A,B,C]。(3) delete_ig_post (pipeline.py:1252-1254) 和 rerender_ig_post_image (pipeline.py:1228-1230) 修改批次后只执行 (_posts_dir/ig_latest.json).write_text(...)，未走 storage.save_json，远端 ig_batches 记录保持旧内容 [A,B,C]；对照同文件 delete_post_from_batch (pipeline.py:877-878) 对普通帖删除正确调用了 storage.save_json("post_batches", ...)，证明 IG 路径是遗漏而非有意设计。(4) 本地 data/posts 缓存丢失（换机/清理——项目卖点就是"换机/冷启动自动恢复"，storage.py:88 注释）后，load_latest_ig → storage.load_json 本地未命中 → get_record 拉回旧远端批次并回写本地缓存（storage.py:57-64）→ 已删除的帖子 B 复活、重渲图退回旧引用。(5) _delete_post_image (pipeline.py:850-859) 只删本地文件不删 OSS 对象，故复活帖子的图片仍可经 ensure_file 从 OSS 拉回展示，用户完全无感知数据回退。这是真实的数据一致性/数据复活缺陷。定级 major：不崩溃、需要本地缓存缺失这一条件才显现，但一旦触发即静默数据损坏（用户明确删除的内容复活），且该条件正是项目远端接管功能的核心使用场景。

## [12] MAJOR — 冷缓存场景全部按 Path.exists 判定图片存在，从不调 storage.ensure_file 回源 OSS，identity/cover/landing/export 静默降级
**位置**: `app/pipeline.py:36`（维度: pipeline）

**缺陷与触发路径**: storage 层提供 ensure_file(本地缺失时从 OSS 拉回)，server.py 的 /img、/upload 路由都在用，但 pipeline 内所有文件存在性判断只查本地：_first_source_image/_existing_source_images(第31-39行)、generate_landing 里 cover_lp 与帖子图收集(第923、950行)、_inline_landing_posts(第381行)、_image_bytes 的 local_path 分支(第335行)。触发路径：JSON 记录已从远端回源(换机或清空 data/ 后 list_characters 自动恢复记录)，但图片仅存在于 OSS → build_identity/build_cover_spec/generate_cover(use_reference=True) 静默拿不到参考图，生成的长相 DNA/封面与原角色脱钩；generate_landing 当作『无封面无帖图』纯文字生成；export_characters_zip 导出的 landing.html 缺帖子图(第381行直接 skip)、cover 仅靠可能已过期的 provider URL 兜底，产物残缺且无任何报错。

**验证证据**: 证实。storage.py 的设计契约明确是"本地文件为缓存"（storage.py:1-9），JSON 记录在冷缓存下会自动回源（list_json 注释"换机/冷启动自动恢复"，storage.py:88-93 回写本地；load_json:57-64 同样回源），且 ensure_file(storage.py:212-236) 正是为图片回源而设并被 server.py:696/704 的 /img、/upload 路由使用——即 UI 里图片能正常显示。但 pipeline 所有消费图片的路径只做 Path.exists 判断，从不调 ensure_file，形成契约断裂。完整触发链（前提 ARCA_STORAGE_KEY 已配置、数据已 migrate_all 推到远端；换机或清空 data/ 后）：1) 打开 UI → list_characters→storage.list_json 自动恢复 persona/批次/landing JSON，但 data/uploads、data/images 仍空；2) 上传源图路径存于 record["source_images"]（server.py:188-191 经 save_file 双写 OSS），此时 _existing_source_images(pipeline.py:33) 全部过滤掉 → build_identity(595-596) uris=[] 纯文字生成长相 DNA、build_cover_spec(621-622) 无参考、generate_cover(use_reference=True)(686-689) 静默丢弃参考图——用户显式勾选"参考原图"却得到与源照片脱钩的新脸，零报错；3) generate_landing(923-924,948-951) 帖子图 local_path 不存在 → post_imgs 为空，落地页当作"无帖图"纯文字生成、相册槽位全空——而同一批图片经 /img 路由（会回源）在 UI 里明明可见，行为自相矛盾；4) export_characters_zip：_image_bytes(334-339) local 分支落空只能兜底下载 provider URL（生图接口返回的临时签名 URL，过期即 cover 缺失），_inline_landing_posts(381-383) 直接 skip → 导出的 landing.html 残留指向 "/img/..." 的相对 URL 且槽位为空，脱离服务器打开必然破图，产物残缺且无任何报错。唯一可辩护点是 pipeline.py:32 注释"uploads may be cleaned up"表明缺图静默降级曾是刻意容忍的状态，但那是 arca 接管前针对"源图被 delete_character 清理"的语义；接管后同一判断无法区分"已清理"与"仅未缓存"，在存储层明示 local=cache 且提供了 ensure_file 的前提下，pipeline 不回源导致存在于 OSS 的数据被当作不存在，属真实正确性缺陷而非风格问题。

## [13] MAJOR — 同一角色 record 的读-改-写无任何锁，后台批量任务与前台请求并发时互相丢写
**位置**: `app/pipeline.py:707`（维度: pipeline）

**缺陷与触发路径**: save_character 全量覆盖整个 persona JSON。server.py 的 batch_cover/batch_ig_posts 等任务在后台 ThreadPoolExecutor 里跑 pipeline.generate_cover(load_character→生成→save_character，窗口长达数十秒)，同时 FastAPI 同步端点在线程池中可并发执行 regenerate_opening/regenerate_persona/arca 同步等同样 load→save 的操作。触发路径：对角色A发起批量封面任务，任务进行中用户在 UI 上刷开场白 → 两条线程各自基于旧快照写回 → 后写者覆盖前写者：要么新生成的 cover/identity 丢失，要么新 opening 被回滚。同类竞态：同一批次两条帖子并发点『重绘』(rerender_post_image 第828-847行整批 load→save)，先完成的那张图的 image 字段被后保存的旧快照覆盖丢失。

**验证证据**: 证实。save_character(pipeline.py:49)经storage.save_json(storage.py:32-34)对整份persona JSON做全量覆盖写，pipeline.py/storage.py全文无任何锁（tasks.py的_LOCK只护任务注册表）。触发链1：POST /api/characters/batch_cover(server.py:432-459)经tasks.run(tasks.py:67-87)在后台ThreadPoolExecutor执行generate_cover——pipeline.py:649 load快照后进入数十秒的generate_image网络调用，line 707全量写回；窗口内用户调POST /api/opening（同步def端点，FastAPI放AnyIO线程池真并发）→regenerate_opening(pipeline.py:278-290) load同一角色、改persona.opening、全量save。后写者用旧快照覆盖前写者：cover线程后写则新opening被回滚，opening线程后写则新cover/style_id/identity丢失；arca存储开启时stale快照还经put_record推到远端。触发链2：rerender_post_image(pipeline.py:831-846)整批load→改单post→整批save，同一batch两个post并发重绘（独立HTTP请求，server.py:479-484）时先完成者的post.image被后完成者的旧快照整批覆盖丢失。无版本号/CAS/合并等任何缓解。经典lost-update，且UI明确支持后台任务进行中继续操作（返回task_id轮询），非假设场景。

## [14] MAJOR — /api/personas 批量任务无部分失败语义：单组失败导致整任务报错并丢失已生成角色
**位置**: `app/server.py:205`（维度: server）

**缺陷与触发路径**: 与 import_json 的 _job 不同（那里每个 source 有 try/except），create_personas 的 _job 里 pipeline.create_personas_from_images 直接裸调。触发路径：上传 5 张图（one_per_image=True），第 3 组的某个语言 LLM 调用抛异常 → 异常穿透 _job → tasks.run 把任务标记为 error。但前 2 组（以及第 3 组中已完成的其它语言 future，create_personas_from_images 的 with 块会等它们跑完并 save_character 落盘）已经写入磁盘。前端 runTask 抛错，用户看到『全部失败』，拿不到 characters 列表，with_cover 封面阶段也整体跳过；用户重试会为已成功的组再次生成，产生成批重复角色。

**验证证据**: 证实。完整触发链：(1) POST /api/personas 上传 5 图、one_per_image=true → groups=5（server.py:200-201）。(2) _job 内 for group in groups 循环对 pipeline.create_personas_from_images 裸调、无 try/except（server.py:205-211），与同文件 import_json 的 _job 形成鲜明对照——后者每个 source 有 try/except 并注释 "keep batch resilient"（server.py:303-304），且返回 ok 列表 + errors 映射，证明批量部分失败语义是本项目的既定设计意图而非可选风格。(3) 每个成功语言的 create_persona_one_lang 在返回前即 save_character 落盘（pipeline.py:84-99，chat_json 是可失败的 LLM 网络调用）。(4) 第 3 组某语言 chat_json 抛异常时，create_personas_from_images 里 fut.result()（pipeline.py:122）重抛；with ThreadPoolExecutor 退出走 shutdown(wait=True)（无 cancel_futures），该组其余语言 future 照常跑完并落盘——磁盘上留下前 2 组全部记录 + 第 3 组部分记录。(5) 异常穿透 _job → tasks.run._wrap 置 status="error"、result=None（tasks.py:78-85）。(6) 前端 pollTask 对 status==="error" 直接 throw（web/app.js:43），create 页 catch 只显示"失败"（web/app.js:346-348），characters/count/cover_errors 全部拿不到，with_cover 封面阶段整体跳过；且 pendingFiles 只在成功分支清空（app.js:344），失败后原样保留，用户点重试会对全部 5 组重新生成——已成功的组获得全新 char_id/group_id，产生成批重复角色，进而污染角色库与后续 arca 同步。唯一轻微缓解：已落盘角色仍会出现在 /api/characters 列表中（pipeline.list_characters 扫盘），并非彻底丢失，但任务报告与磁盘状态不一致 + 重试必然重复生成，属真实正确性缺陷。

## [15] MAJOR — 角色记录 load-modify-save 无任何锁，后台同步任务与前台写接口并发导致丢更新（arca_character_id 丢失→远端重复建角色）
**位置**: `app/server.py:342`（维度: server）

**缺陷与触发路径**: 所有写路径（PUT /api/persona、批量封面 _job、arca_sync._job 等）都走 pipeline.load_character → 改字段 → save_character 整体覆盖写同一 character.json，无互斥。触发路径：用户对角色 A 发起 /api/arca/sync（后台任务，耗时数十秒），期间在 UI 保存人设（PUT /api/persona 先 load 到旧记录）；sync_character 成功后写入 arca_character_id/arca_form_digest/arca_rebuild_gen 并 save；随后 update_persona 用不含这些字段的旧记录 save → 同步映射被覆盖丢失。UI 显示『未同步』，下次同步 need_create=True，用新 form_digest 幂等键在 arca 上再建一个重复角色（旧角色仍在线）。批量封面任务与 sync 并发同一角色同理。

**验证证据**: 证实。触发链：(1) POST /api/arca/sync 后台线程（tasks.py:87 独立 ThreadPoolExecutor）执行 sync_character，在 arca_sync.py:65 load_character 读入无 arca_character_id 的记录，随后经 tos_upload + create_character（arca_client.py:137-165，3s 轮询最长 300s）耗时数十秒；(2) 期间用户 PUT /api/persona（server.py:342-344，FastAPI 线程池，真并发）load 到同一份旧记录并改 persona；(3) sync 成功后在 arca_sync.py:175-182 写入 arca_character_id/arca_rebuild_gen/arca_form_digest 并 save_character（pipeline.py:49 → storage.py:32-41 全量 write_text + 远端 put_record upsert，无合并无锁——全仓 grep 仅 tasks.py:13 有一把保护任务字典的锁）；(4) update_persona 随后用不含这些字段的旧记录整体覆盖保存，本地+远端同步映射双双丢失；(5) UI 按 pipeline.py:70 的 arca_synced=bool(arca_character_id) 显示未同步，下次 sync 在 arca_sync.py:95 判定 need_create=True，且 persona 已改导致 form_digest 变化、幂等键（creaction-{char_id}-{digest}-g{gen}）与首次不同，arca 幂等缓存拦不住 → 远端新建重复角色，旧角色仍在线且本地已无映射可删。反向交错则丢用户 persona 编辑；批量封面任务与 sync 并发同一角色同理。这是设计内正常操作序列（同步等待期间继续编辑保存），窗口数十秒，非假设性问题。

## [16] MAJOR — 上传文件目标名用毫秒时间戳+请求内序号，跨请求并发同毫秒必然撞名互相覆盖
**位置**: `app/server.py:188`（维度: server）

**缺陷与触发路径**: dest = UPLOAD_DIR / f"{int(time.time()*1000)}_{len(saved)}{ext}"，len(saved) 只在单请求内递增。FastAPI 同步端点在线程池并发执行：两个 /api/personas 请求在同一毫秒各自处理第 1 个文件（序号都是 0、扩展名相同）→ 生成同一路径，storage.save_file 本地 write_bytes + OSS 同 key put 都是后者覆盖前者 → 两个角色组的 source_images 指向同一张图，先上传方的原图永久丢失，后续 regenerate_persona/生图全部基于错图。

**验证证据**: 证实。触发链：server.py:168 create_personas 为同步 def 端点，FastAPI 在 AnyIO 线程池并发执行；两个 /api/personas 请求（如用户双击提交/双标签页同时提交）同一毫秒到达 server.py:188，dest = UPLOAD_DIR / f"{int(time.time()*1000)}_{len(saved)}{ext}" 中时间戳相同、len(saved) 均为 0（请求内局部变量）、扩展名同为 .png → 生成同一路径且无 exists() 检查/随机后缀；storage.py:120 local.write_bytes(data) 直接覆盖本地文件，storage.py:125 OSS 同 key put 同样后写覆盖先写。两个请求随后各自把同一路径写入各自角色组 source_images，先上传方原图内容被静默替换为另一请求的图片，永久丢失，后续 regenerate_persona/生图基于错图。请求内不撞（序号递增），但跨请求无任何串行化机制。

## [17] MAJOR — /api/posts、/api/ig_posts、批量 regenerate 等长耗时 LLM+生图调用仍是同步路由，超过反代读超时即向前端假报失败
**位置**: `app/server.py:463`（维度: server）

**缺陷与触发路径**: make_landing 的 docstring 已明确该部署环境存在反向代理读超时并因此把 landing 改成了任务化，但 POST /api/posts（count_per_type×多类型×生图）、/api/ig_posts（3~9 条帖子+配图）、/api/characters/regenerate_persona（多角色 LLM）仍在请求线程内同步完成。触发路径：勾选 with_images 生成一批帖子耗时超过反代超时 → 前端收到 502/超时并提示失败，但服务端线程继续跑完并落盘 → 用户以为失败而重试，产生重复批次/重复扣 LLM 配额，且第一批成为无人知晓的孤儿数据。

**验证证据**: 证实。触发链完整且有仓库内部证据支持：(1) 部署环境确有反代读超时——app/server.py:555-560 make_landing 的 docstring 明确记载"exceeds the reverse-proxy read timeout and surfaces as a 502"，即 502 是该部署下已实际观测到的现象，并因此把 /api/landing 任务化；(2) POST /api/posts (server.py:463) 仍在请求线程内同步执行 pipeline.generate_posts (pipeline.py:749-825)：先按类型并发 chat_json 生成文本，再对 count_per_type×类型数 张配图按 MAX_WORKERS=4 (config.py:65) 分波并发生成，每张图走异步任务轮询、单图超时上限 TASK_POLL_TIMEOUT=360s (config.py:62, api_client.py:219-236)——例如勾选 3 类×3 条带图共 9 图需 3 波生图，总耗时轻松超过已知会 502 的~1 分钟阈值（landing 单次 LLM 就已超时）；(3) 前端 web/app.js:853-889 用普通 api() fetch 同步等待 /api/posts 响应，收到反代 502 后 res.ok 为 false → 抛错 → 显示"失败：Bad Gateway"并重新启用按钮；(4) uvicorn 同步 def 路由跑在线程池里，客户端断开不会中断线程，pipeline.py:823 storage.save_json 照常落盘第一批 → 用户看到失败提示后重试，产生重复批次、双倍 LLM/生图消耗。同理 /api/characters/regenerate_persona (server.py:348, 前端 app.js:648) 多角色时也会触发。唯一轻微夸大处："孤儿数据无人知晓"不完全准确——get_batches (/api/posts/{char_id}) 会列出所有批次，第一批仍可见，但"前端假报失败 + 重复生成/重复扣配额"的核心缺陷成立。定级 major 而非 critical：不损坏既有数据、不崩溃，但产生误导性失败和重复数据/费用，且是同一部署下已被 landing 端点证实会发生的现象。

## [18] MAJOR — submit_image 未捕获非 JSON 响应与读超时，故障切换失效且异常裸抛
**位置**: `app/api_client.py:203`（维度: periphery）

**缺陷与触发路径**: submit_image 的 except 只捕获 SSLError/ConnectionError，而 chat() 还捕获 (RequestException, KeyError, ValueError)。触发路径：第一个 provider 返回 502/网关 HTML（r.json() 抛 json.JSONDecodeError ⊂ ValueError），或发生 ReadTimeout（属 Timeout⊂RequestException，非 ConnectionError）→ 异常直接冲出 submit_image，既不重试也不切换到池中其余 3 个健康域名，generate_image 整个失败且抛出的是裸 requests/json 异常而非 APIError。config.py 注释明确承诺『连接失败时按顺序故障切换到后续域名』，此处不成立。

**验证证据**: 已在项目 venv（requests 2.34.2）端到端复现：本地假 provider 返回 502+HTML，池中另有第二个 provider，调用 submit_image() 直接裸抛 requests.exceptions.JSONDecodeError，第二个域名从未被尝试。根因：api_client.py:203-204 的 except 只捕获 (SSLError, ConnectionError)，而 JSONDecodeError（MRO: InvalidJSONError→RequestException→ValueError）和 ReadTimeout（Timeout→RequestException）都不属于这两类；对照 chat() 在 line 102 有 (RequestException, KeyError, ValueError) 兜底分支，证明 submit_image 缺失该分支是缺陷而非设计。触发链：pipeline.py:692/733/1057/1075 → generate_image → submit_image → 第一个域名 502 网关 HTML 或 ReadTimeout → r.json()/requests.post 抛异常 → 冲出函数，不重试、不故障切换到其余健康域名，整次图片生成失败且抛裸 requests 异常而非 APIError，违反 config.py:8 '连接失败时按顺序故障切换到后续域名' 的承诺。另 502 时 line 194 的状态码重试判断因 r.json() 先抛而不可达。

## [19] MAJOR — generate_image 下载图片未校验 HTTP 状态，错误页字节被当作 PNG 落盘并上传 OSS
**位置**: `app/api_client.py:261`（维度: periphery）

**缺陷与触发路径**: requests.get(url, timeout=120).content 没有 raise_for_status。触发路径：poll 完成后到下载之间签名 URL 过期或 CDN 返回 403/404/5xx → 返回的 HTML/JSON 错误页字节被 storage.save_file 以 image/png 写入本地 {char_id}_xxx.png 并上传 OSS 私有桶（本地缓存删除后 ensure_file 还会把坏文件拉回来）。后续 /img/ 接口返回损坏图片、pipeline 的 file_to_data_uri 把错误页当参考图喂给模型，而调用方拿到的 result 里 url/local_path 看起来完全正常，属静默数据损坏。

**验证证据**: 证实。api_client.py:261 `requests.get(url, timeout=120).content` 无 raise_for_status（对比 arca_client.py:59 有），CDN 返回 403/404/5xx 时错误页字节直接进入 storage.save_file（storage.py:120 无条件 write_bytes + 以 image/png 上传 OSS，无任何魔数/PIL 校验，全仓 grep 确认）。调用方 pipeline.py:692/733/1057/1075 收到的 result 正常，坏路径被 save_character 固化为成功；file_to_data_uri（api_client.py:44）按扩展名标 image/png 把坏文件当参考图喂模型，server.py:696 ensure_file 还会从 OSS 把坏文件拉回。触发链：poll completed → 下载时签名 URL 过期或 CDN 瞬时 4xx/5xx → HTML 错误页落盘为 .png 并上传私有桶 → 静默数据损坏且传播。

## [20] MAJOR — send_message 在指定 session_id 但加载失败时静默新建会话，对话上下文丢失且开场白重复注入
**位置**: `app/chat.py:471`（维度: periphery）

**缺陷与触发路径**: loaded is None 时不区分『会话确实不存在』和『远端存储瞬时故障』（storage.load_json 对远端异常只 warn 并返回 None）。触发路径：换机后本地无缓存、latest() 刚从远端拿到会话 X 的 session_id，用户发消息时远端恰好 5xx/超时 → _load_session 返回 None → 静默 fork 出全新 session（新 id、重新插入 opening 消息），LLM 在零历史下回复，且新会话被 _save_session 持久化；用户视角是对话记忆突然清零，而请求方指定的 session_id 被无提示丢弃。

**验证证据**: 证实。触发链：(1) storage.py:52-56 load_json 本地缺失时回源 get_record，远端 5xx/超时抛 StorageError/requests 异常，被 except Exception 吞掉只 warn 并 return None，与 arca_storage.py:38-45 中仅 404 才合法返回 None 的「记录不存在」语义混淆；(2) chat.py:471-473 send_message 对 loaded is None 不报错，直接 _new_session 生成新 session_id 并重新注入 opening，调用方显式指定的 session_id（server.py:620-627 由 POST /api/chat 直传）被静默丢弃；(3) 冷缓存状态由代码自身制造：chat.py:411-421 _latest_session 远端 query 命中后不回写本地缓存（对比 storage.list_json 会回写），换机场景下 latest() 刚返回会话 X，紧接的 send_message 必然再次回源，一次瞬时故障即触发；(4) fork 会话经 chat.py:502 _save_session 持久化，本地 mtime 最新导致后续 _latest_session 永远优先返回 fork，原会话本地视角永久失联，LLM 在零历史下回复。用户表现为对话记忆清零+开场白重复，无任何错误提示。即使「session_id 不存在时新建」是产品意图，代码也无法区分 not-found 与瞬时故障，故障路径下的静默 fork 是真实正确性缺陷。

## [21] MAJOR — chat/submit_image 对单 provider 的业务错误（如 401 无效 key）直接 raise，跳过池中其余 provider
**位置**: `app/api_client.py:93`（维度: periphery）

**缺陷与触发路径**: 循环体内 raise APIError(msg) 不被任何 except 捕获，直接冲出整个 provider 轮询。触发路径：通过 POPOP_LLM_API_PROVIDERS 配置了两个各带独立 key 的 provider，A 的 key 过期/欠费返回 200+{"error":{...}}（或 401 带 error 壳，均不含 wait 且状态码不在 429/500/503）→ 所有 chat/submit_image 调用永久失败，即使 B 完全健康也永远轮不到；轮询指针还会让约一半请求先撞上 A。provider 专属错误应记为 last_err 后 break 到下一个 provider。

**验证证据**: 证实。app/api_client.py:93 的 raise APIError(msg) 在 try 块内，但 except 分支（95-104 行）只捕获 SSLError/ConnectionError/(RequestException, KeyError, ValueError)，APIError 直接继承 Exception（15 行）不被捕获，直接冲出双层循环，绕过 last_err 聚合和 105 行 "chat failed on all API domains" 兜底。submit_image 同病：198 行 raise，203 行 except 只捕获 SSL/ConnectionError。触发链：POPOP_LLM_API_PROVIDERS 配两个异 key provider（config.py 20-50 行显式支持，注释称 round-robin 分发）→ A 的 key 过期，网关返回 401 + OpenAI 格式 JSON {"error":{"message":...}} → 86 行 "error" in data 为真 → 89 行条件为假（无 "wait"，401 不在 429/500/503）→ 93 行 raise 冲出 → B 永远轮不到；轮询起点交替（31-34 行）使约 50% 的 chat/chat_json/submit_image 调用永久失败，尽管 B 健康。对照证据：同一 401 若响应体非 JSON，r.json() 抛 ValueError 被 102 行捕获后最终会切换到 B，说明失败切换本是设计意图，唯独 JSON 业务错误壳漏掉，属于逻辑错误而非设计选择。

## [22] MAJOR — 落地页单生成：勾选角色与当前载入 HTML 所属角色不一致时，用 A 的 HTML 迭代并覆盖 B 的落地页
**位置**: `web/app.js:1682`（维度: frontend）

**缺陷与触发路径**: btnLanding 单个分支取 charId = checked[0] || LD_ACTIVE_CHAR（L1682），而 current_html 取的是编辑器里 LD_ACTIVE_CHAR 的 ldCurrentHtml（L1696，isEdit=true 时发送）。勾选框点击被 char-pick 拦截不会切换 LD_ACTIVE_CHAR（L1532-1534 的 return）。触发路径：点击角色 A 卡片载入其历史落地页（ldCurrentHtml=A 的 HTML）→ 只勾选角色 B 的复选框（不点卡片）→ 点「生成」→ 请求 {char_id: B, current_html: A 的 HTML} → 后端把 A 页面的迭代结果保存为 B 的落地页，B 原有落地页被错误内容覆盖（数据损坏）。

**验证证据**: 证实。触发链：点角色A卡片→app.js L1532-1541 设 LD_ACTIVE_CHAR=A 并经 loadLandingHistory()(L1720) 把 A 的落地页载入 ldCurrentHtml；再只勾选 B 的复选框——L1533 `if (e.target.closest(".char-pick")) return;` 使卡片点击逻辑被短路，LD_ACTIVE_CHAR 与 ldCurrentHtml 均不变；点「生成」后 checked.length===1 走单个分支，L1682 `charId = checked[0]`=B，L1685 isEdit=true（ldCurrentHtml 是 A 的 HTML），L1696 发送 {char_id:B, current_html:A的HTML}。后端 server.py:553 透传至 pipeline.generate_landing (pipeline.py:900)，以 A 的 HTML 迭代后在 L984 `storage.save_json("landings", char_id, ...)` 覆盖 B 的 landing_latest.json（注释：单角色只保留最新一份，重新生成直接覆盖），B 原落地页被 A 页面的迭代结果错误覆盖且无版本可恢复。更糟的是响应后 L1704 loadLandingHistory() 重载的是 A 的历史页，用户看不到写入 B 的错误内容，属静默数据污染。代码 L1641 注释自述意图为「对当前载入的角色迭代」，L1682 取 checked[0] 与 current_html 来源(LD_ACTIVE_CHAR)不一致即缺陷根源。

## [23] MAJOR — 聊天回车发送绕过发送按钮 disabled，允许并发 /api/chat 导致会话分叉、消息丢失
**位置**: `web/app.js:1463`（维度: frontend）

**缺陷与触发路径**: sendChatMessage 只禁用了 #btnChatSend（L1420），输入框不禁用；L1463 的 keydown Enter 处理器直接调用 sendChatMessage()，不检查请求是否在途。触发路径：新会话下发第一条消息（session_id=null，请求在途）→ 立刻再输入第二条按 Enter → 两个 POST /api/chat 都带 session_id=null → 服务端创建两个独立会话；前端 CHAT_SESSION_ID/CHAT_MESSAGES 被后返回的响应整体覆盖（L1433-1434），先返回那条用户消息从 UI 消失并落入孤儿会话，后续聊天只延续其中一个会话，消息永久丢失。

**验证证据**: 证实。前端 web/app.js:1411-1450 sendChatMessage 无在途守卫，仅禁用 #btnChatSend（L1420），#chatInput 不禁用；L1463 keydown Enter 处理器无条件调用 sendChatMessage()。后端 app/chat.py:471-473 在 session_id 为空时 _new_session 生成全新会话；app/server.py:620 的 send_chat 为同步 def，FastAPI 在线程池并发执行，且每请求含数秒 LLM 调用，并发窗口大。触发链：新会话（CHAT_SESSION_ID=null，app.js L1212/1292/1302/1454）发第一条消息在途 → 输入第二条按 Enter → 两个 POST /api/chat 均带 session_id=null → 服务端创建两个独立会话文件 → 前端 L1433-1434 用后返回响应整体覆盖 CHAT_SESSION_ID/CHAT_MESSAGES，先返回那条消息从 UI 消失并落入前端永不再加载的孤儿会话（latest 只按 mtime 取一条），后续聊天只延续胜出会话。附带：session_id 非空时并发同样成立——两请求各自 _load_session 同一文件、append 后 _save_session，后写覆盖先写丢一轮对话。唯一细节出入：孤儿会话文件仍在磁盘，但前端无途径访问，用户视角等同永久丢失。

## [24] MAJOR — selectChatChar 无过期响应保护：快速切换角色后，旧角色的 session_id 会被写给新角色
**位置**: `web/app.js:1270`（维度: frontend）

**缺陷与触发路径**: selectChatChar 先同步设 CHAT_ACTIVE_CHAR=charId 再 await 两个请求（L1279-1281），响应回来后无条件写 CHAT_ACTIVE_REC/CHAT_SESSION_ID/CHAT_MESSAGES，不校验 charId 是否仍等于 CHAT_ACTIVE_CHAR。触发路径：点角色 A → 立刻点角色 B → B 的响应先回、A 的后回 → 界面标题/消息/CHAT_SESSION_ID 全变成 A 的，但 CHAT_ACTIVE_CHAR=B 且列表高亮 B → 此时发消息，POST /api/chat 带 {char_id: B, session_id: A 的会话} → 消息写错会话/报错，且用户看到的是 A 的聊天界面。

**验证证据**: 证实。web/app.js selectChatChar(L1270-1313) 在 await 前同步写 CHAT_ACTIVE_CHAR=charId（L1272），await Promise.all 两个请求（L1279-1282）后无任何 `charId === CHAT_ACTIVE_CHAR` 的过期校验，直接写 CHAT_ACTIVE_REC(L1283)、chatTitle(L1286)、CHAT_SESSION_ID/CHAT_MESSAGES(L1298-1300)。触发链：(1) 点角色 A → 发起 A 的两个请求；(2) A 未返回时点角色 B → CHAT_ACTIVE_CHAR=B，高亮 B，发起 B 的请求；(3) B 响应先回、A 后回——完全现实：两组请求独立，且 app/storage.py load_json 在 arca 接管开启时会远端回源（网络级延迟差），A/B 完成顺序不保证；(4) A 的迟到响应无条件覆盖：UI 标题/消息/CHAT_SESSION_ID 全变成 A 的，但 CHAT_ACTIVE_CHAR 仍为 B、列表高亮 B——界面与状态分裂；(5) 用户在看似 A 的聊天界面发消息，sendChat(L1427-1429) POST {char_id: B, session_id: A 的 session_id}；(6) 后端 app/chat.py:471 _load_session(B, A_session) 因会话文件按 char_id 目录存放（L197-198）而查不到 → 返回 None → L473 静默为 B 新建会话，用 B 的人设回复本该发给 A 的消息并持久化到 B 名下；前端随后用返回的 B 会话覆盖 CHAT_SESSION_ID/CHAT_MESSAGES（L1433-1434），A 的历史界面突变为 B 的新会话。消息路由到错误角色、状态错乱，属真实正确性 bug；无既有数据被破坏且可自行恢复，故 major 而非 critical。

## [25] MAJOR — loadLandingHistory / loadLatestIg 竞态：慢响应把前一个角色的内容渲染到当前角色名下
**位置**: `web/app.js:1713`（维度: frontend）

**缺陷与触发路径**: 两个函数都是先改全局激活角色再发请求，响应回来不校验是否仍是当前角色。落地页触发路径：点角色 A 卡片 → 立刻点角色 B → A 的 /api/landing/A 响应后到 → ldCurrentHtml 被写成 A 的 HTML 而 LD_ACTIVE_CHAR=B → 点「生成」（isEdit=true）→ A 的 HTML 被当作 current_html 迭代后保存为 B 的落地页（数据被错误覆盖，与 L1682 缺陷不同路径同后果）。IG 侧（loadLatestIg L1030-1045）同样：IG_ACTIVE_CHAR=B 时展示的是 A 的帖子，「删除/重绘」按钮会对 char B + A 的 post_id 发请求（404 报错）。

**验证证据**: 证实。loadLandingHistory (web/app.js:1713-1729) 在 await api("/api/landing/"+charId) 返回后不校验 charId 是否仍等于 LD_ACTIVE_CHAR，直接写全局 ldCurrentHtml；api() (app.js:15) 无 abort/代数守卫。触发链：点角色 A（有落地页，响应大而慢）→ 立刻点角色 B（无落地页，/api/landing/B 返回 {} 极快）→ B 响应先到置空 → A 响应后到把 ldCurrentHtml 写成 A 的 HTML，此时 LD_ACTIVE_CHAR=B、卡片高亮 B 但预览显示 A 的页面 → 点「生成」(app.js:1682-1696)：charId=B、isEdit=true、current_html=A 的 HTML → 后端 generate_landing (pipeline.py:900-986) 基于 A 的 HTML 迭代并 storage.save_json 覆盖写入 B 的 landing_latest.json（"单角色只保留最新一份，重新生成直接覆盖"），B 的落地页被 A 派生内容静默污染。GET 端点为 sync def 走 FastAPI 线程池并发 + 浏览器多连接，大小响应乱序完全现实。IG 侧 loadLatestIg (app.js:1030-1045) 同构：await 后无校验，A 的慢响应会把 A 的帖子渲染在 IG_ACTIVE_CHAR=B 名下；删除/重绘 (app.js:1177/1197) 用 B + A 的 post_id 请求，post_id 含 uuid (pipeline.py:23-24) 不会误删他人数据，后端抛 not found → 404 报错，属错误展示+操作失败。

## [26] MINOR — _source_image_is_referenced 只 glob 本地 personas 目录，与远端主存脱节，可能误删仍被引用的共享上传图
**位置**: `app/pipeline.py:294`（维度: pipeline）

**缺陷与触发路径**: storage 接管后本地 personas 只是缓存、可能不全(设计上支持清缓存后按需回源)。delete_character 判断共享上传图是否还被引用时(第567-579行)仅扫本地 config.PERSONA_DIR。触发路径：清空 data/personas 后只访问过角色A详情页(load_character 只回源缓存了A)，同组共享同一上传源图的其它语言变体B仅存在于远端 → 删除A时本地 glob 找不到任何引用 → 共享上传图被 unlink；之后B被回源使用时 _first_source_image 判定源图不存在，identity/cover 静默失去视觉锚点(源图虽在 OSS，但如上一条所述 pipeline 不会 ensure_file 拉回)。

**验证证据**: 证实：pipeline.py:294-303 的 _source_image_is_referenced 仅 glob 本地 PERSONA_DIR，而 storage.py:1-9 明确本地只是缓存（list_json 远端合并 limit=500 无分页、query 异常静默退化纯本地 storage.py:80-82）。delete_character 在 pipeline.py:527 先 unlink 被删角色自身 json 再跑守卫，同函数 537-541 行删 post_batches/chats 时查了远端、唯独此守卫没查。触发链：arca 接管开启，清空 data/personas 缓存但 uploads 仍在（设计明示本地可清），列表加载时远端 query 瞬时失败或 >500 截断导致同组变体 B 未回写本地 → 删除 A 时守卫找不到 B → 共享上传源图被 unlink（567-579 行）→ B 回源后 _existing_source_images(pipeline.py:31-39) 只做裸 Path.exists() 无 OSS 回源，build_identity(595)/cover(621)/regenerate(144 等) 静默丢失视觉锚点。另有独立路径：B.json 因非原子写损坏时 299-300 行 continue 视为不引用，纯本地模式也触发。定级 minor 的理由：OSS 副本不被删（无任何 OSS delete），/upload 路由 ensure_file 可偶然自愈；正常 UI 删除必经列表页会把 B 回写缓存，故需叠加边缘状态（query 失败/>500/损坏文件/直调 API）才触发。

## [27] MINOR — delete_character 级联删除不含 OSS：图片/上传件永久残留，且可被 /img 路由复活
**位置**: `app/pipeline.py:546`（维度: pipeline）

**缺陷与触发路径**: delete_character 删了本地 images/posts/landing/chat 和远端 JSON 记录，但 api_client.generate_image、server 上传都通过 storage.save_file 双写到了 OSS(creaction-data/images/…、creaction-data/uploads/…)，删除流程没有任何 OSS 对象删除。触发路径：删除角色后其所有封面/帖子图在 OSS 永久堆积(资源泄漏)；且任何持有旧文件名的引用(如导出的旧 landing、浏览器缓存的 /img/<name> URL)再次访问时 server.py:696 的 ensure_file 会把已删角色的图片从 OSS 拉回本地 data/images，本地删除被逆转，与『删除角色及所有归属数据』语义矛盾。

**验证证据**: 证实。链条完整：(1) api_client.py:263 生成图经 storage.save_file 双写本地+OSS（storage.py:117-127，key=creaction-data/images/…），上传件同理；(2) pipeline.py:516-581 delete_character 只删本地文件和 arca JSON 记录（delete_record on personas/ig_batches/landings/post_batches/chats），全仓 grep 确认 app/ 下不存在任何 OSS 对象删除代码（无 delete_object/tos_delete）；(3) server.py:693-698 GET /img/{name} 本地缺失时 storage.ensure_file(storage.py:212-236) 从 OSS get_object 拉回并 write_bytes 回写 data/images。触发链：配置 ARCA_STORAGE_KEY → 生成角色 → DELETE /api/characters → OSS 中该角色全部图片/上传件永久残留（无任何回收路径）；随后任何持旧文件名的请求（导出 landing 里的 /img/<char_id>_cover.png、缓存 URL）返回 200 并把已删图片重新落盘本地，违反「删除角色及所有归属数据」语义。限定：persona 记录本地+远端均已删，角色不会在 UI 复活，复活的只是孤儿图片，故非 major。

## [28] MINOR — /img、/upload 的 OSS 回源与 FileResponse 之间存在写读竞态，并发请求会拿到截断文件
**位置**: `app/server.py:696`（维度: server）

**缺陷与触发路径**: storage.ensure_file 在本地缺失时从 OSS 拉取后用非原子的 local.write_bytes 落盘；exists() 在写入中途即返回 True。触发路径：冷启动/换机后同一封面图在页面上出现两处（角色列表+详情），浏览器并发发出两个 GET /img/xxx.png：请求 A 进入 write_bytes 写入中，请求 B 的 ensure_file 看到 local.exists()=True 直接 FileResponse → stat 到的是半截文件，按当时大小发送 → 客户端收到截断图片（浏览器显示破图）；若 A 尚未写入任何字节则 B 返回 0 字节 200。

**验证证据**: 证实。app/server.py:693-706 两个端点为同步 def，FastAPI 在线程池并行执行；app/storage.py:212-236 ensure_file 用 local.exists() 判断后以非原子的 local.write_bytes(data)（open('wb') 截断/创建 + write，无 tmp+rename、无锁）落盘。触发链一（写入窗口）：冷启动本地缺图且 arca 启用，同一文件两个并发 GET，请求1进入 write_bytes 刚 open('wb') 创建文件时，请求2的 exists() 返回 True 直接 FileResponse——实测安装版 starlette FileResponse 在发送时才 os.stat 并以 st_size 设 Content-Length，stat 到 0/半截文件即以 200 返回空/截断图片。触发链二（窗口更宽）：两请求都在对方写盘前通过 exists()==False，各自进行秒级 OSS 下载；请求1写完开始按完整大小流式发送时，请求2的 write_bytes open('wb') 将文件截断为 0，请求1短读致实发字节 < Content-Length，uvicorn/h11 连接异常、客户端破图。定 minor：磁盘最终内容完整（两次写同一数据），损坏仅为瞬时响应层面，刷新即恢复，且只发生在冷启动回源窗口。

## [29] MINOR — _latest_session 远端回退按 created_at 排序，取到的不是最近活跃会话
**位置**: `app/chat.py:416`（维度: periphery）

**缺陷与触发路径**: 本地为空时 query_records(order_by="created_at", desc=True, limit=1) 取的是『创建最晚』的记录，而 put_record 是 upsert，会话每次 send_message 只更新 updated_at 不变 created_at。触发路径：角色有会话 A（day1 创建）和 B（day2 创建，客户端未带 session_id 时会新建会话所以多会话是常态），用户 day3 继续在 A 里聊天；换机/清空本地缓存后 GET /api/chat/{char_id}/latest → 返回 B（created_at 最大），用户最近的对话 A『消失』，继续输入会写进已废弃的 B。arca_storage.query_records 的行里本就带 updated_at，应按 updated_at 排序（这也与本地分支按 mtime=最后写入时间的语义一致）。

**验证证据**: 证实。触发链完整：(1) 后端 record.go upsert 为 `ON CONFLICT ... DO UPDATE SET data=EXCLUDED.data, updated_at=NOW()`，created_at 永远停在会话首存时间；buildOrderClause 中 order_by="created_at" 按行列排序，故 chat.py:414-416 的远端回退取的是"创建最晚"而非"最近活跃"的会话。(2) 多会话是常态：web/app.js forceNew(新对话按钮) 置空 session_id → send_message 走 _new_session。(3) 活跃序与创建序倒挂可自然发生：旧标签页仍持有 session A 的 id，另一标签页新建 B 后用户回到旧标签页继续聊 A → 远端 A(created早,updated晚)、B(created晚,updated早)。(4) 换机/清空 data/chats/（storage.py 注明该回退就是为"换机/冷启动自动恢复"设计）后 GET /api/chat/{char_id}/latest 返回 B，用户最近对话 A 不可见，后续消息全部写入已废弃的 B。与本地分支按 mtime(最后写入) 的语义相悖。附注：候选建议的 order_by="updated_at" 修法本身不成立——buildOrderClause 会将其映射为 data->>'updated_at'（恒 NULL），应改用 data 内字段 "updated" 或后端增列支持。定级 minor：需要多会话+活跃序倒挂+本地缓存为空三重条件，后果是恢复路径呈现/续写错误会话，远端数据本身未损坏。

## [30] MINOR — _parse_json 去围栏用 split("```", 2) 会在正文内部的 ``` 处截断，合法 JSON 解析失败
**位置**: `app/api_client.py:127`（维度: periphery）

**缺陷与触发路径**: 当模型输出以 ```json 开头且 JSON 字符串值内部含有字面 ```（chat 的 html_file/text 消息里角色分享含 markdown 代码块的备忘录、教程内容时完全可能出现），split("```", 2) 的 t[1] 在第一个内部 ``` 处被截断，json.loads 失败后的兜底 find/rfind 也只作用于截断后的 body → 本来合法的输出被判『模型未返回合法 JSON 数组』，send_message 直接抛 ValueError 报 500。正确做法应剥离首行围栏并从末尾 rsplit 收尾围栏。

**验证证据**: 已实证确认。触发链：(1) POST /api/chat → server.py:622 → chat.send_message → api_client.parse_json_text (chat.py:490)。(2) 当模型把 JSON 数组包在 ```json ... ``` 围栏里（这正是 _parse_json 存在的原因，chat prompt 第179行"markdown 코드블록 전부 금지"恰说明这是已知失败模式），且某个 text/html_file 消息的字符串值内含字面 ```（如角色分享含代码块的备忘录/教程，反引号在 JSON 字符串里无需转义、原样出现），api_client.py:127 的 t.split("```", 2) 使 t[1] 在第一个内部 ``` 处截断，JSON 字符串被拦腰截断。(3) json.loads 失败后，第140-146行的 find/rfind 兜底只作用于已截断的 body，同样失败。用 ./.venv/bin/python 实测：содержимое '```json\n[{"type":"text","data":{"content":"메모:\n```python\nprint(1)\n```"}}]\n```' 抛 JSONDecodeError "Unterminated string"；多元素变体抛 "Expecting ',' delimiter"（兜底切片也救不回）；同样内容不带围栏则解析成功——证明是围栏剥离逻辑而非内容本身的问题。(4) chat.py:493 转成 ValueError，server.py:620-627 无异常处理 → HTTP 500，且会话（含用户消息）未保存。同一函数还服务 pipeline.py 的 chat_json（persona/帖子生成），同理可触发。正确做法是剥首行围栏后从尾部 rsplit 收尾围栏。定级 minor：需要"模型违规加围栏"与"字符串值内含 ```"两个数据依赖条件同时成立，影响限于单次请求失败（可重试），无数据损坏。

## [31] MINOR — 角色名等 LLM 可控文本未转义直接拼入 innerHTML，可注入 HTML/脚本
**位置**: `web/app.js:398`（维度: frontend）

**缺陷与触发路径**: renderCharList(L397-400)、showCharDetail 的 h3(L755)、renderPostCharOptions 的 <option>(L849)、renderIgCharGrid(L1011)、renderLdCharGrid(L1530) 都把 c.name / localized(p.name) 不经 escapeHtml 直接模板拼进 innerHTML；同类还有 renderPosts/renderIgPosts 的 p.image.error(L903, L1129) 和 loadLandingHistory 的 page.style_text(L1722)。角色名来自 LLM 生成或用户导入的角色 JSON（/api/personas/import_json 接受任意 JSON 文件）。触发路径：导入一个 name 为 "<img src=x onerror=alert(1)>" 的角色 JSON → 打开「② 角色」视图 → onerror 脚本执行；即使只是名字里带 < 或 >（LLM 输出并不罕见）也会破坏卡片结构，勾选框/删除按钮错位失效。对比 chat 视图（L1256）是做了 escapeHtml 的，说明其它视图属遗漏。

**验证证据**: 证实。渲染侧：web/app.js:397-400 renderCharList 将 c.name 未经 escapeHtml 拼入 card.innerHTML（同类遗漏见 L755/L849/L1011/L1530/L903/L1129/L1722），而同文件 chat 视图 L1251/L1256/L1286 对同一字段做了 escapeHtml（L814 定义），证明转义是既有约定、此处属遗漏。数据侧：app/pipeline.py:54-73 list_characters 把 persona.name 原样返回 /api/characters；persona.name 由 LLM chat_json 输出原样存盘（pipeline.py:207-218），且 server.py:340-345 PUT /api/persona 接受任意 persona JSON 原样覆盖，是零 LLM 的确定性写入路径。触发链：PUT /api/persona 设 name="<img src=x onerror=alert(1)>"（或导入 JSON 经 LLM 本地化保留尖括号）→ 打开「② 角色」视图 → innerHTML 注入 → onerror 执行；名字仅含 "<字母" 也会吞掉后续 </div> 结构，导致卡片标签/删除按钮错位失效，属可观察错误行为。因是 localhost 单用户工具、注入源限于本机与 LLM 输出，实际影响以 UI 结构损坏为主，故 minor。

## [32] MINOR — arcaDeleteOne 成功路径不恢复按钮，依赖未 await 的 loadCharacters；刷新失败则按钮永久卡死
**位置**: `web/app.js:624`（维度: frontend）

**缺陷与触发路径**: arcaDeleteOne 把 btn.disabled=true、textContent="…"（L611-612），只有 catch 分支恢复（L627-628）；成功分支（含 r.errors 非空的逻辑失败分支）完全依赖 fire-and-forget 的 loadCharacters()（L624）重建卡片来"恢复"。触发路径：删除请求成功后 GET /api/characters 或 /api/styles 失败（服务重启中/瞬时网络故障）→ loadCharacters 的 rejection 无人捕获（unhandled rejection），列表不刷新 → 该卡片按钮永远停在禁用的"…"，且「☁️ 已同步」徽标仍显示已同步的过期状态，用户无法再从卡片操作。

**验证证据**: 证实。arcaDeleteOne（web/app.js:607-630）在 L610-611 置 btn.disabled=true、textContent="…"，成功分支（含 r.errors 非空分支）无任何按钮恢复代码，仅 L624 fire-and-forget 调用 loadCharacters()（无 await 无 .catch）；loadCharacters（L357-368）内 await api("/api/characters") 与 ensureStyles() 的 api("/api/styles")，api()（L15-23）在 !res.ok 或网络错误时 reject；全文件无 unhandledrejection/onerror 兜底。触发链：点 ☁️🗑 → /api/arca/delete 任务成功 → 随后 GET /api/characters（或首次 GET /api/styles）因服务重启/瞬时网络故障失败 → loadCharacters rejection 无人捕获、renderCharList 不执行 → 按钮永久禁用停在"…"，「☁️ 已同步」徽标保留过期状态，且无任何失败提示（此前已弹成功 toast），只能整页刷新恢复。同文件其他按钮（L601-604、L658-659）均用 finally 恢复，此处为确切遗漏。限定条件：需瞬时故障窗口、服务端删除已成功、无数据损坏、刷新可恢复，故定 minor。

## [33] MINOR — renderThumbs 每次重渲染都为全部待传图片新建 ObjectURL 且从不 revoke，Blob 内存泄漏
**位置**: `web/app.js:270`（维度: frontend）

**缺陷与触发路径**: renderThumbs 对 pendingFiles 里每个文件调用 URL.createObjectURL(f)（L270），而 addFiles 每次追加文件都会整体重渲染缩略图，旧的 blob URL 既没有复用也没有 URL.revokeObjectURL；上传成功/失败后 renderThumbs() 清空 DOM 时同样不回收。触发路径：连续粘贴/拖入 N 批图片（每批触发一次全量重建）→ 产生 O(N²) 个指向图片 Blob 的 URL 常驻内存，页面长开（该工具是常驻工作台）内存持续增长，大图场景可达数 GB 直至标签页崩溃。

**验证证据**: 证实：web/app.js L270 每次 renderThumbs 为 pendingFiles 全量新建 blob URL，全文件仅 L504-511 导出路径有 revokeObjectURL；addFiles（L257-263）每批追加都整体重建，上传成功后 L343-344 清空 pendingFiles 再 renderThumbs 也只清 DOM。按 File API 规范，未 revoke 的 blob URL 会把底层 Blob 钉在 URL store 直到页面 unload，而粘贴路径（L196-217，getAsFile，注释明说不落盘）产生的是内存态 Blob。触发链：上传视图连续 Cmd+V 粘贴截图并上传 → 会话内所有粘贴过的图片字节永不释放，长开页面内存单调增长，仅刷新可恢复。但候选的量级描述夸大：同一文件的重复 URL 只是小注册表条目不复制图片字节，泄漏量是 O(粘贴图片总字节) 而非 O(N²) 份数据，磁盘选择的 File 为磁盘背书；不会导致数据损坏，通常也到不了标签页崩溃。
