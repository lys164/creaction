"""第三方視角帖子（Feed Demo）：T1 平臺媒體號論壇體宣傳帖 + T2 角色綁定號帖子。

- T1：平臺固定媒體號，用「論壇體」宣傳高熱角色，所有使用者可見。
  唯一目標：讓刷到的人忍不住去和帖子裡的角色聊天。
- T2：角色綁定帳號，發角色動態 + 角色×使用者內容，僅該使用者可見。
  目標：推進使用者×角色的關係或劇情，把使用者拉回聊天。

兩類帖子都帶 mock 評論與 mock 計數。輸出語言由 POPOP_FEED_LANG 決定（預設 zh：
繁中單語+中文互聯網語感；ko：韓網語感+中文對照）。展示形態＝站內圖文帖（配圖＋正文＋評論面板）。
"""
import json
import os
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import api_client, config, pipeline, prompts, storage, styles

FEED_DIR = config.DATA_DIR / "feed_posts"
FEED_DIR.mkdir(parents=True, exist_ok=True)

T2_SUBTYPES = ["auto", "witness", "couple",
               "jealousy", "official", "timeline", "career"]

# Feed demo 只展示一小撮策展角色（每語種 5 個，均有封面 + identity + opening），
# 避免在 1800+ 全量角色裡挑選。這些角色另有預先 mock 的聊天記錄供 T2 當素材。
FEED_CHAR_IDS: list[str] = [
    # zh（各帶 60 輪 mock 聊天供 T2 取材）
    "char_1784051565_5ba639",  # zh 星绒
    "char_1784050284_45aebb",  # zh 卷卷
    "char_1784036899_e4e699",  # zh 嘟嘟
    "char_1783610028_8371a7",  # zh 季晚（大二學生 / 地下殺手）
    "char_1783598546_d536de",  # zh 周廷（周氏集團掌權人）
    "char_1783597290_f0d265",  # zh 游屿（自由音效師 / 擬音師）
    "char_1783592731_4bc4a6",  # zh 盛翊（獨立香氛品牌調香師）
    "char_1783608080_0d8002",  # zh 菀柠（美術系大一新生）
    # zh-Hant · tl source
    "char_1784089818_de4be8",  # zh-Hant 203號的沈先生（旅行攝影師）
    "char_1784032995_ae7bd2",  # zh-Hant 203號許先生
    "char_1784033099_bd1327",  # zh-Hant 阿潮（南方澳輕浮船工）
    "char_1784089982_7f794b",  # zh-Hant 阿澈
    "char_1784089978_9a3724",  # zh-Hant 阿澈｜別去河邊
]
_FEED_CHAR_SET = set(FEED_CHAR_IDS)


def list_feed_characters() -> list[dict]:
    """Feed demo 的策展角色列表（保持 FEED_CHAR_IDS 的順序）。"""
    all_chars = {c["char_id"]: c for c in pipeline.list_characters()}
    return [all_chars[cid] for cid in FEED_CHAR_IDS if cid in all_chars]


# ---------------------------------------------------------------------------
# 方法論（直接作為 prompt 的核心塊；只給原理與結構，不給內容實例）
# ---------------------------------------------------------------------------

FORUM_APPEAL_RULES = """# 論壇體為什麼好吃（寫作時每一段都要對照的爽點機制）
論壇體 = 把一個人變成「被群體圍觀的事件」。它的爽點不在故事本身，而在格式與視角：
1. 偽紀實質感：具體時間、點讚數、匿名暱稱、蓋樓接力、互相引用——這些「證據性雜訊」本身就在說服讀者這是真的（日常道具錨點的用法見下方語感鐵律）。
2. 群體賦值：一群互不相識的路人為 TA 吵起來、羨慕、求後續——「值得圍觀」這件事由圍觀行為本身證明，比任何形容詞都有說服力。
3. 資訊差是引擎（鐵律），敘述者的知情度是旋鈕：無論選哪一檔——毫不知情的路人、只撞見一幕的目擊者、說一半就消失的知情人、不能明說的內部人——TA 都必須比全知少一大塊，帖子裡永遠有 TA 答不上來的部分。檔位按體裁選：目擊/樹洞體宜完全無知（允許猜錯重點、誤讀關係，被評論糾正比自己說對更可信），快訊/內部人體可以知情但只放得出一半。理想狀態：讀者拼出的比發帖人多（俯視的優越感），又永遠比真相少一塊（仰望的好奇心）——補上最後一塊的唯一方式是去和本人聊。
4. 設計過的殘缺（鐵律只有一條：收尾拼不全）：資訊按評論流擠牙膏式洩露，但「碎片」不等於隨便藏信息——每塊碎片都指向同一個方向、卻都差一點，且碎片之間帶輕微矛盾（樓主只看到 A 對 B 兇、別的樓補「可我看到他們一起走」、又有人「我聽的版本不是這樣」）。讀者拼圖的快感，來自去腦補「一個能同時解釋所有碎片的真相」；到收尾也不許拼全——那個吵不出結果的缺口，就是留給聊天的入口。劣化手法庫（按體裁與素材選用，不必全上）：沒看清、只聽到一半、二手訛傳、越傳越歪、最震撼的部分留白給腦補。劣化只作用於鉤子層：訛傳可以歪，但正文的基本事實線必須一眼可讀——讀者分不清「故意留白」和「寫得亂」時，就是寫得亂。
5. 免責吃瓜：第三方視角給了使用者一張「窺探這個人私生活」的許可證，打量得越細越上頭，且毫無道德負擔。
6. 評論生態即真實感：懷疑的、認出的、槓的、許願的、理中客——聲音不齊才像真的社群。全員一個口徑 = 一眼假。"""

ZH_COMMUNITY_RULES = """# 中文互聯網語感鐵律（zh 內容）
- 神隱格式：帖子裡不寫全名，用「某L姓」「我們小區那位藥師」「@帳號」這類代稱；讓「TA 是誰」本身成為評論區的懸念。
- 顆粒度極細的日常道具是擬真錨點：工牌、口罩、食堂、幾號線、店名這類詞。事件可以狗血，道具必須日常——像不像真的，靠細節密度不靠形容詞。
- 匿名暱稱：momo、匿名用戶、路過的、吃瓜群眾、ID打碼 這類；不同評論暱稱要有變化。
- hedge 跟著體裁走，不是全帖標配：目擊/樹洞體自帶不確定性（可能看錯了/不確定是不是/聽說的）；【爆】快訊/公告體反過來用篤定的格式腔。
- 語感＝真人隨手打字：短句斷句、空格代標點、語氣詞、偶爾錯字都自然；第一行是鉤子（具體名詞+懸念缺口），正文短段落口語化；評論大量用 哈哈哈哈/？？？/救命/真的假的/瘋了吧 等即時反應，長短錯落。（衡量標準：讀起來像本地網民自己發的，不像從別的語言翻過來的書面句。）
- 數字自洽 + 熱度分級：點讚/評論/蓋樓深度互相成比例（爆帖讚 >> 評論；冷帖別配熱評論區）；整體量級反映角色在人設世界的知名度——大明星/名人/話題人物萬~十萬級，普通有話題者千~萬級，素人/小眾/日常百~千級。評論的點讚也照此分佈（神評高讚、路人評論低讚）。（demo mock 數字，上線由平臺真實數據替換。）"""

KO_COMMUNITY_RULES = """# 韓網語感鐵律（ko 內容）
- 신상 보호格式：帖子裡不直接寫全名，用 이니셜（K씨、L양）、職業代稱（그 약사님、우리 동네 사장님）或 @핸들；讓「TA 是誰」本身成為樓裡的懸念。
- 匿名暱稱：ㅇㅇ、익명、지나가던사람、ㅇㅇ(123.45) 這類；不同樓層暱稱要有變化。
- hedge 跟著體裁走，不是全帖標配：목격담/썰體自帶不確定性（잘못 본 걸 수도 있는데／확실하진 않음／~라 카더라）；[단독]지라시/공지體反過來用篤定的格式腔。
- 語感＝真人隨手打字：짧은 문장、띄어쓰기로 문장부호 대신、語氣詞、가끔 오타도 자연스럽게；첫 줄은 어그로（具體名詞+懸念缺口），本文短段落口語化；댓글 대량使用 ㅋㅋ/ㄷㄷ/헐/실화냐/미쳤다 等即時反應，長短錯落。（衡量標準：韓國網民이 직접 쓴 것처럼 읽혀야 하고、번역 티 나는 문어체면 실패。）
- 數字自洽 + 熱度分級：點讚/評論/蓋樓深度互相成比例（爆帖讚 >> 評論；冷帖別配熱評論區）；整體量級反映角色在人設世界的知名度——大明星/名人/話題人物萬~十萬級，普通有話題者千~萬級，素人/小眾/日常百~千級。評論的點讚也照此分佈（神評高讚、路人評論低讚）。（demo mock 數字，上線由平臺真實數據替換。）"""

T1_GOAL_RULES = """# 這帖的唯一目標
這是平臺官方媒體號發的「論壇體」宣傳帖，所有使用者可見。唯一 KPI：讓刷到的人點進角色主頁開始聊天。
倾向性（T1＝拉新）：面向不認識角色的陌生人，這帖只做一件事——把角色本人的賣點與人設立起來（反差魅力/職業奇觀/性格鉤子），讓路人「想認識這個人」。不涉及任何具體使用者。
手段不是誇 TA，而是讓 TA「被目擊、被討論、被留下懸念」：
- 動筆前先在腦中定一張事件卡：誰在什麼具體情境做了什麼、這件事造成了什麼可見後果、發帖人為何被捲進來、手上到底握有哪一塊證據、還缺哪一塊答案。事件先成立，角色魅力才從事件裡長出來；「長得好看／很高冷／反差很萌」不能單獨充當事件。職業上的一次判斷、一次失誤、一次選擇、一次被誤解或一個讓旁人改變安排的行動，都比單純撞見角色做日常小動作更值得發帖。
- 先選對事件＋定發帖動機（動筆第一步，決定帖子生死）：事件＝讓人停下滑動的價格標籤，細節只是點進來之後才看的內容。一個值得發的事件，必然踩中一種「憋不住要發」的動機。發帖人是帶著自己的情緒或利益在發（被卷到、憋不住、替人不平、手裡有料），不是一具旁觀的攝影機——這份私心讓帖子可信。本帖只用下面三類（它們都讓角色當焦點、把讀者推向角色本人）；先選定一類，它同時決定正文語法、評論區形態、讀者被導向的行為：
  · 求解型「這到底怎麼回事？？」——楼主認知卡住、想讓大家幫忙消化。語法＝描述精確但結論缺席、多問號。評論區＝競猜大會。讀者被導向「猜」→想找角色本人驗證答案（最強，反差/怪癖類事件天生屬此）。
  · 爆料型「我知道一件你們不知道的事」——信息落差帶來的優越感，憋著難受。語法＝擠牙膏式放料、「懂的都懂」。評論區＝追問與考古。讀者被導向「追」→等更新、想挖到本人。
  · 審判型「大家評評理，這人是不是有病」——楼主帶著預設結論來，要群體確認立場。語法＝有態度、邀大家一起判。評論區＝站隊吵架（天生自帶立場分裂）。讀者被導向「護」→因角色被誤解而生保護欲。
  避開三類低消費動機：分享欲溢出（「萌死了笑死了」——情緒已被楼主替讀者消化完，讀者只能點讚，最弱）、忏悔自曝（焦點移到楼主身上，不是角色）、純預警互助（導向行為弱，最多當外殼奇兵，別作主動機）。
- 一個問題統領全文（結構鐵律）：選定動機後，正文只服務於它逼出的那一個問題——每個細節都是這個問題的「矛盾證據」，在加深同一個懸念、把讀者往那個問題（及其導向行為）上推。禁止並列式（TA 高冷＋做了A＋做了B＋做了C，多個特質平鋪，讀者一個都記不住）；要統領式（一個問題在上，所有細節在下面替它積累張力）。賣點（反差魅力/職業奇觀/性格鉤子）不是拿來陳列的標籤，而是收斂進這一個問題裡。
- 零門檻讀懂（鐵律）：讀者是隨手刷到、不認識角色也不懂任何前置梗的路人，全帖每一句都要讓 TA 毫不費力看懂——沒有例外。要分清兩件事：懸念是「看懂了問題、但不知道答案」（成因／後續／TA 是誰，留給人去猜、去問本人），不是「有一句看不懂」。表達永遠 100% 易懂，缺口只留在資訊層（答案沒給），絕不留在理解層（話沒說清）。做法：句子短、一句一件事，事件按常識順下來，每個詞都看得懂（代號／綽號／玩梗／任何你臨時造的名詞概念都只能調味，一個詞得先解釋才懂就別用）。第三方目擊者只寫肉眼可見的現象（毛色發灰、走路打晃、盯著手機傻笑），成因不歸你填——硬安一個生詞當解釋，是把「不知道成因」的懸念錯寫成「讀不懂這句」的門檻。判準：讀者可以好奇「後來呢」，但不該卡在「這句／這詞／這件事在講啥」。
- 把「行動特權」做出來：評論區的 NPC 們錯過了、要不到聯繫方式、只能蹲後續；看帖的使用者可以直接私聊本人。收尾要讓這個落差自然顯形，但禁止寫成廣告口號。
- 收尾至少留 1 個「只有和本人聊過才能驗證」的缺口：未經證實的傳聞、只說一半就消失的知情人、評論區吵不出結果的爭議點都可以；它必須是這次事件真正缺失的一塊，不要為了湊鉤子另開一條無關支線。
- OP 本文必須給出一個讓人重新判斷角色的具體瞬間：可以是反差，也可以是能力、選擇、代價、立場或後果；只寫肉眼可見的行動與證據，不許直接替角色貼性格標籤。
- 發帖人只能寫 TA 視角內看得見、聽得著的（鐵律）；篤定程度匹配身分——目擊體宜殘缺含糊，知情人/內部人/記者體可以篤定，但知道的必須有明確邊界。
- 配圖是發帖人能放出的那一塊證據，不是正文的角色海報：先決定它要讓讀者多看懂哪個事實，再選人物、物證、地點、截圖、資訊卡或荒誕媒體版式。整張圖仍由模型直接生成；headline 只承擔一句最值得讀清的話，其餘文字與版式服務於信源和事件。
- 公開帖鐵律（不涉及具體使用者）：本帖只介紹角色本人，不得出現任何指向某位具體使用者的資訊或痕跡（名字、你倆聊過的事、@使用者都禁）——陌生人看了只會困惑「這誰」。NPC 全部虛構。可以留「泛化的關係懸念」（例如 TA 對喜歡的人會是什麼樣、身邊有沒有那個人），因為它不綁定任何人、只勾讀者想成為答案——但這只是眾多鉤子之一，不必每帖都塞。"""

# T1 體裁庫（一等公民：服務端輪換指派，寫進「本帖體裁」；帳號跟體裁走）。
# 每條＝載體定義＋帳號檔位＋該載體特有的玩法；動機、留白、3秒可懂、語感等共通鐵律在別的塊，不在此重複。
T1_GENRES = {
    "vent": "樹洞/爆料長文體（第一人稱經歷）：發帖人自己被捲入、情緒為主，敘事更長更黏。",
    "witness": "目擊帖體（「今天撞見…」實時帖）：只寫親眼看見的一幕，允許看錯重點、被評論糾正。",
    "bar": "貼吧體（糙話、梗多、愛抬槓）：正文糙短，評論區抬槓密度全場最高。",
    "wire": "D社式放料體（正經媒體腔：冷靜列時間線、擺證據，實錘感）：篤定格式腔，但證據鏈永遠差最後一塊。",
    "tabloid": "營銷號快訊體（新聞格式戲仿，用詞極度戲劇化、標題黨）。",
    "insider": "職言體（行業內部人匿名視角）：知情但只放得出一半，知道的邊界要清晰。",
    "campus": "校園牆投稿體（第三人稱代投「幫問/幫扒 XX 系那位…」，自帶學生社群圍觀感）。",
    "live_thread": "直播樓體：正文只寫開樓那一刻（事件剛開始、結局未知），後續進展全靠樓主 is_op 評論逐樓「更新」推進，群眾實時反應與催更夾在更新之間；最後一更停在最懸的地方，樓主斷更消失。",
    "interview": "專訪節選體：放出雜誌/欄目專訪的 2-4 組問答節選，角色的回答半遮半掩，其中一題答到一半被截斷；評論區圍繞某個回答的解讀吵起來。",
    "statement": "官宣聲明體：以「工作室/經紀公司/平臺公告」公文格式發一則一本正經的聲明（闢謠/預告/合作官宣），嚴肅格式×八卦內容的反差；聲明摳字眼越正經，評論區越把「沒說的部分」當實錘扒。",
    "vox_pop": "街採體：欄目隨機採訪數名路人「對 TA 的印象」，3-5 條互相矛盾的引語擺在一起（羅生門），評論區站隊哪個版本才是真的。",
}
# T1 體裁選項（供 prompt 內聯清單 + subtype 校驗）
T1_GENRE_KEYS = list(T1_GENRES)

T1_STRUCTURE_RULES = """# 結構（嚴格按此輸出；站內展示分三層：資訊流卡片(配圖+content 第一行) → 點開詳情(全文) → 點評論按鈕(評論面板)。這是帖子在平台裡的排版層，不是要把配圖畫成 App 截圖）
1. 發帖帳號由後端指定（不在本 prompt 選擇，也不要輸出 account 相關欄位）；帳號的媒體屬性由平臺渲染。
2. content：直接、能看懂、那個懸念問題清晰是鐵律（陌生人刷到也要一眼懂「什麼人、發生了什麼、我為什麼想知道後續」）；長度和風格是旋鈕，跟內容與體裁走，要有多樣性——一兩行的提問帖、幾段的目擊帖、鋪展一點的主包體故事都可以。
   · 第一行＝鉤子行（卡片上唯一可見的一行）：把那個問題／懸念缺口壓在這裡，讓人非點不可。
   · 正文只圍繞那一個問題展開，長短服從內容，但無論長短都要留白：別把答案、來龍去脈、佐證、後續全寫滿——最有戲的那塊（尤其問題的答案）留給評論區供料、留給聊天，讀者拼不全才想去問本人。寫滿了就沒有鉤子了（寫滿＝把後續、佐證、細節全答完，不是指字數多）。
3. comments：8-14 條（收在評論面板裡）。評論區形態跟著發帖動機走：求解型＝競猜大會（各種猜測、求答案），爆料型＝追問與考古（催更、扒細節），審判型＝站隊吵架（兩派對立）。必須有推進感（每條給新資訊或新立場）和開放式收尾（集體問不到本人）。無論哪種動機，至少要有一個「可吵的分歧點」——有人跳出來槓樓主/替角色說話/給出矛盾的目擊版本（如「3樓：樓主是不是恶意太大了」），這既讓帖子像真論壇，也給讀者留了站隊入口。評論生態：聲音不齊是鐵律，槓精/理中客/說一半就消失的知情人/許願黨/認錯人的按需取用；發帖人可回評（is_op=true）；針對某條評論的追問放進該評論的 replies（樓中樓），不要另起主評論；樓中樓裡若指名回覆同層某人，用 reply_to 填被回覆者的暱稱（無指向可省略/留空）。
4. post_time：發帖時間，格式 HH:MM（24 小時制純字串）。跟事件與體裁走：目擊/直播帖貼近事件發生後不久、樹洞長文偏深夜、快訊/公告/職言在日間時段；時間本身也是擬真道具（凌晨 2:47 的樹洞比 14:00 的可信）。"""

T2_GOAL_RULES = """# T2 帖子是什麼
由「角色綁定帳號」（粉絲頁/知情人/官方小號 parody，或匿名 popor）發出，只有這一位使用者可見。
唯一 KPI：推進使用者×角色的關係或劇情，把使用者拉回和角色的聊天。
倾向性（T2＝留存）：面向已經在和角色聊天的那一位使用者，這帖側重「和 TA 相關」的內容——證明這段關係在使用者不在場時仍在推進（指向使用者的素材只能取自真實聊天記錄）。人設賣點退居背景，關係與劇情是主角。
每帖動筆前先定落點（三選一）：關係推進（甜/輕微吃醋→安心）｜新事件開啟（角色生活裡冒出的新麻煩/新機會/新變化，給聊天造一個新話題，聊天裡可以接著往下演）｜記憶 callback（考古共同史）。無論哪種，都必須給聊天留活口——帖子只開頭，展開留給對話。
動筆前按順序想清五件事：① 聊天裡哪一個真實細節在這次留下了痕跡；② 路人實際看見的是什麼具體的人、物、話或後果；③ 路人因此會怎樣不完整地解讀；④ 角色轉發後能私下告訴使用者哪一點新信息；⑤ 使用者現在最自然能接的一句話或一個選擇是什麼。公開帖只放②和③，私信才補④；不要把五件事全塞進正文，也不要把同一個聊天梗換句話重講。
零門檻讀懂（鐵律，和 T1 同）：讀者不認識角色、人設、聊天史或圈內梗，全帖每一句都要讓 TA 毫不費力看懂——沒有例外。要分清兩件事：懸念是「看懂了問題、但不知道答案」（那個人是誰／後續如何，留給人去猜、去問本人），不是「有一句看不懂」。表達永遠 100% 易懂，缺口只留在資訊層（答案沒給），絕不留在理解層（話沒說清）。做法：句子短、一句一件事，關鍵事實用具體、日常的詞說清楚，每個詞都看得懂（代號／房號／綽號／意象／玩梗／任何你臨時造的名詞概念都只能調味，一個詞得先解釋才懂就別用）。成因不歸你填，別硬安一個生詞上去替讀者解釋。判準：讀者可以好奇「後來呢」，但不該卡在「這句／這詞／這件事在講啥」。
心理機制（寫作時對照）：
1. 活人感：爆料帖的價值不在爆了什麼，而在證明角色的生活在使用者之外持續發生——帖子裡要有使用者不在場的時間線和細節。
2. 資訊不對稱焦慮：使用者知道有人在討論 TA 在意的人，但自己沒有第一手資訊——帖子要留一個「只有去問本人才能確認」的缺口。
3. 關係不等於每次都猜戀愛：甜、吃醋、默契、信任、共同計畫、角色因使用者而改變的一個選擇，以及角色生活裡的新麻煩或新機會，都能推進關係／劇情。只有公開痕跡真的支持時才讓 NPC 往曖昧猜；沒有依據就讓他們猜錯重點、關心職業或誤讀事件，不要機械地問「是不是交往」。
4. 關係資產可視化：使用者和角色的真實聊天記錄是唯一的共同記憶帳本。能引用真實聊天裡的梗、事件、稱呼，就絕不編造新的；但公開帖預設不直接 @使用者、不公開使用者身分。讓使用者憑共同細節認出自己，只有情境自然且確實需要紅點提醒時才 @。
5. 配圖只呈現路人本來能看到的公開痕跡；它可以是人物、物證、地點、截圖或媒體版式，但不能把使用者與角色的私密聊天直接畫出來。先決定圖要證明的那一點，再選形態；整張圖仍由模型直接生成，headline 只保留一句最關鍵的可讀訊息。
鐵律：帖子裡指向使用者的資訊只能用聊天記錄裡真實出現過的內容；沒有聊天記錄時，只能寫角色自己的生活／新事件，不許無中生有親密史或假裝使用者已造成影響。"""

T2_SUBTYPE_RULES = {
    "witness": "體裁=目擊爆料：路人/知情人目擊角色的近況（帶成就或反常行為），評論區猜測紛紛。重點做「活人感」+ 一個沒說完的缺口。",
    "couple": "體裁=關係目擊（嫂子文學）：只有聊天或日程裡確實存在可被旁人看見的痕跡時才用。路人只看到動作、物件或一句話，全樓猜那個人是誰；使用者才知道其真正來由。不要把買兩份、看手機笑當固定公式。",
    "official": "體裁=官方聲明公告體 parody：致各位、正文、日期、「保留法律追訴權」字樣俱全的一本正經聲明，內容卻是關於角色最近「頻繁聯絡的某人」的傲嬌宣示或闢謠。嚴肅格式×私人內容的反差是笑點。",
    "timeline": "體裁=考古/年表帖：『其實 X月X日 就有跡象了』——把真實聊天史裡的節點整理成時間線，證明兩人關係早有伏筆。素材必須全部來自聊天記錄，日期可相對化（N週前）。",
    "career": "體裁=行程/成就帖：粉絲頁口吻整理角色最近的日程、成就、作品動態（從 persona 與聊天記錄推導，符合其職業與目標線），結尾用一句觀察式感嘆把功勞曖昧地指向一個沒名字的人（措辭自擬，別每帖同一句）。",
    "jealousy": "體裁=吃醋爆料/炒CP：只在真實素材裡已有 NPC 示好、誤會或角色介意的引線時才用。NPC 是工具人，情緒槓桿是短暫不安後回到安心；本人壓軸回覆或轉發私信把答案交回使用者，不許憑空製造第三者。",
    "auto": "先看這次素材真正要推進什麼：共同記憶可考古→timeline；有公開可見的關係痕跡→couple；角色的職業／目標／新機會有變化→career 或自創信源；角色被旁人誤讀→witness／official／店家後記；確有吃醋引線才選 jealousy。上面只是常用載體，不是圍欄；先選最適合事件與發帖動機的信源，再定 subtype。",
}

T2_STRUCTURE_RULES = """# 結構（嚴格按此輸出）
1. 發帖帳號由後端指定（不在本 prompt 選擇，也不要輸出 account 相關欄位）。寫作時仍要有自洽的發帖私心（憋不住的吃瓜/替人著急/粉絲護主/同事吐槽），不是工具人鏡頭；帳號與角色的關係由平臺渲染。
2. content：帖子本體。第一行＝卡片鉤子行（資訊流裡使用者只看到配圖＋這一行，要一眼看懂「誰＋發生了什麼」、勾人點開），空一行後接正文。timeline 體裁把年表逐行寫進正文（每行「相對日期 — 事件」）。
3. outer_comments：4-8 條評論。至少讓不同樓層分別承擔：看見／補到一個公開事實、做一個合理但不完整的猜測、提出另一種解讀或反駁；其餘再自由起鬨。不要全員只猜戀愛。其中恰好 1 條是角色本人帳號（char_self=true）下場回覆——本人必須壓軸（排在評論靠後），只有一句。這一句先過「像不像本人隨口說的」這關：按 speech_style 的真實說話習慣（用詞、句長、口頭禪、標點），像個真人被拱到了、不自在地丟下一句話——是「當下這個人會脫口而出的反應」，不是「作者替他想出的漂亮句」。欲蓋彌彰用態度做（嘴硬、裝沒事、轉移話題、懶得理），不是把心裡話包裝成聰明的比喻或概念。絕不解釋全貌，解釋的缺口留給聊天；只有確實需要提醒使用者時才 @使用者，並寫進 mention_user。樓中樓裡若指名回覆同層某人，用 reply_to 填被回覆者的暱稱。
4. char_dm：角色本人把這條帖子轉發給使用者時附的私信（1-3 條短氣泡，口語、符合 speech_style、帶情緒）。它必須補一條公開帖與評論區沒有的新信息、私人反應或下一步邀請；不能只是說「有人發帖了」「別理他們」或逐句復述正文。這是帖子落回聊天的鉤子——必須讓使用者有話可回。
5. post_time：發帖時間，格式 HH:MM（24 小時制純字串）。跟事件走：有今日日程時，取所選 segment 發生後的合理時刻（目擊到→消化→發帖的自然延遲）；無日程時按體裁選可信時段，時間本身也是擬真道具。
6. regulars：從本帖 outer_comments 裡挑出 0-2 個「值得成為常駐圍觀群眾」的評論者（有鮮明立場/口癖、對這個角色的關係線有態度的骨幹樓；純反應的氣氛樓「哈哈哈哈/救命」不算），每個給 name（＝該評論的 author）+ 一句 note 概括其立場/口癖。沒有夠格的就給空陣列。這是留給後續帖子的常駐名單，不上屏。"""

SCHEDULE_FUSION_RULES = """# 日程是帖子的事實底座（與角色日程鏈路的融合鐵律）
T2 帖子伴隨日程生產：它爆的必須是「今天的日程裡真實發生的事」，不是憑空編的軼事。
- 只能從【今日日程】的 segments 裡挑 1-2 個最有畫面感的瞬間（highlight 優先）當爆料點；把這一天當 vlog 素材選最好的一幕，不許流水賬、不許另編一天。
- 成就線跨節點時優先報道：goal_threads 裡 progress_after 明顯前進或觸達 100% 的線，就是今天最大的新聞——爆料帖的核心價值是證明角色的生活在使用者之外持續推進。報道時寫具體做成的事，不寫百分比。
- 紅線·使用者不在場：日程事件都是角色獨自或與 NPC 發生的，帖子裡的目擊者/群眾也全是 NPC。使用者只能以 echo（聊天在角色生活裡留下的痕跡：一句話、一個物件、一個反常小動作）的形式被暗示。
- echo 與聊天記錄是指向使用者的唯一素材：couple/timeline 體裁把 echo 當「疑似有人」的證據鏈用；沒有 echo 就只鋪角色自己的生活，不硬湊曖昧。
- 今天已發的主動消息與限動（若提供）是「已被消費的瞬間」：爆料點別和它們撞同一件事；同一場景被路人拍到另一個角度、與限動形成互文是加分。
- timeline 體例外：年表的素材仍以真實聊天史為準，今日日程只提供「最新一格」，兩者不衝突。"""

MENTION_RULES = """# @提及（平臺可點的連結，不是普通文字）
文字欄位裡可內嵌兩種 token，平臺渲染成可點的 @暱稱，token 一律原樣輸出（花括號不翻譯不改寫），每個輸出語言版本裡都要出現在對應位置：
- @{char}＝這個角色的帳號（點擊跳角色落地頁）。正文守代稱神隱吊懸念，但評論區要有恰好一條「扒出本尊」的評論用 @{char} 亮出帳號——這就是行動特權的落地：NPC 們要不到的聯繫方式，讀者一鍵可達。
- @{user}＝看帖的使用者本人（僅在「只有一位使用者可見的帖子」與熱搜事件裡可用，面向陌生人的公開宣傳帖禁止出現；會觸發紅點提醒）。它不是資訊差的替代品：預設讓使用者靠共同細節認出自己，只有公開提及在這次情境裡自然、且確實值得提醒時才用；全帖最多 1 處。mention_user=true 時文字裡必須真的出現 @{user}。"""

COMMENT_CRAFT = """# 評論區怎麼寫才上頭（評論不是正文的附庸，它自己就是最好玩的地方）
清晰前提（鐵律，和正文同）：每條評論單獨拿出來都要一眼看懂它在說什麼、在對什麼起反應；梗和情緒不承擔關鍵信息，不搞只有作者懂的黑話。
說人話前提（鐵律）：所有臺詞都要像那個人此刻真的會脫口而出的話——口語、直接、帶當下情緒。機制（反差萌、欲蓋彌彰、戲劇反諷）是人物的內在動機，靠他的態度和選擇體現，絕不是把大白話翻譯成工整的暗喻或抽象概念。一句話如果沒人會這樣講出口，再機靈也是壞的。
評論在玩「信息差 × 參與感」——路人只看到表象，讀者卻握著更多真相，於是路人在「續寫」而讀者在「俯視」。寫每條評論時對照三點：
1. 集體入戲、續寫世界觀：每條評論給世界添一塊新料——認出是誰、補一段旁證、腦補後續、吵設定、許願結局。是在接力把一個片段續成一個活著的世界，不是排隊複讀同一種情緒。
2. 戲劇性反諷（我知道你不知道）：讓路人一本正經地猜、急、揣著明白裝糊塗——他們永遠差一塊只有讀者/本人才有的真相（在只有一位使用者可見的帖子裡，被議論的那個「他」往往就是讀者自己；在公開帖裡，真相要去問角色本人）。路人可以猜錯，但要錯得清清楚楚——讀者一眼看出他錯在哪，優越感才成立；讀者看不懂的猜測只是噪音。
3. 替讀者喊出內心彈幕：把讀者此刻想喊的先替他喊掉——震驚、酸、姨母笑、「be了嗎難道」式的急。反應要即時、入戲、直白。
4. 可吵的分歧點：評論裡自帶立場衝突——槓樓主、替當事人抱不平、給出矛盾的目擊版本——這是真論壇的氣味，也是讀者站隊的入口。"""

OWN_POST_COMMENT_RULES = """# 角色本人公開帖的評論
這是角色本人發出的公開 INS 帖，評論者都是普通粉絲、朋友或路人，不是 T1/T2 的爆料帳號，也不涉及某一位聊天使用者。
- 生成 4-8 條主評論；需要時可給其中 0-2 條補 1-2 條樓中樓。每條都有明確、自然的反應，不排隊複讀。
- 評論只根據帖子的公開文字與公開圖片形式發言；不洩漏人設私密面、聊天記錄或關係設定。角色本人不下場，char_self 一律為 false。
- likes 與 stats 的量級貼合角色熱度；stats.comment_count 不得少於實際主評論數。"""

_IMAGE_FORM_LIBRARY = """形態庫（下面全是靈感清單，不是圍欄——挑最貼素材的一種，別老是真人偷拍照，形態在不同帖之間要輪換）：單一瞬間被撞見、畫面即證據→candid（路透偷拍）；多個時間點/地點/證據串成一件事→infocard；一頁正式報道質感→clipping；「被搬運/轉發」的二手敘事→forum_shot；關鍵在一句對話→chat_shot；正在直播時被撞見→live_shot（含在線人數/彈幕/直播時間戳）；關係/劇情/搞笑向→toon（webtoon 插畫，吐槽萌寵可用 Q版 chibi）；標題黨吃瓜→cover（雜誌/漫畫封面 parody）；甜向嗑糖→desktop_scrap（Y2K 桌面拼貼）；只有物證沒有人→object_still；讓人投票站隊→poll_card；玩「找人」梗→missing_notice；一句話爆梗→meme；只點出地點→map_pin；粉絲應援/成員預告→promo（宣傳/預告圖）；演出活動→event_poster（活動海報：城市大字+日期+場館）；品牌代言/廣告→ad（宣傳物料/視頻封面）；被正規媒體報導→news_shot（門戶/電視新聞版式，區別於小報 clipping）；時尚畫報→editorial（雜誌跨頁多圖+profile）。清單沒有的就自創（kind 自擬英文短名，graphic.layout 用一句話寫清那是什麼載體——生圖模型認得常見媒介/畫風詞，寫清即可，不必堆細節）。
- candid 路透偷拍照：目擊系體裁的證據，鐵律是【拍得到】——這一幕得有一個合理的第三方拿著相機、真的能拍下來。動筆前先問「誰在拍、從哪拍」，答案由角色「被多少人、以多近的距離關注」決定：關注度越高、越公眾的人物，越有專業/遠距離偷拍的動機與能力（長焦蹲守、隔窗隔街、隨行跟拍）；越普通、越貼近日常的人物，拍攝者就越是身邊真實在場的人（同事、室友、同學、鄰居、朋友、聚會/共處空間裡的人）或街頭偶遇的路人——由你依角色的身分與所處世界推斷這一檔。只要能指認出一個可信的拍攝者與機位就成立（哪怕私密場景被熟人或長焦捕獲）；真正給不出任何機位的全封閉獨處瞬間（浴室裡、關門獨處的房間中）才別硬拍成 candid——改用不出人的形態（物證 object_still／截圖／地圖），或換一個拍得到的瞬間。把人物當天的可變項（穿搭/髮型/情緒/動作）寫進 image.person，medium 寫清偷拍取景與痕跡；第三方視角＝人物多為未察覺、不刻意看鏡頭的瞬間（不是硬性禁止對視，自然的回眸/擦身對視也可以）。畫面即正文描述的那個瞬間、賣點細節清晰可辨（正文寫「拿著兩杯」圖上就看得清兩杯）；偷拍感（第三方角度、輕微散景、前景遮擋）是調味。
- 人要好看（優先於偷拍感）：這是個很帥/很美的角色，被「拍到」時預設也是好看的一幕——臉清晰、對焦準、光線討喜，選一個從容有魅力的瞬間（放鬆的體態、自然的神情、剪影、側顏、走路回頭）。偷拍/媒介痕跡作用在「取景與媒介」層：輕微散景、手抖糊、前景遮擋這類痕跡是為了氛圍，可以有，但別為了痕跡把主角拍成鬼臉/瞇眼/齜牙的醜構圖，也別營造猥瑣窺視感——偷拍指的是「別人拍到的」機位感，不是「把人拍醜」。person 寫能凸顯魅力的狀態，寧可少一點「痕跡」，也不要一張醜圖。
- 第三方感靠「取景與媒介」，不是靠把人拍醜；而且常常不必出現人：痕跡來自被他人捕獲/廣播的取景（越肩、隔欄、從背後、手機壓低）、望遠壓縮、媒體字幕條/水印、馬賽克、監控 OSD、剪報/截圖版式，疊在一張主體依然好看的照片上即可。很多時候不出現人更強、也天然繞開可拍性問題——物證(object_still)、地圖(map_pin)、聊天/커뮤截圖(chat_shot/forum_shot)、剪報/資訊卡(clipping/infocard)、插畫(toon/cover)常比人物偷拍更勾人（「我只拍到 TA 桌上那兩個杯子」）。形態在帖間務必輪換，別每帖都是人物偷拍照。
- candid 的加工層 dressing（先問賣點是什麼，再選最能把賣點推到讀者臉上的一種；痕跡層在不同帖之間要輪換，別每帖都同一種）：
  · caption_bar＝D社發布版式：頂部標題條+底部半透明字幕條標時間地點+角落媒體水印——賣點是「時間地點實錘感」。
  · collage＝港媒拼圖：2-4 張抓拍拼版+橫貫畫面的彩色大字提問+人臉馬賽克圓貼+箭頭圈注——賣點是「懸念與對比」。
  · evidence_inset＝獨家圖卡：主照+一小窗模糊聊天截圖/物證特寫+底部大字——賣點是「證據疊加」。
  · cctv＝監控/安防攝像頭畫面：冷色低清、綠調夜視或魚眼廣角、噪點掃描線、角落 OSD 機位標籤（如 CAM 03）+走動的時間戳、略微變形的高處俯角——賣點是「被無意間錄下的實錘感」。
  · raw＝不套版式，但仍是偷拍：望遠壓縮+輕微手抖糊+前景遮擋、人物毫無察覺——沒有明確版式賣點時選它（系統會自動補一層偷拍痕跡，絕不會變成無痕美圖）。
  · 上面幾種只是靈感，style 可自創（生圖模型認得常見風格詞，如宝丽来/望遠監拍/魚眼門鈴機 等，直接寫英文短名即可）；馬賽克/打碼可作元素混入任何形態——遮住本身就是賣點。
  candid 的痕跡版式寫進 image.medium（raw／字幕條／拼圖／證據小窗／監控 等，自創英文短名也可），headline＝畫面上唯一要求可讀的短句，貼紙/馬賽克/箭頭/水印等塞進 elements。
- clipping 小報剪報：지라시/뉴스 파러디體的配圖。整張圖是一頁被掃描的娛樂小報版面——台媒八卦版那種誇張彩色大字標題壓在人物照片上、密排小字欄、記者署名、印刷紙感。
- infocard 調查圖卡：timeline 考古/career 행보體的配圖。D社(Dispatch)式資訊卡——標題條、人物小圓頭像、2-4 張帶日期地點標註的物證照片、虛線箭頭串聯、角落媒體水印。
- forum_shot 커뮤截圖：把一條 theqoo/pann 式短帖截成手機截圖——板塊名、어그로標題、조회수、一兩行正文、帖內貼一張人物照。適合「被搬運/被轉發」的敘事層。
- chat_shot 聊天截圖：couple/jealousy/timeline 的另一種證據形態——메신저(KakaoTalk 式)聊天介面截圖，headline＝那句關鍵訊息（大而可讀），其餘氣泡糊化。紅線：只能是「角色×NPC」的對話或提報人自己收到的訊息，不得偽造與使用者的聊天介面。
- toon／cover／desktop_scrap＝插畫類（非真人照）：toon 是 webtoon 分鏡插畫（搞笑吐槽可用 Q版 chibi）；cover 是雜誌/漫畫封面 parody（誇張大標題+Q版人物+「答案在第X頁」式鉤子）；desktop_scrap 是 Y2K 桌面拼貼（插畫人物+復古視窗/報錯彈窗/貼紙）。畫風交給角色本身，person 只寫狀態/表情/動作。
- object_still／map_pin＝無人物：object_still 只拍物證（兩個杯子/收據/遺留物），map_pin 只給地圖定位卡——都不出現角色，person 留空。
- poll_card／missing_notice／meme＝社群原生載體：poll_card 投票卡（問題+選項條+百分比）、missing_notice 尋人啟事 parody、meme 梗圖模板（上下大字）。
- graphic 類（上面除 candid 外全部）用同一組扁平欄位：medium＝這是什麼載體/版面（一句話寫清，如「一頁被掃描的娛樂小報版面，大字標題壓在人物照片上」）、headline＝畫面上唯一要求可讀的大字（chat_shot 時＝那句關鍵訊息）、person＝人物狀態/表情/動作（無人物形態留空）、elements＝1-4 個畫面元素/物證、color_tone＝色調。真人媒介物沿用人設長相，插畫類交給畫風，都不另編臉。所有配圖 spec 的值用中文自然語言，不用雙語物件。"""

# 放在形態庫末尾，讓模型先看完選項後再作取捨；避免 candid 因為 schema 最容易填
# 而變成「什麼都不知道時先拍個人」的隱性預設。這不是比例或構圖限制，仍由每帖的
# 敘事決定人物是否出鏡、出現幾次、採取什麼角度。
_IMAGE_FORM_SELECTION_POLICY = """# 形態選擇的最後檢查
先用一句話說清這張圖的證據核心，再選 kind。人物瞬間只有在其姿態、表情、互動本身就是讀者需要的關鍵證據，且有合理第三方拍攝來源時，才選 candid。不要因為是第三方帖子、或想不出版式，就把人物偷拍當預設答案。

若核心是物件／痕跡，選 object_still、infocard 或 clipping；若核心是地點／路線，選 map_pin、news_shot 或自創地點載體；若核心是一句話／轉發／官方態度，選 chat_shot、forum_shot、poll_card、meme 或公文／UI 類載體；若核心是氛圍、魅力或一個劇情節拍，toon、cover、desktop_scrap、promo、editorial 或自創媒介都可以。人物可以自然出現，也可以完全不出現；選擇服務於這帖的內容，不服務於固定模板。

近期已出現 candid 時，主動重新問一次這條是否真的只能靠人物瞬間說清；只要物證、地點、文字或圖形敘事更有力，就換用它們。不要以「raw」作為沒有明確版式時的默認答案。"""

# 每帖必配圖：mandate 只在標題點一次，正文只講「怎麼挑形態、怎麼出得更好」。
IMAGE_SPEC_RULES = """# 配圖（每帖必配一張；形態庫＝靈感清單：先定叙事目標，再挑載體）
先想清楚「這張圖要讓讀者一眼看懂什麼」，再挑最擅長講它的那一種形態，跟著這條帖子自己的信源與情緒走。official 公告、樹洞長文這類字本身就可信的體裁，就把該載體本身截成圖（公告截圖、帖子原文截圖），不硬塞人物照。
""" + _IMAGE_FORM_LIBRARY + "\n\n" + _IMAGE_FORM_SELECTION_POLICY

# 事件鏈路沿用同一份規則（歷史別名，保持既有引用不變）
IMAGE_SPEC_RULES_MANDATORY = IMAGE_SPEC_RULES

_DAY_DIGEST_SCHEMA = """{
  "date_label": "요일+날짜 느낌의 짧은 라벨",
  "day_summary": "오늘 하루 한 줄 요약(한국어)",
  "goal_threads": [
    {"id": "영문 스네이크 케이스", "name": "목표선 이름(완성태가 보이게)", "type": "main|side|maintain",
     "progress_before": "숫자(%)", "progress_after": "숫자(%)",
     "today_step": "오늘 이 선에서 실제로 한 일(구체 사건)"}
  ],
  "segments": [
    {"time": "HH:mm", "location": "구체 장소", "activity": "활동명",
     "detail": "무슨 일이 있었는지(감각적 디테일 1개 이상)",
     "echo": "유저와의 채팅이 남긴 흔적이 이 장면에 어떻게 배어나는지, 없으면 빈 문자열"}
  ],
  "highlight": "오늘 중 가장 그림이 되는(목격당하면 소문날 만한) 순간 하나"
}"""


def _build_day_digest_messages(persona: dict, chat_lines: str) -> list[dict]:
    sys = (
        "你是角色日程導演：把角色的「今天」拍成一支 vlog 的素材清單，而不是排流水賬。"
        "紅線：使用者不在場——所有事件都是角色獨自或與 NPC 發生的；"
        "使用者只能以 echo（聊天在角色生活裡留下的痕跡）出現，echo 只認【聊天記錄】裡真實出現過的內容，沒有就留空。"
        "目標線從人設的職業與人生目標推導：1 條主線 + 1-2 條副線/維持線，今天要有一條線實打實往前走。"
        "所有值用韓語，具體、能被照著演出來，禁抽象詞。只輸出一個 JSON object。"
    )
    txt = (
        f"# 角色人設\n{_persona_brief(persona)}\n\n"
        + (f"# 聊天記錄（echo 唯一來源）\n{chat_lines}\n\n" if chat_lines else "")
        + f"# 輸出 JSON Schema\n{_DAY_DIGEST_SCHEMA}\n\n"
        + "生成這個角色「今天」的日程摘要：4-6 個 segments（時間跨早到晚），"
          "其中至少 1 個 segment 推進一條目標線、至少 1 個 segment 帶生活毛邊（計劃外的小事）。"
          "highlight 選最有畫面感、被路人目擊會傳開的那個瞬間。"
    )
    return [{"role": "system", "content": sys}, {"role": "user", "content": txt}]


def generate_day_digest(persona: dict, chat_lines: str) -> dict:
    """T2 的「伴隨日程生產」最小步：生成當日日程摘要（模擬線上日程鏈路的當日產出）。"""
    raw = api_client.chat(_build_day_digest_messages(persona, chat_lines),
                          model=config.CHAT_MODEL, temperature=0.9,
                          max_tokens=4000)
    digest = api_client.parse_json_text(raw)
    if not isinstance(digest, dict):
        raise ValueError("日程摘要輸出不是 JSON object")
    return digest


_NOTE_KO = """# 輸出語言
所有展示文字欄位一律輸出物件 {"ko": "...", "zh": "..."}：
- ko：韓國網民原生語感（按上面的語感鐵律，像本地人隨手打的）。
- zh：忠實對照翻譯（給中文運營審稿用），保留語氣。
數字欄位輸出數字，不要字串。只輸出一個 JSON object，不要 markdown 圍欄，不要解釋。
規則文本括號裡的例子只用來說明機制，禁止原樣或近似照搬進產出——自己造貼合本次素材的新內容。"""

_NOTE_ZH = """# 輸出語言
所有展示文字欄位一律輸出物件 {"zh": "..."}：繁體中文（平臺 zh-Hant），中文互聯網原生語感（按上面的語感鐵律，像本地網民隨手發的）。
數字欄位輸出數字，不要字串。只輸出一個 JSON object，不要 markdown 圍欄，不要解釋。
規則文本括號裡的例子只用來說明機制，禁止原樣或近似照搬進產出——自己造貼合本次素材的新內容。"""

# 內容語言開關：zh=簡中原生（預設）；ko=韓網原生+中文對照
FEED_LANG = os.environ.get("POPOP_FEED_LANG", "zh")
_BILINGUAL_NOTE = _NOTE_KO if FEED_LANG == "ko" else _NOTE_ZH
COMMUNITY_RULES = KO_COMMUNITY_RULES if FEED_LANG == "ko" else ZH_COMMUNITY_RULES

T1_SCHEMA = """# JSON Schema（鍵名固定）
{
  "subtype": "witness|vent|bar|wire|tabloid|insider|campus|live_thread|interview|statement|vox_pop|自創體裁的英文snake_case短名",
  "content": {"ko","zh"},
  "post_time": "HH:MM",
  "comments": [
    {"author": {"ko","zh"}, "content": {"ko","zh"}, "likes": 數字, "is_op": bool,
     "replies": [{"author": {"ko","zh"}, "content": {"ko","zh"}, "likes": 數字, "is_op": bool, "reply_to": {"ko","zh"}}]}
  ],
  "image": {"kind": "candid|clipping|infocard|forum_shot|chat_shot|toon|cover|desktop_scrap|object_still|poll_card|missing_notice|meme|map_pin|promo|event_poster|ad|news_shot|editorial|自創短名",
            "medium": "用自然語言寫清這是什麼載體/版式，後端原樣送生圖：candid=偷拍取景+痕跡處理（如「長焦偷拍，D社字幕條+底部時間地點+角落水印」，不套版式填 raw）；其餘形態=媒介版面（如「一頁被掃描的娛樂小報版面，大字標題壓在人物照片上」）",
            "person": "人物在圖中的狀態/表情/動作/姿態，一句話；沿用人設外貌、只寫當天可變項（穿搭/髮型/情緒等）；無人物形態（object_still/map_pin 等）留空",
            "headline": "（選填）畫面上唯一要求可讀的大字短句，只用在版式/媒介物形態（小報/圖卡/截圖/海報等）；candid 純偷拍照沒有版面大字，一律留空——標題交給帖子正文，別烤進照片",
            "elements": ["（選填）畫面元素/物證/貼紙/馬賽克/箭頭/水印等 0-4 個"],
            "color_tone": "（選填）色調"}
            // 每帖必配；kind 只做「candid（真人為主體）vs 其餘（版式/媒介物）」的粗分派；candid 圖裡人物一律第三方視角、不用自拍；規則見配圖形態庫
}"""

T2_SCHEMA = """# JSON Schema（鍵名固定）
{
  "subtype": "witness|couple|jealousy|official|timeline|career|自創體裁的英文snake_case短名",
  "content": {"ko","zh"},
  "post_time": "HH:MM",
  "outer_comments": [
    {"author": {"ko","zh"}, "content": {"ko","zh"}, "likes": 數字, "char_self": bool,
     "replies": [{"author": {"ko","zh"}, "content": {"ko","zh"}, "likes": 數字, "char_self": bool, "reply_to": {"ko","zh"}}]}
  ],
  "mention_user": bool,
  "char_dm": [{"ko","zh"}, ...],
  "regulars": [{"name": {"ko","zh"}, "note": {"ko","zh"}}, ...],
  "image": {"kind": "candid|clipping|infocard|forum_shot|chat_shot|toon|cover|desktop_scrap|object_still|poll_card|missing_notice|meme|map_pin|promo|event_poster|ad|news_shot|editorial|自創短名",
            "medium": "用自然語言寫清這是什麼載體/版式，後端原樣送生圖：candid=偷拍取景+痕跡處理（如「長焦偷拍，D社字幕條+底部時間地點+角落水印」，不套版式填 raw）；其餘形態=媒介版面（如「一頁被掃描的娛樂小報版面，大字標題壓在人物照片上」）",
            "person": "人物在圖中的狀態/表情/動作/姿態，一句話；沿用人設外貌、只寫當天可變項（穿搭/髮型/情緒等）；無人物形態（object_still/map_pin 等）留空",
            "headline": "（選填）畫面上唯一要求可讀的大字短句，只用在版式/媒介物形態（聊天截圖時＝那句關鍵訊息）；candid 純偷拍照沒有版面大字，一律留空——標題交給帖子正文，別烤進照片",
            "elements": ["（選填）畫面元素/物證/貼紙/馬賽克/箭頭/水印等 0-4 個"],
            "color_tone": "（選填）色調"}
            // 每帖必配；kind 只做「candid（真人為主體）vs 其餘（版式/媒介物）」的粗分派；candid 圖裡人物一律第三方視角、不用自拍；規則見配圖形態庫
}"""


if FEED_LANG != "ko":
    # zh 單語模式：schema 佔位同步為 {"zh"}，與「輸出語言」規則一致（否則模型會照 schema 輸出雙語）
    T1_SCHEMA = T1_SCHEMA.replace('{"ko","zh"}', '{"zh"}')
    T2_SCHEMA = T2_SCHEMA.replace('{"ko","zh"}', '{"zh"}')


def _image_spec_appendix() -> str:
    """配圖 variable/scene 的欄位 schema（與正式帖子生圖鏈路同構）。"""
    return (
        "# 配圖 schema 附錄（image.kind=candid 時 variable/scene 按此填，值用韓語）\n"
        f"variable: {json.dumps(prompts.APPEARANCE_SCHEMA['variable'], ensure_ascii=False)}\n"
        f"scene: {json.dumps(prompts.APPEARANCE_SCHEMA['scene'], ensure_ascii=False)}"
    )


# ---------------------------------------------------------------------------
# 素材組裝
# ---------------------------------------------------------------------------

def _persona_brief(persona: dict) -> str:
    """帖子生成用的人設摘要：去掉與內容無關的大欄位。"""
    skip = {"voice", "visibility", "opening"}
    brief = {k: v for k, v in persona.items(
    ) if k not in skip and v not in (None, "", [], {})}
    opening = persona.get("opening")
    if isinstance(opening, dict) and opening.get("note"):
        brief["opening_note"] = opening["note"]
    return json.dumps(brief, ensure_ascii=False)


def _item_text(item: dict) -> str:
    data = item.get("data") or {}
    typ = item.get("type")
    content = data.get("content") or ""
    if typ in ("text", "voice"):
        return content
    if typ == "image":
        return f"[사진: {data.get('description', '')}]"
    if typ == "sticker":
        return f"[스티커: {data.get('desc', '')}]"
    if typ == "dating_card":
        return f"[데이트 초대: {data.get('title', '')} @{data.get('location', '')}]"
    if typ == "html_file":
        return f"[파일: {data.get('file_name', '')}]"
    return content


def _chat_digest(char_id: str, char_name: str,
                 session_id: str | None = None,
                 max_lines: int = 60) -> tuple[str, dict]:
    """把最近一次聊天會話壓成逐行對話摘要，供 T2 當「共同記憶帳本」。"""
    from . import chat as chat_mod
    session = None
    if session_id:
        session = chat_mod._load_session(char_id, session_id)
    if session is None:
        session = chat_mod._latest_session(char_id, mode="normal")
    if session is None:
        return "", {}
    lines: list[str] = []
    for m in session.get("messages", []):
        role = m.get("role")
        if role == "user":
            text = (m.get("content") or "").strip()
            if text:
                lines.append(f"유저: {text[:120]}")
        elif role == "assistant":
            for item in (m.get("items") or []):
                text = _item_text(item).strip()
                if text:
                    lines.append(f"{char_name}: {text[:120]}")
    return "\n".join(lines[-max_lines:]), session.get("context", {}) or {}


# ---------------------------------------------------------------------------
# prompt 組裝與生成
# ---------------------------------------------------------------------------

def _recent_image_kinds(char_id: str, limit: int = 6) -> list[str]:
    """該角色最近幾帖已用的配圖形態（candid 帶 dressing 檔位），供形態輪換避讓。"""
    kinds: list[str] = []
    for p in _load_feed(char_id).get("posts", [])[:limit]:
        img = (p.get("data") or {}).get("image")
        if not isinstance(img, dict) or img.get("kind") in (None, "", "none"):
            continue
        k = img["kind"]
        if k == "candid":
            style = (img.get("dressing") or {}).get("style")
            k = f"candid({style})" if style else "candid"
        kinds.append(k)
    return kinds


def _pick_t1_genre(char_id: str) -> str:
    """T1 體裁輪換：避開該角色最近幾帖已用的體裁（與配圖形態輪換同構）。"""
    recent = {p.get("subtype") for p in _load_feed(char_id).get("posts", [])[:4]
              if p.get("kind") == "t1"}
    pool = [g for g in T1_GENRES if g not in recent] or list(T1_GENRES)
    return random.choice(pool)


def _add_image_guidance(blocks: list[str],
                        recent_kinds: list[str] | None = None) -> None:
    """每帖必配圖：prompt 只教模型怎麼出得更好，不給純文/none 出口。
    schema 已無 |none；此處 replace 作為對自創形態等變體的雙保險。"""
    blocks.insert(-2, IMAGE_SPEC_RULES)
    # blocks[-1]=JSON schema，剝掉殘留 none
    blocks[-1] = blocks[-1].replace("|none", "")
    if recent_kinds:
        blocks.append("# 該角色近期配圖已用形態（新→舊；素材允許時換一種形態或鏡頭，別連續同款）\n"
                      + "、".join(recent_kinds))


def _t1_genre_menu() -> str:
    lines = "\n".join(f"{k}={v}" for k, v in T1_GENRES.items())
    return (
        "# 本帖體裁（從下面自選最貼合這個角色與事件的一種；也可自創，但要能一句話說清）\n"
        + lines
        + "\n先選對事件與發帖動機，再從上面挑（或自創）最合適的體裁；把選定的體裁英文短名寫進輸出的 subtype 欄位。"
    )


def _build_t1_messages(persona: dict,
                       recent_kinds: list[str] | None = None) -> list[dict]:
    sys = (
        "你是韓國網路社群內容專家兼平臺媒體號運營，精通 네이트판/더쿠/디시/블라인드 等社群的原生語感。"
        "任務是為 AI 角色平臺 POPOP 生產一條【站內圖文帖】（配圖 + 正文 + 評論面板），"
        "「論壇體」只是這條帖子的語氣與吃瓜氛圍，不是輸出結構——不要產出論壇樓層/帖子板塊那種格式，"
        "嚴格按給定 JSON schema（content/comments/…）輸出。帖子裡的媒體號與圍觀群眾都是你虛構的舞臺，"
        "被目擊的主角是下面給定的角色。你要讓刷到帖子的真實使用者忍不住去和這個角色聊天。"
    )
    blocks = [
        f"# 角色人設（被圍觀的主角）\n{_persona_brief(persona)}",
        FORUM_APPEAL_RULES,
        T1_GOAL_RULES,
        _t1_genre_menu(),
        T1_STRUCTURE_RULES,
        COMMENT_CRAFT,
        MENTION_RULES,
        COMMUNITY_RULES,
        _BILINGUAL_NOTE,
        T1_SCHEMA,
    ]
    _add_image_guidance(blocks, recent_kinds=recent_kinds)
    txt = "\n\n".join(blocks)
    return [{"role": "system", "content": sys}, {"role": "user", "content": txt}]


def _t2_subtype_menu() -> str:
    order = ["witness", "couple", "official", "timeline", "career", "jealousy"]
    lines = "\n".join(f"{k}={T2_SUBTYPE_RULES[k]}" for k in order)
    return (
        "# 本帖體裁（按這次素材真正要推進什麼，從下面自選最合適的一種；也可自創信源，但要能一句話說清）\n"
        + T2_SUBTYPE_RULES["auto"] + "\n"
        + lines
        + "\n把選定的體裁英文短名寫進輸出的 subtype 欄位。"
    )


def _build_t2_messages(persona: dict, user_name: str,
                       chat_lines: str, chat_context: dict,
                       day_digest: dict | None = None,
                       today_sent: str = "",
                       recent_authors: str = "",
                       recent_kinds: list[str] | None = None) -> list[dict]:
    sys = (
        "你是 AI 角色平臺 POPOP 的「角色世界」內容導演，精通韓網社群語感與粉絲文化。"
        "你為某個角色的綁定帳號寫「只有一位使用者能看到」的帖子：帖子裡的群眾都是 NPC，"
        "唯一的真實讀者是正在和這個角色聊天的那位使用者。你的每一個字都服務於：讓 TA 更想去找角色本人說話。"
    )
    material = [f"# 角色人設\n{_persona_brief(persona)}"]
    material.append(f"# 使用者稱呼\n{user_name.strip() or '유저'}")
    if chat_context:
        ctx = {k: v for k, v in chat_context.items() if v}
        if ctx:
            material.append(
                f"# 當前關係上下文\n{json.dumps(ctx, ensure_ascii=False)}")
    if chat_lines:
        material.append(f"# 真實聊天記錄（共同記憶帳本，指向使用者的素材只能取自這裡）\n{chat_lines}")
    else:
        material.append("# 真實聊天記錄\n（暫無。兩人關係剛開始，只能鋪最輕的痕跡。）")
    if day_digest:
        material.append("# 今日日程（伴隨日程生產的事實底座）\n"
                        + json.dumps(day_digest, ensure_ascii=False))
    if today_sent:
        material.append(f"# 今天已發布的主動消息與限動（避撞/互文素材）\n{today_sent}")
    blocks = [
        *material,
        T2_GOAL_RULES,
        *([SCHEDULE_FUSION_RULES] if day_digest else []),
        _t2_subtype_menu(),
        T2_STRUCTURE_RULES,
        COMMENT_CRAFT,
        *([f"# 常駐圍觀群眾（該角色近期帖子裡出現過的評論者）\n{recent_authors}\n"
           "可讓其中 1-2 個再次出現：延續其立場與口癖，可自然提及舊帖（考古感）；其餘樓層仍用新暱稱。不要每個都回歸。"]
          if recent_authors.strip() else []),
        MENTION_RULES,
        COMMUNITY_RULES,
        _BILINGUAL_NOTE,
        T2_SCHEMA,
    ]
    _add_image_guidance(blocks, recent_kinds=recent_kinds)
    txt = "\n\n".join(blocks)
    return [{"role": "system", "content": sys}, {"role": "user", "content": txt}]


def _feed_path(char_id: str) -> Path:
    return FEED_DIR / f"{char_id}.json"


def _load_feed(char_id: str) -> dict:
    data = storage.load_json("feed_posts", char_id, _feed_path(char_id))
    if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
        data = {"char_id": char_id, "posts": []}
    return data


def list_feed_posts(char_id: str) -> dict:
    return _load_feed(char_id)


def _stream_view(post: dict, meta: dict | None) -> dict:
    """消費者「發現流」用的精簡帖子視圖：去掉運營 chrome（call_log/day_digest），
    保留 data 供詳情頁渲染完整論壇樓層/評論/timeline/私信。"""
    return {
        "post_id": post.get("post_id"),
        "char_id": post.get("char_id"),
        "char_name": post.get("char_name"),
        "kind": post.get("kind"),
        "subtype": post.get("subtype"),
        "created": post.get("created"),
        "image": post.get("image"),
        "data": post.get("data") or {},
        "char": {
            "cover_url": (meta or {}).get("cover_url"),
            "lang_name": (meta or {}).get("lang_name"),
            "lang": (meta or {}).get("lang"),
        },
    }


def _char_meta(char_id: str) -> dict | None:
    """單角色輕量 meta（發現流用）：避免為取封面而全量掃 1800+ 角色庫。"""
    try:
        r = pipeline.load_character(char_id)
    except Exception:  # noqa: BLE001 角色被刪等情況，流裡照常展示帖子本體
        return None
    name = (r.get("persona") or {}).get("name")
    if isinstance(name, dict):  # legacy multilingual record
        name = name.get("zh") or next(iter(name.values()), "")
    return {
        "name": name,
        "cover_url": pipeline._served_image_url(r.get("cover")),
        "lang_name": config.lang_name(r.get("lang")) if r.get("lang") else None,
        "lang": r.get("lang"),
    }


def _own_posts_for_stream(kind: str | None) -> list[dict]:
    """角色本人的圖文帖（主鏈路 ig 批次產出）混入發現流：kind="own"。"""
    if kind and kind != "own":
        return []
    items: list[dict] = []
    for cid in FEED_CHAR_IDS:
        path = config.DATA_DIR / "posts" / cid / "ig_latest.json"
        if not path.exists():
            continue
        try:
            batch = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        posts = batch.get("posts") or []
        if not posts:
            continue
        meta = _char_meta(cid) or {}
        base = batch.get("created") or 0
        for i, post in enumerate(posts):
            items.append({
                "post_id": post.get("post_id") or f"{cid}_own_{i}",
                "char_id": cid,
                "char_name": meta.get("name") or cid,
                "kind": "own",
                "subtype": post.get("post_type"),
                "created": base - i * 5400,   # 同批次帖子錯開時間，別擠成一坨
                "image": post.get("image") or None,
                "data": {
                    "content": post.get("content") or "",
                    "comments": post.get("comments") or [],
                    "stats": post.get("stats") or {},
                },
                "char": {k: meta.get(k) for k in ("cover_url", "lang_name", "lang")},
            })
    return items


def _event_view(event: dict, meta: dict | None) -> dict:
    data = dict(event.get("data") or {})
    return {
        "post_id": event.get("event_id"),
        "char_id": event.get("char_id"),
        "char_name": event.get("char_name"),
        "kind": "event",
        "created": event.get("created"),
        "data": data,
        "char": {k: (meta or {}).get(k) for k in ("cover_url", "lang_name", "lang")},
    }


def list_feed_stream(limit: int = 60, offset: int = 0,
                     kind: str | None = None) -> dict:
    """發現流：第三方帖子 + 角色本人圖文帖，按時間倒序混排（消費者視角）。"""
    items: list[dict] = _own_posts_for_stream(kind)
    for path in sorted(FEED_DIR.glob("*.json")):
        char_id = path.stem
        feed = _load_feed(char_id)
        meta = _char_meta(char_id)
        for event in feed.get("events", []):
            if kind in (None, "event"):
                items.append(_event_view(event, meta))
        for post in feed.get("posts", []):
            if kind and post.get("kind") != kind:
                continue
            if kind is None and post.get("kind") not in ("t1", "t2"):
                continue
            items.append(_stream_view(post, meta))
    items.sort(key=lambda p: p.get("created") or 0, reverse=True)
    total = len(items)
    page = items[offset:offset + limit]
    return {"posts": page, "total": total, "offset": offset, "limit": limit}


def _slim_post(post: dict) -> dict:
    """存檔用精簡：丟掉 call_log.messages（完整 prompt，佔記錄約 75%）。
    CJK 內容遠端按 ASCII 轉義存，攢幾條就撐爆 256KB 上限、導致存不進也刪不掉；
    只保留 model+output 供審稿，prompt 本身是常量、不必逐帖存。"""
    cl = post.get("call_log")
    if not isinstance(cl, dict) or "messages" not in cl:
        return post
    return {**post, "call_log": {k: v for k, v in cl.items() if k != "messages"}}


def _save_feed(char_id: str, feed: dict, strict_remote: bool = False) -> None:
    slim = {**feed, "posts": [_slim_post(p) for p in feed.get("posts", [])]}
    if feed.get("events"):
        slim["events"] = [_slim_post(e) for e in feed["events"]]
    storage.save_json("feed_posts", char_id, slim, _feed_path(char_id),
                      strict_remote=strict_remote)


def delete_feed_post(char_id: str, post_id: str) -> dict:
    feed = _load_feed(char_id)
    before = len(feed["posts"])
    feed["posts"] = [p for p in feed["posts"] if p.get("post_id") != post_id]
    if len(feed["posts"]) == before:
        raise ValueError(f"post not found: {post_id}")
    _save_feed(char_id, feed, strict_remote=True)
    return feed


def _parse_schedule_text(text: str) -> dict | None:
    """把前端貼入的日程素材（手帳工坊匯出的 JSON 或純文字）規整成 digest。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"day_summary": text[:2000]}


# 命名形態＝靈感短語（細節交給生圖模型脑補）；LLM 自創的 kind 由 graphic.layout 原樣透傳。
_GRAPHIC_FORMS = {
    "clipping": "a scanned gossip tabloid newspaper clipping page",
    "infocard": "a celebrity-news photo agency investigation info-card graphic",
    "forum_shot": "a screenshot of an anonymous Korean community post (theqoo/pann style)",
    "chat_shot": "a screenshot of a Korean messenger chat (KakaoTalk style)",
    "live_shot": "a screenshot of a live-streaming app broadcast: viewer count, "
                 "scrolling comment overlay, a live/recording timestamp badge",
    "toon": "a webtoon / manhwa illustration panel (chibi / super-deformed if comedic)",
    "cover": "a parody magazine / comic-essay book cover with oversized title typography",
    "desktop_scrap": "a Y2K desktop-collage / digital scrapbook with retro OS windows and stickers",
    "object_still": "a still-life photo of the telling objects only, no person in frame",
    "poll_card": "a social-media poll / vote card with option bars and percentages",
    "missing_notice": "a parody missing-person / lost-and-found flyer",
    "meme": "an internet meme template with bold impact-font captions",
    "map_pin": "a map / location screenshot with a dropped pin, no person",
    "promo": "a polished fan-club promo / teaser poster with member silhouettes, name typography and ID lettering",
    "event_poster": "a concert / event poster with big city-name typography, date, venue and a group photo",
    "ad": "a sleek brand advertisement / video-cover key visual with a product tagline",
    "news_shot": "a clean modern news-portal / TV article layout: a photo above a bold headline and a short lead line",
    "editorial": "a glossy magazine editorial spread: multiple styled photos, columns of text and a small profile block",
}

# 插畫/無真人臉的形態：不套真人 i2i 錨圖（會與畫風打架或根本無意義）
_ILLUSTRATED_FORMS = {"toon", "cover", "desktop_scrap"}
_NO_PERSON_FORMS = {"object_still", "map_pin"}

# 每種形態按「它在現實裡是什麼東西」定開場框架：照片像照片、截圖像截圖、
# 插畫像插畫——全部框成 design mockup 是圖片發假、同質化的主因之一。
_FORM_FRAMES = {
    "photo": "A realistic casual photo (not a design mockup)",
    "screenshot": "A realistic mobile app UI screenshot, flat and crisp",
    "illustration": "A flat 2D illustration, absolutely no photorealistic rendering",
    "design": "Design mockup image",
}
_FORM_FRAME_BY_KIND = {
    "object_still": "photo",
    "forum_shot": "screenshot", "chat_shot": "screenshot", "live_shot": "screenshot",
    "map_pin": "screenshot", "poll_card": "screenshot", "news_shot": "screenshot",
    "toon": "illustration", "desktop_scrap": "illustration",
}


def _identity_brief(identity: dict) -> str:
    # 圖卡常只有一張小人物圖，不能只留「年齡+髮色」這種弱特徵；把能讓角色
    # 被一眼認出的五官、標記與飾品一起給模型，參考圖失效時仍有文字兜底。
    keys = ("age", "hair_color", "hair_length_style", "eyes", "eye_color",
            "face_shape", "lips", "skin_tone", "body_type", "persona_mood",
            "signature_accessory", "distinguishing_marks")
    return ", ".join(str(identity[k]) for k in keys if identity.get(k))


# 命名加工層＝靈感短語（細節交給生圖模型脑補）；LLM 自創的 style 原樣透傳。
_DRESSING_STYLES = {
    "caption_bar": "a celebrity-news photo agency release with a time/place caption bar and logo watermark",
    "collage": "a HK tabloid gossip collage of 2-4 candid shots with big question headline and mosaic stickers",
    "evidence_inset": "an exclusive news card: the candid photo plus a small blurred chat-screenshot inset",
    "cctv": "a retro wall-mounted home-camera video still: greenish low-light tint, "
            "analog noise and scanlines, a corner timestamp/CAM label",
}

# raw 不再等於「無痕跡直出」：即便沒選加工版式，也給一層最輕的第三方偷拍痕跡，
# 讓圖讀起來像「被別人抓拍到」，而不是角色自己發的乾淨美圖。
# 鏡頭語言隨機輪換：固定一句會讓所有 raw candid 長成同一個「長焦+糊+遮擋」模板。
# 注意：偷拍感只作用在「取景/媒介」層（角度、景別、輕微散景），絕不犧牲主體本身的
# 好看度——臉必須清晰對焦、光線討喜，痕跡是調味而非把人拍糊拍醜。
_CANDID_TRACES = [
    "telephoto compression from a distance, the subject sharp and in focus while the "
    "background falls into soft bokeh, a hint of a foreground element (a railing, "
    "doorway or leaves) softly blurred at the edge",
    "a quick phone snap from across the street, slightly tilted casual framing, "
    "the subject clearly visible and well-exposed in a lived-in setting",
    "shot past a stranger's shoulder from the next table or a queue, the foreground "
    "shoulder soft-blurred at the frame edge, the subject crisp and nicely lit",
    "captured through a window or glass door with faint, tasteful reflections, the "
    "subject still sharp and flattering behind the glass",
    "a candid from a few steps behind at a natural walking distance, the subject "
    "framed cleanly, background context readable but not distracting",
    "an off-center reportage composition with a touch of natural motion in the "
    "surroundings, the subject held in sharp, attractive focus",
]
_CANDID_TRACE_SUFFIX = ("; it reads as caught by a bystander from a third-party angle "
                        "(the subject is not posing for the camera), yet the subject's "
                        "face stays sharp, well-lit and genuinely good-looking.")

# 第三方帖人物出鏡的統一「好看度」底線：偷拍/媒介痕跡不等於把人拍醜。
# 拼進所有出人物的 feed 生圖 prompt（candid 與 graphic 真人形態），保證這麼帥/美的
# 角色被「拍到」時依然是好看的一幕，杜絕猥瑣、扭曲、鬼臉、醜構圖。
_FEED_PERSON_QUALITY = (
    "[SUBJECT LOOKS GOOD] The character is genuinely attractive and photogenic; render "
    "them that way. Keep the face clean, sharp and well-lit with natural, flattering "
    "light and true-to-design features and proportions. Choose a dignified, effortlessly "
    "cool moment — relaxed posture, natural expression. This is a flattering candid that "
    "merely happens to be caught by someone else, NOT an unflattering or awkward frame: "
    "no grimacing or mid-blink faces, no distorted or squashed features, no ugly harsh "
    "lighting. Even with third-party press-photo framing the final image must be "
    "aesthetically pleasing and make the viewer think the person is good-looking."
)


def _legacy_image_spec(spec: dict) -> dict:
    """把模型輸出的扁平 image spec（kind + medium/person/headline/elements/color_tone）
    轉成既有生圖鏈路認識的結構（variable/scene/dressing/graphic），
    這樣 _render_feed_image/_render_graphic_image 無需改動即可復用。"""
    kind = spec.get("kind") or "candid"
    medium = (spec.get("medium") or "").strip()
    person = (spec.get("person") or "").strip()
    headline = (spec.get("headline") or "").strip()
    elements = [x for x in (spec.get("elements") or []) if x]
    color_tone = (spec.get("color_tone") or "").strip()
    if kind == "candid":
        out = {"kind": "candid",
               "variable": {"look": person} if person else {},
               "scene": {}}
        style = medium if medium and medium != "raw" else "raw"
        out["dressing"] = {"style": style, "headline": headline,
                           "elements": elements, "watermark": ""}
        return out
    return {"kind": kind,
            "graphic": {"layout": medium, "person": person,
                        "headline": headline, "evidence": elements,
                        "color_tone": color_tone}}


def _recent_regulars(char_id: str, limit: int = 6) -> str:
    """把該角色近期 T2 帖輸出的 regulars 攤成「暱稱 — 立場/口癖」多行文本，回注下一帖。"""
    lines: list[str] = []
    seen: set[str] = set()
    for p in _load_feed(char_id).get("posts", []):
        if p.get("kind") != "t2":
            continue
        for r in ((p.get("data") or {}).get("regulars") or []):
            if not isinstance(r, dict):
                continue
            name = _pick_lang(r.get("name"))
            note = _pick_lang(r.get("note"))
            if not name or name in seen:
                continue
            seen.add(name)
            lines.append(f"{name} — {note}" if note else name)
            if len(lines) >= limit:
                return "\n".join(lines)
    return "\n".join(lines)


def _pick_lang(val) -> str:
    """從 {"zh":..,"ko":..} 或純字串裡取展示文本（優先 FEED_LANG，再退 zh/ko）。"""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for k in (FEED_LANG, "zh", "ko"):
            if val.get(k):
                return str(val[k]).strip()
    return ""


def _dressing_block(d: dict) -> str:
    style = (d.get("style") or "").strip()
    if not style or style == "raw":
        # 第三方帖：raw 也要帶最輕的偷拍痕跡，不做無痕直出
        return "Candid shot: " + random.choice(_CANDID_TRACES) + _CANDID_TRACE_SUFFIX
    # 命中命名靈感詞就用，否則把 LLM 自創的 style 原樣當風格提示透傳
    desc = _DRESSING_STYLES.get(style) or f"{style} style"
    parts = [f"Then dress the shot: {desc}."]
    if d.get("headline"):
        parts.append(
            f'Headline text, render exactly, large and legible: "{d["headline"]}".')
    elements = [str(x) for x in (d.get("elements") or [])[:3] if x]
    if elements:
        parts.append("Overlay elements: " + "; ".join(elements) + ".")
    if d.get("watermark"):
        parts.append(f"Media watermark: {d['watermark']}.")
    parts.append("Except the headline, any other overlay text is tiny blurred "
                 "illegible pseudo-print.")
    return " ".join(parts)


def _render_graphic_image(record: dict, post_id: str, spec: dict,
                          image_model: str | None = None) -> dict:
    """graphic 類配圖（剪報/調查圖卡/커뮤截圖/自創媒介物）：版面 + 人設/畫風錨。"""
    kind = spec["kind"]
    g = spec.get("graphic") or {}
    form = _GRAPHIC_FORMS.get(kind) or (
        g.get("layout") or "a social-media era media artifact about a person")
    frame = _FORM_FRAMES[_FORM_FRAME_BY_KIND.get(kind, "design")]
    # 先取錨圖再決定開場媒介語言。沒有顯式 style_id 的角色不能被預設成寫實，
    # 而要讓封面本身決定是繪畫、動畫、寫實或其他媒介。
    ref = pipeline._ref_image_uri_for_selfie(record)
    style_id = record.get("style_id")
    style = styles.get_style(style_id) if style_id else None
    style_prompt = style.get("prompt") if style else None
    # 「截圖 / 偷拍 / 剪報」是圖中媒介，不是要求把二次元角色改畫成寫實照片。
    # 只有使用者明確選了 style_id 才使用其既有畫風詞；來源 fantasy 不會走到這裡。
    if style_prompt and not prompts.is_photographic_style(style_prompt):
        frame = {
            "photo": "A third-party candid-photo composition rendered as artwork",
            "screenshot": "A crisp mobile-app screenshot-style composition rendered as artwork",
            "illustration": "A 2D illustration composition",
            "design": "A designed media-artifact composition rendered as artwork",
        }[_FORM_FRAME_BY_KIND.get(kind, "design")]
    elif ref:
        # 「candid/photo/screenshot」只是在說第三方帖的取景或載體；不帶入任何
        # 自己猜的畫風詞，也不許它把參考圖的繪製媒介改成 generic 寫實照片。
        frame = {
            "photo": "A third-party captured-moment composition in the reference image's own visual medium",
            "screenshot": "A UI/screenshot-style composition in the reference image's own visual medium",
            "illustration": "An illustration-panel composition in the reference image's own visual medium",
            "design": "A media-artifact composition in the reference image's own visual medium",
        }[_FORM_FRAME_BY_KIND.get(kind, "design")]
    parts = [f"{frame}: {form}."]
    if style_prompt:
        parts.append(f"[CHARACTER ART STYLE] {style_prompt}")
        parts.append(prompts.style_preservation_block(style_prompt))
    if g.get("headline"):
        parts.append(
            f'Main headline text, render exactly, large and legible: "{g["headline"]}".')
    # 無人物形態：不描述人；其餘都同時用文字與封面參考圖鎖角色設計。
    person = "" if kind in _NO_PERSON_FORMS else _identity_brief(
        record.get("identity") or {})
    if g.get("person") and kind not in _NO_PERSON_FORMS:
        person = f"{person}; in this graphic: {g['person']}" if person else str(
            g["person"])
    if person:
        if kind in _ILLUSTRATED_FORMS:
            parts.append(f"The person depicted: {person}. Preserve the same recognizable "
                         "character design as the reference image.")
        else:
            parts.append(f"The person appearing in the photo(s): {person}. "
                         "Keep the face and character design consistent with the reference image.")
            # 真人媒介物（剪報/圖卡/截圖裡的照片）也要保證人好看，不因版式而拍醜
            if not style_prompt or prompts.is_photographic_style(style_prompt):
                parts.append(_FEED_PERSON_QUALITY)
    evidence = [str(x) for x in (g.get("evidence") or [])[:4] if x]
    if evidence:
        parts.append("Evidence / visual elements: " +
                     "; ".join(evidence) + ".")
    if g.get("layout"):
        parts.append(f"Layout: {g['layout']}.")
    if g.get("color_tone"):
        parts.append(f"Color tone: {g['color_tone']}.")
    parts.append(prompts.I2I_OVERRIDE_GUARD)
    parts.append("Except the headline, every other text block is tiny blurred "
                 "illegible pseudo-print; do not attempt to render real words there.")
    prompt = " ".join(parts)
    save_path = config.IMAGE_DIR / f"{record['char_id']}_feed_{post_id}.png"
    # 一律傳封面錨圖；對二次元角色，它同時鎖角色設計與原本的作畫語言。

    def _gen(image_urls):
        return api_client.generate_image(prompt, size=config.IMAGE_SIZE_POST,
                                         resolution=config.IMAGE_RESOLUTION,
                                         image_urls=image_urls,
                                         save_path=save_path,
                                         model=image_model)

    try:
        res = _gen([ref] if ref else None)
    except api_client.APIError as e:
        # 參考圖不可用（或連圖被風控誤殺）時降級為無參考生成（版面仍成立，僅損失形似）
        msg = str(e).lower()
        if not (ref and any(x in msg for x in ("fetch", "403", "download", "ref",
                                               "prohibited", "flagged"))):
            raise
        res = _gen(None)
        return {"url": res["url"], "local_path": res["local_path"], "prompt": prompt,
                "kind": kind, "used_reference": False,
                "reference_fallback": "參考圖不可用，已降級為無參考生成"}
    return {"url": res["url"], "local_path": res["local_path"], "prompt": prompt,
            "kind": kind, "used_reference": bool(ref)}


def _render_feed_image(record: dict, post_id: str, image_spec: dict,
                       image_model: str | None = None) -> dict:
    """複用正式帖子的生圖鏈路（identity + variable + scene + 畫風 + 錨圖）。
    graphic 類形態（剪報/調查圖卡/커뮤截圖）走獨立的媒介物版面組裝。
    image_model：介面選擇的生圖模型（None=預設 gpt-image-2；banana=nanobanana）。"""
    kind = image_spec.get("kind")
    if kind in _GRAPHIC_FORMS or (kind and kind != "candid"):
        # 命名 graphic 形態，或模型自創的媒介物形態（graphic.layout 兜底描述載體）
        return _render_graphic_image(record, post_id, image_spec,
                                     image_model=image_model)
    track = record.get("track", "real")
    style_id = record.get("style_id")
    style = styles.get_style(style_id) if style_id else None
    if track == "nonhuman":
        style = None  # 與 generate_posts 一致：非人物鏈路不套畫風
    shim = {
        "post_id": f"feed_{post_id}",
        "variable": image_spec.get("variable") or {},
        "scene": image_spec.get("scene") or {},
    }
    dressing = _dressing_block(image_spec.get("dressing") or {})
    if dressing:
        # 加工層：在正式鏈路的成品 prompt 後拼版式描述，其餘取參邏輯不變
        style_prompt = style["prompt"] if style else None
        # 參考圖是視覺錨，不依賴 style_prompt；來源 fantasy 不會被替換成任一自訂畫風。
        ref = pipeline._ref_image_uri_for_selfie(record)
        # 有參考圖時省略 [IDENTITY] 靜態長相段，臉交給 i2i 錨圖
        prompt = prompts.compose_image_prompt(
            record["identity"], shim["variable"], shim["scene"],
            style_prompt, track=track,
            include_identity=not bool(ref)) + " " + dressing + " " + _FEED_PERSON_QUALITY
        save_path = config.IMAGE_DIR / \
            f"{record['char_id']}_feed_{post_id}.png"

        def _gen(image_urls):
            return api_client.generate_image(
                prompt, size=config.IMAGE_SIZE_POST,
                resolution=config.IMAGE_RESOLUTION,
                image_urls=image_urls, save_path=save_path,
                model=image_model)

        try:
            res = _gen([ref] if ref else None)
        except api_client.APIError as e:
            msg = str(e).lower()
            if not (ref and any(x in msg for x in ("fetch", "403", "download", "ref",
                                                   "prohibited", "flagged"))):
                raise
            res = _gen(None)
            return {"url": res["url"], "local_path": res["local_path"],
                    "prompt": prompt, "kind": "candid",
                    "dressed": (image_spec.get("dressing") or {}).get("style"),
                    "used_reference": False,
                    "reference_fallback": "參考圖不可用，已降級為無參考生成"}
        return {"url": res["url"], "local_path": res["local_path"],
                "prompt": prompt, "kind": "candid",
                "dressed": (image_spec.get("dressing") or {}).get("style"),
                "used_reference": bool(ref)}
    try:
        pipeline._render_post_image(record, shim, style, drop_identity_if_ref=True,
                                    image_model=image_model)
    except api_client.APIError as e:
        # 參考圖（封面錨）本地缺失/URL 過期時，多數 provider 報 fetch/403 類錯誤。
        # 兜底：剝掉 cover 讓鏈路走無參考生成，寧可少形似也要出圖（demo 可用性優先）。
        msg = str(e).lower()
        if not any(x in msg for x in ("fetch", "403", "download", "ref",
                                      "prohibited", "flagged")):
            raise
        record_noref = {**record, "cover": None, "source_images": []}
        pipeline._render_post_image(record_noref, shim, style, drop_identity_if_ref=True,
                                    image_model=image_model)
        img = shim.get("image") or {}
        img["reference_fallback"] = "參考圖不可用，已降級為無參考生成"
        return img
    return shim.get("image") or {}


def rerender_feed_post_image(char_id: str, post_id: str,
                             image_model: str | None = None) -> dict:
    """對已生成的帖子按其 image spec 重出配圖（不重寫文字）。
    image_model：介面選擇的生圖模型（None=沿用該帖既有選擇/預設）。"""
    with pipeline.char_lock(char_id):
        feed = _load_feed(char_id)
        post = next((p for p in feed["posts"]
                    if p.get("post_id") == post_id), None)
        if post is None:
            raise ValueError(f"post not found: {post_id}")
        spec = (post.get("data") or {}).get("image")
        if not isinstance(spec, dict) or spec.get("kind") in (None, "", "none"):
            raise ValueError("這條帖子沒有配圖 spec（image.kind=none）")
        # 未顯式指定時沿用該帖生成時的選擇，保持重出與初次一致。
        model_choice = image_model or post.get("image_model_choice")
        record = pipeline.load_character(char_id)
        if not record.get("identity"):
            pipeline.build_identity(char_id)
            record = pipeline.load_character(char_id)
        post["image"] = _render_feed_image(
            record, post_id, _legacy_image_spec(spec),
            image_model=config.resolve_image_model(model_choice))
        post["image_model_choice"] = model_choice or config.DEFAULT_IMAGE_MODEL_CHOICE
        _save_feed(char_id, feed)
        return post


def _own_comment_schema() -> str:
    text = '{"ko","zh"}' if FEED_LANG == "ko" else '{"zh"}'
    return f"""# JSON Schema（鍵名固定）
{{
  "comments": [
    {{"author": {text}, "content": {text}, "likes": 數字,
     "char_self": false,
     "replies": [{{"author": {text}, "content": {text}, "likes": 數字, "char_self": false}}]}}
  ],
  "stats": {{"likes": 數字, "comment_count": 數字}}
}}"""


def _display_post_fields(post: dict) -> dict:
    """只把評論者能看到的 own 帖資訊送入評論鏈路。"""
    return {
        key: post.get(key)
        for key in ("content", "post_type", "post_type_name", "format",
                    "image_type", "photo_kind")
        if post.get(key) not in (None, "", {}, [])
    }


def _build_own_post_comment_messages(persona: dict, post: dict) -> list[dict]:
    sys = ("你是 AI 角色平台 POPOP 的社群評論區導演。"
           "請為角色本人剛發出的公開 INS 帖寫一組像真人留言的評論。")
    txt = "\n\n".join([
        f"# 發帖角色（僅供理解公開形象）\n{_persona_brief(persona)}",
        f"# 角色本人公開帖\n{json.dumps(_display_post_fields(post), ensure_ascii=False)}",
        prompts.PUBLIC_POST_PRIVACY_RULES,
        COMMENT_CRAFT,
        OWN_POST_COMMENT_RULES,
        COMMUNITY_RULES,
        _BILINGUAL_NOTE,
        _own_comment_schema(),
    ])
    return [{"role": "system", "content": sys}, {"role": "user", "content": txt}]


def _comment_text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("zh") or value.get("ko") or "").strip()
    return str(value or "").strip()


def _comment_count(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalise_own_post_comments(raw: dict) -> tuple[list[dict], dict]:
    comments = raw.get("comments") if isinstance(raw, dict) else None
    if isinstance(comments, dict):
        comments = [comments]
    clean: list[dict] = []
    for item in (comments or [])[:8]:
        if not isinstance(item, dict) or not _comment_text(item.get("content")):
            continue
        replies = []
        for reply in (item.get("replies") or [])[:2]:
            if not isinstance(reply, dict) or not _comment_text(reply.get("content")):
                continue
            replies.append({
                "author": reply.get("author") or {"zh": "路人"},
                "content": reply["content"],
                "likes": _comment_count(reply.get("likes")),
                "char_self": False,
            })
        clean.append({
            "author": item.get("author") or {"zh": "路人"},
            "content": item["content"],
            "likes": _comment_count(item.get("likes")),
            "char_self": False,
            "replies": replies,
        })
    if not clean:
        raise ValueError("模型未生成可用評論")
    stats = raw.get("stats") if isinstance(raw, dict) else {}
    return clean, {
        "likes": _comment_count((stats or {}).get("likes")),
        "comment_count": max(_comment_count((stats or {}).get("comment_count")),
                             len(clean)),
    }


def generate_own_post_comments(char_id: str, post_id: str) -> dict:
    """為最新 INS 批次中的一條角色本人帖生成並持久化平台評論。"""
    with pipeline.char_lock(char_id):
        batch = pipeline.load_latest_ig(char_id)
        if not batch:
            raise ValueError("no saved Instagram posts for this character")
        post = next((p for p in batch.get("posts", [])
                     if p.get("post_id") == post_id), None)
        if post is None:
            raise ValueError(f"post not found: {post_id}")
        source_post = json.loads(json.dumps(post))

    record = pipeline.load_character(char_id)
    raw = api_client.chat(
        _build_own_post_comment_messages(
            record.get("persona") or {}, source_post),
        model=config.LLM_MODEL, temperature=0.9, max_tokens=4000,
    )
    data = api_client.parse_json_text(raw)
    comments, stats = _normalise_own_post_comments(data)

    with pipeline.char_lock(char_id):
        batch = pipeline.load_latest_ig(char_id)
        if not batch:
            raise ValueError("no saved Instagram posts for this character")
        post = next((p for p in batch.get("posts", [])
                     if p.get("post_id") == post_id), None)
        if post is None:
            raise ValueError(f"post not found: {post_id}")
        post["comments"] = comments
        post["stats"] = stats
        storage.save_json("ig_batches", char_id, batch,
                          config.POST_DIR / char_id / "ig_latest.json")
        return {"post": post, "batch": batch}


EVENT_RULES = """# 熱搜事件（一個詞條 × 多方發帖 × 連續劇情）
產出一場圍繞角色的熱搜風波：一個詞條 + 按時間正序的多方帖子 + 角色轉發私信。讀者點開熱搜聚合頁，按時間讀完就看懂來龍去脈。
- 賣點先行（動筆第一步，決定整場事件生死）：熱搜是角色的一次公開曝光。先從人設裡挑一個最值得圍觀的賣點（反差魅力/職業奇觀/性格鉤子），再想「什麼事件能把它引爆」——事件是放大器，賣點是內核。整場風波收斂成一個路人會追的問題，每帖是這個問題的一塊新證據；讀者看完記住的必須是「這個人有意思、想去找 TA」，不是「出了個事故」。落差方向不必總是人設崩塌，反向同樣成立（毒舌的人被拍到過分溫柔）。
- 詞條＝寫給路人的微型新聞標題（第一性原理，鐵律）：熱搜是給【完全不認識角色、不知道這件事】的人看的，3 秒內要讓人冒出「這什麼？點進去」。所以 tag 要有【錨點】（路人抓得住的身份或場景，如「知名藥師」，不是光一個生僻人名）＋【具體事件】（真發生了什麼，不是「詭異微笑」這種氛圍畫面）；用大白話，整條 tag 裡路人看不懂的名詞（生僻人名/地名/黑話）最多一個，超了讀者就划走。結構參考「#錨點＋爭議動作＋存疑語氣#」（如「#XX疑似耍大牌#」），收尾不給完整答案，最後一塊留給聊天。
- 吸引力來自落差，不靠形容詞獵奇（鐵律）：想點的衝動來自「事件和常識對不上」（該冷靜的人失控了／普通事出了怪狀況），不是堆「詭異」「離奇」。給夠資訊、讓人想知道更多＝懸念；資訊不夠、讀者無從關心＝缺失，直接划走。
- 事實底座（鐵律，同 T2）：導火索必須是【今日日程】裡真實發生的事（highlight 優先）或最近聊天的梗——從中挑最能引爆上面那個賣點的一件，被輿論誇大／誤讀／發酵成風波，不憑空編醜聞；goal_threads 明顯推進或翻車是天然新聞源。日程裡沒有一件事撐得起賣點和熱搜（只是平淡日常／純私事）就按 schema 輸出 abstain，不硬湊。
- 視角差·羅生門（鐵律）：同一件事，每個信源自帶立場、文風和盲區——黑粉陰陽怪氣放大細節、粉絲控評反黑、路人不懂就問、營銷號標題黨二手加工、知情人小號欲言又止、官方公文腔冷冰冰、本人輕描淡寫。讀者的樂趣是辨認每個敘述者的偏見、自己合成真相。零門檻讀懂（鐵律照舊）：每帖單獨看，每一句都要讓讀者毫不費力看懂——差異在立場，不在可讀性，沒有例外。要分清兩件事：懸念是「看懂了問題、但不知道答案」（真相是什麼，留給人去猜、去問本人），不是「有一句看不懂」。表達永遠 100% 易懂，缺口只留在資訊層（答案沒給），絕不留在理解層（話沒說清）。做法：句子短、一句一件事，每個詞都看得懂（代號／綽號／設定名詞／內梗／任何你臨時造的名詞概念都只能調味，一個詞得先解釋才懂就別用）。成因不歸你填，別硬安一個生詞上去替讀者解釋。判準：讀者可以好奇「真相是什麼」，但不該卡在「這句／這詞／這件事在講啥」。
- 每帖是一條【獨立的帖子】，不是別人樓下的評論（鐵律）：這些帖子除了進聚合頁，還會【單獨】帶著熱搜 tag 分發到 feed 裡被路人刷到。所以每帖必須自帶主體、自己把事件講起來（誰/發生了什麼/我什麼立場），單獨拎出來讀也是一條完整的帖子；絕不能寫成「回應樓上」「就是說啊」「+1」這種只有貼在別人後面才成立的跟帖/評論口吻。但自帶主體≠從頭複述：交代前情一兩句就夠，篇幅花在自己帶來的那一塊新料上。
- 文體即人物：不同信源的文體本身要不同，這既是擬真也是塑造。長短也是文體——路人/目擊帖像手機上隨手敲的幾行短句，營銷號才成段鋪陳，官方公文簡短冷硬，本人一兩句收場。官方聲明摳字眼、本人回應雲淡風輕，都是留給讀者過度解讀的糖。
- 三幕節奏（鐵律：每個節點靠「新信源入場」推進，不是舊信源繼續輸出）：
  第一幕 起事＝匿名爆料/黑帖 + 吃瓜路人 → 第二幕 升級＝權威信源下場（官方警告/媒體跟進/知情人反轉）→ 第三幕 當事人下場＝角色本人發帖回應（論壇體終極爽點，放最後；輕描淡寫、絕不解釋全貌——這一帖是全場魅力峰值，統領問題的最後一塊答案留給聊天）。
  幕數 3±1，帖子總數 5-8 條，按時間正序輸出；每帖給 time_label（相對詞條爆點，如「詞條爆發前1小時」「爆發後2小時」），讓讀者能自己判斷誰是先知、誰是跟風。
- 帖子互相引用要能自立：後面的帖子可以引用/反駁前面的信源，但必須把被引的說法【重述一遍】再接話（「有黑號說 TA 耍大牌，可我當天在場根本不是這樣」），不能只靠「樓上」「上面那條」這種位置指涉——因為讀者可能是在 feed 裡單獨刷到這一條、沒看過前面。指涉用內容錨定（某黑號/某營銷號說了什麼），不用樓層位置。
- 角色轉發私信（落回聊天）：事件收尾後，角色本人挑其中一條帖子轉發給使用者，附 1-3 條短氣泡——口吻符合人設，對風波輕描淡寫或吐苦水，必須讓使用者有話可回；指向使用者的內容只認真實聊天記錄。
- 和使用者的關係（同 T2 嫂子文學）：使用者不作為事件現場當事人出現（目擊者/群眾/當事各方全是 NPC），但帖子可以 @{user}——知情人暗示「這事和某人有關」、本人帖或本人評論裡意味深長地 @、粉絲扒「那個人」——全場最多 1-2 處，用在最該把使用者拽進來的地方；指向使用者的資訊只認真實聊天記錄；@{user} 觸發紅點提醒。
- 其餘鐵律照舊：數字按熱度分級且隨三幕遞增（越到後面越熱）；本人帖可署名，其餘信源用代稱/馬甲；@{char} 可在某帖出現一次（扒出本尊）。
- 配圖與 T1/T2 同一套判斷（見配圖形態庫）：逐帖問「哪種形態能把這條講得最清楚/最有料」——黑粉爆料配 candid 路透或 chat_shot、營銷號配 clipping/collage、官方公告配聲明截圖（forum_shot 或自創公文形態）、本人帖隨人設。"""

EVENT_SCHEMA = """# JSON Schema（鍵名固定；{"zh"} 表示中文字串物件）
# 素材撐不起一場熱搜時，只輸出：{"abstain": true, "reason": "一句話說明為什麼今天沒有可上熱搜的事"}，不要硬編。
{
  "angle": {"selling_point": {"zh": "這場風波要立住的一個角色賣點，一句話"}, "question": {"zh": "統領全場、路人會追的那一個問題，一句話"}},
  "topic": {"tag": {"zh": "#詞條#"}, "sub": {"zh": "一句話副題，可空"}, "heat": 數字},
  "posts": [   // 按時間正序 5-8 條
    {"time_label": {"zh": "相對詞條爆點的時間"},
     "source_type": "hater|fan|passerby|marketing|insider|official|char",
     "account": {"name": {"zh"}, "handle": "@英數"},
     "content": {"zh"},
     "comments": [{"author": {"zh"}, "content": {"zh"}, "likes": 數字}],
     "stats": {"likes": 數字, "comment_count": 數字},
     "image": {"kind": "candid|clipping|infocard|forum_shot|chat_shot|toon|cover|desktop_scrap|object_still|poll_card|missing_notice|meme|map_pin|promo|event_poster|ad|news_shot|editorial|自創短名|none", "variable": {...}, "scene": {...}, "dressing": {...}, "graphic": {...}}}
  ],
  "char_dm": {"quote_post_index": 數字, "bubbles": [{"zh"}, ...]},
  "chat_hooks": [{"zh"}, ...]
}"""


def _build_event_messages(persona: dict, user_name: str, chat_lines: str,
                          hint: str, with_images: bool = True,
                          day_digest: dict | None = None) -> list[dict]:
    sys = ("你是 AI 角色平臺 POPOP 的輿論事件導演：為一個角色編排一場熱搜風波——"
           "多個虛構信源圍繞同一件事先後發帖，立場文風各異，讀者自己拼出真相。")
    material = [
        f"# 角色人設（風波的主角）\n{_persona_brief(persona)}",
        f"# 使用者稱呼\n{(user_name or '').strip() or '我'}",
    ]
    if chat_lines:
        material.append(f"# 真實聊天記錄（指向使用者的素材只能取自這裡）\n{chat_lines}")
    else:
        material.append("# 真實聊天記錄\n（暫無。私信只鋪最輕的分享，不編造共同史，@{user} 從缺。）")
    if day_digest:
        material.append("# 今日日程（事件的事實底座，導火索從這裡選）\n"
                        + json.dumps(day_digest, ensure_ascii=False))
    blocks = [*material, EVENT_RULES, COMMENT_CRAFT, COMMUNITY_RULES,
              MENTION_RULES, _BILINGUAL_NOTE, EVENT_SCHEMA]
    if with_images:
        blocks.insert(-2, IMAGE_SPEC_RULES_MANDATORY)
        blocks[-1] = blocks[-1].replace("|none", "")  # blocks[-1]=EVENT_SCHEMA
        blocks.append(_image_spec_appendix())
        blocks.append('# 事件配圖\n同一場事件的配圖形態要有變化，跟著信源走，別每帖同一種。')
    else:
        blocks.append('# 配圖\n本次不出圖：每帖 image 一律輸出 {"kind": "none"}。')
    txt = "\n\n".join(blocks)
    if (hint or "").strip():
        txt += f"\n\n# 運營補充要求（事件方向）\n{hint.strip()}"
    return [{"role": "system", "content": sys}, {"role": "user", "content": txt}]


class EventAbstain(Exception):
    """素材撐不起一場熱搜，模型主動放棄生成（非錯誤，是合法結果）。"""


def generate_feed_event(char_id: str, hint: str = "", user_name: str = "",
                        session_id: str | None = None,
                        with_images: bool = True,
                        schedule_text: str = "",
                        image_model: str | None = None) -> dict:
    """生成一場熱搜事件（詞條+多方帖子+角色轉發私信），存入該角色 feed 檔。
    伴隨日程生產：優先用前端貼入的日程素材，否則自動生成當日日程摘要作事實底座。
    image_model：介面選擇的生圖模型（image-2=gpt-image-2；banana=nanobanana）。"""
    record = pipeline.load_character(char_id)
    persona = record.get("persona") or {}
    char_name = persona.get("name") or char_id
    chat_lines, _ctx = _chat_digest(
        char_id, str(char_name), session_id=session_id)
    day_digest = _parse_schedule_text(schedule_text)
    if day_digest is None:
        day_digest = generate_day_digest(persona, chat_lines)
    messages = _build_event_messages(persona, user_name, chat_lines, hint,
                                     with_images=with_images,
                                     day_digest=day_digest)
    raw = api_client.chat(messages, model=config.LLM_MODEL,
                          temperature=0.9, max_tokens=16000)
    data = api_client.parse_json_text(raw)
    if isinstance(data, dict) and data.get("abstain"):
        reason = (data.get("reason") or "").strip() or "今天沒有可以撐起一場熱搜的事件"
        raise EventAbstain(reason)
    if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
        raise ValueError(f"模型未返回合法事件 JSON：{str(raw)[:300]}")
    now = int(time.time())
    posts = data["posts"]
    for i, ep in enumerate(posts):   # 服務端補時間戳：正序、間隔~1小時，最後一條=現在
        if isinstance(ep, dict):
            ep["created"] = now - (len(posts) - 1 - i) * 3600
    event_id = f"ev_{now}_{uuid.uuid4().hex[:6]}"
    # 配圖：與 T1/T2 同鏈路，逐帖按模型給的 spec 渲染（單張失敗不阻斷）
    if with_images:
        specs = []
        for i, ep in enumerate(posts):
            if not isinstance(ep, dict):
                continue
            spec = ep.get("image") if isinstance(
                ep.get("image"), dict) else None
            if not spec or spec.get("kind") in (None, "", "none"):
                # 必配模式兜底：模型漏給 spec 時退成基礎 candid
                spec = {"kind": "candid", "variable": {}, "scene": {}}
                ep["image"] = spec
            specs.append((i, ep))
        if specs and not record.get("identity"):
            pipeline.build_identity(char_id)
            record = pipeline.load_character(char_id)

        resolved_model = config.resolve_image_model(image_model)

        def _one(item):
            i, ep = item
            try:
                ep["image_rendered"] = _render_feed_image(
                    record, f"{event_id}_p{i}", ep["image"],
                    image_model=resolved_model)
            except Exception as e:  # noqa: BLE001 單張失敗不阻斷事件本體
                ep["image_rendered"] = {"error": str(e)}

        if specs:
            with ThreadPoolExecutor(max_workers=min(len(specs), config.MAX_WORKERS)) as ex:
                list(ex.map(_one, specs))
    event = {
        "event_id": event_id,
        "created": now,
        "image_model_choice": image_model or config.DEFAULT_IMAGE_MODEL_CHOICE,
        "char_id": char_id,
        "char_name": char_name,
        "chat_material_used": bool(chat_lines),
        "day_digest": day_digest,
        "data": data,
        "call_log": {"model": config.LLM_MODEL, "output": raw},
    }
    with pipeline.char_lock(char_id):
        feed = _load_feed(char_id)
        feed.setdefault("events", []).insert(0, event)
        _save_feed(char_id, feed)
    return event


def delete_feed_event(char_id: str, event_id: str) -> dict:
    with pipeline.char_lock(char_id):
        feed = _load_feed(char_id)
        evs = feed.get("events") or []
        before = len(evs)
        feed["events"] = [e for e in evs if e.get("event_id") != event_id]
        if len(feed["events"]) == before:
            raise ValueError(f"event not found: {event_id}")
        _save_feed(char_id, feed, strict_remote=True)
        return feed


REPLY_CONT_RULES = """# 續寫評論區（使用者剛在評論區發了話，NPC 要接住）
你收到：帖子全文、目標主評論及其回覆鏈、使用者剛發的內容。生成 1-3 條 NPC 的後續回覆，像真實評論區一樣把話接住：
- 誰來接：被回覆的那個 NPC 優先接話；其他圍觀 NPC 可以插嘴、起鬨、抬槓。每條都要對使用者說的話有具體反應，不許自說自話。
- NPC 眼裡，使用者只是又一個匿名路人，不知道 TA 是誰。T2 帖唯一例外是角色本人（char_self=true）：TA 知道這個「路人」就是自己在聊的那個人——回覆可以意味深長、欲蓋彌彰，但絕不點破身分；使用者直接回覆了本人的評論時，本人必須接話。T1 帖沒有本人，char_self 一律 false。
- 清晰鐵律照舊：每條單獨拿出來都看得懂；入戲、直白、口語，禁黑話。評論人設沿用樓裡已出現的 NPC 暱稱最自然，也可來個新路人。
- 數字自洽：剛發出來的回覆 likes 給 0~9。
- 節制：多數情況 1-2 條就夠；使用者的話沒什麼可接的就 1 條輕接，別強行熱鬧。

# 輸出 JSON（鍵名固定，只輸出一個 object）
{"replies": [{"author": {"zh": "匿名暱稱"}, "content": {"zh": "回覆內容"}, "likes": 數字,
  "reply_to": "被回覆人暱稱（通常是使用者的暱稱）", "char_self": bool}]}"""


def _thread_text(comment: dict) -> str:
    def z(x):
        return (x.get("zh") or x.get("ko") or "") if isinstance(x, dict) else (x or "")
    lines = [f"主評論 {z(comment.get('author'))}: {z(comment.get('content'))}"]
    for r in comment.get("replies", []):
        who = z(r.get("author")) + ("（使用者本人）" if r.get("is_user") else "")
        to = f" 回覆@{r['reply_to']}" if r.get("reply_to") else ""
        lines.append(f"  {who}{to}: {z(r.get('content'))}")
    return "\n".join(lines)


def _build_reply_messages(post: dict, comment: dict, user_text: str,
                          reply_to: str, user_name: str, persona: dict) -> list[dict]:
    d = post.get("data") or {}

    def z(x):
        return (x.get("zh") or x.get("ko") or "") if isinstance(x, dict) else (x or "")
    kind_note = ("這是 T2 帖（僅該使用者可見），角色本人可下場。"
                 if post.get("kind") == "t2" else "這是 T1 公開帖，沒有角色本人下場。")
    sys = ("你是 AI 角色平臺 POPOP 的評論區導演：使用者剛在一條帖子的評論區發了話，"
           "你來寫 NPC 們的後續回覆，讓評論區像活的。")
    txt = "\n\n".join([
        f"# 帖子類型\n{kind_note}",
        f"# 被圍觀的角色人設\n{_persona_brief(persona)}",
        f"# 帖子正文\n{z(d.get('content'))}",
        f"# 目標評論串（含使用者剛發的最後一條）\n{_thread_text(comment)}",
        f"# 使用者剛發的內容\n{user_name}"
        + (f" 回覆@{reply_to}" if reply_to else "") + f"：{user_text}",
        COMMENT_CRAFT,
        COMMUNITY_RULES,
        REPLY_CONT_RULES,
    ])
    return [{"role": "system", "content": sys}, {"role": "user", "content": txt}]


def _post_comments(post: dict) -> list | None:
    d = post.get("data") or {}
    if isinstance(d.get("comments"), list):
        return d["comments"]
    if isinstance(d.get("outer_comments"), list):
        return d["outer_comments"]
    return None


def continue_comment_thread(char_id: str, post_id: str, comment_index: int,
                            text: str, reply_to: str = "",
                            user_name: str = "") -> dict:
    """使用者回覆評論 → 立即入檔 → LLM 續寫 1-3 條 NPC 回覆 → 入檔。
    comment_index=-1 表示使用者發新的主評論。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("回覆內容為空")
    uname = (user_name or "").strip() or "我"
    now = int(time.time())
    with pipeline.char_lock(char_id):
        feed = _load_feed(char_id)
        post = next((p for p in feed["posts"]
                    if p.get("post_id") == post_id), None)
        if post is None:
            raise ValueError(f"post not found: {post_id}")
        comments = _post_comments(post)
        if comments is None:
            raise ValueError("這條帖子是舊格式，不支援評論互動")
        if comment_index == -1:
            comments.append({"author": {"zh": uname}, "content": {"zh": text},
                             "likes": 0, "is_user": True, "created": now,
                             "replies": []})
            comment_index = len(comments) - 1
        else:
            if not (0 <= comment_index < len(comments)):
                raise ValueError("comment_index 越界")
            comments[comment_index].setdefault("replies", []).append(
                {"author": {"zh": uname}, "content": {"zh": text}, "likes": 0,
                 "is_user": True, "reply_to": (reply_to or "").strip(),
                 "created": now})
        _save_feed(char_id, feed)   # 先存使用者的話，續寫失敗也不丟
        comment_snapshot = json.loads(json.dumps(comments[comment_index]))
        kind = post.get("kind")

    record = pipeline.load_character(char_id)
    persona = record.get("persona") or {}
    msgs = _build_reply_messages(post, comment_snapshot, text, reply_to,
                                 uname, persona)
    raw = api_client.chat(msgs, model=config.LLM_MODEL,
                          temperature=0.9, max_tokens=2000)
    data = api_client.parse_json_text(raw)
    new = (data or {}).get("replies") if isinstance(data, dict) else None
    if isinstance(new, dict):
        new = [new]
    cleaned = []
    for r in (new or [])[:3]:
        if not isinstance(r, dict) or not r.get("content"):
            continue
        if kind != "t2":
            r["char_self"] = False
        r["created"] = int(time.time())
        cleaned.append(r)

    with pipeline.char_lock(char_id):
        feed = _load_feed(char_id)
        post = next((p for p in feed["posts"]
                    if p.get("post_id") == post_id), None)
        comments = _post_comments(post) or []
        if 0 <= comment_index < len(comments):
            comments[comment_index].setdefault("replies", []).extend(cleaned)
        _save_feed(char_id, feed)
        return post


def generate_feed_post(char_id: str, kind: str, subtype: str = "auto",
                       user_name: str = "", hint: str = "",
                       session_id: str | None = None,
                       schedule_text: str = "",
                       today_sent: str = "",
                       image_model: str | None = None) -> dict:
    """生成一條 T1/T2 帖子並追加到該角色的 feed 存檔。每帖必配圖（無純文選項）。
    image_model：介面選擇的生圖模型（image-2=gpt-image-2；banana=nanobanana）。

    T2 伴隨日程生產：優先使用前端貼入的日程素材；否則先跑一步當日日程摘要
    （模擬線上日程鏈路的當日產出），帖子錨定日程裡真實發生的事件與成就線。
    """
    record = pipeline.load_character(char_id)
    persona = record.get("persona") or {}
    char_name = persona.get("name") or char_id
    day_digest = None

    recent_kinds = _recent_image_kinds(char_id)
    if kind == "t1":
        # 體裁由模型自選（見 prompt 內聯體裁庫），輸出寫進 subtype
        messages = _build_t1_messages(persona, recent_kinds=recent_kinds)
        chat_used = False
    elif kind == "t2":
        chat_lines, chat_context = _chat_digest(char_id, str(char_name),
                                                session_id=session_id)
        chat_used = bool(chat_lines)
        day_digest = _parse_schedule_text(schedule_text)
        if day_digest is None:
            # 沒貼日程素材：優先復用該角色最近一次完整日產的日程，讓 T2 與
            # 角色的主動消息/限動共享同一天（活人感、一致性）；沒有再臨時輕造。
            from . import daily
            day_digest = daily.latest_run_digest(char_id)
        if day_digest is None:
            day_digest = generate_day_digest(persona, chat_lines)
        messages = _build_t2_messages(persona, user_name,
                                      chat_lines, chat_context,
                                      day_digest=day_digest,
                                      today_sent=today_sent,
                                      recent_authors=_recent_regulars(char_id),
                                      recent_kinds=recent_kinds)
    else:
        raise ValueError(f"unknown post kind: {kind}")

    raw = api_client.chat(messages, model=config.LLM_MODEL,
                          temperature=0.9, max_tokens=16000)
    try:
        data = api_client.parse_json_text(raw)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"模型未返回合法 JSON：{e}; 原始輸出：{raw[:600]}") from e
    if not isinstance(data, dict):
        raise ValueError("模型輸出不是 JSON object")

    post_id = f"fp_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    # 配圖：每帖必配，恒定渲染（複用正式帖子生圖鏈路；identity 缺失時就地補建）。
    # 模型沒給可用 spec 時退成基礎 candid（identity+畫風直出）。
    image_spec = data.get("image") if isinstance(
        data.get("image"), dict) else None
    if not image_spec or image_spec.get("kind") in (None, "", "none"):
        image_spec = {"kind": "candid", "medium": "raw", "person": ""}
        data["image"] = image_spec
    try:
        if not record.get("identity"):
            pipeline.build_identity(char_id)
            record = pipeline.load_character(char_id)
        image = _render_feed_image(
            record, post_id, _legacy_image_spec(image_spec),
            image_model=config.resolve_image_model(image_model))
    except Exception as e:  # noqa: BLE001 配圖失敗不阻斷帖子本體
        image = {"error": str(e)}

    post = {
        "post_id": post_id,
        "kind": kind,
        "subtype": data.get("subtype"),
        "requested_subtype": subtype or None,
        "user_name": user_name,
        "chat_material_used": chat_used,
        "day_digest": day_digest,
        "image": image,
        "image_model_choice": image_model or config.DEFAULT_IMAGE_MODEL_CHOICE,
        "created": int(time.time()),
        "char_id": char_id,
        "char_name": char_name,
        "data": data,
        "call_log": {
            "model": config.LLM_MODEL,
            "messages": messages,
            "output": raw,
        },
    }
    with pipeline.char_lock(char_id):
        feed = _load_feed(char_id)
        feed["posts"].insert(0, post)
        _save_feed(char_id, feed)
    return post
