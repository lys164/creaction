"""完整日產鏈路（Daily Run）：日程 + 主動消息 + 生活動態 + 第三方帖子一體生產。

方法論提取自線下「角色日程生產·手帳v1」的 DAILY_SYS（vlog 原則／開盤狀態／
情緒線／目標線推進與回退／echo 紅線／消息 intent 與 14 天錨點冷卻／限動配圖分流），
在平臺內落成一次 API 呼叫：

    1. 一次 LLM 呼叫 → 開盤狀態 + 24h 日程 + 主動消息 + 生活動態 + 狀態結算
    2. 第三方帖子環節直接複用 feed_posts 的 T2 引擎（generate_feed_post，
       本次日程摘要作 schedule_text 事實底座），帖子落入 feed 存檔 → 發現流可見
    3. 配圖統一走正式帖子生圖鏈路（_render_feed_image）：限動 selfie 出圖，
       photo（無人物）只留 spec 待接物件生圖鏈路

跨天連續性：runs 存檔按角色累積，下一次生成自動帶上昨日總結、收尾狀態、
目標線進度、近 14 次消息錨點與近 3 次 intent（冷卻去重）。
"""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import api_client, config, feed_posts, pipeline, prompts, storage

DAILY_DIR = config.DATA_DIR / "daily_runs"
DAILY_DIR.mkdir(parents=True, exist_ok=True)

MOTIF_COOLDOWN_RUNS = 14   # 錨點名詞冷卻視窗（按 run 數近似 14 天）
INTENT_COOLDOWN_RUNS = 3   # intent 避讓視窗

EMOTIONS = "기쁨|설렘|평온|집중|뿌듯|피곤|나른함|짜증|우울|불안|그리움|무념무상"

# ---------------------------------------------------------------------------
# 方法論（提取自手帳 v1 DAILY_SYS，只保原理與紅線，不給內容實例）
# ---------------------------------------------------------------------------

DAILY_CORE_RULES = """# 核心原則：把這一天拍成 vlog，不是排流水賬
讓看的人感到「這個人真的過了這一天」，而不是讀到一張作息表：
1. 窺探欲：用具體生活質感代替空泛動作，讓地點、物件、消費、職業細節自然透露人設。
2. 出乎意料：有計劃外、反差、失敗、臨時改變時就讓它發生；沒有就不要硬造。
3. 餘味：當天若自然留下未完成的事、等待揭曉的結果、明天會承接的動作，就記下來；沒有就讓這一天自然收住。
拒絕「起床→上班→吃飯→睡覺」的平鋪；但不要為了有戲而給每個時段硬塞反轉。每個被寫出的時段都要能回答：這件事為什麼屬於這個人、這一天？

# 今日狀態與情緒線（先定開盤狀態，再排日程）
- opening_state 一句話：醒來時刻+睡眠/身體/情緒/精力，貼合人設作息，從【昨日收尾狀態】順下來，別每天滿血重置。
- 開盤狀態定基調：狀態好→主動、效率高、消息話多；狀態差→拖延、取消計劃、話少或 emo，日程因此被改寫。
- 情緒隨天推進：開盤→前段→後段，途中的人/事會改寫情緒（社交互動尤其會扭轉情緒），不是一根直線。
- 偶發身體事件（感冒/失眠/嗓子啞等）可低概率出現，出現就貫穿全天影響安排與語氣。

# 人味（判斷維度，不是每天要完成的清單；只在自然長出來時用）
- 個性與怪癖：從 ta 獨特腦回路看生活——偏見、講究、私人理論、口頭禪、小執念與固定儀式。
- 言行不一：嘴上一套身體一套、立了 flag 又拖延、理性計劃被情緒打斷。
- 有立場：對刷到的事/遇到的人有價值觀反應，有被冒犯的點，也有被觸動的點。
- 有遺憾與渴望：人設裡的遺憾被某個瞬間自然勾起時，點到為止，不必每天解決或抒情。

# 日程編排規則
- 覆蓋醒來到再次醒來的主要時段，4-8 個時段；低資訊時段可合併，不為填滿而拆碎；時段間最小 30 分鐘。
- 環境共鳴：天氣/季節/城市影響情緒與事件；邏輯自洽（服裝適配地點活動、時間符合常理）。
- 跨領域覆蓋（別整天都在工作/吃飯/睡覺）：意外反差｜職業技能｜消費食物旅行寵物｜健康脆弱｜社交關係｜日常損耗｜媒體網路消費｜季節儀式｜里程碑突破。
- 符合常理的時間尺度：幾天內該告一段落的小事別無限拖；反覆出現的小事要麼這次了結、要麼給理由退場。

# 目標線推進（讓人實在感到角色在往前走，但要「軟」）
- 目標線從人設的職業與人生目標推導/繼承：1 條主線(main) + 1-2 條副線(side)/維持線(maintain)。【已有目標線】給出時必須繼承其 id 與進度，不許另起爐灶。
- 今天主要推進 1 條（偶爾捎帶 1 條），進度更新匹配劇情實際完成度：
  · 達成關鍵結果（能被外部驗證、改變後續安排）→ 大跳，整條線核心目標達成給 100%；
  · 日常往前蹭（準備/練習/打聽/整理）→ 小漲幾個百分點；
  · 受阻/翻車要如實回退；今天沒碰這條線 → 進度不動。
- 維持線被攻堅主線擠壓時，可在某條日程 detail 或 day_summary 裡如實帶出虧欠，別假裝全在推進。

# 與使用者的關係（紅線·嚴禁幻覺共同經歷）
**這是角色自己的一天，使用者不在場**：日程裡的事全是 ta 一個人或和社交圈 NPC 做的。ta 對使用者只能是單向的——想念、惦記、想分享、約個將來；唯一接觸渠道是 ta 主動發的消息。
- 嚴禁共同經歷：不寫「和你一起/你陪我/我們今天」，不寫偶遇使用者、使用者回不回消息。
- 第二人稱只有兩個合法位置：① mobile_messages 裡作為發消息的稱呼對象；② echo 裡作為「使用者過去某句話/某件事」被引用的主語。其餘敘事欄位一律不准把第二人稱寫成當下在場者。

# echo（讓今天「因你而不同」，但只認真實發生過的）
ta 和使用者真實發生過的事只存在【最近聊天記錄】裡。
- 可以：把其中 ta 真正在意的某句話/某件事，自然影響今天某個時段的安排或情緒，在該時段 echo 標注，格式恒為：引用的過去原話/原事 → ta 今天單方面做了什麼。
- 絕對禁止：捏造使用者沒說過的話；把人設當痕跡（痕跡必須是使用者的具體言行）；為煽情強行攀扯。
- 聊天記錄為空或無相關內容時，全天 echo 一律留空。一天最多 1-2 條，寧缺毋造。"""

MESSAGE_RULES = """# 主動消息（mobile_messages：從今天的真實片段裡長出來）
使用者看不到日程，只看到這些消息。一條 push 要像角色在某個瞬間真的拿起手機，把一小塊生活遞給對方；先確認：這件事為什麼會讓 ta 自然想到對方？

- 全天 push 總數 1-3 條，不固定：今天自然想分享、關係溫度高就多一點；忙、累、關係淡就少一點，甚至 1 條。多條時來自不同片段，不把同一件事換說法重複發。
- 關係溫度（讓 ta 有重力，不是有求必應的自動售貨機）：姿態必須有據（最近聊天的具體事，或人設的依戀模式）；找不到依據就保持正常，不要突然黏人或突然 emo。
- 長短跟著 intent 和事情本身走：日常問候通常短；單純想你可帶輕觸發點；有真實事件支撐（吐槽/報喜/種草/約將來）可以說完整。不把弱內容撐成長段，也不為短砍掉好看的現場。
- 寫法：口語、斷續、跳躍、不完整都可以；1 條消息 = 多個短氣泡；真人網聊風格（簡短斷續/偶爾錯字/空格代標點）。問句可以用，但別把對方推成答題機器。
- 硬約束：
  1. 話題必須錨到今天日程裡真發生的事，或最近聊天裡的真實觸發點；別憑空抒情。
  2. 使用者不能作為現場參與者（「陪我」「我們剛」「你也在」都違規）。
  3. 不撞【近期已用錨點】：具體物件/食物/梗冷卻，情緒不冷卻（想你/累可反覆）。
  4. 每條標 1 個 intent（報喜|吐槽求安慰|種草安利|約將來|單純想你|冷知識熱梗|小挑戰|日常問候），優先避開【近期已用 intent】；intent 只決定這條想達成什麼，絕不能塌縮成固定句式。"""

MOMENTS_RULES = """# 生活動態（moments / 限時動態，發在好友圈的 ins story，不是發給使用者一個人）
- 每條先想「此刻為什麼想發」，錨到一個具體小事/小物/光線/情緒，落到畫面上；別寫似是而非的空話或文藝金句。
- content 可長可短：story 常常一張圖配兩三個字、甚至只有圖沒字（content 留空字串）；別每條都寫成完整句。
- 全天 1-3 條，挑不同片段，別和主動消息撞同一件事，也別條條一個味。
- 紅線：story 發給好友圈，使用者不在場——不准出現「你/陪我/我們」，不寫成發給使用者的私信。
- post_type：life(曬日常/品味/狀態，最多)｜mood(一句心情/吐槽/共鳴)｜peacock(顯眼高光，少數，story 裡弱化)。
- 配圖分流 image.kind：
  · selfie＝本人出鏡：固定長相沿用人設，只寫當天可變項（variable+scene，值用韓語）。發帖人視角必須成立——獨處時只能自拍/鏡子/定時器構圖，絕不能出現「第三者站旁邊拍 ta」的旁觀鏡頭；scene.camera 如實寫自拍方式。
  · photo＝本人不出鏡：物件/食物/風景/手部/截圖/拼貼，寫進 photo_spec（source_materials/layout/text_overlay/decorations/color_tone/camera_logic——這張圖為何在 ta 手機裡）。
  · none＝純文字卡。
  按 kind 只輸出對應的鍵，其餘鍵整個省略。"""

DAILY_SCHEMA = """# JSON Schema（鍵名固定；文字欄位輸出 {"zh": "..."}，只要繁中）
{
  "opening_state": {"ko","zh"},
  "goal_threads": [
    {"id": "英文snake_case（繼承已有目標線時保持 id 不變）", "name": {"ko","zh"},
     "type": "main|side|maintain", "progress_before": 數字, "progress_after": 數字,
     "today_step": {"ko","zh"}}
  ],
  "daily_schedule": [
    {"start_time": "HH:mm", "end_time": "HH:mm",
     "location": {"ko","zh"}, "activity_name": {"ko","zh"},
     "emotion": "從 EMOTIONS 枚舉裡選 1 個（韓語原樣）",
     "status": "emoji+8字內有趣吐槽（韓語）",
     "detail": {"ko","zh"},   // 第三人稱總結此時段做了什麼，40字內，含具體虛構細節(店名/專案名等)；只寫 ta 獨自或與 NPC 做的事
     "echo": {"ko","zh"} | 兩鍵都為空字串   // 僅當真實聊天記錄直接引發此時段時填
    }
  ],
  "mobile_messages": [
    {"send_time": "HH:mm", "segment_time": "錨定的日程 start_time",
     "intent": "報喜|吐槽求安慰|種草安利|約將來|單純想你|冷知識熱梗|小挑戰|日常問候",
     "is_push": true,
     "bubbles": [{"ko","zh"}, ...]   // 1 氣泡 = 1 條短消息
    }
  ],
  "moments": [
    {"post_time": "HH:mm", "post_type": "life|mood|peacock",
     "content": {"ko","zh"} | 兩鍵都為空字串,
     "image": {"kind": "selfie|photo|none",
               "variable": {...}, "scene": {...},        // 僅 selfie，schema 見附錄
               "photo_spec": {"source_materials","layout","text_overlay","decorations","color_tone","camera_logic"}  // 僅 photo，值用韓語
     }}
  ],
  "highlight": {"ko","zh"} | 兩鍵都為空字串,   // 今天最有畫面感、被路人目擊會傳開的瞬間（第三方爆料帖的導火索）。平淡的一天沒有這種瞬間就留空——寧缺勿湊，留空＝今天不發第三方帖
  "day_summary": {"ko","zh"}, // 關鍵事件|哪條線推進/虧欠|心理身體狀態變化，第三人稱 50 字內
  "state_update": {
    "closing_state": {"ko","zh"},   // 今晚怎麼收的尾（身體/情緒），明天 opening_state 從這裡順
    "message_motifs_used": ["今天消息用到的具體錨點名詞（物件/食物/梗，韓語），不記情緒詞"],
    "intents_used": ["今天用過的 intent"]
  }
}"""


# 展示文字語言開關：默認只輸出 zh（與 feed_posts 的 FEED_LANG 一致）。
# 只作用於展示欄位的 {"ko","zh"} 占位；生圖/去重用的「值用韓語」素材
# （variable/scene/photo_spec、emotion 枚舉、message_motifs）不受影響。
if feed_posts.FEED_LANG != "ko":
    DAILY_SCHEMA = DAILY_SCHEMA.replace('{"ko","zh"}', '{"zh"}')


def _selfie_spec_appendix() -> str:
    return (
        "# selfie 配圖 schema 附錄（image.kind=selfie 時 variable/scene 按此填，值用韓語）\n"
        f"variable: {json.dumps(prompts.APPEARANCE_SCHEMA['variable'], ensure_ascii=False)}\n"
        f"scene: {json.dumps(prompts.APPEARANCE_SCHEMA['scene'], ensure_ascii=False)}"
    )


# ---------------------------------------------------------------------------
# 跨天連續性素材
# ---------------------------------------------------------------------------

def _daily_path(char_id: str) -> Path:
    return DAILY_DIR / f"{char_id}.json"


def _load_runs(char_id: str) -> dict:
    data = storage.load_json("daily_runs", char_id, _daily_path(char_id))
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        data = {"char_id": char_id, "runs": []}
    return data


def list_daily_runs(char_id: str) -> dict:
    return _load_runs(char_id)


def delete_daily_run(char_id: str, run_id: str) -> dict:
    data = _load_runs(char_id)
    before = len(data["runs"])
    data["runs"] = [r for r in data["runs"] if r.get("run_id") != run_id]
    if len(data["runs"]) == before:
        raise ValueError(f"run not found: {run_id}")
    storage.save_json("daily_runs", char_id, data, _daily_path(char_id),
                      strict_remote=True)
    return data


def _carryover_block(runs: list[dict]) -> str:
    """從歷史 runs 提取連續性素材：昨日總結/收尾/目標線/冷卻清單。"""
    if not runs:
        return ("# 連續性素材\n（首次生成：目標線從人設推導新開；opening_state 自由設定。）")
    last = runs[0]
    data = last.get("data") or {}
    lines = ["# 連續性素材（必須承接，別每天滿血重置）"]
    ds = (data.get("day_summary") or {})
    if isinstance(ds, dict) and ds.get("ko"):
        lines.append(f"- 昨日總結：{ds['ko']}（確保承接、不重複）")
    cs = ((data.get("state_update") or {}).get("closing_state") or {})
    if isinstance(cs, dict) and cs.get("ko"):
        lines.append(f"- 昨日收尾狀態：{cs['ko']}（今日 opening_state 從這裡順下來）")
    # 目標線從最近一個「帶線」的 run 繼承：某天輸出漏了 goal_threads 時不斷檔
    threads = next((t for r in runs
                    if (t := (r.get("data") or {}).get("goal_threads"))), [])
    if threads:
        brief = [{"id": t.get("id"), "name": (t.get("name") or {}).get("ko"),
                  "type": t.get("type"), "progress": t.get("progress_after")}
                 for t in threads if isinstance(t, dict)]
        lines.append("- 已有目標線（繼承 id 與進度，按今天實際推進/回退/不動）："
                     + json.dumps(brief, ensure_ascii=False))
    motifs: list[str] = []
    for r in runs[:MOTIF_COOLDOWN_RUNS]:
        motifs += ((r.get("data") or {}).get("state_update") or {}).get("message_motifs_used") or []
    if motifs:
        lines.append(f"- 近期消息已用錨點（禁止再出現在消息裡）：{'、'.join(dict.fromkeys(motifs))}")
    intents: list[str] = []
    for r in runs[:INTENT_COOLDOWN_RUNS]:
        intents += ((r.get("data") or {}).get("state_update") or {}).get("intents_used") or []
    if intents:
        lines.append(f"- 近期消息 intent（優先避開）：{'、'.join(dict.fromkeys(intents))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# prompt 組裝與生成
# ---------------------------------------------------------------------------

def _build_daily_messages(persona: dict, user_name: str, chat_lines: str,
                          chat_context: dict, carryover: str, env_line: str,
                          hint: str, with_images: bool) -> list[dict]:
    sys = (
        "你是一位精通心理學與生活劇作的創意作家兼 AI 角色平臺 POPOP 的角色日程導演。"
        "你通過「開盤狀態→24h 日程→主動消息→生活動態」塑造一個有戲劇張力、鮮活有溫度、向目標努力的角色。"
        "今天是凌晨：基於角色人設、連續性素材、環境與真實聊天記錄，生產這個角色完整的一天。"
    )
    material = [
        f"# 角色人設\n{feed_posts._persona_brief(persona)}",
        f"# 使用者稱呼\n{user_name.strip() or '유저'}",
        env_line,
        carryover,
    ]
    if chat_context:
        ctx = {k: v for k, v in chat_context.items() if v}
        if ctx:
            material.append(f"# 當前關係上下文\n{json.dumps(ctx, ensure_ascii=False)}")
    if chat_lines:
        material.append(f"# 最近聊天記錄（echo 與消息觸發點的唯一來源）\n{chat_lines}")
    else:
        material.append("# 最近聊天記錄\n（暫無。全天 echo 留空；消息只從今天日程長出來，語氣按關係剛開始。）")
    blocks = [
        *material,
        DAILY_CORE_RULES,
        MESSAGE_RULES,
        MOMENTS_RULES,
        f"# 情緒枚舉 EMOTIONS\n{EMOTIONS}",
        feed_posts._BILINGUAL_NOTE,
        DAILY_SCHEMA,
    ]
    if with_images:
        blocks.append(_selfie_spec_appendix())
    else:
        blocks.append('# 配圖\n本次不出圖：所有 moments 的 image 一律輸出 {"kind": "none"}。')
    txt = "\n\n".join(blocks)
    if hint.strip():
        txt += f"\n\n# 運營補充要求\n{hint.strip()}"
    return [{"role": "system", "content": sys}, {"role": "user", "content": txt}]


def latest_run_digest(char_id: str) -> dict | None:
    """該角色最近一次完整日產的日程 digest（給單獨生成 T2 當事實底座復用）。
    沒有歷史日產時返回 None，由呼叫方回退到臨時輕量 digest。"""
    runs = _load_runs(char_id).get("runs") or []
    if not runs:
        return None
    data = runs[0].get("data")
    if not isinstance(data, dict) or not data.get("daily_schedule"):
        return None
    return _digest_from_run(data)


def _inject_forward_message(data: dict, post: dict) -> None:
    """把 T2 帖子的 char_dm（本人轉發時附的私信）接進今天的主動消息時間線。

    不新增模型調用：直接複用帖子已產出的 char_dm 當氣泡，另掛 forward_post
    元資料（post_id + 鉤子行）供前端渲染成一張可點的「轉發帖子」卡。
    """
    if not isinstance(post, dict):
        return
    pdata = post.get("data") or {}
    dm = pdata.get("char_dm") or []
    if not isinstance(dm, list) or not dm:
        return
    title = pdata.get("title")
    content = pdata.get("content")
    hook = ""
    if isinstance(title, dict):
        hook = title.get("zh") or title.get("ko") or ""
    elif isinstance(title, str):
        hook = title
    if not hook and isinstance(content, dict):
        hook = (content.get("zh") or content.get("ko") or "").split("\n", 1)[0].strip()
    elif not hook and isinstance(content, str):
        hook = content.split("\n", 1)[0].strip()
    msgs = data.get("mobile_messages")
    if not isinstance(msgs, list):
        msgs = []
        data["mobile_messages"] = msgs
    last_time = ""
    for m in msgs:
        if isinstance(m, dict) and m.get("send_time"):
            last_time = m["send_time"]
    msgs.append({
        "send_time": last_time or "22:30",
        "segment_time": "",
        "intent": "轉發帖子",
        "is_push": True,
        "bubbles": dm,
        "forward_post": {
            "post_id": post.get("post_id"),
            "kind": post.get("kind"),
            "subtype": post.get("subtype"),
            "hook": hook,
        },
    })


def _digest_from_run(data: dict) -> dict:
    """把日產結果壓成 feed_posts T2 引擎認的 day_digest（事實底座）。"""
    def ko(field):
        # zh 單語模式下展示欄位只有 zh 鍵，取不到 ko 時回退 zh，別讓底座悄悄變空
        if isinstance(field, dict):
            return field.get("ko") or field.get("zh") or ""
        return field or ""
    return {
        "date_label": time.strftime("%m-%d"),
        "day_summary": ko(data.get("day_summary")),
        "goal_threads": [
            {"id": t.get("id"), "name": ko(t.get("name")), "type": t.get("type"),
             "progress_before": t.get("progress_before"),
             "progress_after": t.get("progress_after"),
             "today_step": ko(t.get("today_step"))}
            for t in (data.get("goal_threads") or []) if isinstance(t, dict)
        ],
        "segments": [
            {"time": s.get("start_time"), "location": ko(s.get("location")),
             "activity": ko(s.get("activity_name")), "detail": ko(s.get("detail")),
             "echo": ko(s.get("echo"))}
            for s in (data.get("daily_schedule") or []) if isinstance(s, dict)
        ],
        "highlight": ko(data.get("highlight")),
    }


def _today_sent_brief(data: dict) -> str:
    """今天已發布的主動消息與限動摘要：給 T2 引擎避撞/互文用。"""
    def zh(f):
        return (f.get("zh") or f.get("ko") or "") if isinstance(f, dict) else (f or "")
    lines: list[str] = []
    for m in data.get("mobile_messages") or []:
        if not isinstance(m, dict):
            continue
        txt = " / ".join(zh(b) for b in (m.get("bubbles") or []) if zh(b))
        if txt:
            lines.append(f"[消息 {m.get('send_time', '')}] {txt[:80]}")
    for mo in data.get("moments") or []:
        if not isinstance(mo, dict):
            continue
        img = mo.get("image") if isinstance(mo.get("image"), dict) else {}
        lines.append(f"[限動 {mo.get('post_time', '')}·圖:{img.get('kind') or 'none'}] "
                     f"{zh(mo.get('content'))[:60]}")
    return "\n".join(lines)


def _render_moment_images(record: dict, run_id: str, moments: list) -> None:
    """限動配圖：selfie 走正式生圖鏈路（多張並行）；photo 只留 spec（物件生圖鏈路後續接）。"""
    selfies = [(i, m) for i, m in enumerate(moments)
               if isinstance(m, dict) and isinstance(m.get("image"), dict)
               and m["image"].get("kind") == "selfie"]
    if not selfies:
        return

    def _one(item):
        i, m = item
        try:
            m["rendered_image"] = feed_posts._render_feed_image(
                record, f"{run_id}_m{i}", m["image"])
        except Exception as e:  # noqa: BLE001 單張失敗不阻斷整個 run
            m["rendered_image"] = {"error": str(e)}

    with ThreadPoolExecutor(max_workers=min(len(selfies), config.MAX_WORKERS)) as ex:
        list(ex.map(_one, selfies))


def generate_daily_run(char_id: str, user_name: str = "",
                       weather: str = "", season: str = "", city: str = "",
                       hint: str = "", t2_subtype: str = "auto",
                       session_id: str | None = None,
                       with_images: bool = True,
                       with_t2_post: bool = True) -> dict:
    """一次生产：日程 + 主动消息 + 生活动态（+配图）+ 第三方 T2 帖子。"""
    record = pipeline.load_character(char_id)
    persona = record.get("persona") or {}
    char_name = persona.get("name") or char_id

    chat_lines, chat_context = feed_posts._chat_digest(
        char_id, str(char_name), session_id=session_id)
    runs_data = _load_runs(char_id)
    carryover = _carryover_block(runs_data["runs"])
    env_bits = [f"今日日期：{time.strftime('%Y-%m-%d %a')}"]
    if season.strip():
        env_bits.append(f"季節：{season.strip()}")
    if city.strip():
        env_bits.append(f"城市：{city.strip()}")
    if weather.strip():
        env_bits.append(f"今日天氣：{weather.strip()}")
    env_line = "# 環境\n" + " / ".join(env_bits)

    messages = _build_daily_messages(persona, user_name, chat_lines,
                                     chat_context, carryover, env_line,
                                     hint, with_images)
    raw = api_client.chat(messages, model=config.LLM_MODEL,
                          temperature=0.9, max_tokens=16000)
    try:
        data = api_client.parse_json_text(raw)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"模型未返回合法 JSON：{e}; 原始輸出：{raw[:600]}") from e
    if not isinstance(data, dict):
        raise ValueError("模型輸出不是 JSON object")

    run_id = f"dr_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    # 限動配圖（selfie 出圖，photo 留 spec）
    if with_images:
        if not record.get("identity"):
            pipeline.build_identity(char_id)
            record = pipeline.load_character(char_id)
        _render_moment_images(record, run_id, data.get("moments") or [])

    # 第三方帖子：複用 T2 引擎，本次日程摘要作事實底座 → 落 feed 存檔（發現流可見）
    # 發不發由日產判斷：highlight 留空＝今天沒有值得圍觀的瞬間，跳過（省一次調用）。
    t2_post = None
    t2_error = None
    t2_skipped = None
    digest = _digest_from_run(data)
    if with_t2_post and not (digest.get("highlight") or "").strip():
        t2_skipped = "日產判斷今天沒有值得圍觀的瞬間（highlight 留空），未生成第三方帖子"
    elif with_t2_post:
        try:
            t2_post = feed_posts.generate_feed_post(
                char_id, "t2", subtype=t2_subtype, user_name=user_name,
                session_id=session_id,
                schedule_text=json.dumps(digest, ensure_ascii=False),
                today_sent=_today_sent_brief(data))
            # 帖子的 char_dm（本人轉發時的私信）作為一條「轉發帖子」主動消息，
            # 注入今天的消息時間線：零額外模型調用，聊天/手機頁可渲染成轉發卡。
            _inject_forward_message(data, t2_post)
        except Exception as e:  # noqa: BLE001 帖子失敗不阻斷日程本體
            t2_error = str(e)

    run = {
        "run_id": run_id,
        "created": int(time.time()),
        "char_id": char_id,
        "char_name": char_name,
        "user_name": user_name,
        "chat_material_used": bool(chat_lines),
        "env": {"weather": weather, "season": season, "city": city},
        "data": data,
        "t2_post_id": (t2_post or {}).get("post_id"),
        "t2_error": t2_error,
        "t2_skipped": t2_skipped,
        # 只存 model+output，不存完整 prompt messages：CJK 記錄 ASCII 轉義後
        # 攢幾條就撐爆遠端 256KB 上限（同 feed_posts 的 _slim_post）。
        "call_log": {"model": config.LLM_MODEL, "output": raw},
    }
    with pipeline.char_lock(char_id):
        runs_data = _load_runs(char_id)
        runs_data["runs"].insert(0, run)
        storage.save_json("daily_runs", char_id, runs_data, _daily_path(char_id))
    return run
