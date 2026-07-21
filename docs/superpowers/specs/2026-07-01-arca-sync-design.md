# creaction → arca-i18n 資料同步 設計檔案

日期：2026-07-01
狀態：待評審

## 1. 目標與邊界

- **模式**：本地生成/落盤邏輯**保持不變**；新增一條**單向推送**能力，把 creaction 產出的資料同步進 arca-i18n（真產品後端）。
- **觸發**：手動按鈕（使用者選中角色後點「匯入POPOP」）。
- **同步實體**：① 人設 + 外貌/封面；② INS 帖子；③ 落地頁。**不含**聊天、畫風庫、語音。
- **本地檔案仍是事實源與快取**，同步只讀本地、寫 arca；arca 返回的 id 回寫本地用於去重/冪等。

### 關鍵決策（已確認）
1. **落地頁**：把本地自包含 HTML 上傳到 arca 的 **TOS 公有桶**，用返回的 CDN 直鏈作為 `landing_page_url`（arca 無「上傳 HTML 本體」介面，只存 URL 且需公網可達）。
2. **鑑權**：預設用 arca 共享金鑰 `Auth.AccessSecret` **本地 PyJWT 自籤**（HS256，claim `{uid, exp}`）；config 可切換為調內網 `/internal/tool/gen_jwt_token`。
3. **歸屬使用者**：固定 `uid`（環境變數 `ARCA_UID`）；所有同步角色掛其名下。建角色會凍結錢包額度，該 uid 需有額度或目標環境錢包未掛載(local/dev 放行)。

## 2. arca 接入合同（均已讀 arca-i18n 真倉實現核實）

倉庫：`/Users/wangcheng/go_repo/src/github.com/Dreamelse-AI/arca-i18n`

| 環節 | 介面 | 語義（已核實） |
|---|---|---|
| 取 token | `POST /internal/tool/gen_jwt_token {uid, expires_in?}` | 真實實現（`internal/logic/auth/…` → `biz_common.InternalToolGenJwtToken` → `utils.GenerateToken(AccessSecret, expire, uid, did)`）。HS256 籤 `{uid,exp}`，返回 `{jwt_token, help_header:"Bearer <token>", expires_in}`。**/internal 內網路徑**。金鑰明文見 `etc/arca-api*.yaml` 的 `Auth.AccessSecret`，本地可自籤復刻。 |
| 傳圖/HTML | `POST /file/tos_credential {expires_in?, use_public?}` | 返回 STS 臨時憑證 `{access_key_id, secret_access_key, session_token, bucket, region, endpoint, expires_in}`。客戶端用 TOS/S3 相容 SDK PUT 上傳，自定 `object_key`，拼 `StorageObject{bucket_name, object_key, object_type, url}`。落地頁 HTML 傳公有桶（`use_public:true`）。 |
| 建角色 | `POST /character/create` → `AsyncTaskSubmitResp{task_id}` | **非同步**，需 `Authorization: Bearer <jwt>`。輪詢 `POST /task/get_status {task_id}` → `GetTaskStatusResp{status, result, error_message,…}`，`status ∈ processing/ready/failed`；`ready` 後 `result` 是 `CreateCharacterResp` 的 JSON 串，取 `character_id`。**會 `FreezeQuota(SceneCreateCharacter)`**（額度不足 → 40402；local/錢包未掛載放行），走 `AuditRequestByConfig("CreateCharacter")`（稽核碼 40001/40002/50001/50002，僅 Env≠local 且配置了 moderation 時生效），支援 `idempotency-key` 冪等（Redis 快取結果）。 |
| 外貌(可選) | `POST /character/create_appearance` → `{outfit_id, appearance_id}` | **同步**。需 `character_id` + `image(StorageObject)` + `appearance_name`；`outfit_id` 可選（不傳=新皮膚）。⚠️ `gen_appearance` 是**樁**（`todo`+裸`return`），跳過。 |
| 帖子 | `POST /post/create` → `{post_id}` | **同步**，`動態模組`，需 JWT。欄位：`character_id`(opt)、`title`(opt)、`content`(opt 單串)、`images[]`(UserUploadImage，先傳 TOS)、`visibility`(**必填** options=1公開\|2好友\|3私密)。走 `AuditRequestByConfig("CreatePost")`。**建帖即釋出**：impl `internal/impl/post/user.go` 寫 `Status: PostStatusPublished`「無草稿箱，直接釋出」，**無需再調 /post/publish**（arca.api 中「還需 /post/publish」註釋已過時，且 `/post/publish` 未註冊為路由——以實現為準）。 |
| 落地頁 | 無獨立寫介面 | arca 僅在建角色時把 `character_create_form.landing_page_url` 字串**原樣落庫**（不校驗域名/可達性）到 `character_version.landing_page_urls`。`/landing_data/*` 只讀、HMAC 驗籤。 |

### 請求體要點
`CreateCharacterReq{ character_create_form: CharacterCreateForm, source(必填, options=character|friend|system) }`

`CharacterCreateForm`（全 optional，除 source）：
`images[]*UserUploadImage`、`name*`、`tags[]`、`species*`、`gender*(options=male|female|other)`、`voice_id*`、`profile*`、`disposition*`、`anonymous_tags[]`、`visibility*(public|private)`、`customized_settings map[string]string`、`opening_prologue[]`、`landing_page_style*`、`landing_page_url*`、`creators_note*`

`UserUploadImage{ name?, image_type(options=aigc|upload), media: StorageObject, is_main_pic?, tags[]? }`

`StorageObject{ bucket_name, object_key, object_type(video|image|音訊), request_id?, url?, description? }`

## 3. 欄位對映（creaction → arca）

### 3.1 人設（角色 record → CharacterCreateForm）
| creaction | arca 欄位 | 備註 |
|---|---|---|
| `persona.name` | `name` | |
| persona 性別 | `gender` | 歸一到 `male\|female\|other` |
| persona 物種/species | `species` | 預設可空 |
| persona 簡介/一句話 | `profile` | |
| persona 性格 | `disposition` | |
| 角色開場白 | `opening_prologue[]` | 每句一條；`opening.messages` 元素是 `{type,data:{content}}` 物件，取 `data.content` 作 `text`（非序列化整個物件），`type=voice` → `output_type=tts`，否則 `text`。 |
| 四語言 schema 中 arca 無對應位的欄位 | `customized_settings` map | 保真承載，key 用穩定命名 |
| `record.cover`（封面圖，TOS 上傳後） | `images[]`（`is_main_pic:true, image_type:aigc`） | 建角色時一併帶入，**無需**單獨 create_appearance |
| 落地頁 CDN URL | `landing_page_url` | 見 3.3 |
| — | `source` | 固定 `"character"` |

> creaction 是「每語言一個獨立角色」，故一個 arca 角色天然對應一種語言的人設。

### 3.2 INS 帖子（batch.posts[] → /post/create）
| creaction | arca |
|---|---|
| `post.content[lang]`（該角色語言） | `content` |
| `post.image`（TOS 上傳後） | `images[].media`（`image_type:aigc`） |
| — | `character_id`（上一步建角色返回） |
| — | `visibility`（預設 1 公開，可配） |

### 3.3 落地頁（landing_latest.json → landing_page_url）
- 取 `html_filled`（cover/post 圖已內聯的自包含 HTML）。
- `/file/tos_credential {use_public:true}` → PUT 到公有桶（object_key 如 `landing/{char_id}/{page_id}.html`，content-type text/html）→ 得 CDN URL。
- 該 URL 作為建角色的 `landing_page_url` 一併提交。

## 4. creaction 側程式碼結構（隔離，不改現有生成邏輯）

- **新增 `app/arca_client.py`** — 純 arca HTTP 客戶端：
  - `get_token()`：按 `ARCA_JWT_MODE`（`local`|`endpoint`）取 JWT；local 用 PyJWT + `ARCA_ACCESS_SECRET` 籤 `{uid,exp}`。
  - `_headers()`：`Authorization: Bearer`、`X-Language`、`X-Region`、`X-App-Version`（佔位可配）。
  - `tos_upload(bytes, key, content_type, public)`：調 `/file/tos_credential` + TOS SDK PUT，返回 `StorageObject`。
  - `create_character(form)`：POST + 輪詢 `/task/get_status` 到終態，返回 `character_id`（復刻現有 `api_client.poll_task` 模式）。
  - `create_post(...)` / `create_appearance(...)`：同步呼叫。
- **新增 `app/arca_sync.py`** — 編排單角色同步：
  1. 冪等檢查（record 已有 `arca_character_id` 則跳過建角色，除非 `force`）。
  2. 上傳封面 → 上傳落地頁 HTML（若選同步）→ 建角色 → 拿 `character_id`。
  3. 遍歷最近帖子批次：逐條上傳配圖 → `create_post` → 記 `arca_post_id`。
  4. 把 `arca_character_id` / 各 `arca_post_id` 回寫本地 record 與 batch（去重依據）。
- **`app/config.py`** — 新增環境變數（全部留佔位預設）：
  `ARCA_BASE_URL`、`ARCA_UID`、`ARCA_JWT_MODE`(local|endpoint)、`ARCA_ACCESS_SECRET`、`ARCA_JWT_EXPIRES`、`ARCA_REGION`、`ARCA_APP_VERSION`、`ARCA_POST_VISIBILITY`、`ARCA_SYNC_LANDING`(bool)。
- **`app/server.py`** — 新增 `POST /api/arca/sync {char_ids, options}`：用現有 `tasks.py` 後臺任務系統跑，立即返回 `task_id`，前端輪詢 `/api/tasks/{id}` 看進度/逐角色結果。
- **`web/`** — 角色列表加「匯入POPOP」按鈕 + 進度/結果展示，複用現有批次任務 UI 模式。

## 5. 錯誤處理與冪等

- **匹配策略（同名+同建立者為權威）**：同步前先調 `POST /character/list_my_characters`（本人自建列表，天然同建立者）按 persona.name 精確匹配：命中 → 在該角色上原地更新、帖子掛它（本地對映自動換綁，若換了角色則清空舊帖子對映）；未命中且本地有對映 → 視為過期，清對映換代後新建。列表查詢失敗時 fail-open 回退本地對映。本地 `arca_character_id` 僅作快取，不再是一致性依據。
- **建角色**：帶 `idempotency-key`；本地 `arca_character_id` 存在即跳過（除非 force 重推）。
- **帖子**：本地 `post_id → arca_post_id` 對映去重，已同步不重發（`/post/create` 冪等性未確認，靠本地對映防重）。
- **稽核/扣費失敗**（40402 餘額不足 / 40001 等稽核碼）：逐角色、逐帖子把錯誤落到任務結果裡展示，**不中斷整批**（沿用現有 batch-resilient 風格）。
- **token/網路失敗**：整批快速失敗並回顯原因。

## 6. 硬約束與待聯調確認

**已核實（硬約束）**：
- **`ARCA_UID` 必須是 arca 目標環境裡真實存在、未登出的使用者**。全域性中介軟體 `OptionalJwtAuthMiddleware`（`arca.go` `server.Use`）在驗籤+exp 之外，還查庫 `dreamelse."user"` 校驗 `uid` 存在且 `deleted_at IS NULL`（結果 Redis 快取）；編造的 uid（即使簽名正確）會被拒 **HTTP 403「使用者不存在或已登出」**。本地自籤與內網 gen_jwt_token 兩種取 token 方式都受此約束。→ 接入前需先在目標環境備好一個真實 uid。

- **失敗任務也會被冪等快取回放（已核實並踩坑）**：arca 非同步任務框架按 `Idempotency-Key` 快取任務 24h，任務 failed 後同鍵重試只會拿到快取的失敗結果（服務端不再執行、也無新日誌）。客戶端冪等鍵包含「失敗嘗試鹽」`-a{n}`（每次 create 失敗 +1 落盤），重試自動換鍵。

**待聯調確認（不影響架構）**：
- `ARCA_BASE_URL`（各環境）、該 `ARCA_UID` 的錢包額度、`X-Region/X-App-Version` 的強制性與合法值。
- `gender / visibility / customized_settings` 的取值與 arca 展示端的相容。
- TOS 公有桶 PUT 的具體 SDK/簽名細節與 CDN 域名。

## 7. 非目標 / 已知風險

- 不做雙向同步、不做 arca→本地回拉。
- 不同步聊天/畫風/語音。
- （旁註，屬 arca 後端而非本專案）`landing_page_url` 建角色時無任何 URL 校驗、原樣落庫，理論上是儲存型開放重定向面；本設計只寫自家 TOS CDN 鏈，不放大該問題。
