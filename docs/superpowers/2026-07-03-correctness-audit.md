# 全專案正確性審查報告（2026-07-03）

多 agent 審查：6 維度並行查詢 + 每候選獨立對抗驗證（預設誤報立場）。
候選 35 → **確認 33**（證偽 2）。0 critical / 25 major / 8 minor。

## [1] MAJOR — force 重建失敗後帖子被重複發到舊角色，且舊 arca_post_ids 對映被覆蓋丟失
**位置**: `app/arca_sync.py:194`（維度: arca-sync）

**缺陷與觸發路徑**: 觸發路徑：角色已同步且已發過帖（record 有 arca_character_id 和 arca_post_ids），呼叫 sync_character(force=True, sync_posts=True)，而本次重建失敗（如封面檔案被清理導致「缺少封面圖」，或 create_character 網路/業務失敗）。此時 need_create 分支只把錯誤落入 result["errors"]，但 result["arca_character_id"] 仍是第 67 行初始化的舊角色 id；第 186 行 cid 取到舊角色後進入發帖迴圈，第 194 行 `pid in synced and not force` 因 force=True 不跳過 → 對每條已同步過的帖子再次 create_post（後端 /post/create 無冪等鍵，必產生新帖）→ 同一帖子在舊角色名下出現兩份；且 `synced[pid] = arca_pid` 覆蓋舊對映並在第 214 行持久化，舊 arca_post_id 永久丟失，重複帖無法再被識別/清理。force 語義應是發到重建出的新角色，建角色失敗時應跳過發帖。

**驗證證據**: 證實。觸發鏈完整：角色已同步（record 有 arca_character_id 和 arca_post_ids），封面檔案缺失後調 sync_character(force=True, sync_posts=True)。(1) arca_sync.py:95 force 強制 need_create；:147 因 _upload_cover 返回空主動拋「缺少封面圖」，:183 except 只落 errors，result["arca_character_id"] 仍是 :67 初始化的舊角色 id；(2) :186 cid 取到舊角色，sync_posts=True 進入發帖迴圈；(3) :194 `pid in synced and not force` 因 force=True 不跳過已同步帖子；(4) arca_client.py:300-319 create_post 無任何冪等鍵且 /post/create 即釋出，必在舊角色名下產生重複公開帖；(5) :209 `synced[pid]=arca_pid` 覆蓋舊 arca_post_id（synced 即 record["arca_post_ids"] 本體），:214 save_character 持久化，舊對映永久丟失，重複帖無法再識別/清理。對比 force 成功路徑（:171-174 pop 舊對映、發到新角色）可確認失敗路徑違背 force 語義，是真實邏輯錯誤+資料損壞。另有同源路徑：冪等回放返回相同 cid 時 :171 條件不成立、對映不清，:194 同樣重複發帖。

## [2] MAJOR — character_exists 對已軟刪角色返回 True，arca_sync 的冪等回放防護（換代重建兜底）實際失效
**位置**: `app/arca_client.py:224`（維度: arca-sync）

**缺陷與觸發路徑**: character_exists 用 POST /character/detail 判活並聲稱「軟刪/失效返回 False」，但已對照 Go 後端核實：GetCharacterDetail 走 getCharacterByID（characterdao.GetByCharacterID 明確註釋「不過濾 is_deleted」），buildCharacterDetail 只校驗 Status；而使用者自刪（delete_type=1）只把 is_deleted 置 2、不改 Status → detail 對已刪角色返回成功，character_exists 返回 True。觸發路徑：角色首次建立成功但本地儲存失敗/程式中斷（record 無 arca_character_id、gen 未變），之後該角色在 arca 端被刪除；再次 sync 時用與首次相同的冪等鍵 create → arca 冪等快取回放已刪角色的 cid → arca_sync.py 第 163 行 alive 檢查因本缺陷判定「存活」→ 不換代重建，本地繫結死角色並報同步成功；隨後 sync_posts 的 /post/create 同樣不過濾 is_deleted，帖子會「成功」發到已刪除角色名下（對外不可見，等於靜默丟資料），直到下次 updateBasicInfo 報「角色不存在」才自愈。第 161-170 行專為該場景設計的兜底成為死程式碼。

**驗證證據**: 證實。觸發鏈：(1) 首次 sync_character 建角色成功但在 arca_sync.py:182 pipeline.save_character 前程式中斷/儲存失敗（:183 except 吞掉），磁碟 record 無 arca_character_id、gen=0；(2) 該角色在 arca 端被使用者自刪——Go 側 character_delete.go 只執行 UpdateByID {"is_deleted": 2}，Status 不變；(3) 24h 內重跑 sync：arca_sync.py:158 冪等鍵與首次相同（gen=0 無 salt），arca 兩層冪等快取（async_task.go:452-468 asyncTaskTTL=24h 回放舊 task_id；character_create_core.go:78-110 結果快取 24h）回放已刪角色 cid；(4) arca_sync.py:163 調 character_exists → POST /character/detail → GetCharacterDetail(character_query.go:251) 用 getCharacterByID → character_repository.go:102 GetByCharacterID 僅 Where("character_id=?") 不過濾 is_deleted（對比 :116 GetActiveByCharacterID 才有 is_deleted=0），buildCharacterDetail(character_query.go:588) 只校驗 Status→detail 對軟刪角色返回成功，character_exists 返回 True，與其 docstring「軟刪返回 False」相悖；(5) alive=True → arca_sync.py:166-170 專為冪等回放死角色設計的換代重建兜底不觸發，本地繫結死角色並報同步成功；(6) sync_posts 時 post/user.go:1683 getCharacterOwnerUserId 同樣用不過濾 is_deleted 的 GetByCharacterID，帖子成功發到已刪除（對外不可見）角色名下，靜默丟資料，直到 updateBasicInfo 報錯才自愈。缺陷根因在 character_exists 用 /character/detail 判活無法識別 is_deleted=2 的使用者自刪角色。

## [3] MAJOR — create_post 的 visibility 引數（含 ARCA_POST_VISIBILITY 配置）被後端靜默忽略，私密/好友可見配置不生效
**位置**: `app/arca_client.py:310`（維度: arca-sync）

**缺陷與觸發路徑**: 觸發路徑：設定 ARCA_POST_VISIBILITY=3（私密）或呼叫 create_post(..., visibility=3) 後同步帖子 → 帖子仍按角色的 is_public 公開發布。已對照 Go 後端核實：internal/impl/post/user.go 的 CreatePost 中 `visibility := visibilityForCharacter(author.IsPublic)`，原始碼註釋明示「請求裡的 visibility 欄位保留但忽略（相容老客戶端）」。客戶端與 config 註釋（1公開2好友3私密）給出了一個完全不生效的開關，期望發私密帖的內容會被公開發布，屬靜默錯誤行為。

**驗證證據**: 證實。客戶端 app/arca_client.py:310 將 visibility（預設取 config.py:160 的 ARCA_POST_VISIBILITY，註釋宣稱"1公開2好友3私密"）發給 /post/create，但 Go 後端 internal/impl/post/user.go:65-66 明確忽略請求中的 visibility，改用 visibilityForCharacter(author.IsPublic)（post.go:62-69：公開角色→1，私有→3）。觸發鏈：角色按預設 visibility="public" 同步（arca_mapping.py:165-167）→ 使用者設 ARCA_POST_VISIBILITY=3 或顯式傳 visibility=3 → 帖子仍以 visibility=1 公開落庫並可對外分享（landing.go:16-17），無任何報錯。creaction 全倉庫也未呼叫後端存在的 /post/update_visibility（user.go:300）做補償。期望私密的內容被靜默公開發布，屬真實正確性缺陷；因預設配置路徑下結果恰好一致、需顯式改配置才觸發，定級 major 而非 critical。

## [4] MAJOR — 同一角色併發同步無任何互斥：帖子重複釋出 + record 整檔案讀改寫互相覆蓋
**位置**: `app/arca_sync.py:54`（維度: arca-sync）

**缺陷與觸發路徑**: 觸發路徑：server.py 的 /api/arca/sync 每次呼叫都把批次 job 丟進 tasks.py 的 ThreadPoolExecutor(max_workers=4) 並行執行；使用者連點兩次「同步」（或兩個批次都包含同一 char_id，或 sync 與 /api/arca/delete 併發）→ 兩個執行緒同時 load_character 拿到相同快照：(a) 都看到 pid 不在 synced → 各自 create_post（無冪等鍵）→ 同一帖子在 arca 上重複兩份；(b) 結束時各自 save_character 整記錄覆蓋寫，後寫者抹掉先寫者回寫的 arca_post_ids/arca_character_id/arca_rebuild_gen（delete 併發時可復活剛被清掉的對映，指向已刪角色）。sync_character/remove_from_arca 內無按 char_id 的鎖或去重。

**驗證證據**: 證實。觸發鏈：(1) server.py:631-650 每次 POST /api/arca/sync 都經 tasks.run→ThreadPoolExecutor(max_workers=4).submit 並行執行，HTTP 立即返回；tasks.py 的 _LOCK 只護任務狀態字典。(2) arca_sync.py/pipeline.py:42-51/storage.py:32-64 全鏈路無按 char_id 的互斥，save_character 是整記錄 JSON 覆蓋寫。(3) web/app.js 中 #btnArcaSync(561)、#btnArcaSyncPosts(632)、卡片刪除按鈕(607) 三個按鈕互不禁用，UI 即可製造同 char_id 併發。具體場景：角色 X 已同步、帖子 p1 未同步，兩個帶 sync_posts 的請求併發 → 兩執行緒各自 load_character 拿到相同快照，都在 arca_sync.py:194 看到 p1 不在 synced；帖子迴圈含 tos_upload+create_post 網路呼叫(60s 超時)，競態視窗數秒寬 → 雙方各調 arca_client.create_post(arca_client.py:300-315，_headers 不帶 Idempotency-Key，對比 create_character:144 有帶)，/post/create 即釋出 → 同一帖子在 arca 重複釋出兩份；隨後兩執行緒先後 save_character(:214) 整檔案覆蓋，先寫者的 arca_post_id 對映被抹掉，重複帖永久失聯無法清理。sync∥delete 分支同樣成立：remove_from_arca(:242-246) 刪角色並清對映+gen+1 後，持舊快照的 sync 執行緒 save_character 把已刪角色的 arca_character_id 與舊 gen 整體寫回（該分支下次 sync 經"角色不存在"自愈，但重複帖分支無自愈）。角色建立本身因冪等鍵(char_id+digest+gen 相同)不會重複，帖子重複與對映覆蓋是真實損害。

## [5] MAJOR — list_json 本地 key(檔名 stem) 與遠端複合 key 不一致，post_batches 列表重複且經 migrate_all 無限繁殖髒記錄
**位置**: `app/storage.py:85`（維度: storage）

**缺陷與觸發路徑**: save_json 寫 post_batches 時遠端 key 是 f"{char_id}__{batch_id}"（pipeline.py:823），本地檔案是 posts/<char_id>/batch_x.json；list_batches(pipeline.py:1016) 調 list_json 時本地取 p.stem="batch_x"，遠端行 key="charA__batch_x"，第 85 行 `key in out` 去重永遠不命中 → 同一批次在返回 dict 裡出現兩份，前端批次列表每個批次顯示兩次（都透過 char_id 過濾）。更嚴重的連鎖：第 90 行把遠端行按 key 回寫為 posts/charA/charA__batch_x.json；migrate_all(storage.py:164-168) 遍歷該目錄所有 *.json 時會把它再以 key="charA__charA__batch_x" 遷到遠端，下一輪 list 又回寫 charA__charA__batch_x.json……每跑一輪 list+migrate 字首翻倍，遠端髒記錄和本地快取檔案無限膨脹，列表出現三份、四份重複。觸發路徑：配置 ARCA_STORAGE_KEY → 生成一個帖子批次 → 調 GET 批次列表介面即出現重複；再執行 /api/arca/storage/migrate 後重複數繼續增加。

**驗證證據**: 證實。觸發鏈：(1) pipeline.py:823 save_json 遠端 key=f"{char_id}__{batch_id}" 但本地檔案為 posts/<char_id>/<batch_id>.json；(2) 配置 ARCA_STORAGE_KEY 後生成一個批次，GET 批次列表 → list_batches(pipeline.py:1016) 調 list_json(storage.py:67-94)：本地 glob 得 key="batch_yyy"，遠端 query_records（無 match 過濾）返回 key="char_xxx__batch_yyy"，storage.py:85 的 `key in out` 永不命中，同一批次以兩個 key 進入返回 dict，兩份 data.char_id 均匹配，pipeline.py:1018 過濾不掉 → 列表每個批次重複兩次；(3) storage.py:90 將遠端行回寫為 posts/char_xxx/char_xxx__batch_yyy.json，migrate_all(storage.py:161-168) 對目錄 glob *.json 時對該檔案生成 key="char_xxx__char_xxx__batch_yyy" 推到遠端，下一輪 list 再回寫、再 migrate 再加字首 → 髒記錄/快取檔案每輪遞增，列表重複數遞增為 3、4 份。另有同根因跨角色汙染：list_json 遠端查詢不按 char 過濾，charA 的行被快取進 charB 目錄，migrate 生成 charB__charA__batch_yyy。

## [6] MAJOR — list_json 遠端 query 不帶 match 過濾且 limit=500 無分頁：跨角色資料寫入他人目錄、超 500 條靜默丟資料
**位置**: `app/storage.py:79`（維度: storage）

**缺陷與觸發路徑**: list_batches 按單角色目錄調 list_json，但第 79 行 query_records(collection, limit=500) 拉的是整個 post_batches 集合（所有角色），第 88-91 行把遠端獨有行全部回寫進當前角色的 posts/<char_id>/ 目錄——charB 的批次檔案被寫進 charA 的目錄（檔名 charB__batch_y.json），隨後被 migrate_all 以錯誤 key 再遷移（見上條），本地快取目錄被跨角色汙染。同時 limit=500 無 offset 翻頁：任何集合遠端記錄超過 500 條時（如 personas 冷啟動換機恢復、多角色批次累計），第 501 條起的遠端獨有記錄被靜默截斷，list 結果不完整且永遠不會回寫恢復——使用者在新機器上看到部分角色"丟失"。觸發路徑：兩個及以上角色各生成過批次 → 刪除 charA 本地 posts 目錄（或換機）→ 調 charA 批次列表 → charB 的批次 json 落入 charA 目錄；或遠端 personas>500 條 → 角色列表缺失。

**驗證證據**: 證實。觸發鏈：pipeline.py:1016 list_batches(charA) → storage.py:79 query_records("post_batches", limit=500) 不帶 match，拉回全部角色的行；遠端 key 為 "{char_id}__{batch_id}"（pipeline.py:823）而本地 key 是 p.stem="batch_xxx"（storage.py:73），故 storage.py:85 的 "key in out" 去重永不命中——(a) charA 自己的已上雲批次以兩個 key 同時進入 out 且都透過 char_id 過濾，arca 啟用後批次列表每條重複兩次；(b) charB 的行經 88-91 行回寫為 posts/charA/charB__batch_y.json，跨角色汙染本地目錄；(c) migrate_all（storage.py:164-168）再把汙染檔案以 "charA__charB__batch_y" 為 key put 上雲，形成級聯的錯誤 key 髒記錄，每輪 list+migrate 字首再疊一層；(d) limit=500 無 offset 翻頁（後端合同 limit≤500，arca_storage.py:7），集合超 500 條時遠端獨有記錄靜默截斷且永不回寫恢復。僅需配置 ARCA_STORAGE_KEY + 兩角色各有批次 + 調批次列表即觸發。

## [7] MAJOR — 刪除鏈路遠端失敗僅 warn/被吞且無補償機制，已刪資料被 list/load 回源"復活"
**位置**: `app/storage.py:102`（維度: storage）

**缺陷與觸發路徑**: delete_json 第 101-104 行遠端 delete_record 失敗只 log.warning；pipeline.delete_character(pipeline.py:532-544) 更是整段 except: pass 吞掉——若刪 personas 遠端記錄時網路瞬斷（或刪到一半拋異常，後續集合全部跳過），本地檔案已刪但遠端記錄仍在。而 load_json/list_json 的回源邏輯會無條件把遠端記錄回寫本地快取（storage.py:57-63, 88-93），migrate_all 只推不刪，沒有任何路徑修復該不一致。觸發路徑：配置遠端 → 刪除角色時遠端請求失敗（僅 warn/靜默）→ 下次開啟角色列表 list_json("personas") 從遠端拉回該記錄並回寫 personas/<id>.json → 使用者已刪除的角色重新出現，且永久復活（每次刪本地、遠端仍在、再復活）。

**驗證證據**: 證實。觸發鏈：(1) server.py:399 delete_characters → pipeline.delete_character；pipeline.py:527 先 unlink 本地 personas/<cid>.json。(2) pipeline.py:533-544 遠端 delete_record("personas") 網路瞬斷拋異常時被 `except Exception: pass` 整段吞掉，且首條失敗會跳過 ig_batches/landings/post_batches/chats 的所有遠端刪除；函式仍返回 True，API 報告刪除成功。(3) 下次角色列表 server.py:147 → pipeline.py:56 list_json("personas")：本地已刪、遠端仍在，storage.py:83-93 無條件把遠端獨有記錄併入並回寫 personas/<cid>.json——已刪角色在 UI 復活且本地檔案重建；pipeline.py:1016 list_json("post_batches") 同樣復活殘留批次。(4) 無補償：migrate_all 只推不刪（storage.py:130-209），delete_json 遠端失敗只 warn（storage.py:101-104），無 tombstone/重試。候選唯一誇大處是"永久復活"——使用者網路恢復後再刪一次遠端會成功，可自愈；但"報成功卻靜默復活+部分失敗留孤兒記錄"是真實資料一致性缺陷。

## [8] MAJOR — rerender_ig_post_image / delete_ig_post 直接 write_text 繞過 storage.save_json，遠端 ig_batches 停留舊版本
**位置**: `app/pipeline.py:1228`（維度: storage）

**缺陷與觸發路徑**: IG 批次的其餘寫路徑都走 storage.save_json("ig_batches", char_id, ...)（pipeline.py:1200）雙寫本地+遠端，但 rerender_ig_post_image(1228-1230) 和 delete_ig_post(1252-1254) 只 write_text 本地 ig_latest.json，遠端記錄不更新。觸發路徑：配置遠端 → 生成 IG 批次（遠端已存）→ 刪除其中一條 IG 帖（僅本地更新）→ 本地快取丟失/換機後 load_latest_ig 回源 → 已刪除的帖子連同舊圖重新出現；重繪的圖片資訊同理回退到舊版本。與模組宣告的"本地為快取、遠端為主"語義矛盾，屬於讀寫路徑不一致導致的資料回退。

**驗證證據**: 證實。觸發鏈：配置 ARCA_STORAGE_KEY（enabled()=True）→ 生成 IG 批次時 pipeline.py:1200 storage.save_json 雙寫本地+遠端 ig_batches → 調 delete_ig_post（1252-1254）或 rerender_ig_post_image（1228-1230）時只裸 write_text 本地 ig_latest.json，遠端記錄仍是舊版本 → 本地快取丟失/換機後 load_latest_ig（1205-1207）走 storage.load_json，storage.py:50-64 本地 miss 時從 arca_storage.get_record 回源並回寫本地快取 → 已刪除的帖子復活（舊圖經 OSS ensure_file 仍可完整顯示）、重繪圖片回退舊版，且髒資料被回寫固化到本地。同檔案對照證明這是遺漏而非設計：普通帖子的等價操作 rerender（844）和 delete_post_from_batch（877）都正確走 storage.save_json 雙寫。與 storage.py docstring "arca 為主、本地為快取"語義直接矛盾。

## [9] MAJOR — save_json 非原子寫 + load_json 讀到半截檔案時用遠端舊版本回寫覆蓋，執行緒池併發下本地資料回退
**位置**: `app/storage.py:60`（維度: storage）

**缺陷與觸發路徑**: save_json 用 write_text 直接截斷重寫（無臨時檔案+rename），tasks.py 執行緒池裡的批次生成任務會對同一 batch json 漸進多次儲存（pipeline.py:823/844/877），同時 HTTP 請求執行緒可併發 load_json 同一檔案：讀到寫了一半的 JSON → JSONDecodeError 被吞（第 48 行）→ 走遠端 get 拿到上一版資料 → 第 60 行回寫本地。若該回寫落在任務執行緒最終一次 write_text 之後（且晚於其遠端 put 前的取數），本地檔案被覆蓋回舊版本，直到下次儲存前 load_batch/前端輪詢讀到的都是回退資料。觸發路徑：遠端啟用 + 批次生成任務儲存的同時前端輪詢批次詳情，命中寫入視窗即資料回退。

**驗證證據**: 證實。觸發鏈全部可在程式碼定位：(1) server.py 端點均為同步 def（AnyIO 執行緒池併發），DELETE /api/posts/{c}/{b}/{p}（→pipeline.py:877 save_json）或 POST .../image（→pipeline.py:844）可與 GET /api/posts/{c}/{b}（server.py:547→load_batch→load_json）對同一 batch 檔案真併發。(2) storage.py:34 write_text 以 'w' 開啟即截斷、無 tmp+rename，存在讀到空/半截 JSON 的視窗。(3) storage.py:47-49 本地解析失敗被 pass 吞掉，當作快取缺失走遠端 get_record。(4) save_json 順序是先本地寫 V2 再 put_record(V2)，故讀者在寫視窗內發起的 get_record 必然拿到遠端舊 V1，且其回寫（storage.py:60）晚一個網路 RTT 落盤，必然覆蓋寫者已完成的本地 V2 → 本地=V1、遠端=V2。(5) load_json 本地命中即返回（45-47 行）、list_json 本地 key 存在跳過遠端同 key（85 行），回退黏性持續到該 batch 下次儲存。具體後果：刪帖場景下 pipeline.py:875 已物理 unlink 圖片檔案，帖子卻在本地"復活"成壞圖；重渲染場景下新圖資訊丟失回退。候選描述中"pipeline.py:823 對同一檔案漸進多次儲存"細節不準確（823 是新 batch 單次儲存），但 844/877 重存 vs GET 輪詢的主觸發鏈成立。限定條件：需 ARCA_STORAGE_KEY 啟用遠端 + 命中毫秒級寫視窗，故非 critical。

## [10] MAJOR — list_batches 本地/遠端 key 名稱空間不一致：批次重複出現且跨角色快取汙染
**位置**: `app/pipeline.py:1016`（維度: pipeline）

**缺陷與觸發路徑**: generate_posts 用遠端 key `{char_id}__{batch_id}` 儲存，但本地檔名是 `{batch_id}.json`。storage.list_json 以本地檔案 stem(`batch_x`) 和遠端 key(`char__batch_x`) 去重，兩者永不相等 → 同一批次在返回 dict 裡出現兩次，且都透過 `b.get("char_id")==char_id` 過濾 → 列表頁每個批次顯示兩條。觸發路徑：配置了 ARCA_STORAGE_KEY(config.py 預設已配) → 生成一個帖子批次 → 調 GET /api/posts/{char_id}/batches → 重複。次生破壞：list_json 對『遠端獨有』的行回寫快取，會把【所有角色】的批次寫進當前角色目錄 data/posts/<charA>/<charB>__<batch>.json；之後 storage.migrate_all 會把這些汙染檔案以 `charA__charB__batch` 的垃圾 key 再推到遠端，垃圾記錄 data 裡 char_id=charB，又會出現在 charB 的 list_batches 裡，重複條目持續放大。

**驗證證據**: 證實。寫入不對稱：pipeline.py:823 遠端 key=`{char_id}__{batch_id}`，本地檔案=`{batch_id}.json`（batch_id 形如 batch_<ts>_<hex>，pipeline.py:23）。storage.list_json（storage.py:67-94）以本地 stem(`batch_x`) 和遠端 key(`charA__batch_x`) 在第85行 `key in out` 判重，永不相等 → 同一批次兩個條目；query_records 無 match 過濾返回全集合所有角色記錄。pipeline.py:1018 只按 data 內 char_id 過濾，本地/遠端兩份 data 相同都透過 → GET /api/posts/{char_id}（server.py:474）每個批次顯示兩條。預設觸發：config.py:136/167 的 ARCA_BASE_URL/ARCA_STORAGE_KEY 均有非空預設值，enabled()=True。次生汙染確認：storage.py:88-93 把所有"遠端獨有"行（含其他角色的）回寫當前角色目錄 data/posts/<charA>/<charB>__batch_x.json；migrate_all（storage.py:161-168）對角色目錄 glob *.json，把汙染檔案以 `charA__charB__batch_x` 垃圾 key 推遠端，data 中 char_id=charB 使其再進入 charB 的 list_batches，重複持續放大。觸發鏈：預設配置 → 生成一個帖子批次 → GET /api/posts/{char_id} → 該批次重複出現兩次；執行遷移後進一步放大並跨角色擴散。

## [11] MAJOR — rerender_ig_post_image / delete_ig_post 直接 write_text 寫本地，繞過 storage.save_json，遠端 ig_batches 永不更新
**位置**: `app/pipeline.py:1228`（維度: pipeline）

**缺陷與觸發路徑**: generate_instagram_posts 走 storage.save_json("ig_batches", char_id, ...) 雙寫，但 rerender_ig_post_image(第1228-1230行) 和 delete_ig_post(第1252-1254行) 修改批次後只 `(_posts_dir/ig_latest.json).write_text(...)`，遠端記錄保持舊內容。觸發路徑：刪除一條 IG 帖(或重渲一張圖) → 本地已更新；隨後換機、或本地 data/posts 快取被清理 → load_latest_ig 本地未命中回源遠端 → 拉回舊批次並回寫本地快取 → 已刪除的帖子『復活』、重渲的圖片回退為舊圖；且被刪帖的圖片本地已被 _delete_post_image 刪掉而 OSS 仍在，/img 路由 ensure_file 還能把它拉回來展示。

**驗證證據**: 證實。觸發鏈完整：(1) app/config.py:167 ARCA_STORAGE_KEY 有預設值，arca_storage.enabled() 預設為 True，storage 層處於"遠端為主、本地為快取"模式（app/storage.py:1-9 檔案明確 load_json"缺失時從遠端拉並回寫本地快取"）。(2) generate_instagram_posts (pipeline.py:1200-1201) 用 storage.save_json("ig_batches", char_id, ...) 雙寫，遠端記錄含帖子 [A,B,C]。(3) delete_ig_post (pipeline.py:1252-1254) 和 rerender_ig_post_image (pipeline.py:1228-1230) 修改批次後只執行 (_posts_dir/ig_latest.json).write_text(...)，未走 storage.save_json，遠端 ig_batches 記錄保持舊內容 [A,B,C]；對照同檔案 delete_post_from_batch (pipeline.py:877-878) 對普通帖刪除正確呼叫了 storage.save_json("post_batches", ...)，證明 IG 路徑是遺漏而非有意設計。(4) 本地 data/posts 快取丟失（換機/清理——專案賣點就是"換機/冷啟動自動恢復"，storage.py:88 註釋）後，load_latest_ig → storage.load_json 本地未命中 → get_record 拉回舊遠端批次並回寫本地快取（storage.py:57-64）→ 已刪除的帖子 B 復活、重渲圖退回舊引用。(5) _delete_post_image (pipeline.py:850-859) 只刪本地檔案不刪 OSS 物件，故復活帖子的圖片仍可經 ensure_file 從 OSS 拉回展示，使用者完全無感知資料回退。這是真實的資料一致性/資料復活缺陷。定級 major：不崩潰、需要本地快取缺失這一條件才顯現，但一旦觸發即靜默資料損壞（使用者明確刪除的內容復活），且該條件正是專案遠端接管功能的核心使用場景。

## [12] MAJOR — 冷快取場景全部按 Path.exists 判定圖片存在，從不調 storage.ensure_file 回源 OSS，identity/cover/landing/export 靜默降級
**位置**: `app/pipeline.py:36`（維度: pipeline）

**缺陷與觸發路徑**: storage 層提供 ensure_file(本地缺失時從 OSS 拉回)，server.py 的 /img、/upload 路由都在用，但 pipeline 內所有檔案存在性判斷只查本地：_first_source_image/_existing_source_images(第31-39行)、generate_landing 裡 cover_lp 與帖子圖收集(第923、950行)、_inline_landing_posts(第381行)、_image_bytes 的 local_path 分支(第335行)。觸發路徑：JSON 記錄已從遠端回源(換機或清空 data/ 後 list_characters 自動恢復記錄)，但圖片僅存在於 OSS → build_identity/build_cover_spec/generate_cover(use_reference=True) 靜默拿不到參考圖，生成的長相 DNA/封面與原角色脫鉤；generate_landing 當作『無封面無帖圖』純文字生成；export_characters_zip 匯出的 landing.html 缺帖子圖(第381行直接 skip)、cover 僅靠可能已過期的 provider URL 兜底，產物殘缺且無任何報錯。

**驗證證據**: 證實。storage.py 的設計契約明確是"本地檔案為快取"（storage.py:1-9），JSON 記錄在冷快取下會自動回源（list_json 註釋"換機/冷啟動自動恢復"，storage.py:88-93 回寫本地；load_json:57-64 同樣回源），且 ensure_file(storage.py:212-236) 正是為圖片回源而設並被 server.py:696/704 的 /img、/upload 路由使用——即 UI 裡圖片能正常顯示。但 pipeline 所有消費圖片的路徑只做 Path.exists 判斷，從不調 ensure_file，形成契約斷裂。完整觸發鏈（前提 ARCA_STORAGE_KEY 已配置、資料已 migrate_all 推到遠端；換機或清空 data/ 後）：1) 開啟 UI → list_characters→storage.list_json 自動恢復 persona/批次/landing JSON，但 data/uploads、data/images 仍空；2) 上傳源圖路徑存於 record["source_images"]（server.py:188-191 經 save_file 雙寫 OSS），此時 _existing_source_images(pipeline.py:33) 全部過濾掉 → build_identity(595-596) uris=[] 純文字生成長相 DNA、build_cover_spec(621-622) 無參考、generate_cover(use_reference=True)(686-689) 靜默丟棄參考圖——使用者顯式勾選"參考原圖"卻得到與源照片脫鉤的新臉，零報錯；3) generate_landing(923-924,948-951) 帖子圖 local_path 不存在 → post_imgs 為空，落地頁當作"無帖圖"純文字生成、相簿槽位全空——而同一批圖片經 /img 路由（會回源）在 UI 裡明明可見，行為自相矛盾；4) export_characters_zip：_image_bytes(334-339) local 分支落空只能兜底下載 provider URL（生圖介面返回的臨時簽名 URL，過期即 cover 缺失），_inline_landing_posts(381-383) 直接 skip → 匯出的 landing.html 殘留指向 "/img/..." 的相對 URL 且槽位為空，脫離伺服器開啟必然破圖，產物殘缺且無任何報錯。唯一可辯護點是 pipeline.py:32 註釋"uploads may be cleaned up"表明缺圖靜默降級曾是刻意容忍的狀態，但那是 arca 接管前針對"源圖被 delete_character 清理"的語義；接管後同一判斷無法區分"已清理"與"僅未快取"，在儲存層明示 local=cache 且提供了 ensure_file 的前提下，pipeline 不回源導致存在於 OSS 的資料被當作不存在，屬真實正確性缺陷而非風格問題。

## [13] MAJOR — 同一角色 record 的讀-改-寫無任何鎖，後臺批次任務與前臺請求併發時互相丟寫
**位置**: `app/pipeline.py:707`（維度: pipeline）

**缺陷與觸發路徑**: save_character 全量覆蓋整個 persona JSON。server.py 的 batch_cover/batch_ig_posts 等任務在後臺 ThreadPoolExecutor 裡跑 pipeline.generate_cover(load_character→生成→save_character，視窗長達數十秒)，同時 FastAPI 同步端點線上程池中可併發執行 regenerate_opening/regenerate_persona/arca 同步等同樣 load→save 的操作。觸發路徑：對角色A發起批次封面任務，任務進行中使用者在 UI 上刷開場白 → 兩條執行緒各自基於舊快照寫回 → 後寫者覆蓋前寫者：要麼新生成的 cover/identity 丟失，要麼新 opening 被回滾。同類競態：同一批次兩條帖子併發點『重繪』(rerender_post_image 第828-847行整批 load→save)，先完成的那張圖的 image 欄位被後儲存的舊快照覆蓋丟失。

**驗證證據**: 證實。save_character(pipeline.py:49)經storage.save_json(storage.py:32-34)對整份persona JSON做全量覆蓋寫，pipeline.py/storage.py全文無任何鎖（tasks.py的_LOCK只護任務登入檔）。觸發鏈1：POST /api/characters/batch_cover(server.py:432-459)經tasks.run(tasks.py:67-87)在後臺ThreadPoolExecutor執行generate_cover——pipeline.py:649 load快照後進入數十秒的generate_image網路呼叫，line 707全量寫回；視窗內使用者調POST /api/opening（同步def端點，FastAPI放AnyIO執行緒池真併發）→regenerate_opening(pipeline.py:278-290) load同一角色、改persona.opening、全量save。後寫者用舊快照覆蓋前寫者：cover執行緒後寫則新opening被回滾，opening執行緒後寫則新cover/style_id/identity丟失；arca儲存開啟時stale快照還經put_record推到遠端。觸發鏈2：rerender_post_image(pipeline.py:831-846)整批load→改單post→整批save，同一batch兩個post併發重繪（獨立HTTP請求，server.py:479-484）時先完成者的post.image被後完成者的舊快照整批覆蓋丟失。無版本號/CAS/合併等任何緩解。經典lost-update，且UI明確支援後臺任務進行中繼續操作（返回task_id輪詢），非假設場景。

## [14] MAJOR — /api/personas 批次任務無部分失敗語義：單組失敗導致整任務報錯並丟失已生成角色
**位置**: `app/server.py:205`（維度: server）

**缺陷與觸發路徑**: 與 import_json 的 _job 不同（那裡每個 source 有 try/except），create_personas 的 _job 裡 pipeline.create_personas_from_images 直接裸調。觸發路徑：上傳 5 張圖（one_per_image=True），第 3 組的某個語言 LLM 呼叫拋異常 → 異常穿透 _job → tasks.run 把任務標記為 error。但前 2 組（以及第 3 組中已完成的其它語言 future，create_personas_from_images 的 with 塊會等它們跑完並 save_character 落盤）已經寫入磁碟。前端 runTask 拋錯，使用者看到『全部失敗』，拿不到 characters 列表，with_cover 封面階段也整體跳過；使用者重試會為已成功的組再次生成，產生成批重複角色。

**驗證證據**: 證實。完整觸發鏈：(1) POST /api/personas 上傳 5 圖、one_per_image=true → groups=5（server.py:200-201）。(2) _job 內 for group in groups 迴圈對 pipeline.create_personas_from_images 裸調、無 try/except（server.py:205-211），與同檔案 import_json 的 _job 形成鮮明對照——後者每個 source 有 try/except 並註釋 "keep batch resilient"（server.py:303-304），且返回 ok 列表 + errors 對映，證明批次部分失敗語義是本專案的既定設計意圖而非可選風格。(3) 每個成功語言的 create_persona_one_lang 在返回前即 save_character 落盤（pipeline.py:84-99，chat_json 是可失敗的 LLM 網路呼叫）。(4) 第 3 組某語言 chat_json 拋異常時，create_personas_from_images 裡 fut.result()（pipeline.py:122）重拋；with ThreadPoolExecutor 退出走 shutdown(wait=True)（無 cancel_futures），該組其餘語言 future 照常跑完並落盤——磁碟上留下前 2 組全部記錄 + 第 3 組部分記錄。(5) 異常穿透 _job → tasks.run._wrap 置 status="error"、result=None（tasks.py:78-85）。(6) 前端 pollTask 對 status==="error" 直接 throw（web/app.js:43），create 頁 catch 只顯示"失敗"（web/app.js:346-348），characters/count/cover_errors 全部拿不到，with_cover 封面階段整體跳過；且 pendingFiles 只在成功分支清空（app.js:344），失敗後原樣保留，使用者點重試會對全部 5 組重新生成——已成功的組獲得全新 char_id/group_id，產生成批重複角色，進而汙染角色庫與後續 arca 同步。唯一輕微緩解：已落盤角色仍會出現在 /api/characters 列表中（pipeline.list_characters 掃盤），並非徹底丟失，但任務報告與磁碟狀態不一致 + 重試必然重複生成，屬真實正確性缺陷。

## [15] MAJOR — 角色記錄 load-modify-save 無任何鎖，後臺同步任務與前臺寫介面併發導致丟更新（arca_character_id 丟失→遠端重複建角色）
**位置**: `app/server.py:342`（維度: server）

**缺陷與觸發路徑**: 所有寫路徑（PUT /api/persona、批次封面 _job、arca_sync._job 等）都走 pipeline.load_character → 改欄位 → save_character 整體覆蓋寫同一 character.json，無互斥。觸發路徑：使用者對角色 A 發起 /api/arca/sync（後臺任務，耗時數十秒），期間在 UI 儲存人設（PUT /api/persona 先 load 到舊記錄）；sync_character 成功後寫入 arca_character_id/arca_form_digest/arca_rebuild_gen 並 save；隨後 update_persona 用不含這些欄位的舊記錄 save → 同步對映被覆蓋丟失。UI 顯示『未同步』，下次同步 need_create=True，用新 form_digest 冪等鍵在 arca 上再建一個重複角色（舊角色仍線上）。批次封面任務與 sync 併發同一角色同理。

**驗證證據**: 證實。觸發鏈：(1) POST /api/arca/sync 後臺執行緒（tasks.py:87 獨立 ThreadPoolExecutor）執行 sync_character，在 arca_sync.py:65 load_character 讀入無 arca_character_id 的記錄，隨後經 tos_upload + create_character（arca_client.py:137-165，3s 輪詢最長 300s）耗時數十秒；(2) 期間使用者 PUT /api/persona（server.py:342-344，FastAPI 執行緒池，真併發）load 到同一份舊記錄並改 persona；(3) sync 成功後在 arca_sync.py:175-182 寫入 arca_character_id/arca_rebuild_gen/arca_form_digest 並 save_character（pipeline.py:49 → storage.py:32-41 全量 write_text + 遠端 put_record upsert，無合併無鎖——全倉 grep 僅 tasks.py:13 有一把保護任務字典的鎖）；(4) update_persona 隨後用不含這些欄位的舊記錄整體覆蓋儲存，本地+遠端同步對映雙雙丟失；(5) UI 按 pipeline.py:70 的 arca_synced=bool(arca_character_id) 顯示未同步，下次 sync 在 arca_sync.py:95 判定 need_create=True，且 persona 已改導致 form_digest 變化、冪等鍵（creaction-{char_id}-{digest}-g{gen}）與首次不同，arca 冪等快取攔不住 → 遠端新建重複角色，舊角色仍線上且本地已無對映可刪。反向交錯則丟使用者 persona 編輯；批次封面任務與 sync 併發同一角色同理。這是設計內正常操作序列（同步等待期間繼續編輯儲存），視窗數十秒，非假設性問題。

## [16] MAJOR — 上傳檔案目標名用毫秒時間戳+請求內序號，跨請求併發同毫秒必然撞名互相覆蓋
**位置**: `app/server.py:188`（維度: server）

**缺陷與觸發路徑**: dest = UPLOAD_DIR / f"{int(time.time()*1000)}_{len(saved)}{ext}"，len(saved) 只在單請求內遞增。FastAPI 同步端點線上程池併發執行：兩個 /api/personas 請求在同一毫秒各自處理第 1 個檔案（序號都是 0、副檔名相同）→ 生成同一路徑，storage.save_file 本地 write_bytes + OSS 同 key put 都是後者覆蓋前者 → 兩個角色組的 source_images 指向同一張圖，先上傳方的原圖永久丟失，後續 regenerate_persona/生圖全部基於錯圖。

**驗證證據**: 證實。觸發鏈：server.py:168 create_personas 為同步 def 端點，FastAPI 在 AnyIO 執行緒池併發執行；兩個 /api/personas 請求（如使用者雙擊提交/雙標籤頁同時提交）同一毫秒到達 server.py:188，dest = UPLOAD_DIR / f"{int(time.time()*1000)}_{len(saved)}{ext}" 中時間戳相同、len(saved) 均為 0（請求內區域性變數）、副檔名同為 .png → 生成同一路徑且無 exists() 檢查/隨機字尾；storage.py:120 local.write_bytes(data) 直接覆蓋本地檔案，storage.py:125 OSS 同 key put 同樣後寫覆蓋先寫。兩個請求隨後各自把同一路徑寫入各自角色組 source_images，先上傳方原圖內容被靜默替換為另一請求的圖片，永久丟失，後續 regenerate_persona/生圖基於錯圖。請求內不撞（序號遞增），但跨請求無任何序列化機制。

## [17] MAJOR — /api/posts、/api/ig_posts、批次 regenerate 等長耗時 LLM+生圖呼叫仍是同步路由，超過反代讀超時即向前端假報失敗
**位置**: `app/server.py:463`（維度: server）

**缺陷與觸發路徑**: make_landing 的 docstring 已明確該部署環境存在反向代理讀超時並因此把 landing 改成了任務化，但 POST /api/posts（count_per_type×多型別×生圖）、/api/ig_posts（3~9 條帖子+配圖）、/api/characters/regenerate_persona（多角色 LLM）仍在請求執行緒內同步完成。觸發路徑：勾選 with_images 生成一批帖子耗時超過反代超時 → 前端收到 502/超時並提示失敗，但服務端執行緒繼續跑完並落盤 → 使用者以為失敗而重試，產生重複批次/重複扣 LLM 配額，且第一批成為無人知曉的孤兒資料。

**驗證證據**: 證實。觸發鏈完整且有倉庫內部證據支援：(1) 部署環境確有反代讀超時——app/server.py:555-560 make_landing 的 docstring 明確記載"exceeds the reverse-proxy read timeout and surfaces as a 502"，即 502 是該部署下已實際觀測到的現象，並因此把 /api/landing 任務化；(2) POST /api/posts (server.py:463) 仍在請求執行緒內同步執行 pipeline.generate_posts (pipeline.py:749-825)：先按型別併發 chat_json 生成文字，再對 count_per_type×型別數 張配圖按 MAX_WORKERS=4 (config.py:65) 分波併發生成，每張圖走非同步任務輪詢、單圖超時上限 TASK_POLL_TIMEOUT=360s (config.py:62, api_client.py:219-236)——例如勾選 3 類×3 條帶圖共 9 圖需 3 波生圖，總耗時輕鬆超過已知會 502 的~1 分鐘閾值（landing 單次 LLM 就已超時）；(3) 前端 web/app.js:853-889 用普通 api() fetch 同步等待 /api/posts 響應，收到反代 502 後 res.ok 為 false → 拋錯 → 顯示"失敗：Bad Gateway"並重新啟用按鈕；(4) uvicorn 同步 def 路由跑線上程池裡，客戶端斷開不會中斷執行緒，pipeline.py:823 storage.save_json 照常落盤第一批 → 使用者看到失敗提示後重試，產生重複批次、雙倍 LLM/生圖消耗。同理 /api/characters/regenerate_persona (server.py:348, 前端 app.js:648) 多角色時也會觸發。唯一輕微誇大處："孤兒資料無人知曉"不完全準確——get_batches (/api/posts/{char_id}) 會列出所有批次，第一批仍可見，但"前端假報失敗 + 重複生成/重複扣配額"的核心缺陷成立。定級 major 而非 critical：不損壞既有資料、不崩潰，但產生誤導性失敗和重複資料/費用，且是同一部署下已被 landing 端點證實會發生的現象。

## [18] MAJOR — submit_image 未捕獲非 JSON 響應與讀超時，故障切換失效且異常裸拋
**位置**: `app/api_client.py:203`（維度: periphery）

**缺陷與觸發路徑**: submit_image 的 except 只捕獲 SSLError/ConnectionError，而 chat() 還捕獲 (RequestException, KeyError, ValueError)。觸發路徑：第一個 provider 返回 502/閘道器 HTML（r.json() 拋 json.JSONDecodeError ⊂ ValueError），或發生 ReadTimeout（屬 Timeout⊂RequestException，非 ConnectionError）→ 異常直接衝出 submit_image，既不重試也不切換到池中其餘 3 個健康域名，generate_image 整個失敗且丟擲的是裸 requests/json 異常而非 APIError。config.py 註釋明確承諾『連線失敗時按順序故障切換到後續域名』，此處不成立。

**驗證證據**: 已在專案 venv（requests 2.34.2）端到端復現：本地假 provider 返回 502+HTML，池中另有第二個 provider，呼叫 submit_image() 直接裸拋 requests.exceptions.JSONDecodeError，第二個域名從未被嘗試。根因：api_client.py:203-204 的 except 只捕獲 (SSLError, ConnectionError)，而 JSONDecodeError（MRO: InvalidJSONError→RequestException→ValueError）和 ReadTimeout（Timeout→RequestException）都不屬於這兩類；對照 chat() 在 line 102 有 (RequestException, KeyError, ValueError) 兜底分支，證明 submit_image 缺失該分支是缺陷而非設計。觸發鏈：pipeline.py:692/733/1057/1075 → generate_image → submit_image → 第一個域名 502 閘道器 HTML 或 ReadTimeout → r.json()/requests.post 拋異常 → 衝出函式，不重試、不故障切換到其餘健康域名，整次圖片生成失敗且拋裸 requests 異常而非 APIError，違反 config.py:8 '連線失敗時按順序故障切換到後續域名' 的承諾。另 502 時 line 194 的狀態碼重試判斷因 r.json() 先拋而不可達。

## [19] MAJOR — generate_image 下載圖片未校驗 HTTP 狀態，錯誤頁位元組被當作 PNG 落盤並上傳 OSS
**位置**: `app/api_client.py:261`（維度: periphery）

**缺陷與觸發路徑**: requests.get(url, timeout=120).content 沒有 raise_for_status。觸發路徑：poll 完成後到下載之間簽名 URL 過期或 CDN 返回 403/404/5xx → 返回的 HTML/JSON 錯誤頁位元組被 storage.save_file 以 image/png 寫入本地 {char_id}_xxx.png 並上傳 OSS 私有桶（本地快取刪除後 ensure_file 還會把壞檔案拉回來）。後續 /img/ 介面返回損壞圖片、pipeline 的 file_to_data_uri 把錯誤頁當參考圖餵給模型，而呼叫方拿到的 result 裡 url/local_path 看起來完全正常，屬靜默資料損壞。

**驗證證據**: 證實。api_client.py:261 `requests.get(url, timeout=120).content` 無 raise_for_status（對比 arca_client.py:59 有），CDN 返回 403/404/5xx 時錯誤頁位元組直接進入 storage.save_file（storage.py:120 無條件 write_bytes + 以 image/png 上傳 OSS，無任何魔數/PIL 校驗，全倉 grep 確認）。呼叫方 pipeline.py:692/733/1057/1075 收到的 result 正常，壞路徑被 save_character 固化為成功；file_to_data_uri（api_client.py:44）按副檔名標 image/png 把壞檔案當參考圖喂模型，server.py:696 ensure_file 還會從 OSS 把壞檔案拉回。觸發鏈：poll completed → 下載時簽名 URL 過期或 CDN 瞬時 4xx/5xx → HTML 錯誤頁落盤為 .png 並上傳私有桶 → 靜默資料損壞且傳播。

## [20] MAJOR — send_message 在指定 session_id 但載入失敗時靜默新建會話，對話上下文丟失且開場白重複注入
**位置**: `app/chat.py:471`（維度: periphery）

**缺陷與觸發路徑**: loaded is None 時不區分『會話確實不存在』和『遠端儲存瞬時故障』（storage.load_json 對遠端異常只 warn 並返回 None）。觸發路徑：換機後本地無快取、latest() 剛從遠端拿到會話 X 的 session_id，使用者發訊息時遠端恰好 5xx/超時 → _load_session 返回 None → 靜默 fork 出全新 session（新 id、重新插入 opening 訊息），LLM 在零歷史下回復，且新會話被 _save_session 持久化；使用者視角是對話記憶突然清零，而請求方指定的 session_id 被無提示丟棄。

**驗證證據**: 證實。觸發鏈：(1) storage.py:52-56 load_json 本地缺失時回源 get_record，遠端 5xx/超時拋 StorageError/requests 異常，被 except Exception 吞掉只 warn 並 return None，與 arca_storage.py:38-45 中僅 404 才合法返回 None 的「記錄不存在」語義混淆；(2) chat.py:471-473 send_message 對 loaded is None 不報錯，直接 _new_session 生成新 session_id 並重新注入 opening，呼叫方顯式指定的 session_id（server.py:620-627 由 POST /api/chat 直傳）被靜默丟棄；(3) 冷快取狀態由程式碼自身製造：chat.py:411-421 _latest_session 遠端 query 命中後不回寫本地快取（對比 storage.list_json 會回寫），換機場景下 latest() 剛返回會話 X，緊接的 send_message 必然再次回源，一次瞬時故障即觸發；(4) fork 會話經 chat.py:502 _save_session 持久化，本地 mtime 最新導致後續 _latest_session 永遠優先返回 fork，原會話本地視角永久失聯，LLM 在零歷史下回復。使用者表現為對話記憶清零+開場白重複，無任何錯誤提示。即使「session_id 不存在時新建」是產品意圖，程式碼也無法區分 not-found 與瞬時故障，故障路徑下的靜默 fork 是真實正確性缺陷。

## [21] MAJOR — chat/submit_image 對單 provider 的業務錯誤（如 401 無效 key）直接 raise，跳過池中其餘 provider
**位置**: `app/api_client.py:93`（維度: periphery）

**缺陷與觸發路徑**: 迴圈體內 raise APIError(msg) 不被任何 except 捕獲，直接衝出整個 provider 輪詢。觸發路徑：透過 POPOP_LLM_API_PROVIDERS 配置了兩個各帶獨立 key 的 provider，A 的 key 過期/欠費返回 200+{"error":{...}}（或 401 帶 error 殼，均不含 wait 且狀態碼不在 429/500/503）→ 所有 chat/submit_image 呼叫永久失敗，即使 B 完全健康也永遠輪不到；輪詢指標還會讓約一半請求先撞上 A。provider 專屬錯誤應記為 last_err 後 break 到下一個 provider。

**驗證證據**: 證實。app/api_client.py:93 的 raise APIError(msg) 在 try 塊內，但 except 分支（95-104 行）只捕獲 SSLError/ConnectionError/(RequestException, KeyError, ValueError)，APIError 直接繼承 Exception（15 行）不被捕獲，直接衝出雙層迴圈，繞過 last_err 聚合和 105 行 "chat failed on all API domains" 兜底。submit_image 同病：198 行 raise，203 行 except 只捕獲 SSL/ConnectionError。觸發鏈：POPOP_LLM_API_PROVIDERS 配兩個異 key provider（config.py 20-50 行顯式支援，註釋稱 round-robin 分發）→ A 的 key 過期，閘道器返回 401 + OpenAI 格式 JSON {"error":{"message":...}} → 86 行 "error" in data 為真 → 89 行條件為假（無 "wait"，401 不在 429/500/503）→ 93 行 raise 衝出 → B 永遠輪不到；輪詢起點交替（31-34 行）使約 50% 的 chat/chat_json/submit_image 呼叫永久失敗，儘管 B 健康。對照證據：同一 401 若響應體非 JSON，r.json() 拋 ValueError 被 102 行捕獲後最終會切換到 B，說明失敗切換本是設計意圖，唯獨 JSON 業務錯誤殼漏掉，屬於邏輯錯誤而非設計選擇。

## [22] MAJOR — 落地頁單生成：勾選角色與當前載入 HTML 所屬角色不一致時，用 A 的 HTML 迭代並覆蓋 B 的落地頁
**位置**: `web/app.js:1682`（維度: frontend）

**缺陷與觸發路徑**: btnLanding 單個分支取 charId = checked[0] || LD_ACTIVE_CHAR（L1682），而 current_html 取的是編輯器裡 LD_ACTIVE_CHAR 的 ldCurrentHtml（L1696，isEdit=true 時傳送）。勾選框點選被 char-pick 攔截不會切換 LD_ACTIVE_CHAR（L1532-1534 的 return）。觸發路徑：點選角色 A 卡片載入其歷史落地頁（ldCurrentHtml=A 的 HTML）→ 只勾選角色 B 的核取方塊（不點卡片）→ 點「生成」→ 請求 {char_id: B, current_html: A 的 HTML} → 後端把 A 頁面的迭代結果儲存為 B 的落地頁，B 原有落地頁被錯誤內容覆蓋（資料損壞）。

**驗證證據**: 證實。觸發鏈：點角色A卡片→app.js L1532-1541 設 LD_ACTIVE_CHAR=A 並經 loadLandingHistory()(L1720) 把 A 的落地頁載入 ldCurrentHtml；再只勾選 B 的核取方塊——L1533 `if (e.target.closest(".char-pick")) return;` 使卡片點選邏輯被短路，LD_ACTIVE_CHAR 與 ldCurrentHtml 均不變；點「生成」後 checked.length===1 走單個分支，L1682 `charId = checked[0]`=B，L1685 isEdit=true（ldCurrentHtml 是 A 的 HTML），L1696 傳送 {char_id:B, current_html:A的HTML}。後端 server.py:553 透傳至 pipeline.generate_landing (pipeline.py:900)，以 A 的 HTML 迭代後在 L984 `storage.save_json("landings", char_id, ...)` 覆蓋 B 的 landing_latest.json（註釋：單角色只保留最新一份，重新生成直接覆蓋），B 原落地頁被 A 頁面的迭代結果錯誤覆蓋且無版本可恢復。更糟的是響應後 L1704 loadLandingHistory() 過載的是 A 的歷史頁，使用者看不到寫入 B 的錯誤內容，屬靜默資料汙染。程式碼 L1641 註釋自述意圖為「對當前載入的角色迭代」，L1682 取 checked[0] 與 current_html 來源(LD_ACTIVE_CHAR)不一致即缺陷根源。

## [23] MAJOR — 聊天回車傳送繞過傳送按鈕 disabled，允許併發 /api/chat 導致會話分叉、訊息丟失
**位置**: `web/app.js:1463`（維度: frontend）

**缺陷與觸發路徑**: sendChatMessage 只禁用了 #btnChatSend（L1420），輸入框不禁用；L1463 的 keydown Enter 處理器直接呼叫 sendChatMessage()，不檢查請求是否在途。觸發路徑：新會話下發第一條訊息（session_id=null，請求在途）→ 立刻再輸入第二條按 Enter → 兩個 POST /api/chat 都帶 session_id=null → 服務端建立兩個獨立會話；前端 CHAT_SESSION_ID/CHAT_MESSAGES 被後返回的響應整體覆蓋（L1433-1434），先返回那條使用者訊息從 UI 消失並落入孤兒會話，後續聊天只延續其中一個會話，訊息永久丟失。

**驗證證據**: 證實。前端 web/app.js:1411-1450 sendChatMessage 無在途守衛，僅禁用 #btnChatSend（L1420），#chatInput 不禁用；L1463 keydown Enter 處理器無條件呼叫 sendChatMessage()。後端 app/chat.py:471-473 在 session_id 為空時 _new_session 生成全新會話；app/server.py:620 的 send_chat 為同步 def，FastAPI 線上程池併發執行，且每請求含數秒 LLM 呼叫，併發視窗大。觸發鏈：新會話（CHAT_SESSION_ID=null，app.js L1212/1292/1302/1454）發第一條訊息在途 → 輸入第二條按 Enter → 兩個 POST /api/chat 均帶 session_id=null → 服務端建立兩個獨立會話檔案 → 前端 L1433-1434 用後返回響應整體覆蓋 CHAT_SESSION_ID/CHAT_MESSAGES，先返回那條訊息從 UI 消失並落入前端永不再載入的孤兒會話（latest 只按 mtime 取一條），後續聊天只延續勝出會話。附帶：session_id 非空時併發同樣成立——兩請求各自 _load_session 同一檔案、append 後 _save_session，後寫覆蓋先寫丟一輪對話。唯一細節出入：孤兒會話檔案仍在磁碟，但前端無途徑訪問，使用者視角等同永久丟失。

## [24] MAJOR — selectChatChar 無過期響應保護：快速切換角色後，舊角色的 session_id 會被寫給新角色
**位置**: `web/app.js:1270`（維度: frontend）

**缺陷與觸發路徑**: selectChatChar 先同步設 CHAT_ACTIVE_CHAR=charId 再 await 兩個請求（L1279-1281），響應回來後無條件寫 CHAT_ACTIVE_REC/CHAT_SESSION_ID/CHAT_MESSAGES，不校驗 charId 是否仍等於 CHAT_ACTIVE_CHAR。觸發路徑：點角色 A → 立刻點角色 B → B 的響應先回、A 的後回 → 介面標題/訊息/CHAT_SESSION_ID 全變成 A 的，但 CHAT_ACTIVE_CHAR=B 且列表高亮 B → 此時發訊息，POST /api/chat 帶 {char_id: B, session_id: A 的會話} → 訊息寫錯會話/報錯，且使用者看到的是 A 的聊天介面。

**驗證證據**: 證實。web/app.js selectChatChar(L1270-1313) 在 await 前同步寫 CHAT_ACTIVE_CHAR=charId（L1272），await Promise.all 兩個請求（L1279-1282）後無任何 `charId === CHAT_ACTIVE_CHAR` 的過期校驗，直接寫 CHAT_ACTIVE_REC(L1283)、chatTitle(L1286)、CHAT_SESSION_ID/CHAT_MESSAGES(L1298-1300)。觸發鏈：(1) 點角色 A → 發起 A 的兩個請求；(2) A 未返回時點角色 B → CHAT_ACTIVE_CHAR=B，高亮 B，發起 B 的請求；(3) B 響應先回、A 後回——完全現實：兩組請求獨立，且 app/storage.py load_json 在 arca 接管開啟時會遠端回源（網路級延遲差），A/B 完成順序不保證；(4) A 的遲到響應無條件覆蓋：UI 標題/訊息/CHAT_SESSION_ID 全變成 A 的，但 CHAT_ACTIVE_CHAR 仍為 B、列表高亮 B——介面與狀態分裂；(5) 使用者在看似 A 的聊天介面發訊息，sendChat(L1427-1429) POST {char_id: B, session_id: A 的 session_id}；(6) 後端 app/chat.py:471 _load_session(B, A_session) 因會話檔案按 char_id 目錄存放（L197-198）而查不到 → 返回 None → L473 靜默為 B 新建會話，用 B 的人設回覆本該發給 A 的訊息並持久化到 B 名下；前端隨後用返回的 B 會話覆蓋 CHAT_SESSION_ID/CHAT_MESSAGES（L1433-1434），A 的歷史介面突變為 B 的新會話。訊息路由到錯誤角色、狀態錯亂，屬真實正確性 bug；無既有資料被破壞且可自行恢復，故 major 而非 critical。

## [25] MAJOR — loadLandingHistory / loadLatestIg 競態：慢響應把前一個角色的內容渲染到當前角色名下
**位置**: `web/app.js:1713`（維度: frontend）

**缺陷與觸發路徑**: 兩個函式都是先改全域性啟用角色再發請求，響應回來不校驗是否仍是當前角色。落地頁觸發路徑：點角色 A 卡片 → 立刻點角色 B → A 的 /api/landing/A 響應後到 → ldCurrentHtml 被寫成 A 的 HTML 而 LD_ACTIVE_CHAR=B → 點「生成」（isEdit=true）→ A 的 HTML 被當作 current_html 迭代後儲存為 B 的落地頁（資料被錯誤覆蓋，與 L1682 缺陷不同路徑同後果）。IG 側（loadLatestIg L1030-1045）同樣：IG_ACTIVE_CHAR=B 時展示的是 A 的帖子，「刪除/重繪」按鈕會對 char B + A 的 post_id 發請求（404 報錯）。

**驗證證據**: 證實。loadLandingHistory (web/app.js:1713-1729) 在 await api("/api/landing/"+charId) 返回後不校驗 charId 是否仍等於 LD_ACTIVE_CHAR，直接寫全域性 ldCurrentHtml；api() (app.js:15) 無 abort/代數守衛。觸發鏈：點角色 A（有落地頁，響應大而慢）→ 立刻點角色 B（無落地頁，/api/landing/B 返回 {} 極快）→ B 響應先到置空 → A 響應後到把 ldCurrentHtml 寫成 A 的 HTML，此時 LD_ACTIVE_CHAR=B、卡片高亮 B 但預覽顯示 A 的頁面 → 點「生成」(app.js:1682-1696)：charId=B、isEdit=true、current_html=A 的 HTML → 後端 generate_landing (pipeline.py:900-986) 基於 A 的 HTML 迭代並 storage.save_json 覆蓋寫入 B 的 landing_latest.json（"單角色只保留最新一份，重新生成直接覆蓋"），B 的落地頁被 A 派生內容靜默汙染。GET 端點為 sync def 走 FastAPI 執行緒池併發 + 瀏覽器多連線，大小響應亂序完全現實。IG 側 loadLatestIg (app.js:1030-1045) 同構：await 後無校驗，A 的慢響應會把 A 的帖子渲染在 IG_ACTIVE_CHAR=B 名下；刪除/重繪 (app.js:1177/1197) 用 B + A 的 post_id 請求，post_id 含 uuid (pipeline.py:23-24) 不會誤刪他人資料，後端拋 not found → 404 報錯，屬錯誤展示+操作失敗。

## [26] MINOR — _source_image_is_referenced 只 glob 本地 personas 目錄，與遠端主存脫節，可能誤刪仍被引用的共享上傳圖
**位置**: `app/pipeline.py:294`（維度: pipeline）

**缺陷與觸發路徑**: storage 接管後本地 personas 只是快取、可能不全(設計上支援清快取後按需回源)。delete_character 判斷共享上傳圖是否還被引用時(第567-579行)僅掃本地 config.PERSONA_DIR。觸發路徑：清空 data/personas 後只訪問過角色A詳情頁(load_character 只回源快取了A)，同組共享同一上傳源圖的其它語言變體B僅存在於遠端 → 刪除A時本地 glob 找不到任何引用 → 共享上傳圖被 unlink；之後B被回源使用時 _first_source_image 判定源圖不存在，identity/cover 靜默失去視覺錨點(源圖雖在 OSS，但如上一條所述 pipeline 不會 ensure_file 拉回)。

**驗證證據**: 證實：pipeline.py:294-303 的 _source_image_is_referenced 僅 glob 本地 PERSONA_DIR，而 storage.py:1-9 明確本地只是快取（list_json 遠端合併 limit=500 無分頁、query 異常靜默退化純本地 storage.py:80-82）。delete_character 在 pipeline.py:527 先 unlink 被刪角色自身 json 再跑守衛，同函式 537-541 行刪 post_batches/chats 時查了遠端、唯獨此守衛沒查。觸發鏈：arca 接管開啟，清空 data/personas 快取但 uploads 仍在（設計明示本地可清），列表載入時遠端 query 瞬時失敗或 >500 截斷導致同組變體 B 未回寫本地 → 刪除 A 時守衛找不到 B → 共享上傳源圖被 unlink（567-579 行）→ B 回源後 _existing_source_images(pipeline.py:31-39) 只做裸 Path.exists() 無 OSS 回源，build_identity(595)/cover(621)/regenerate(144 等) 靜默丟失視覺錨點。另有獨立路徑：B.json 因非原子寫損壞時 299-300 行 continue 視為不引用，純本地模式也觸發。定級 minor 的理由：OSS 副本不被刪（無任何 OSS delete），/upload 路由 ensure_file 可偶然自愈；正常 UI 刪除必經列表頁會把 B 回寫快取，故需疊加邊緣狀態（query 失敗/>500/損壞檔案/直調 API）才觸發。

## [27] MINOR — delete_character 級聯刪除不含 OSS：圖片/上傳件永久殘留，且可被 /img 路由復活
**位置**: `app/pipeline.py:546`（維度: pipeline）

**缺陷與觸發路徑**: delete_character 刪了本地 images/posts/landing/chat 和遠端 JSON 記錄，但 api_client.generate_image、server 上傳都透過 storage.save_file 雙寫到了 OSS(creaction-data/images/…、creaction-data/uploads/…)，刪除流程沒有任何 OSS 物件刪除。觸發路徑：刪除角色後其所有封面/帖子圖在 OSS 永久堆積(資源洩漏)；且任何持有舊檔名的引用(如匯出的舊 landing、瀏覽器快取的 /img/<name> URL)再次訪問時 server.py:696 的 ensure_file 會把已刪角色的圖片從 OSS 拉回本地 data/images，本地刪除被逆轉，與『刪除角色及所有歸屬資料』語義矛盾。

**驗證證據**: 證實。鏈條完整：(1) api_client.py:263 生成圖經 storage.save_file 雙寫本地+OSS（storage.py:117-127，key=creaction-data/images/…），上傳件同理；(2) pipeline.py:516-581 delete_character 只刪本地檔案和 arca JSON 記錄（delete_record on personas/ig_batches/landings/post_batches/chats），全倉 grep 確認 app/ 下不存在任何 OSS 物件刪除程式碼（無 delete_object/tos_delete）；(3) server.py:693-698 GET /img/{name} 本地缺失時 storage.ensure_file(storage.py:212-236) 從 OSS get_object 拉回並 write_bytes 回寫 data/images。觸發鏈：配置 ARCA_STORAGE_KEY → 生成角色 → DELETE /api/characters → OSS 中該角色全部圖片/上傳件永久殘留（無任何回收路徑）；隨後任何持舊檔名的請求（匯出 landing 裡的 /img/<char_id>_cover.png、快取 URL）返回 200 並把已刪圖片重新落盤本地，違反「刪除角色及所有歸屬資料」語義。限定：persona 記錄本地+遠端均已刪，角色不會在 UI 復活，復活的只是孤兒圖片，故非 major。

## [28] MINOR — /img、/upload 的 OSS 回源與 FileResponse 之間存在寫讀競態，併發請求會拿到截斷檔案
**位置**: `app/server.py:696`（維度: server）

**缺陷與觸發路徑**: storage.ensure_file 在本地缺失時從 OSS 拉取後用非原子的 local.write_bytes 落盤；exists() 在寫入中途即返回 True。觸發路徑：冷啟動/換機後同一封面圖在頁面上出現兩處（角色列表+詳情），瀏覽器併發發出兩個 GET /img/xxx.png：請求 A 進入 write_bytes 寫入中，請求 B 的 ensure_file 看到 local.exists()=True 直接 FileResponse → stat 到的是半截檔案，按當時大小傳送 → 客戶端收到截斷圖片（瀏覽器顯示破圖）；若 A 尚未寫入任何位元組則 B 返回 0 位元組 200。

**驗證證據**: 證實。app/server.py:693-706 兩個端點為同步 def，FastAPI 線上程池並行執行；app/storage.py:212-236 ensure_file 用 local.exists() 判斷後以非原子的 local.write_bytes(data)（open('wb') 截斷/建立 + write，無 tmp+rename、無鎖）落盤。觸發鏈一（寫入視窗）：冷啟動本地缺圖且 arca 啟用，同一檔案兩個併發 GET，請求1進入 write_bytes 剛 open('wb') 建立檔案時，請求2的 exists() 返回 True 直接 FileResponse——實測安裝版 starlette FileResponse 在傳送時才 os.stat 並以 st_size 設 Content-Length，stat 到 0/半截檔案即以 200 返回空/截斷圖片。觸發鏈二（視窗更寬）：兩請求都在對方寫盤前透過 exists()==False，各自進行秒級 OSS 下載；請求1寫完開始按完整大小流式傳送時，請求2的 write_bytes open('wb') 將檔案截斷為 0，請求1短讀致實發位元組 < Content-Length，uvicorn/h11 連線異常、客戶端破圖。定 minor：磁碟最終內容完整（兩次寫同一資料），損壞僅為瞬時響應層面，重新整理即恢復，且只發生在冷啟動回源視窗。

## [29] MINOR — _latest_session 遠端回退按 created_at 排序，取到的不是最近活躍會話
**位置**: `app/chat.py:416`（維度: periphery）

**缺陷與觸發路徑**: 本地為空時 query_records(order_by="created_at", desc=True, limit=1) 取的是『建立最晚』的記錄，而 put_record 是 upsert，會話每次 send_message 只更新 updated_at 不變 created_at。觸發路徑：角色有會話 A（day1 建立）和 B（day2 建立，客戶端未帶 session_id 時會新建會話所以多會話是常態），使用者 day3 繼續在 A 裡聊天；換機/清空本地快取後 GET /api/chat/{char_id}/latest → 返回 B（created_at 最大），使用者最近的對話 A『消失』，繼續輸入會寫進已廢棄的 B。arca_storage.query_records 的行裡本就帶 updated_at，應按 updated_at 排序（這也與本地分支按 mtime=最後寫入時間的語義一致）。

**驗證證據**: 證實。觸發鏈完整：(1) 後端 record.go upsert 為 `ON CONFLICT ... DO UPDATE SET data=EXCLUDED.data, updated_at=NOW()`，created_at 永遠停在會話首存時間；buildOrderClause 中 order_by="created_at" 按行列排序，故 chat.py:414-416 的遠端回退取的是"建立最晚"而非"最近活躍"的會話。(2) 多會話是常態：web/app.js forceNew(新對話按鈕) 置空 session_id → send_message 走 _new_session。(3) 活躍序與建立序倒掛可自然發生：舊標籤頁仍持有 session A 的 id，另一標籤頁新建 B 後使用者回到舊標籤頁繼續聊 A → 遠端 A(created早,updated晚)、B(created晚,updated早)。(4) 換機/清空 data/chats/（storage.py 註明該回退就是為"換機/冷啟動自動恢復"設計）後 GET /api/chat/{char_id}/latest 返回 B，使用者最近對話 A 不可見，後續訊息全部寫入已廢棄的 B。與本地分支按 mtime(最後寫入) 的語義相悖。附註：候選建議的 order_by="updated_at" 修法本身不成立——buildOrderClause 會將其對映為 data->>'updated_at'（恆 NULL），應改用 data 內欄位 "updated" 或後端增列支援。定級 minor：需要多會話+活躍序倒掛+本地快取為空三重條件，後果是恢復路徑呈現/續寫錯誤會話，遠端資料本身未損壞。

## [30] MINOR — _parse_json 去圍欄用 split("```", 2) 會在正文內部的 ``` 處截斷，合法 JSON 解析失敗
**位置**: `app/api_client.py:127`（維度: periphery）

**缺陷與觸發路徑**: 當模型輸出以 ```json 開頭且 JSON 字串值內部含有字面 ```（chat 的 html_file/text 訊息裡角色分享含 markdown 程式碼塊的備忘錄、教程內容時完全可能出現），split("```", 2) 的 t[1] 在第一個內部 ``` 處被截斷，json.loads 失敗後的兜底 find/rfind 也只作用於截斷後的 body → 本來合法的輸出被判『模型未返回合法 JSON 陣列』，send_message 直接拋 ValueError 報 500。正確做法應剝離首行圍欄並從末尾 rsplit 收尾圍欄。

**驗證證據**: 已實證確認。觸發鏈：(1) POST /api/chat → server.py:622 → chat.send_message → api_client.parse_json_text (chat.py:490)。(2) 當模型把 JSON 陣列包在 ```json ... ``` 圍欄裡（這正是 _parse_json 存在的原因，chat prompt 第179行"markdown 코드블록 전부 금지"恰說明這是已知失敗模式），且某個 text/html_file 訊息的字串值內含字面 ```（如角色分享含程式碼塊的備忘錄/教程，反引號在 JSON 字串裡無需轉義、原樣出現），api_client.py:127 的 t.split("```", 2) 使 t[1] 在第一個內部 ``` 處截斷，JSON 字串被攔腰截斷。(3) json.loads 失敗後，第140-146行的 find/rfind 兜底只作用於已截斷的 body，同樣失敗。用 ./.venv/bin/python 實測：содержимое '```json\n[{"type":"text","data":{"content":"메모:\n```python\nprint(1)\n```"}}]\n```' 拋 JSONDecodeError "Unterminated string"；多元素變體拋 "Expecting ',' delimiter"（兜底切片也救不回）；同樣內容不帶圍欄則解析成功——證明是圍欄剝離邏輯而非內容本身的問題。(4) chat.py:493 轉成 ValueError，server.py:620-627 無異常處理 → HTTP 500，且會話（含使用者訊息）未儲存。同一函式還服務 pipeline.py 的 chat_json（persona/帖子生成），同理可觸發。正確做法是剝首行圍欄後從尾部 rsplit 收尾圍欄。定級 minor：需要"模型違規加圍欄"與"字串值內含 ```"兩個資料依賴條件同時成立，影響限於單次請求失敗（可重試），無資料損壞。

## [31] MINOR — 角色名等 LLM 可控文字未轉義直接拼入 innerHTML，可注入 HTML/指令碼
**位置**: `web/app.js:398`（維度: frontend）

**缺陷與觸發路徑**: renderCharList(L397-400)、showCharDetail 的 h3(L755)、renderPostCharOptions 的 <option>(L849)、renderIgCharGrid(L1011)、renderLdCharGrid(L1530) 都把 c.name / localized(p.name) 不經 escapeHtml 直接模板拼進 innerHTML；同類還有 renderPosts/renderIgPosts 的 p.image.error(L903, L1129) 和 loadLandingHistory 的 page.style_text(L1722)。角色名來自 LLM 生成或使用者匯入的角色 JSON（/api/personas/import_json 接受任意 JSON 檔案）。觸發路徑：匯入一個 name 為 "<img src=x onerror=alert(1)>" 的角色 JSON → 開啟「② 角色」檢視 → onerror 指令碼執行；即使只是名字裡帶 < 或 >（LLM 輸出並不罕見）也會破壞卡片結構，勾選框/刪除按鈕錯位失效。對比 chat 檢視（L1256）是做了 escapeHtml 的，說明其它檢視屬遺漏。

**驗證證據**: 證實。渲染側：web/app.js:397-400 renderCharList 將 c.name 未經 escapeHtml 拼入 card.innerHTML（同類遺漏見 L755/L849/L1011/L1530/L903/L1129/L1722），而同檔案 chat 檢視 L1251/L1256/L1286 對同一欄位做了 escapeHtml（L814 定義），證明轉義是既有約定、此處屬遺漏。資料側：app/pipeline.py:54-73 list_characters 把 persona.name 原樣返回 /api/characters；persona.name 由 LLM chat_json 輸出原樣存檔（pipeline.py:207-218），且 server.py:340-345 PUT /api/persona 接受任意 persona JSON 原樣覆蓋，是零 LLM 的確定性寫入路徑。觸發鏈：PUT /api/persona 設 name="<img src=x onerror=alert(1)>"（或匯入 JSON 經 LLM 本地化保留尖括號）→ 開啟「② 角色」檢視 → innerHTML 注入 → onerror 執行；名字僅含 "<字母" 也會吞掉後續 </div> 結構，導致卡片標籤/刪除按鈕錯位失效，屬可觀察錯誤行為。因是 localhost 單使用者工具、注入源限於本機與 LLM 輸出，實際影響以 UI 結構損壞為主，故 minor。

## [32] MINOR — arcaDeleteOne 成功路徑不恢復按鈕，依賴未 await 的 loadCharacters；重新整理失敗則按鈕永久卡死
**位置**: `web/app.js:624`（維度: frontend）

**缺陷與觸發路徑**: arcaDeleteOne 把 btn.disabled=true、textContent="…"（L611-612），只有 catch 分支恢復（L627-628）；成功分支（含 r.errors 非空的邏輯失敗分支）完全依賴 fire-and-forget 的 loadCharacters()（L624）重建卡片來"恢復"。觸發路徑：刪除請求成功後 GET /api/characters 或 /api/styles 失敗（服務重啟中/瞬時網路故障）→ loadCharacters 的 rejection 無人捕獲（unhandled rejection），列表不重新整理 → 該卡片按鈕永遠停在禁用的"…"，且「☁️ 已同步」徽標仍顯示已同步的過期狀態，使用者無法再從卡片操作。

**驗證證據**: 證實。arcaDeleteOne（web/app.js:607-630）在 L610-611 置 btn.disabled=true、textContent="…"，成功分支（含 r.errors 非空分支）無任何按鈕恢復程式碼，僅 L624 fire-and-forget 呼叫 loadCharacters()（無 await 無 .catch）；loadCharacters（L357-368）內 await api("/api/characters") 與 ensureStyles() 的 api("/api/styles")，api()（L15-23）在 !res.ok 或網路錯誤時 reject；全檔案無 unhandledrejection/onerror 兜底。觸發鏈：點 ☁️🗑 → /api/arca/delete 任務成功 → 隨後 GET /api/characters（或首次 GET /api/styles）因服務重啟/瞬時網路故障失敗 → loadCharacters rejection 無人捕獲、renderCharList 不執行 → 按鈕永久禁用停在"…"，「☁️ 已同步」徽標保留過期狀態，且無任何失敗提示（此前已彈成功 toast），只能整頁重新整理恢復。同檔案其他按鈕（L601-604、L658-659）均用 finally 恢復，此處為確切遺漏。限定條件：需瞬時故障視窗、服務端刪除已成功、無資料損壞、重新整理可恢復，故定 minor。

## [33] MINOR — renderThumbs 每次重渲染都為全部待傳圖片新建 ObjectURL 且從不 revoke，Blob 記憶體洩漏
**位置**: `web/app.js:270`（維度: frontend）

**缺陷與觸發路徑**: renderThumbs 對 pendingFiles 裡每個檔案呼叫 URL.createObjectURL(f)（L270），而 addFiles 每次追加檔案都會整體重渲染縮圖，舊的 blob URL 既沒有複用也沒有 URL.revokeObjectURL；上傳成功/失敗後 renderThumbs() 清空 DOM 時同樣不回收。觸發路徑：連續貼上/拖入 N 批圖片（每批觸發一次全量重建）→ 產生 O(N²) 個指向圖片 Blob 的 URL 常駐記憶體，頁面長開（該工具是常駐工作臺）記憶體持續增長，大圖場景可達數 GB 直至標籤頁崩潰。

**驗證證據**: 證實：web/app.js L270 每次 renderThumbs 為 pendingFiles 全量新建 blob URL，全檔案僅 L504-511 匯出路徑有 revokeObjectURL；addFiles（L257-263）每批追加都整體重建，上傳成功後 L343-344 清空 pendingFiles 再 renderThumbs 也只清 DOM。按 File API 規範，未 revoke 的 blob URL 會把底層 Blob 釘在 URL store 直到頁面 unload，而貼上路徑（L196-217，getAsFile，註釋明說不落盤）產生的是記憶體態 Blob。觸發鏈：上傳檢視連續 Cmd+V 貼上截圖並上傳 → 會話內所有貼上過的圖片位元組永不釋放，長開頁面記憶體單調增長，僅重新整理可恢復。但候選的量級描述誇大：同一檔案的重複 URL 只是小登入檔條目不復製圖片位元組，洩漏量是 O(貼上圖片總位元組) 而非 O(N²) 份資料，磁碟選擇的 File 為磁碟背書；不會導致資料損壞，通常也到不了標籤頁崩潰。
