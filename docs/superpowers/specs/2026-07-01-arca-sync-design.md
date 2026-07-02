# creaction → arca-i18n 数据同步 设计文档

日期：2026-07-01
状态：待评审

## 1. 目标与边界

- **模式**：本地生成/落盘逻辑**保持不变**；新增一条**单向推送**能力，把 creaction 产出的数据同步进 arca-i18n（真产品后端）。
- **触发**：手动按钮（用户选中角色后点「导入POPOP」）。
- **同步实体**：① 人设 + 外貌/封面；② INS 帖子；③ 落地页。**不含**聊天、画风库、语音。
- **本地文件仍是事实源与缓存**，同步只读本地、写 arca；arca 返回的 id 回写本地用于去重/幂等。

### 关键决策（已确认）
1. **落地页**：把本地自包含 HTML 上传到 arca 的 **TOS 公有桶**，用返回的 CDN 直链作为 `landing_page_url`（arca 无「上传 HTML 本体」接口，只存 URL 且需公网可达）。
2. **鉴权**：默认用 arca 共享密钥 `Auth.AccessSecret` **本地 PyJWT 自签**（HS256，claim `{uid, exp}`）；config 可切换为调内网 `/internal/tool/gen_jwt_token`。
3. **归属用户**：固定 `uid`（环境变量 `ARCA_UID`）；所有同步角色挂其名下。建角色会冻结钱包额度，该 uid 需有额度或目标环境钱包未挂载(local/dev 放行)。

## 2. arca 接入合同（均已读 arca-i18n 真仓实现核实）

仓库：`/Users/wangcheng/go_repo/src/github.com/Dreamelse-AI/arca-i18n`

| 环节 | 接口 | 语义（已核实） |
|---|---|---|
| 取 token | `POST /internal/tool/gen_jwt_token {uid, expires_in?}` | 真实实现（`internal/logic/auth/…` → `biz_common.InternalToolGenJwtToken` → `utils.GenerateToken(AccessSecret, expire, uid, did)`）。HS256 签 `{uid,exp}`，返回 `{jwt_token, help_header:"Bearer <token>", expires_in}`。**/internal 内网路径**。密钥明文见 `etc/arca-api*.yaml` 的 `Auth.AccessSecret`，本地可自签复刻。 |
| 传图/HTML | `POST /file/tos_credential {expires_in?, use_public?}` | 返回 STS 临时凭证 `{access_key_id, secret_access_key, session_token, bucket, region, endpoint, expires_in}`。客户端用 TOS/S3 兼容 SDK PUT 上传，自定 `object_key`，拼 `StorageObject{bucket_name, object_key, object_type, url}`。落地页 HTML 传公有桶（`use_public:true`）。 |
| 建角色 | `POST /character/create` → `AsyncTaskSubmitResp{task_id}` | **异步**，需 `Authorization: Bearer <jwt>`。轮询 `POST /task/get_status {task_id}` → `GetTaskStatusResp{status, result, error_message,…}`，`status ∈ processing/ready/failed`；`ready` 后 `result` 是 `CreateCharacterResp` 的 JSON 串，取 `character_id`。**会 `FreezeQuota(SceneCreateCharacter)`**（额度不足 → 40402；local/钱包未挂载放行），走 `AuditRequestByConfig("CreateCharacter")`（审核码 40001/40002/50001/50002，仅 Env≠local 且配置了 moderation 时生效），支持 `idempotency-key` 幂等（Redis 缓存结果）。 |
| 外貌(可选) | `POST /character/create_appearance` → `{outfit_id, appearance_id}` | **同步**。需 `character_id` + `image(StorageObject)` + `appearance_name`；`outfit_id` 可选（不传=新皮肤）。⚠️ `gen_appearance` 是**桩**（`todo`+裸`return`），跳过。 |
| 帖子 | `POST /post/create` → `{post_id}` | **同步**，`动态模块`，需 JWT。字段：`character_id`(opt)、`title`(opt)、`content`(opt 单串)、`images[]`(UserUploadImage，先传 TOS)、`visibility`(**必填** options=1公开\|2好友\|3私密)。走 `AuditRequestByConfig("CreatePost")`。**建帖即发布**：impl `internal/impl/post/user.go` 写 `Status: PostStatusPublished`「无草稿箱，直接发布」，**无需再调 /post/publish**（arca.api 中「还需 /post/publish」注释已过时，且 `/post/publish` 未注册为路由——以实现为准）。 |
| 落地页 | 无独立写接口 | arca 仅在建角色时把 `character_create_form.landing_page_url` 字符串**原样落库**（不校验域名/可达性）到 `character_version.landing_page_urls`。`/landing_data/*` 只读、HMAC 验签。 |

### 请求体要点
`CreateCharacterReq{ character_create_form: CharacterCreateForm, source(必填, options=character|friend|system) }`

`CharacterCreateForm`（全 optional，除 source）：
`images[]*UserUploadImage`、`name*`、`tags[]`、`species*`、`gender*(options=male|female|other)`、`voice_id*`、`profile*`、`disposition*`、`anonymous_tags[]`、`visibility*(public|private)`、`customized_settings map[string]string`、`opening_prologue[]`、`landing_page_style*`、`landing_page_url*`、`creators_note*`

`UserUploadImage{ name?, image_type(options=aigc|upload), media: StorageObject, is_main_pic?, tags[]? }`

`StorageObject{ bucket_name, object_key, object_type(video|image|音频), request_id?, url?, description? }`

## 3. 字段映射（creaction → arca）

### 3.1 人设（角色 record → CharacterCreateForm）
| creaction | arca 字段 | 备注 |
|---|---|---|
| `persona.name` | `name` | |
| persona 性别 | `gender` | 归一到 `male\|female\|other` |
| persona 物种/species | `species` | 缺省可空 |
| persona 简介/一句话 | `profile` | |
| persona 性格 | `disposition` | |
| 角色开场白 | `opening_prologue[]` | 每句一条；`opening.messages` 元素是 `{type,data:{content}}` 对象，取 `data.content` 作 `text`（非序列化整个对象），`type=voice` → `output_type=tts`，否则 `text`。 |
| 四语言 schema 中 arca 无对应位的字段 | `customized_settings` map | 保真承载，key 用稳定命名 |
| `record.cover`（封面图，TOS 上传后） | `images[]`（`is_main_pic:true, image_type:aigc`） | 建角色时一并带入，**无需**单独 create_appearance |
| 落地页 CDN URL | `landing_page_url` | 见 3.3 |
| — | `source` | 固定 `"character"` |

> creaction 是「每语言一个独立角色」，故一个 arca 角色天然对应一种语言的人设。

### 3.2 INS 帖子（batch.posts[] → /post/create）
| creaction | arca |
|---|---|
| `post.content[lang]`（该角色语言） | `content` |
| `post.image`（TOS 上传后） | `images[].media`（`image_type:aigc`） |
| — | `character_id`（上一步建角色返回） |
| — | `visibility`（默认 1 公开，可配） |

### 3.3 落地页（landing_latest.json → landing_page_url）
- 取 `html_filled`（cover/post 图已内联的自包含 HTML）。
- `/file/tos_credential {use_public:true}` → PUT 到公有桶（object_key 如 `landing/{char_id}/{page_id}.html`，content-type text/html）→ 得 CDN URL。
- 该 URL 作为建角色的 `landing_page_url` 一并提交。

## 4. creaction 侧代码结构（隔离，不改现有生成逻辑）

- **新增 `app/arca_client.py`** — 纯 arca HTTP 客户端：
  - `get_token()`：按 `ARCA_JWT_MODE`（`local`|`endpoint`）取 JWT；local 用 PyJWT + `ARCA_ACCESS_SECRET` 签 `{uid,exp}`。
  - `_headers()`：`Authorization: Bearer`、`X-Language`、`X-Region`、`X-App-Version`（占位可配）。
  - `tos_upload(bytes, key, content_type, public)`：调 `/file/tos_credential` + TOS SDK PUT，返回 `StorageObject`。
  - `create_character(form)`：POST + 轮询 `/task/get_status` 到终态，返回 `character_id`（复刻现有 `api_client.poll_task` 模式）。
  - `create_post(...)` / `create_appearance(...)`：同步调用。
- **新增 `app/arca_sync.py`** — 编排单角色同步：
  1. 幂等检查（record 已有 `arca_character_id` 则跳过建角色，除非 `force`）。
  2. 上传封面 → 上传落地页 HTML（若选同步）→ 建角色 → 拿 `character_id`。
  3. 遍历最近帖子批次：逐条上传配图 → `create_post` → 记 `arca_post_id`。
  4. 把 `arca_character_id` / 各 `arca_post_id` 回写本地 record 与 batch（去重依据）。
- **`app/config.py`** — 新增环境变量（全部留占位默认）：
  `ARCA_BASE_URL`、`ARCA_UID`、`ARCA_JWT_MODE`(local|endpoint)、`ARCA_ACCESS_SECRET`、`ARCA_JWT_EXPIRES`、`ARCA_REGION`、`ARCA_APP_VERSION`、`ARCA_POST_VISIBILITY`、`ARCA_SYNC_LANDING`(bool)。
- **`app/server.py`** — 新增 `POST /api/arca/sync {char_ids, options}`：用现有 `tasks.py` 后台任务系统跑，立即返回 `task_id`，前端轮询 `/api/tasks/{id}` 看进度/逐角色结果。
- **`web/`** — 角色列表加「导入POPOP」按钮 + 进度/结果展示，复用现有批量任务 UI 模式。

## 5. 错误处理与幂等

- **匹配策略（同名+同创建者为权威）**：同步前先调 `POST /character/list_my_characters`（本人自建列表，天然同创建者）按 persona.name 精确匹配：命中 → 在该角色上原地更新、帖子挂它（本地映射自动换绑，若换了角色则清空旧帖子映射）；未命中且本地有映射 → 视为过期，清映射换代后新建。列表查询失败时 fail-open 回退本地映射。本地 `arca_character_id` 仅作缓存，不再是一致性依据。
- **建角色**：带 `idempotency-key`；本地 `arca_character_id` 存在即跳过（除非 force 重推）。
- **帖子**：本地 `post_id → arca_post_id` 映射去重，已同步不重发（`/post/create` 幂等性未确认，靠本地映射防重）。
- **审核/扣费失败**（40402 余额不足 / 40001 等审核码）：逐角色、逐帖子把错误落到任务结果里展示，**不中断整批**（沿用现有 batch-resilient 风格）。
- **token/网络失败**：整批快速失败并回显原因。

## 6. 硬约束与待联调确认

**已核实（硬约束）**：
- **`ARCA_UID` 必须是 arca 目标环境里真实存在、未注销的用户**。全局中间件 `OptionalJwtAuthMiddleware`（`arca.go` `server.Use`）在验签+exp 之外，还查库 `dreamelse."user"` 校验 `uid` 存在且 `deleted_at IS NULL`（结果 Redis 缓存）；编造的 uid（即使签名正确）会被拒 **HTTP 403「用户不存在或已注销」**。本地自签与内网 gen_jwt_token 两种取 token 方式都受此约束。→ 接入前需先在目标环境备好一个真实 uid。

- **失败任务也会被幂等缓存回放（已核实并踩坑）**：arca 异步任务框架按 `Idempotency-Key` 缓存任务 24h，任务 failed 后同键重试只会拿到缓存的失败结果（服务端不再执行、也无新日志）。客户端幂等键包含「失败尝试盐」`-a{n}`（每次 create 失败 +1 落盘），重试自动换键。

**待联调确认（不影响架构）**：
- `ARCA_BASE_URL`（各环境）、该 `ARCA_UID` 的钱包额度、`X-Region/X-App-Version` 的强制性与合法值。
- `gender / visibility / customized_settings` 的取值与 arca 展示端的兼容。
- TOS 公有桶 PUT 的具体 SDK/签名细节与 CDN 域名。

## 7. 非目标 / 已知风险

- 不做双向同步、不做 arca→本地回拉。
- 不同步聊天/画风/语音。
- （旁注，属 arca 后端而非本项目）`landing_page_url` 建角色时无任何 URL 校验、原样落库，理论上是存储型开放重定向面；本设计只写自家 TOS CDN 链，不放大该问题。
