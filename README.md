# POPOP 角色生产链路

批量上传图片 → 调模型扩写人设 schema（中/日/韩/英）→ 反写外貌 prompt → 选画风重绘封面图 → 勾选帖子类型批量生产帖子（图文，4 语言）+ 配图。

## 链路

```
图片
 └─(gemini-3.1-pro 视觉)→ 人设 schema（4 语言）
       └─(反写)→ 外貌 identity（固定外貌 DNA，韩文键值）
             ├─ identity + 画风 → 重绘封面图（gpt-image-2，图生图）
             └─ 批量帖子：
                  角色 + 勾选帖子类型
                   └─→ content（中/日/韩/英）+ variable + scene（每条不同）
                         └─ identity + variable + scene + 画风 → 配图
```

- **LLM**：`gemini-3.1-pro-preview`（视觉 + 文本）
- **生图**：`gpt-image-2`（异步任务，支持图生图保持人物一致性）
- **网关**：APIMart (`https://api.apimart.ai/v1`)，OpenAI 兼容

## 运行

```bash
cd popop_pipeline
bash run.sh
# 浏览器打开 http://127.0.0.1:8077
```

首次运行会自动安装 `requirements.txt` 依赖。

## 界面

1. **批量上传 / 生成人设**：拖拽多图，可填创作补充要求；默认每张图各生成一个角色。
2. **角色 / 人设 / 封面**：查看人设（4 语言）、完整 JSON、外貌 identity；选画风重绘封面图。
3. **生成 INS 帖子（9 条）**：选角色 + 画风，一键推断 TA 最近会发的 9 条 ins 帖子。
4. **帖子类型生产**：按 Excel 的 8 类帖子类型批量生成（孔雀开屏/展示生活/伪媒体…）。
5. **画风库**：粘贴 JSON 覆盖画风词（数组，元素含 `id` / `name` / `prompt`）。

## INS 帖子链路（核心）

每个角色生成 9 条「最近的 ins」，由模型根据兴趣/生活方式/在意的东西推断，文案是角色本人语气（4 语言）：

```
人设 → 推断最近 9 条 ins
  每条:
   ├─ content（中/日/韩/英，角色语气）
   ├─ format: image_text | text_only
   └─ image_text 时:
        ├─ image_type=selfie → 用 selfie schema 出 prompt
        │     拼入【重绘封面】做图生图（image-2，保持人脸一致）
        └─ image_type=photo  → photo_prompt 直接文生图（人不出镜）
```

- **selfie schema**：style(固定写实自拍风) / variable / shooting(含 capture_mode、filter、shot_size、angle…) / scene。
- **photo**：食物、风景、物件、宠物、手部特写等，贴合角色品味，无人脸。
- **text_only**：金句、碎碎念、提问等，不出图。

## 帖子类型生产（来源：POPOP-帖子类型及数据来源.xlsx）

| id | 类型 | 数据目标 |
|----|------|---------|
| peacock | 孔雀开屏·表达交友诉求 | 吸引用户聊天 |
| life | 展示角色生活 | 吸引用户聊天 |
| media | 伪·媒体/广告/商单/活动出席 | 吸引用户聊天 |
| forum | 角色剧情/论坛体 | 吸引用户聊天 |
| about_user_public | 角色公开发关于用户帖 | 用户聊天后反馈 |
| about_user_anon | 角色匿名发关于用户帖 | 用户聊天后反馈 |
| media_feedback | 伪·媒体·心动指令 | 用户聊天后反馈 |
| daily_topic | 生活主题帖（宠物/美食/旅游…） | 角色生活 |

## 外貌 schema（identity / variable / scene）

- `identity`：固定外貌 DNA，所有图片保持一致（生成人设后反写一次）。
- `variable`：每条帖子的当天可变外貌（表情、妆容、发型、穿搭、姿势…）。
- `scene`：每条帖子的场景与镜头（活动、地点、道具、时间、camera）。

最终生图 prompt = `identity + variable + scene`（平铺）+ 画风 prompt。

## 目录

```
popop_pipeline/
├─ app/
│  ├─ config.py       # 密钥 / 模型 / 路径 / 语言
│  ├─ api_client.py   # APIMart 客户端（chat 视觉 + 生图轮询）
│  ├─ prompts.py      # 人设/外貌/帖子 prompt + 帖子类型表 + schema
│  ├─ styles.py       # 画风库（占位，待补词）
│  ├─ pipeline.py     # 编排 + 落盘
│  └─ server.py       # FastAPI
├─ web/               # 单页前端
├─ data/              # uploads / personas / posts / images / styles.json
├─ requirements.txt
└─ run.sh
```

## 配置

密钥与模型在 `app/config.py`，也可用环境变量覆盖：
`POPOP_API_KEY` / `POPOP_LLM_MODEL` / `POPOP_IMAGE_MODEL` / `POPOP_MAX_WORKERS`。

## 说明

- 人设扩写较慢（大 schema + 4 语言推理，单角色约 1-2 分钟）；生图单张约 1-2 分钟，批量已并发处理。
- 所有 API key 内容为创建者关系填充，自部署请替换。
- 生成图片公网链接 24h 过期，已自动下载到 `data/images/` 持久化。
