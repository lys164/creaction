# POPOP 角色生產鏈路

批次上傳圖片 → 調模型擴寫人設 schema（中/日/韓/英）→ 反寫外貌 prompt → 選畫風重繪封面圖 → 勾選帖子型別批次生產帖子（圖文，4 語言）+ 配圖。

## 鏈路

```
圖片
 └─(gemini-3.1-pro 視覺)→ 人設 schema（4 語言）
       └─(反寫)→ 外貌 identity（固定外貌 DNA，韓文鍵值）
             ├─ identity + 畫風 → 重繪封面圖（gpt-image-2，圖生圖）
             └─ 批次帖子：
                  角色 + 勾選帖子型別
                   └─→ content（中/日/韓/英）+ variable + scene（每條不同）
                         └─ identity + variable + scene + 畫風 → 配圖
```

- **LLM**：`gemini-3.1-pro-preview`（視覺 + 文字）
- **生圖**：`gpt-image-2`（非同步任務，支援圖生圖保持人物一致性）
- **閘道器**：APIMart (`https://api.apimart.ai/v1`)，OpenAI 相容

## 執行

```bash
cd popop_pipeline
bash run.sh
# 瀏覽器開啟 http://127.0.0.1:8077
```

首次執行會自動安裝 `requirements.txt` 依賴。

## 介面

1. **批次上傳 / 生成人設**：拖拽多圖，可填創作補充要求；預設每張圖各生成一個角色。
2. **角色 / 人設 / 封面**：檢視人設（4 語言）、完整 JSON、外貌 identity；選畫風重繪封面圖。
3. **生成 INS 帖子（9 條）**：選角色 + 畫風，一鍵推斷 TA 最近會發的 9 條 ins 帖子。
4. **帖子型別生產**：按 Excel 的 8 類帖子型別批次生成（孔雀開屏/展示生活/偽媒體…）。
5. **畫風庫**：貼上 JSON 覆蓋畫風詞（陣列，元素含 `id` / `name` / `prompt`）。

## INS 帖子鏈路（核心）

每個角色生成 9 條「最近的 ins」，由模型根據興趣/生活方式/在意的東西推斷，文案是角色本人語氣（4 語言）：

```
人設 → 推斷最近 9 條 ins
  每條:
   ├─ content（中/日/韓/英，角色語氣）
   ├─ format: image_text | text_only
   └─ image_text 時:
        ├─ image_type=selfie → 用 selfie schema 出 prompt
        │     拼入【重繪封面】做圖生圖（image-2，保持人臉一致）
        └─ image_type=photo  → photo_prompt 直接文生圖（人不出鏡）
```

- **selfie schema**：style(固定寫實自拍風) / variable / shooting(含 capture_mode、filter、shot_size、angle…) / scene。
- **photo**：食物、風景、物件、寵物、手部特寫等，貼合角色品味，無人臉。
- **text_only**：金句、碎碎念、提問等，不出圖。

## 帖子型別生產（來源：POPOP-帖子型別及資料來源.xlsx）

| id | 型別 | 資料目標 |
|----|------|---------|
| peacock | 孔雀開屏·表達交友訴求 | 吸引使用者聊天 |
| life | 展示角色生活 | 吸引使用者聊天 |
| media | 偽·媒體/廣告/商單/活動出席 | 吸引使用者聊天 |
| forum | 角色劇情/論壇體 | 吸引使用者聊天 |
| about_user_public | 角色公開發關於使用者帖 | 使用者聊天后反饋 |
| about_user_anon | 角色匿名發關於使用者帖 | 使用者聊天后反饋 |
| media_feedback | 偽·媒體·心動指令 | 使用者聊天后反饋 |
| daily_topic | 生活主題帖（寵物/美食/旅遊…） | 角色生活 |

## 外貌 schema（identity / variable / scene）

- `identity`：固定外貌 DNA，所有圖片保持一致（生成人設後反寫一次）。
- `variable`：每條帖子的當天可變外貌（表情、妝容、髮型、穿搭、姿勢…）。
- `scene`：每條帖子的場景與鏡頭（活動、地點、道具、時間、camera）。

最終生圖 prompt = `identity + variable + scene`（平鋪）+ 畫風 prompt。

## 目錄

```
popop_pipeline/
├─ app/
│  ├─ config.py       # 金鑰 / 模型 / 路徑 / 語言
│  ├─ api_client.py   # APIMart 客戶端（chat 視覺 + 生圖輪詢）
│  ├─ prompts.py      # 人設/外貌/帖子 prompt + 帖子型別表 + schema
│  ├─ styles.py       # 畫風庫（佔位，待補詞）
│  ├─ pipeline.py     # 編排 + 落盤
│  └─ server.py       # FastAPI
├─ web/               # 單頁前端
├─ data/              # uploads / personas / posts / images / styles.json
├─ requirements.txt
└─ run.sh
```

## 配置

金鑰與模型在 `app/config.py`，也可用環境變數覆蓋：
`POPOP_API_KEY` / `POPOP_LLM_MODEL` / `POPOP_CHAT_MODEL` / `POPOP_IMAGE_MODEL` / `POPOP_MAX_WORKERS`。

## 說明

- 人設擴寫較慢（大 schema + 4 語言推理，單角色約 1-2 分鐘）；生圖單張約 1-2 分鐘，批次已併發處理。
- 所有 API key 內容為建立者關係填充，自部署請替換。
- 生成圖片公網連結 24h 過期，已自動下載到 `data/images/` 持久化。
