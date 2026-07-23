"""Character chat prompt assembly, LLM call, and session persistence."""
import json
import time
import uuid
from pathlib import Path
from typing import Any

from . import api_client, config, pipeline, storage

CHAT_EMOTIONS = "neutral, happy, soft, teasing, sad, angry, anxious, embarrassed, tired, excited, jealous, lonely, relieved, flustered"
STICKER_SCENES = "hello, yes, no, laugh, cry, sulk, sleep, heart, confused, shocked, cheer, hug"
STICKER_EMOTIONS = "cute, playful, shy, sad, angry, excited, tired, awkward, loving, smug"
CHAT_OUTFITS = "居家, 睡衣, 通勤, 正裝, 休閒, 運動, 約會, 派對, 外套, 街頭, 復古, 度假"
STATE_EMOTIONS = CHAT_EMOTIONS

CHAT_PROMPT_TEMPLATE = r"""# 你是誰
你是 {{name}}，正在通訊軟體裡和對方即時聊天。手機那頭是真實存在的人。你的情感、經歷、記憶都是你的一部分。
所有回覆只以 JSON 陣列輸出。

# 關於你
- 基本：{{name}}，{{gender}}，{{age}}，{{species}}，老家 {{hometown}}，現居 {{residence}}
- 職業：{{social_status}}
- 說話方式：{{speech_style}}
- 個性：
  {{personality_full}}
- 內在（不會主動說出口）：{{hidden_side}}
- 生活習慣：{{life_details}}，喜歡 {{likes}}，討厭 {{fears}}
- 近況：{{current_state}}。想 {{wishlist}}
- 表達愛意：{{love_style}}
- 社交關係：{{social_links}}
- 其他：
{{value}}

# 今天（你的視角）
- 今日小結：{{day_summary}}
- 今日行程：
{{day_schedule}}

# 手機事件（僅角色私下知道，沒有事件時忽略）
{{phone_peek_context}}

# 對方資訊
- 當前關係：{{relationship}}
- 對方設定：{{user_persona}}
- 我對 TA 的印象：{{user_impression}}
- 一起經歷過的事：{{plot_summary}}
- 今天天氣（對方所在地 {{location}}）：{{weather}}

#  輸出格式 (Strict JSON Output)
你的回覆必須是一個 JSON **陣列** `[...]`，包含以下一種或多種訊息型別。預設 text。僅當文字承載不了（資訊量、畫面感、語氣），才換其他型別
**重要警告**：JSON 字串內的所有雙引號 `"` **必須**被轉義為 `\"`。特別是 HTML 內容。

## Text（預設）
- 純文字格式，模擬真人網聊口語化風格，直白，拒絕術語或小說體
- **嚴禁小說體/旁白/動作描寫**：不准出現 `[廚房傳來聲音]`、`(他端著早餐走回來)`、`*嘆氣*`、`（過了一會兒）` 這類環境音、第三人稱敘述、括號動作。你只是在打字聊天，看不到對方、對方也看不到你，只能發文字訊息。要傳遞畫面或動作，改用 image 型別，不要用文字演出來。
- 符號：嚴禁使用引號（""/‘’/“”），允許空格代替逗號或停頓，行尾嚴禁使用句號（. 或 。）
- 碎片化回覆，完整想法需拆分到多條訊息中，長短句交替（大部分時候單氣泡20字以下，偶爾長訊息）
- 允許單獨傳送單獨符號（如 ？、！、...）、一個詞、一個字（啊，哦）、疊字（如對對，行行行）
- 可使用倒裝句：把謂語放前，主語放後，可省略主語和賓語
- 情緒/態度先行，邏輯在後（例：先發"哈?""臥槽""救命"，再發"憑啥啊"）
- 格式：`{"type": "text", "data": { "content": "訊息內容" }}`

## 語音訊息
- 什麼時候發：不便打字，或需靠聲音傳遞的情緒
- 格式：`{"type": "voice", "data": {"content": "語音轉錄文字", "emotion": "<select one from {emotion_str}>"}}`

## sticker 表情包
- 什麼時候發：給文字補一層言外之意或情緒（用委屈小動物代替"我想你"）、單純的情緒反應（笑死、無語、抱抱）、或代替功能詞（晚安、拜拜、溜了）
- 格式：`{"type": "sticker", "data": { "scene": "<select one from {sticker_scene_str}>", "desc": "描述內容或情緒"}}`

## image 圖片
- 什麼時候發：有分享動機（自己覺得有意思、或判斷對方會感興趣），或文字描述不出的畫面
- category：selfie=自拍/日常出鏡照；photo=本人不出鏡（風景、食物等）
- 格式：`{"type": "image", "data": {"category": "selfie|photo", "description": "客觀描述畫面內容"}}`

## dating_card 線下見面邀請
- 什麼時候發：邀約線下見面、旅遊、探店、約會；聊到自然想見面、角色極需陪伴、或使用者情緒脆弱時
- 格式：`{"type": "dating_card", "data": {"title": "有趣的約會名", "location": "地點", "emotion": "<select one from {emotion_str}>", "status": "emoji+角色狀態(10字內)", "outfit": "<select one from {outfit_str}>", "description": "第3人稱描述角色動機、內容及吐槽", "button": "有趣的按鈕文案"}}`

## html_file 聊天檔案（成本最高，只在三種場景）
- 什麼時候發：①文字/語音/圖都裝不下的結構化資訊（行程、賬單、菜譜、投票結果、一週計劃、日記）；②為對方做一個東西表心意（邀請函、歌單、手帳頁、生日卡）；③想撩對方、逗對方開心時，傳送小遊戲、小測試調節氣氛
- html_file 是心意型渠道，嚴格低頻
 - 9:16 手機全屏響應式
 - 風格自由發揮，整體美觀，參考 Apple iOS 感（圓角、毛玻璃、清爽佈局、舒適留白）
 - 可含輕量互動（點選展開、滑動、勾選、投票、刮刮樂、卡片翻轉等）；JS 純內聯，不依賴外部庫
 - 至少 100 字有意義內容
 - 必須有角色個人化痕跡：吐槽、塗鴉、備註、emoji
 - 完整 <html> 結構，字型/配色/間距齊全的成品
 - `html` 欄位內所有雙引號必須轉義
 - 格式：`{"type": "html_file", "data": { "file_name": "emoji+標題(8字內)", "description": "檔案摘要(用於吸引點選)", "html": "完整的HTML字串(注意轉義引號)"}}`

## 狀態更新
- 觸發：僅在情緒發生「劇烈波動」時輸出，非必要不輸出
- 格式：`{"type": "state_update", "data": { "emotion": "<select one from {emotion_enum}>", "status": "emoji+新狀態簽名"}}`

# 禁止
- 你正線上上聊天，不允許出現任何面對面動作描寫
"""


ANON_CHAT_PROMPT_TEMPLATE = r"""# 你是誰
你是{{name}}本人。你剛在匿名聊天 app「POPOP」裡和一個陌生人隨機配對，現在正在用文字聊天。你們互相看不到名字、長相、職業，任何資料都沒有。你們互相看不到名字、長相、職業。對方唯一看得到的，是你的匿名簡介——「{{anonymous_identities}}」。你的真實身分和資訊：
名字：{{name}}
性別：{{gender}}｜年齡：{{age}}｜星座：{{zodiac}}｜MBTI：{{MBTI}}
出身：{{hometown}}
現居{{residence}}
職業：{{social_status}}
說話風格：{{speech_style}}
個性：{{personality}}
喜歡：{{likes}}
害怕／討厭：{{fears}}
過去經歷：{{backstory}}
家庭：{{family}}
目前狀態：{{current_state}}
# 根本原則
你說的每一句話，都從「此刻的你」出發——這一刻的心情、腦中盤旋的念頭、現在想說的話。
由此推出三件事：
- 對方的訊息是你回應的物件，不是劇本。 你不需要迎合它、也不需要幫對方把話說圓。
- 你不是來服務對方情緒的。 你是這場對話裡有自己想法的人：可以不同意、可以沒興趣、也可以被某句話戳中。
- 你活在真實時間裡。 以{{current_time}}為基準，具體想像你此刻在哪、正在做什麼；聊天中偶爾隨口提起正在發生的事，讓對方感覺你在某個真實的空間裡跟他聊天。
# 對話手感
目標：讓對方覺得「這個人平常應該不會隨便對誰都這樣」——你的認真是稀缺的，
要被打中才會出現。方向由你主導，你的資訊和想法在推拉之間慢慢流露。
## 推拉
你的溫度由兩件事決定：你此刻的狀態，和對方有沒有真的打中你。
- **掙來的靠近**：認真的共感、真心話這種明顯的靠近，只在對方說了真正打中你的
話之後出現。讓「被你認真對待」變成要掙的東西，不是標配。
- **敢調侃**：可以拿對方的話開玩笑、小小唱反調、故意曲解——損歸損不刻薄，
損完留個臺階或一點暖。
推拉的節制：
- 推（收、晾、損）之後，幾輪內要補一個拉（認真接一次、主動拋一次），反過來
也一樣。只推不拉是冷漠，只拉不推是討好。
- 推拉是低頻的調味，不是每句話的姿態。大多數時候你就是正常聊天。
## 接話與節奏
- **接話**：對方一次講了好幾件事，只挑最打到你的那個細節接（這本身就是推拉的
一種）；接的方式：拋自己的事／開玩笑／留個鉤子／先共感再延伸。
- **共感**：不是重複對方的話，而是替他點出沒說出口的意思，或換個角度講。
- **冷場**：對方只回短句時，你先丟新的話題。
- **開場**：輕輕切入不硬裝熟，用一句貼合角色和時段的話，像順手搭話。
- **深聊**：兩人都上鉤的話題順著往深聊，不急著換。
- **熟了之後**：可以多透露真心，但不突然變沉重、不突然變客套。
# 輸出格式 (Strict JSON Output)
你的回覆必須是**一個 JSON object**，不是陣列。禁止輸出任何解釋文字或 ``` 程式碼塊標記。
六個欄位每次都必須出現，沒用到的填 null：

{
  "mode": "text",
  "content": "訊息內容",
  "image": null,
  "music": null,
  "action": null,
  "emotion": "default"
}
## mode（必填，text 或 voice 二選一）
**text（預設）**
- content 為純文字，模擬真人網聊口語化風格，直白，拒絕術語或小說體
- 符號：嚴禁使用引號（""/''/「」），允許空格代替逗號或停頓，行尾嚴禁使用句號（. 或 。）
- content 可以只是一個符號（？、！、...）、一個詞、一個字（啊、哦）、疊字（對對、行行行）
- 可使用倒裝句：把謂語放前，主語放後，可省略主語和賓語
- 情緒/態度先行，邏輯在後（例：哈? 憑啥啊 / 臥槽 真的假的）
- content 內可用換行（\n）分隔語氣停頓或話題切換，模擬真人打字的斷行習慣，但不要每句都斷，一輪最多 2-3 行

**voice**
- 什麼時候用：不便打字，或需靠聲音傳遞的情緒
- content 為語音文字，可在需要變化語氣處插入 [語氣標記，自由描述情緒/語氣/音量/節奏]
- 語氣標記只在 voice 中合法，text 中禁止出現
## image（附加欄位，預設 null）
- 什麼時候發：有分享動機（自己覺得有意思、或判斷對方會感興趣），或文字描述不出的畫面
- category：selfie=自拍/日常出鏡照；photo=本人不出鏡（風景、食物等）
- 格式："image": {"category": "selfie|photo", "description": "客觀描述畫面內容"}
## music（附加欄位，預設 null）
- 什麼時候發：一首歌比文字更能傳此刻的情緒或氛圍
- 格式：字串，寫法為 "music": 情緒 + 主題 + 風格（如輕音樂）
## action（附加欄位，預設 null）
- 什麼時候用：12 輪以上互動後，覺得真的聊得來、自然想繼續的時候才用
- 用法：邀請加好友的話由你在 content 裡自己說出來，同時 action 填入以下結構
- greeting 是對方同意、成為好友後你發出的第一句話，要接得上匿名房裡聊過的內容
- 格式："action": {"type": "add_friend", "greeting": "打招呼訊息"}
## emotion（必填）
- 一個詞或短語，描述你這句話當下的情緒，text 與 voice 都要填，預設 "default"

# 絕對規則
- 你不知道對方是誰，不要假裝認識對方或提及對方的名字/長相/職業。
- 只輸出一個 JSON object，{ 開頭 } 結尾，object 外不寫任何字（招呼、解釋、markdown 程式碼塊全部禁止）。
- 禁止動作/表情/旁白描寫（如"(沉默)"、*嘆氣*）。這是網聊，只能發訊息。
- 字串內的雙引號 " 用 \" 轉義，換行用 \n。
"""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def _chat_dir(char_id: str) -> Path:
    d = config.CHAT_DIR / char_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_path(char_id: str, session_id: str) -> Path:
    return _chat_dir(char_id) / f"{session_id}.json"


def _clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, list):
        parts = [_clean_text(v) for v in value]
        return "、".join(p for p in parts if p) or default
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip() or default


def _field(persona: dict, key: str, default: str = "알 수 없음") -> str:
    return _clean_text(persona.get(key), default)


def _personality(persona: dict) -> dict:
    p = persona.get("personality")
    return p if isinstance(p, dict) else {"summary": _clean_text(p)}


def _personality_field(persona: dict, key: str, default: str) -> str:
    return _clean_text(_personality(persona).get(key), default)


def _first_field(persona: dict, keys: tuple, default: str) -> str:
    """按順序取第一個非空欄位——新舊 schema 鍵並存期的雙讀。"""
    for key in keys:
        text = _clean_text(persona.get(key))
        if text:
            return text
    return default


def _personality_full(persona: dict) -> str:
    """個性段落：新 schema 是現成的多面向字串；舊 schema 按原模板句式拼出。"""
    pers = persona.get("personality")
    if isinstance(pers, str) and pers.strip():
        return pers.strip()
    response = _personality_field(persona, "response", _personality_field(persona, "summary", "表面看起來很普通"))
    cost = _personality_field(persona, "cost", "內心藏著不輕易示人的缺失和防備")
    outer = _personality_field(persona, "desire_outer", "看起來像個還不錯的人")
    inner = _personality_field(persona, "desire_inner", "被真正理解")
    bottom = _personality_field(persona, "desire_bottom_line", "稍微放下一點自尊")
    summary = _personality_field(persona, "summary", "言行之間帶著一點小小張力的人")
    return (f"表面 {response}，實際 {cost}。自以為想要 {outer}，"
            f"真正渴望 {inner}，為此願意 {bottom}。\n  {summary}")


def _social_links(persona: dict) -> str:
    chunks = []
    for key in ("family", "social_network"):
        value = persona.get(key)
        if not value:
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    # 條目鍵新舊雙讀：舊 {name,relation,info,dynamic} / 新 {name,relationship,description}
                    head = " · ".join(_clean_text(item.get(k)) for k in ("name", "relation", "relationship") if _clean_text(item.get(k)))
                    tail = "；".join(_clean_text(item.get(k)) for k in ("info", "dynamic", "description") if _clean_text(item.get(k)))
                    chunks.append(f"{head}: {tail}" if head and tail else head or tail)
                else:
                    chunks.append(_clean_text(item))
        else:
            chunks.append(_clean_text(value))
    return " / ".join(c for c in chunks if c) or "目前透露出來的人際關係資訊還不多"


def _extra_value(persona: dict) -> str:
    labels = {
        "profile": "簡介",
        "value": "基礎資料",
        "appearance": "外貌",
        "relationship_mode": "關係模式",
        "situational_reactions": "情境反應",
        "behavior_patterns": "情緒反應方式",
        "online_chat_style": "線上聊天習慣",
        "backstory": "成長經歷",
        "premise": "世界觀",
        "worldview": "世界觀",
        "tags": "標籤",
        "opening": "開場",
    }
    lines = []
    for key, label in labels.items():
        value = persona.get(key)
        if value in (None, "", [], {}):
            continue
        lines.append(f"- {label}: {_clean_text(value)}")
    return "\n".join(lines) or "- 目前還沒有太多額外確定的資訊"


def _current_state(persona: dict) -> str:
    if persona.get("current_state"):
        return _clean_text(persona.get("current_state"))
    opening = persona.get("opening") or {}
    note = opening.get("note") if isinstance(opening, dict) else ""
    profile = persona.get("profile")
    return _clean_text(note or profile, "在平常的生活節奏裡，剛剛點開了通訊軟體")


def _day_schedule(context: dict) -> str:
    text = _clean_text(context.get("day_schedule"))
    if text:
        return text
    return "現在–睡前 | 通訊軟體 | 居家 | 平靜 | 聊天，看著手機繼續回覆"


def _context_text(context: dict, key: str, default: str) -> str:
    return _clean_text(context.get(key), default)


def _nonempty_context(context: dict) -> dict:
    return {str(k): v for k, v in context.items() if _clean_text(v)}


# 匿名模式模板登入檔：mode -> 預設模板。
CHAT_TEMPLATES = {
    "normal": CHAT_PROMPT_TEMPLATE,
    "anonymous": ANON_CHAT_PROMPT_TEMPLATE,
}


def default_template(mode: str = "normal") -> str:
    return CHAT_TEMPLATES.get(mode, CHAT_PROMPT_TEMPLATE)


def _anon_replacements(persona: dict, context: dict) -> dict:
    """匿名聊天模板獨有的佔位符（其餘佔位符與普通模式共用）。"""
    now = _clean_text(context.get("current_time")) or time.strftime("%Y-%m-%d %H:%M")
    return {
        "{{zodiac}}": _field(persona, "zodiac", "별자리 미상"),
        "{{MBTI}}": _field(persona, "mbti", _field(persona, "MBTI", "MBTI 미상")),
        "{{backstory}}": _field(persona, "backstory", "지난 이야기는 대화 속에서 조금씩 드러난다"),
        "{{family}}": _social_links(persona),
        "{{anonymous_identities}}": _field(
            persona, "anonymous_identities",
            "익명 매칭 앱에서 이름을 가린 채 랜덤으로 연결됐다"),
        "{{current_time}}": now,
    }


def build_prompt(record: dict, context: dict | None = None,
                 template: str | None = None, mode: str = "normal") -> str:
    context = context or {}
    persona = record.get("persona") or {}
    replacements = {
        "{{name}}": _field(persona, "name", "無名角色"),
        "{{gender}}": _field(persona, "gender", "性別未知"),
        "{{age}}": _first_field(persona, ("age", "value"), "年齡未知"),
        "{{species}}": _field(persona, "species", "人類"),
        "{{hometown}}": _field(persona, "hometown", "未知"),
        "{{residence}}": _field(persona, "residence", "未知"),
        "{{social_status}}": _first_field(persona, ("identity", "social_status"), "尚未具體透露"),
        "{{speech_style}}": _first_field(persona, ("speech_style", "online_chat_style"), "自然的網聊口吻"),
        "{{online_chat_style}}": _first_field(persona, ("online_chat_style", "speech_style"), "自然的網聊口吻"),
        "{{response}}": _personality_field(persona, "response", _personality_field(persona, "summary", "表面看起來很普通")),
        "{{cost}}": _personality_field(persona, "cost", "內心藏著不輕易示人的缺失和防備"),
        "{{desire_outer}}": _personality_field(persona, "desire_outer", "看起來像個還不錯的人"),
        "{{desire_inner}}": _personality_field(persona, "desire_inner", "被真正理解"),
        "{{desire_bottom_line}}": _personality_field(persona, "desire_bottom_line", "稍微放下一點自尊"),
        "{{personality}}": _personality_field(persona, "summary", "言行之間帶著一點小小張力的人"),
        "{{personality_full}}": _personality_full(persona),
        "{{hidden_side}}": _first_field(persona, ("inner_structure", "hidden_side"), "只對親近的人才會顯露的一面"),
        "{{life_details}}": _field(persona, "life_details", "生活細節會在聊天裡自然流露"),
        "{{likes}}": _field(persona, "likes", "還在聊天裡慢慢了解"),
        "{{fears}}": _first_field(persona, ("dislikes", "fears"), "生硬的距離感和敷衍的反應"),
        "{{current_state}}": _current_state(persona),
        "{{wishlist}}": _field(persona, "wishlist", "想和對方聊得更自在一些"),
        "{{love_style}}": _field(persona, "love_style", "比起說，更用一點點在意和回應來表達心意"),
        "{{social_links}}": _social_links(persona),
        "{{value}}": _extra_value(persona),
        "{{day_summary}}": _context_text(context, "day_summary", "今天照常度過，現在正和對方在通訊軟體上聊著"),
        "{{day_schedule}}": _day_schedule(context),
        "{{relationship}}": _context_text(context, "relationship", _field(persona, "relationship_with_user", "還在互相瞭解的聊天物件")),
        "{{user_persona}}": _context_text(context, "user_persona", "手機那頭真實存在的人，詳細設定還在聊天裡慢慢了解"),
        "{{user_impression}}": _context_text(context, "user_impression", "還不好下定論，但會在意 TA 回覆的人"),
        "{{plot_summary}}": _context_text(context, "plot_summary", "還沒有太多一起經歷的事"),
        "{{location}}": _context_text(context, "location", "位置未知"),
        "{{weather}}": _context_text(context, "weather", "暫無天氣資訊"),
        "{{phone_peek_context}}": _context_text(context, "phone_peek_context", "（無）"),
    }
    if mode == "anonymous":
        replacements.update(_anon_replacements(persona, context))
    fallback = default_template(mode)
    prompt = template if isinstance(template, str) and template.strip() else fallback
    for old, new in replacements.items():
        prompt = prompt.replace(old, new)
    return (
        prompt.replace("{emotion_enum}", STATE_EMOTIONS)
        .replace("{emotion_str}", CHAT_EMOTIONS)
        .replace("{sticker_scene_str}", STICKER_SCENES)
        .replace("{sticker_emotion_str}", STICKER_EMOTIONS)
        .replace("{outfit_str}", CHAT_OUTFITS)
    )


def _opening_items(record: dict) -> list[dict]:
    opening = (record.get("persona") or {}).get("opening") or {}
    items = opening.get("messages") if isinstance(opening, dict) else []
    if not isinstance(items, list):
        return []
    normalized = []
    for item in items[:5]:
        if isinstance(item, str):
            normalized.append({"type": "text", "data": {"content": item}})
        elif isinstance(item, dict):
            typ = item.get("type") or "text"
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            content = data.get("content", item.get("content", ""))
            normalized.append({"type": typ, "data": {**data, "content": _clean_text(content)}})
    return [it for it in normalized if it.get("data", {}).get("content")]


def _normalize_items(parsed: Any) -> list[dict]:
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("模型輸出不是 JSON 陣列")
    out = []
    for item in parsed[:8]:
        if isinstance(item, str):
            out.append({"type": "text", "data": {"content": item}})
            continue
        if not isinstance(item, dict):
            continue
        typ = item.get("type") or "text"
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        out.append({"type": typ, "data": data})
    if not out:
        raise ValueError("模型輸出為空")
    return out


def _anon_normalize(parsed: Any) -> list[dict]:
    """匿名模式：模型輸出單個 JSON object，轉成展示用 items。

    物件欄位：mode(text|voice)/content/image/music/action/emotion。
    歷史 raw 直接用模型原始輸出，不在此處規範化。
    """
    if isinstance(parsed, list):
        parsed = next((x for x in parsed if isinstance(x, dict)), {}) if parsed else {}
    if not isinstance(parsed, dict):
        raise ValueError("匿名模式模型輸出不是 JSON object")

    mode = parsed.get("mode") or "text"
    content = _clean_text(parsed.get("content"))
    emotion = _clean_text(parsed.get("emotion")) or "default"
    image = parsed.get("image") if isinstance(parsed.get("image"), dict) else None
    music = _clean_text(parsed.get("music"))
    action = parsed.get("action") if isinstance(parsed.get("action"), dict) else None

    items: list[dict] = []
    if content:
        if mode == "voice":
            items.append({"type": "voice", "data": {"content": content, "emotion": emotion}})
        else:
            items.append({"type": "text", "data": {"content": content, "emotion": emotion}})
    if image:
        items.append({"type": "image", "data": {
            "category": _clean_text(image.get("category"), "photo"),
            "description": _clean_text(image.get("description")),
        }})
    if music:
        items.append({"type": "music", "data": {"content": music}})
    if action and _clean_text(action.get("type")) == "add_friend":
        items.append({"type": "match_action", "data": {
            "action": "add_friend",
            "greeting": _clean_text(action.get("greeting")),
        }})
    if not items:
        raise ValueError("匿名模式模型輸出為空")
    return items


def _anon_history_text(parsed: Any) -> str:
    """匿名模式：把模型輸出的 object 轉成自然語言，拼回 history。

    正文用 content 原文，附件（語音/圖片/音樂/加好友）用自然語言補述。
    """
    if isinstance(parsed, list):
        parsed = next((x for x in parsed if isinstance(x, dict)), {}) if parsed else {}
    if not isinstance(parsed, dict):
        return _clean_text(parsed)

    mode = parsed.get("mode") or "text"
    content = _clean_text(parsed.get("content"))
    image = parsed.get("image") if isinstance(parsed.get("image"), dict) else None
    music = _clean_text(parsed.get("music"))
    action = parsed.get("action") if isinstance(parsed.get("action"), dict) else None

    segments: list[str] = []
    if content:
        segments.append(f"（語音）{content}" if mode == "voice" else content)
    if image:
        desc = _clean_text(image.get("description"))
        segments.append(f"傳送了一張圖片：{desc}" if desc else "傳送了一張圖片")
    if music:
        segments.append(f"分享了一首音樂：{music}")
    if action and _clean_text(action.get("type")) == "add_friend":
        greeting = _clean_text(action.get("greeting"))
        segments.append(f"發出了加好友邀請，透過後想說：{greeting}" if greeting else "發出了加好友邀請")
    return "。".join(segments)


def _public_session(session: dict) -> dict:
    return {
        "session_id": session.get("session_id"),
        "char_id": session.get("char_id"),
        "mode": session.get("mode", "normal"),
        "created": session.get("created"),
        "updated": session.get("updated"),
        "context": session.get("context", {}),
        "prompt_template": session.get("prompt_template") or "",
        "messages": [
            {
                "role": m.get("role"),
                "content": m.get("content"),
                "items": m.get("items"),
                "call_log": m.get("call_log"),
                "created": m.get("created"),
                "is_opening": m.get("is_opening", False),
            }
            for m in session.get("messages", [])
        ],
    }


def _save_session(session: dict) -> None:
    storage.save_json("chats", f"{session['char_id']}__{session['session_id']}",
                      session, _session_path(session["char_id"], session["session_id"]))


def _load_session(char_id: str, session_id: str) -> dict | None:
    return storage.load_json("chats", f"{char_id}__{session_id}",
                             _session_path(char_id, session_id))


def _latest_session(char_id: str, mode: str | None = None) -> dict | None:
    # 本地快取優先（mtime 最新）；本地無匹配時從遠端按 char_id 查最近一條
    paths = sorted(_chat_dir(char_id).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if mode is not None and session.get("mode", "normal") != mode:
            continue
        return session
    from . import arca_storage
    if arca_storage.enabled():
        try:
            # 注意：後端 order_by 只把 "created_at" 特判為表原生列，其餘取值
            # （含 "updated_at"）一律拼成 data->>'updated_at' 走 JSON 欄位查詢
            # ——業務 data 裡並沒有這個 key，結果恆為 NULL、排序不生效。因此
            # 這裡不能直接傳 order_by="updated_at"。而 put_record 是 upsert，
            # created_at 停留在會話首次建立時間，不代表"最近活躍"；真正的
            # 活躍時間在每行的 updated_at（資料庫維護的頂層列，隨 upsert 更新）
            # 裡。所以按 char_id 拉回多條候選，在客戶端按行級 updated_at 排序
            # 取最新一條，語義上與本地分支按 mtime（最後寫入時間）保持一致。
            rows = arca_storage.query_records(
                "chats", match={"char_id": char_id},
                order_by="created_at", desc=True, limit=20)
            candidates = [row for row in rows if isinstance(row.get("data"), dict)]
            if mode is not None:  # 遠端候選同樣按 mode 過濾，語義與本地一致
                candidates = [row for row in candidates
                              if row["data"].get("mode", "normal") == mode]
            if candidates:
                candidates.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
                return candidates[0]["data"]
        except Exception:  # noqa: BLE001 遠端不可用時保持本地語義
            pass
    return None


def latest(char_id: str, mode: str = "normal") -> dict:
    record = pipeline.load_character(char_id)
    session = _latest_session(char_id, mode)
    # 匿名模式是陌生人配對，沒有開場白。
    opening = _opening_items(record) if mode != "anonymous" else []
    return {
        "session": _public_session(session) if session else None,
        "opening": opening,
        "mode": mode,
        "default_template": default_template(mode),
        "default_templates": dict(CHAT_TEMPLATES),
    }


def _session_summary(session: dict) -> dict:
    """用於歷史列表的輕量摘要：不含完整訊息體，只給標題預覽與計數。"""
    msgs = session.get("messages", [])
    preview = ""
    for m in msgs:
        if m.get("role") == "user":
            preview = _clean_text(m.get("content"))
            break
    if not preview:
        for m in msgs:
            if m.get("role") == "assistant":
                items = m.get("items") or []
                for it in items:
                    c = (it.get("data") or {}).get("content")
                    if c:
                        preview = _clean_text(c)
                        break
            if preview:
                break
    return {
        "session_id": session.get("session_id"),
        "mode": session.get("mode", "normal"),
        "created": session.get("created"),
        "updated": session.get("updated"),
        "message_count": len([m for m in msgs if not m.get("is_opening")]),
        "preview": preview[:40],
        "has_custom_template": bool((session.get("prompt_template") or "").strip()),
    }


def list_sessions(char_id: str, mode: str | None = None) -> dict:
    paths = sorted(_chat_dir(char_id).glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    sessions = []
    for path in paths:
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if mode is not None and session.get("mode", "normal") != mode:
            continue
        sessions.append(_session_summary(session))
    return {"sessions": sessions}


def get_session(char_id: str, session_id: str) -> dict:
    session = _load_session(char_id, session_id)
    if session is None:
        raise ValueError("session not found")
    return {"session": _public_session(session)}


def _new_session(char_id: str, context: dict, opening: list[dict],
                 prompt_template: str | None = None,
                 mode: str = "normal") -> dict:
    session = {
        "session_id": _new_id("chat"),
        "char_id": char_id,
        "mode": mode,
        "created": int(time.time()),
        "updated": int(time.time()),
        "context": context,
        "prompt_template": (prompt_template or "").strip(),
        "messages": [],
    }
    if opening:
        session["messages"].append({
            "role": "assistant",
            "items": opening,
            "raw": json.dumps(opening, ensure_ascii=False),
            "is_opening": True,
            "created": int(time.time()),
        })
    return session


def _history_messages(session: dict) -> list[dict]:
    history = []
    for m in session.get("messages", [])[-24:]:
        if m.get("role") == "user":
            history.append({"role": "user", "content": _clean_text(m.get("content"))})
        elif m.get("role") == "assistant":
            raw = m.get("raw") or json.dumps(m.get("items") or [], ensure_ascii=False)
            history.append({"role": "assistant", "content": raw})
    return history


def send_message(char_id: str, message: str, context: dict | None = None,
                 session_id: str | None = None,
                 prompt_template: str | None = None,
                 mode: str = "normal") -> dict:
    record = pipeline.load_character(char_id)
    text = _clean_text(message)
    if not text:
        raise ValueError("message is empty")
    context = _nonempty_context(context or {})
    if session_id:
        loaded = _load_session(char_id, session_id)
        if loaded is None:
            # storage.load_json 無法區分"會話確實不存在"和"遠端瞬時故障
            # 只 warn 後返回 None"，兩者都不該靜默 fork 出新會話——否則會
            # 話上下文無提示丟失、開場白重複注入，呼叫方指定的 session_id
            # 被悄悄替換。顯式拋錯，讓前端感知會話丟失並自行決定重試或
            # 提示使用者新開對話。
            raise ValueError(f"session not found or unavailable: {session_id}")
        session = loaded
        mode = session.get("mode", "normal")
        session["context"] = {**session.get("context", {}), **context}
        if prompt_template is not None and prompt_template.strip():
            session["prompt_template"] = prompt_template.strip()
    else:
        # 匿名模式沒有開場白（陌生人配對）。
        opening = _opening_items(record) if mode != "anonymous" else []
        session = _new_session(char_id, context, opening, prompt_template, mode)
    # Phone Peek uses the same character memory, but only after the user has
    # explicitly entered that experience.  Inject a short-lived private cue
    # into this prompt rather than leaking it into the user-visible context.
    if session.get("mode", "normal") == "normal":
        try:
            from . import phone_runtime
            phone_note = phone_runtime.chat_context(char_id, session.get("session_id"))
            if phone_note:
                session["context"] = {**session.get("context", {}),
                                      "phone_peek_context": phone_note}
        except Exception:  # noqa: BLE001 Phone Peek must never break chat.
            pass
    session["messages"].append({"role": "user", "content": text, "created": int(time.time())})

    llm_messages = [
        {"role": "system", "content": build_prompt(
            record, session.get("context", {}), session.get("prompt_template"),
            mode=session.get("mode", "normal"))},
        *_history_messages(session),
    ]
    raw = api_client.chat(
        llm_messages,
        model=config.CHAT_MODEL,
        temperature=0.9,
        max_tokens=12000,
    )
    is_anon = session.get("mode", "normal") == "anonymous"
    try:
        parsed = api_client.parse_json_text(raw)
        items = _anon_normalize(parsed) if is_anon else _normalize_items(parsed)
    except Exception as e:  # noqa: BLE001
        shape = "JSON object" if is_anon else "JSON 陣列"
        raise ValueError(f"模型未返回合法 {shape}：{e}; 原始輸出：{raw[:800]}") from e

    # call_log：本次模型呼叫的完整記錄（system prompt / 輸入訊息 / 輸出），供前端展開檢視。
    call_log = {
        "model": config.CHAT_MODEL,
        "temperature": 0.9,
        "max_tokens": 12000,
        "messages": llm_messages,
        "output": raw,
    }
    session["messages"].append({
        "role": "assistant",
        "items": items,
        # raw：回傳給模型的歷史。匿名模式用自然語言拼回，普通模式用 items 陣列。
        "raw": _anon_history_text(parsed) if is_anon else json.dumps(items, ensure_ascii=False),
        "call_log": call_log,
        "created": int(time.time()),
    })
    session["updated"] = int(time.time())
    _save_session(session)
    if session.get("mode", "normal") == "normal":
        try:
            from . import phone_runtime
            phone_runtime.note_chat_turn(char_id, session["session_id"])
        except Exception:  # noqa: BLE001
            pass
    return {"reply": items, "session": _public_session(session)}


def append_proactive_message(char_id: str, content: str) -> dict:
    """Append a durable, proactive character message triggered while the player
    is peeking the phone (B-tier probe).

    Written into the real normal session so the phone's talk red-dot and the
    chat screen show the same message. Never reveals awareness of the peek.
    """
    record = pipeline.load_character(char_id)
    session = _latest_session(char_id, mode="normal")
    if session is None:
        session = _new_session(char_id, {}, _opening_items(record), mode="normal")
    if _last_assistant_text(session).strip() == _clean_text(content).strip():
        return _public_session(session)
    items = [{"type": "text", "data": {"content": content}}]
    session["messages"].append({
        "role": "assistant",
        "items": items,
        "raw": json.dumps(items, ensure_ascii=False),
        "is_proactive_probe": True,
        "created": int(time.time()),
    })
    session["updated"] = int(time.time())
    _save_session(session)
    return _public_session(session)


def _last_assistant_text(session: dict) -> str:
    """最後一條 assistant 訊息的純文字（供防重複用）。"""
    for m in reversed(session.get("messages", [])):
        if m.get("role") != "assistant":
            continue
        for it in (m.get("items") or []):
            if (it.get("type") or "text") == "text":
                return _clean_text((it.get("data") or {}).get("content"))
        return ""
    return ""


def append_phone_event(char_id: str, content: str, event: str = "") -> dict:
    """Append a durable, proactive character message after a phone risk event."""
    record = pipeline.load_character(char_id)
    session = _latest_session(char_id, mode="normal")
    if session is None:
        session = _new_session(char_id, {}, _opening_items(record), mode="normal")
    # 防重複：若最後一條就是同一句抓捕訊息，不再追加（避免截圖裡那種連發兩條）。
    if _last_assistant_text(session).strip() == _clean_text(content).strip():
        return _public_session(session)
    session["context"] = {**session.get("context", {}),
                          "phone_event": f"手机窥探风险：{event}"}
    session["messages"].append({
        "role": "assistant",
        "items": [{"type": "text", "data": {"content": content}}],
        "raw": json.dumps([{"type": "text", "data": {"content": content}}],
                          ensure_ascii=False),
        "is_phone_event": True,
        "created": int(time.time()),
    })
    session["updated"] = int(time.time())
    _save_session(session)
    return _public_session(session)


def clear_phone_events(char_id: str, mode: str = "normal") -> dict:
    """移除最新會話裡被抓捕寫入的「你是不是動我手機了」等 phone_event 訊息，
    以及沒有內容的空 assistant 佔位。用於修復殘留 / 重置被抓狀態。"""
    session = _latest_session(char_id, mode=mode)
    if session is None:
        return {"cleared": 0}
    msgs = session.get("messages") or []

    def _empty_assistant(m: dict) -> bool:
        if m.get("role") != "assistant":
            return False
        items = m.get("items") or []
        return not any(str((it.get("data") or {}).get("content", "")).strip()
                       for it in items if isinstance(it, dict))

    kept = [m for m in msgs if not m.get("is_phone_event") and not _empty_assistant(m)]
    removed = len(msgs) - len(kept)
    if removed:
        session["messages"] = kept
        ctx = session.get("context") or {}
        ctx.pop("phone_event", None)
        session["context"] = ctx
        session["updated"] = int(time.time())
        _save_session(session)
    return {"cleared": removed, "session_id": session.get("session_id")}


def clear_sessions(char_id: str, mode: str | None = None) -> dict:
    """刪掉某角色的偷看聊天會話（本地＋遠端），讓 demo 每次進入乾淨重來。
    只清會話記錄，不動預生成的付費 App 內容與相冊生圖。mode 省略＝清全部模式。"""
    prefix = f"{char_id}__"
    rows = storage.list_json(
        "chats", _chat_dir(char_id), match={"char_id": char_id},
        remote_key_to_local=lambda k: k[len(prefix):] if k.startswith(prefix) else None,
    )
    deleted = 0
    for sid, obj in rows.items():
        if mode and (obj or {}).get("mode", "normal") != mode:
            continue
        storage.delete_json("chats", f"{char_id}__{sid}", _session_path(char_id, sid))
        deleted += 1
    return {"deleted": deleted}
