"""查手機（폰 몰래보기）Demo：用 LLM 按角色人設 + 真實聊天 + feed 素材，
一次性生成「一支手機」的全部 20 個功能內容。

設計原則：
- 固定 UI：前端每個功能的版式是寫死的；這裡只產出「注入的內容」。
- 一支手機一個故事：先讓模型設計這個角色的中心秘密 + 密碼由來 + 3 條跨 App
  線索 + 真相卡，再讓每個 App 的內容都服務於同一個秘密（跨 App 能對上）。
- 內容語言：繁體中文（平台預設 zh）。純字串，不做雙語物件。
- read + LLM：只讀角色資料，產物快取到 data/phone_check/<char_id>.json，
  不覆寫任何既有資料。缺角色/生成失敗時回 None，前端保留自帶 mock。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import api_client, chat, config, feed_posts, pipeline

GEN_DIR = config.DATA_DIR / "phone_check"
GEN_DIR.mkdir(parents=True, exist_ok=True)

# 前端 demo 角色 id -> 平台真實 char_id（與 phone_check.py 對齊）
DEMO_CHAR_MAP: dict[str, str] = {
    "yuwi": "char_1783597290_f0d265",
    "shen": "char_1784089818_de4be8",
    "haesu": "char_1783634537_a54d49",
}

# 20 個功能的固定清單（app_id, 前端渲染器 type, 圖示, 名稱）——每個角色都全帶。
APP_ORDER: list[tuple[str, str, str, str]] = [
    ("talk", "chat", "💬", "메신저"),
    ("sns", "sns", "📷", "SNS 부계"),
    ("search", "search", "🔍", "검색"),
    ("feed", "feed", "📺", "추천 피드"),
    ("footprints", "footprints", "🗺", "동선"),
    ("purchase", "list", "🛍", "쇼핑"),
    ("market", "market", "🥕", "당근"),
    ("ledger", "list", "💳", "가계부"),
    ("voice", "voice", "🎙", "음성 메모"),
    ("health", "health", "🩺", "건강"),
    ("folder", "folder", "🗂", "파일"),
    ("gallery", "gallery", "🎞", "사진첩"),
    ("delivery", "list", "🛵", "배달"),
    ("taxi", "list", "🚕", "이동"),
    ("screen", "screentime", "⏱", "스크린타임"),
    ("calls", "calls", "📞", "통화"),
    ("music", "music", "🎵", "음악"),
    ("fortune", "fortune", "🔮", "운세"),
]

# ── 免費 / 付費 兩層 ──────────────────────────────────────────────
# 免費層：跟著日程更新、有紅點提示、進手機即可看（chat 列表最快露資訊）。
# 付費層：解鎖才生成（單次 LLM 呼叫），生成後落盤，可手動重生成。
FREE_APP_IDS = ["talk", "feed", "footprints"]
# sns 主號（대표）在免費層生成；小號（부계）在付費層以 sns_alt 生成後併入 apps.sns.alt。
PAID_APP_IDS = [
    "sns_alt", "search", "purchase", "market", "ledger", "voice",
    "health", "folder", "gallery", "delivery", "taxi", "screen",
    "calls", "music", "fortune",
]
# 前端付費鎖：這些 app 進手機時要花열람권解鎖（sns 主號免費、小號在 app 內再鎖）。
PAID_LOCK_IDS = [
    "search", "purchase", "market", "ledger", "voice", "health",
    "folder", "gallery", "delivery", "taxi", "screen", "calls",
    "music", "fortune",
]

SCHEMA = r"""{
  "meta": {
    "secret": "이 사람의 중심 비밀 한 단락(플레이어에게 안 보임, 후속 유료층 생성이 참조하는 스토리 바이블). 3개 단서가 어느 App에서 나오고 무엇을 가리키는지 요약",
    "hook_line": "캐릭터 카드용 한 줄 훅",
    "pass": "4자리 숫자 비밀번호(문자열)",
    "pass_origin": "비밀번호의 유래 한 줄(생일/기념일/초대장 등, 剧情钩子)",
    "hint": "잠금화면 힌트(왜 이 숫자인지 채팅에서 흘린 단서. 개행 \\n 허용)",
    "lock_msg": "잠금화면 상황 한 줄(폰을 손에 넣은 정황)",
    "date_label": "잠금화면 날짜 라벨",
    "time": "HH:MM", "batt": 배터리숫자,
    "stage_label": "관계 단계 한 줄",
    "stage_pct": "0-100 정수. 75 이상일 때만 trust 이벤트가 열릴 수 있음",
    "pass_changed": {"pass":"들킨 뒤의 새 4자리 비밀번호","lock_msg":"다음 세션 잠금화면에서 보이는 변화","hint":"새 비밀번호를 다시 추리할 수 있는 힌트","reveal":"새 비밀번호의 의미가 드러나는 한 줄"},
    "peek_arc": {
      "trigger_after": "서로 다른 App을 2 또는 3개 연 뒤 발동",
      "signal": {"app":"실시간 알림을 보낸 앱","sender":"발신자/장소","text":"짧고 불길한 알림","time":"방금"},
      "rush": {"seconds":10,"emoji":"이모지","title":"그가 폰을 찾는 현재 상황","body":"8-12초 안에 나가야 하는 감각적인 이유"},
      "caught": {"emoji":"이모지","title":"들켰을 때의 반응","body":"캐릭터 성격에 맞는 반응. 화냄/침묵/모른 척 중 하나","after":"다음 세션의 새 비밀번호나 메시지로 이어지는 한 줄"},
      "exit": {"title":"오늘 본 건 비밀이에요에 해당하는 부드러운 문장","body":"폰을 제자리에 돌려놓고도 남는 죄책감/공모감 한 줄"},
      "mirror": {"unlock_stage":"75-90 정수","emoji":"이모지","title":"고호감 단계의 반전 신뢰 이벤트","body":"그가 먼저 폰을 건네며 정보 비대칭을 내려놓는 장면","cta":"짧은 버튼 문구"}
    }
  },
  "clues": [
    {"id":"c1","icon":"이모지","text":"단서 한 줄(<b>강조</b> 허용). 서로 다른 App에서 발견되는 3개"},
    {"id":"c2","icon":"이모지","text":"..."},
    {"id":"c3","icon":"이모지","text":"..."}
  ],
  "truth": {"emoji":"이모지","title":"진실 카드 제목","body":"진실 본문(여러 줄, \\n 허용). 3개 단서를 하나로 꿰는 중심 비밀","next":"다음 기록 예고 한 줄","next_in":"도착 시점 라벨"},
  "apps": {
    "talk": {"profiles":[
        {"id":"main","name":"대표 프로필명","av":"이모지","sub":"공개 페르소나 한 줄","note":"이 프로필의 방 목록 상단 설명"},
        {"id":"ghost","name":"부계정명","av":"이모지","secret":true,"sub":"연락처 없음 등","status":"알림 꺼짐/숨김 등","note":"부계정을 켰을 때의 설명(뒷계정 발견 모먼트)"}
      ],
      "rooms":[
      {"id":"me","profile":"main","is_user":true,"name":"나","note":"방 설명","msgs":[{"who":"them|me","t":"대사","time":"HH:MM","voice":false}],"draft":"입력창에 쓰다 만 미발신 초안(가장 강한 훅) 또는 \"\"","bookmark":"보관한 메시지 설명 또는 \"\""},
      {"id":"other1","profile":"main","name":"상대 이름(관계 암시)","note":"","msgs":[{"who":"them|other","t":"대사","time":"HH:MM"}]},
      {"id":"self","profile":"ghost","name":"나에게 쓰기","note":"부계정 전용 방","msgs":[{"who":"them","t":"아무도 안 보는 곳에 쓰는 한 줄","time":"HH:MM"}]}
    ]},
    "sns": {"main":{"handle":"@아이디","bio":"공개 페르소나 소개","posts":[{"t":"게시물","time":"","likes":숫자}]},
            "alt":{"handle":"@부계아이디","bio":"뒷계 · 비공개 느낌","posts":[{"t":"0 like 새벽 emo, 그 사람 암시","time":"","likes":0,"hot":true,"clue":"c1|null"}]}},
    "search": {"items":[{"t":"검색어(연속 검색이 심리 궤적)","time":"라벨","hot":false,"clue":null,"d":"탭 시 상세(교차검증/속내) 또는 생략"}],
                "browse":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","t":"방문한 페이지 제목","site":"사이트/앱 종류(지도/블로그/쇼핑 등)","visits":"체류시간/재방문 횟수 라벨 또는 \"\"","time":"라벨","hot":false,"clue":null,"d":"행동이 폭로하는 속내(끝까지 읽음/반복 방문/저장) 또는 생략"}]},
    "feed": {"note":"알고리즘이 밀어주는 것 = 관심사 폭로, 한 줄","cards":[{"emo":"이모지","src":"유튜브/지도/쇼핑 추천 등","t":"추천 콘텐츠","why":"왜 이게 뜨는지(그의 관심사 폭로)","hot":false,"d":"상세 또는 생략"}]},
    "footprints": {"pins":[{"x":0-100,"y":0-100,"lbl":"장소","hot":false}],"trips":[{"route":"동선","time":"라벨","s":"부가","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "purchase": {"orders":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","t":"상품명","price":"₩가격","date":"주문일 라벨","step":"0결제|1상품준비|2배송중|3배송완료 정수","addr":"배송지(누구·어디 폭로) 또는 \"\"","hot":false,"clue":null,"d":"상세(수령일=상대 생일 전날 등 의도 폭로) 또는 생략"}],
                 "cart":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","t":"장바구니에 담아둔 미결제 상품(실행 못한 마음이 더 셈)","price":"₩가격","note":"담아둔 기간/망설임 라벨(예:'3개월째 고민 중')","hot":false,"clue":null,"d":"왜 아직 못 샀는지 속내 또는 생략"}]},
    "market": {"items":[{"emo":"이모지","dir":"buy|sell","t":"상품명","price":"가격 라벨","s":"동네인증/거래 후기(대타인 말투 대비)","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "ledger": {"month":"이번 달 라벨(예:6월)","total":"₩총지출","note":"총평 한 줄 또는 \"\"",
               "txns":[{"emo":"카테고리 이모지","t":"항목명(수취인/상점)","amt":"±₩금액","memo":"손글씨 메모=속내(금액보다 중요: '그녀 오늘 밥 못 먹었을 듯'/'그녀에게 말하지 마'/'6월12일'/공백)","time":"라벨","hot":false,"clue":null,"d":"상세(자동이체/구독/할부/투자 등 장기 미스터리, 교차검증) 또는 생략"}]},
    "voice": {"locked":false,"memos":[{"name":"파일명.wav","len":"m:ss 또는 h:mm:ss","meta":"녹음 시각/편집횟수 등 메타","wave":"calm|mid|burst","hot":false,"clue":null,"script":"재생 시 나오는 내용/전사(감정 농도 최대)"}]},
    "health": {"summary":"이 사람의 몸 상태를 한 줄로 요약(지표 전체를 관통하는 서사)",
                "sleep":{"hours":"오늘 수면 시간 숫자문자열","avg":"주 평균 숫자 또는 생략","note":"수면 급변이 감정 폭로 한 줄","delta_note":"전주 대비 변화 라벨(예:'-40%') 또는 생략","bars":[7개 0-100 숫자(최근 7일 수면)]},
                "week_labels":["요일 라벨 7개 또는 생략(기본 월~일)"],
                "metrics":[{"icon":"이모지","name":"지표명(심박수/걸음 수/스트레스/혈중산소 등)","val":"대표 수치","unit":"단위/시각 라벨","note":"짧은 부가","trend":[7개 숫자(최근 7일 추이)],"hot":false,"clue":null,"d":"다른 App과 교차검증되는 속내(어느 시각에 왜 튀었는지) 또는 생략"}],
                "dreams":[{"date":"날짜 라벨(여러 날에 걸쳐)","t":"꿈 일기 한 줄","hot":false,"d":"상세 또는 생략"}],
                "alarms":[{"time":"HH:MM","label":"알람 라벨(설정자의 의도가 드러나는 한 줄) 또는 \"\"(라벨 없음=더 수상함)","repeat":"매일/평일/금요일마다 등","hot":false,"d":"상세 또는 생략"}]},
    "folder": {"folders":[{"icon":"📁","name":"폴더명","count":"항목 수 라벨","pass":"4자리 또는 null(2차 비번)","pass_hint":"2차 비번 힌트 또는 생략","clue":null,"docs":[{"t":"문서명","s":"부가","time":"라벨","hot":false,"d":"상세 또는 생략"}]}]},
    "gallery": {"tabs":[{"id":"recent","name":"최근"},{"id":"deleted","name":"최근 삭제","note":"30일 후 영구삭제"}],
                "photos":{"recent":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","cap":"캡션","c1":"제목","c2":"라이트박스 설명","hot":false}],
                          "deleted":[{"emo":"이모지","grad":"...","cap":"캡션","countdown":"N일 남음","locked":true,"c1":"제목","c2":"설명","hot":false,"clue":null}]}},
    "delivery": {"orders":[{"emo":"음식 이모지","shop":"가게명","serv":"N인분(1인분인데 2인분=누가 있었다)","items":"메뉴 요약","addr":"배송지(집 아닌 곳=행적 폭로) 또는 \"\"","memo":"요청사항=미니 손글씨(안 매운맛인데 매운맛 등 이상신호)","price":"₩금액","time":"라벨","warn":"serv가 이상하면 true 아니면 생략","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "taxi": {"trips":[{"from":"출발지","to":"도착지","way":"중간 경유지(길에서 누굴 태웠다) 또는 \"\"","time":"심야 시각 라벨(행적 폭로)","fare":"₩요금","pay":"결제수단(평소 안 쓰던 카드=이상)","oddPay":"결제수단이 이상하면 true 아니면 생략","hot":false,"clue":null,"d":"상세: 고가치 시나리오를 반드시 하나 이상 심어라(유저 근처까지 왔지만 연락 안 함/심야에 낯선 주소/유저 주소 입력 후 취소/싸운 뒤 첫 만남 장소로/이전 진술과 모순되는 동선/출발 후 목적지 급변경). 또는 생략"}]},
    "screen": {"total":"Nh Nm","total_note":"부가","bars":[{"app":"앱명","val":"Nh Nm","pct":0-100,"note":"↑/↓% 또는 생략"}],"insights":[{"t":"행동패턴 폭로 한 줄","hot":false,"clue":null,"d":"상세 또는 생략"}],"downloads":[{"icon":"이모지","app":"최근 설치/삭제한 앱","action":"설치함|삭제함|설치 후 N분 만에 삭제 등","time":"라벨","hot":false,"clue":null,"d":"왜 이 앱을 설치했는지 드러나는 상세 또는 생략"}]},
    "calls": {"stats":[{"name":"통화 상대","pct":0-100,"dur":"N회 · N분(횟수 반드시 포함)","hot":false,"clue":null,"zero_ic":"0회일 때 아이콘(예: ⤴) 또는 생략","zero_sub":"0회일 때 부제(예: '통화 0회 · 발신 취소 5회') 또는 생략","zero_tag":"0회일 때 태그(예: '걸었다 끊음') 또는 생략","d":"상세 또는 생략. 0회인 상대는 랭킹에서 빠지고 '통화한 적 없음' 구역에 따로 표시된다. 0회는 '무관심'이 아니라 서사의 핵심일 수 있으니(걸었다 끊음/발신 취소 N건), zero_sub·zero_tag·d로 왜 0회인지 반드시 표현하라"}],
              "recent":[{"dir":"in|out|miss|cancel","name":"상대","s":"통화 길이/부가(발신취소면 '신호 전에 끊음' 등)","time":"라벨","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "music": {"playlists":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","title":"재생목록 제목(날짜/한 줄 감정, 한국인은 이렇게 이름 짓는다)","sub":"곡 수/업데이트 빈도 등 메타","collab":"함께 듣기 상대 아이디 또는 생략(공유 플레이리스트일 때만)","hot":false,"clue":null,"d":"상세 또는 생략"}],
              "recent":[{"title":"곡명","artist":"아티스트","replay":"반복 재생 횟수 라벨 또는 \"\"(노이즈 곡)","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "fortune": {"compat":[{"kind":"궁합|사주","names":"조회한 두 사람(생일 조합 암시)","score":"점수 또는 \"—\"","note":"결과 해석 한 줄","count":"조회 횟수 라벨(반복 조회=집착의 증거)","hot":false,"clue":null,"d":"상세 또는 생략"}],
                "logs":[{"q":"질문 내용(대부분은 심심풀이 일일운세, 하나는 반복되는 절실한 질문)","kind":"일일운세|타로|사주","time":"라벨/반복횟수","hot":false,"clue":null,"d":"상세 또는 생략"}]}
  }
}"""
METHODOLOGY = """# 你在做什麼
你是「查手機」戀愛推理遊戲的內容導演。玩家偷看角色的手機，一個 App 一個 App 地翻，
拼湊出這個人藏在日常數據裡的秘密。你要為【一個角色】生成整支手機的 20 個功能內容。

# 鐵律一：一支手機一個中心秘密
先為這個角色定一個「中心秘密」——一件他沒說出口、但整支手機到處是痕跡的事
（多半是：他對玩家的在意 / 他的某個隱藏面 / 一段沒講的過去）。所有 App 的內容都
是這個秘密在不同數據維度上的投影，跨 App 必須能對上（外賣週二沒單 ↔ 動線週二出門
↔ 搜索欄那天在查感冒藥）。禁止 20 個 App 各說各話。

# 鐵律二：三條跨 App 線索 + 一張真相卡
埋 3 條線索（clues），分散在 3 個不同 App 裡（例如 1 條在搜索、1 條在錄音、1 條在
記帳）。對應 App 的那個 item 上標 "clue":"c1"（點開即收集）。集齊 3 條解鎖 truth
真相卡——把 3 條線索擰成中心秘密的答案。真相要有情緒重量，別平鋪。

# 鐵律三：情緒配比（甜🍬 / 疑🔍 / 痛💔 三味俱全）
不要全甜也不要全虐。大部分 item 是平庸日常噪音（繳費、通勤、跟媽媽的乾巴巴問候）——
噪音讓「有料」的那幾條更炸。有料的 item 標 "hot":true。每個 App 至少 1 條有料。

# 鐵律四：痕跡比內容更狠
最好玩的不是他說了什麼，是痕跡：沒發出去的草稿、手動刪除留下的空檔、記帳備註的改名、
購物車躺了三個月沒下單的東西、凌晨的搜索、心率飆高的時刻、通話時長排行榜裡你的排名。
用元數據和行為模式說話，不要直接寫「他愛你」。
密碼也是痕跡的一種：答對密碼本身就是「你有多懂他」的親密度自測，所以 pass_origin
要嵌進剧情——可以是甜的（把認識那天設成密碼），也可以是要再挖一層的 twist
（一個玩家不認識的日期，翻完手機才明白指什麼）；hint 要讓玩家覺得答案能回聊天裡「套」出來。

# 鐵律五：這不是靜態內容，而是一次「被授權的越軌」
每個角色都必須在 meta.peek_arc 做完一段可運行的微型情緒曲線，而不是只填 App 內容：
1. **解鎖有代價**：pass / hint / lock_msg 要讓玩家清楚「這不是我的手機」，但又覺得自己有資格猜中。
2. **持有信息差**：前 2-3 個 App 先給日常噪音與一個可疑痕跡；不要第一屏就交底。
3. **差點被抓**：signal 必須是和角色生活、聊天或當下場景相容的即時異常（「자?」、定位找手機、玄關感測），rush 固定 10 秒。它不是廉價 jump scare：玩家正在看的那個 App 要因此變成「沒看完」，caught 的反應必須反過來服務角色性格與中心秘密。pass_changed 必填，讓下次解鎖真的進入一個有後果的新狀態。
4. **退出仍有餘溫**：exit 不是責罵玩家，而是糖衣的共謀感——「他還不知道」「你把它放回原處」，讓越界被記住。
5. **高關係的鏡像兌現**：mirror 只在 75-90+ 關係、且真相已看過時才可用。不是再偷看，而是角色主動遞來手機；主動放下信息差，是甜線的關係里程碑。
以上五段要互相對上。懸疑角色的 caught 可以讓疑問更深；甜向角色的 caught 可以是克制、假裝沒看見；不要每個人都用暴怒或跟蹤定位。

# 鐵律六：本地化（韓國市場）
App 皮膚是韓國的（카톡/네이버/배민/카카오T/당근/토스），但**所有輸出文字用繁體中文**，
偶爾夾一兩個韓文詞（부계、썸、ㅇㅇ、반말）調味即可。金額用韓元（₩/만원）。
數字要自洽（通話時長、點讚數、螢幕時間互相成比例）。

# 鐵律七：只認真實素材指向玩家
指向玩家（유저/你）的內容，只能用【真實聊天記錄】裡出現過的梗、事件、稱呼。
沒有聊天記錄支撐時，只鋪「剛開始、還沒挑明」的輕痕跡，不許無中生有親密史。
NPC 全部虛構。角色本人長相/職業/性格嚴格follow人設。"""

_APP_BRIEF = {
    "talk": "메신저(카톡)：至少 3 個聊天室。第一個必為『나(玩家)』房，含一條沒發出去的 draft（最強鉤子）。其他房用來做親疏對比（對某人 반말、某房被靜音置底）。務必給 profiles 兩個檔案:main(對外的人設)＋一個 secret 부계정(小號),小號裡放一個『나에게 쓰기/寫給自己』的房間——沒人看得到的地方,每天一句記錄玩家的日常(부계 발견 모먼트)。每個 room 都要標 profile 歸屬(main 或小號 id)。",
    "sns": "大號 vs 小號(부계)：大號=給世界看的人設；小號=真我（深夜 0 讚 emo、疑似提到某個人）。小號至少 1 條標 clue。",
    "search": "搜索記錄(네이버)：items 是搜尋記錄,連續幾條要能連成一個心理過程(他在笨拙補救/在查一件難以啟齒的事),至少1條凌晨的、1條標 clue。另外務必給 browse(瀏覽記錄) 3-4 條——他點開過的頁面,比搜尋更誠實:用 site(地圖/部落格/購物/YouTube…)+visits(停留N分/回看N次/已收藏) 體現『行為』,最有料的標 hot/clue(反覆看同一頁、讀到底、收藏你要搬去的社區)。前端可用關鍵字實際搜尋這些內容。",
    "feed": "推薦 Feed：混合兩種不同性質的卡片，src 要體現差異——①算法被動推送（유튜브 추천/지도 추천，他沒搜過但演算法一直塞，最能暴露潛意識）②他自己的觀看行為（유튜브 시청 기록，同一支影片重複看 2-3 次、進度條每次拉到同一段，這是主動、有意圖的，和①的『連他自己都没意識到』形成對照）。至少各出現 1 張。",
    "footprints": "動線/足跡：大多通勤噪音，偶爾一個陌生地點（嘴上說加班，定位在別處 / 你家樓下停留 40 分鐘），或固定週期反覆出現的同一陌生地址（每週四晚，連續四週）——週期本身就是謎題。pins 給地圖坐標。",
    "purchase": "購買記錄：已購 + 購物車（未執行的念頭比已購更有戲——躺了很久沒下單的那件）。物流狀態可劇透未來（收貨日=你生日前）。",
    "market": "當(당근)二手：收/賣兩個方向（收絕版舊物=在挽回；賣情侶物=在告別）。砍價語氣 vs 對玩家語氣的反差。",
    "ledger": "記帳(토스)：輸出 month/total/note + txns 陣列（6-9 筆）。**備註(memo)比金額本身更重要**，是這個 App 的靈魂——每筆 hot 的 memo 都要像偷瞄到的手寫字條，例：『她今天應該沒吃飯』『不要告訴她是我』『上次的事，對不起』『6月12日』，或**空白備註但收款對象很可疑**。錢包要能同時滿足兩類幻想之一並選定：①有錢且願意為用戶花（提前預訂禮物/高額轉帳/替用戶付旅費/自動訂閱用戶喜歡的服務）②沒錢但仍在意（餘額不多卻先存禮物預算/分期買用戶想要的東西/取消自己的消費/轉帳給用戶後只剩很少）。必埋：1 筆固定週期自動轉帳或구독（每月同日同額，收款方模糊或備註只一個符號=長線謎題）；1 筆能對上中心秘密的轉帳（金額/日期/備註要和其他 App 對帳）。可選：投資持倉（토스증권/업비트，備註寫目標如『결혼자금』，浮虧仍加碼或突然清倉一半）、備註改過名的一筆（驚喜基金→算了）、分期（對話裡送的禮物其實在分期）。amt 用 +/− 號區分收支。",
    "voice": "錄音：元數據先曝光（檔名+時長+錄音時間就是鉤子——凌晨 3:22 錄了 6 分 40 秒什麼？）。至少 1 條甜向（半成品的歌/練習告白反復 NG/睡前碎碎念）、1 條懸疑向（口袋誤錄的環境音/只錄到後半段）。載體要從角色的職業人設裡長出來（練習室 demo、會議口述、工地對講…），不用通用款。標 clue 的放最有料那條。",
    "health": "健康：要做得像三星헬스/애플건강那樣「豐富、多天、多指標」,不能只有一句。① summary 一句總綱;② sleep 給最近7天曲線(bars)+週均(avg)+變化(delta_note);③ metrics 至少3-4個指標(심박수/걸음 수/스트레스/혈당/혈중산소…),每個都要有 trend(最近7天推移數組),讓玩家看到『某天/某時刻異常』的趨勢,而不是單點;④ dreams 是跨越好幾天的夢境日記(連續幾天同一個人=甜向稀有)。身體不說謊,是跨 App 對賬工具:某指標飆高的時刻要和聊天/動線/搜尋對得上,用 d 說清。",
    "folder": "文件夾：洋蔥結構（新建文件夾套三層）。其中一個要二級密碼（pass），名字曖昧（『要扔掉的東西』），裡面是最深的秘密（2000 字沒寄出的信）。",
    "gallery": "相冊：只有兩個 Tab——最近/最近刪除(30일 倒計時)。刪除區藏最狠的一張（偷拍你 47 張 / 裁掉另一個人臉的合照 / 對焦精準的偷拍），倒計時製造『再不看就永遠消失』的限時壓力。",
    "delivery": "外賣(배민)：份數+地址+備註拼出『和誰、在哪、怎麼吃』。口味異常（不吃辣卻點辣=有人在場）。備註是微型手寫。",
    "taxi": "打車(카카오T)：輸出 trips 陣列（3-5 筆）。行動軌跡本身就是謎題。大多是通勤噪音，但**必須埋入至少一個高價值疑點場景**（用 hot+d 講清）：角色到了用戶附近卻沒聯絡用戶／深夜打車去一個陌生地址／輸入用戶地址後又取消／吵架後去了兩人第一次見面的地方／某趟行程與角色此前說法不一致／出發後臨時改變目的地。進階細節：中途多一個上車點（way，路上接了人）、用了平時不用的卡（pay+oddPay=true）、深夜時間點。轨迹要和動線/聊天/記帳對得上。",
    "screen": "螢幕時間 + 앱 사용 기록：元數據的魅力。某 App 時長暴增、深夜活躍、和你聊天的同時段另一個 App 也在活躍、對你的聊天時長周環比暴跌。downloads 只放 1-2 條近期安裝/刪除記錄：裝過又刪的 App 是『行動衝動』的痕跡，要和主線事件對得上，不能用純獵奇的交友軟體梗。",
    "calls": "通話統計：時長即親密度。stats 做本月排行榜(Top 5)，dur 一定要含『N회/N次』；『你』這種 0회 的對象會自動被踢出排行、改成『通話 없음』小卡，所以 0회 項目要用 d 說清為什麼 0 次(從沒敢打/只有撥出取消)。recent 是最近通話流水，dir 用 in/out/miss/cancel，要看得出撥號人和時間;備註名的信息量(愛心 emoji/單字母/陌生號)、未接+語音留言是甜向彩蛋。",
    "music": "音樂(멜론)：플레이리스트 標題是韓國人的情緒出口，常用日期或一句話命名（不要用『我的歌單』這種空泛名字，要用『2/3』『그 겨울』這種只有他自己懂的標題）。至少 1 個 playlist 要能對上中心秘密的時間線（創建日=劇情裡的某個關鍵日）。最近播放榜第一名可以是一首玩家沒聽過的老歌，反覆重播——用 replay 次數暴露強迫性。可選：一個『함께 듣기』共享歌單，collab 對象的 ID 模糊或已停更（懸疑向最強單品，跟中心秘密掛鉤時才用，不要每個角色硬塞）。",
    "fortune": "運勢(포스텔러풍)：궁합(合婚)查詢至少 1 條，双方生日的組合要對上中心秘密（一組是玩家，另一組留懸念或明確指向秘密裡的另一人）。count 用『反覆查詢 N 次』體現『查的不是結果、是想再看一眼』的強迫性心理。logs 是這個 App 的靈魂——**查詢記錄直接體現角色的願望、恐懼和糾結**。大部分是無意義的『오늘의 운세』日常噪音，但要埋 2-3 條切身問題（타로/사주），問題內容像：『她是不是已經不喜歡我了？』『我應該主動聯繫嗎？』『我們以後會住在一起嗎？』『她身邊是不是有別人？』『如果我現在離開，她會不會難過？』。**體現關係進度感**：同一個問題可能被反覆詢問（第一次問→關係變化後再問→time 標『反覆 N 次』），或問題本身在演化（從『她喜歡我嗎』變成『怎麼讓她安心』）。可用一條標註『이 기록은 삭제되었다(此記錄已刪除)』體現角色想抹掉自己問過的痕跡。不要寫成通用占卜文案。",
}
def _persona_brief(persona: dict) -> str:
    skip = {"voice", "visibility", "anonymous_identities"}
    brief = {k: v for k, v in persona.items()
             if k not in skip and v not in (None, "", [], {})}
    return json.dumps(brief, ensure_ascii=False)


def _chat_lines_text(char_id: str, char_name: str, max_lines: int = 50) -> str:
    """最近一次 normal 會話壓成逐行對話，供指向玩家的素材唯一來源。"""
    session = chat._latest_session(char_id, mode="normal")
    if session is None:
        return ""
    lines: list[str] = []
    for m in session.get("messages", []):
        role = m.get("role")
        if role == "user":
            t = (m.get("content") or "").strip()
            if t:
                lines.append(f"유저(玩家): {t[:120]}")
        elif role == "assistant":
            for item in (m.get("items") or []):
                t = feed_posts._item_text(item).strip()
                if t:
                    lines.append(f"{char_name}: {t[:120]}")
    return "\n".join(lines[-max_lines:])


def _feed_text(char_id: str, limit: int = 6) -> str:
    feed = feed_posts.list_feed_posts(char_id)
    out = []
    for post in (feed.get("posts") or [])[:limit]:
        data = post.get("data") or {}
        c = data.get("content")
        if isinstance(c, dict):
            c = c.get("zh") or c.get("ko") or ""
        if c:
            out.append(f"- [{post.get('kind')}] {str(c).strip()[:140]}")
    return "\n".join(out)


def _material_blocks(persona: dict, chat_lines: str, feed_lines: str) -> list[str]:
    material = [f"# 角色人設（被偷看的手機主人）\n{_persona_brief(persona)}"]
    if chat_lines:
        material.append("# 真實聊天記錄（指向玩家的素材只能取自這裡）\n" + chat_lines)
    else:
        material.append("# 真實聊天記錄\n（暫無。兩人關係剛開始，只鋪最輕的痕跡，不編造親密史。）")
    if feed_lines:
        material.append("# 這個角色的第三方帖子（世界觀氛圍參考）\n" + feed_lines)
    return material


def _app_briefs_for(app_ids: list[str]) -> str:
    alias = {"sns_alt": "sns"}
    out = []
    for aid in app_ids:
        brief = _APP_BRIEF.get(alias.get(aid, aid))
        if brief:
            out.append(f"- {aid}：{brief}")
    return "\n".join(out)


def _build_free_messages(persona: dict, char_name: str,
                         chat_lines: str, feed_lines: str) -> list[dict]:
    """免費層：定中心秘密 + meta/clues/truth + 免費 App（chat/feed/footprints + sns 主號）。"""
    sys = (
        "你是韓國戀愛推理遊戲『查手機』的內容導演，精通把一個人的秘密藏進手機的日常數據裡。"
        "這是【免費層】：玩家一進手機就能看到的內容，也是整支手機的『故事骨架』。"
        "你要先定下中心秘密（meta.secret，後續付費層會照著它生成），再產出鎖屏/密碼 meta、"
        "3 條跨 App 線索的分佈規劃、真相卡，以及免費 App 的內容。"
        "所有展示文字用繁體中文（可偶爾夾韓文詞調味）。只輸出一個 JSON object，不要 markdown 圍欄，不要解釋。"
    )
    blocks = [
        METHODOLOGY,
        "# 這次只產出【免費層】：\n"
        "- meta（含 secret 故事聖經、hook_line、pass/pass_origin/hint/lock_msg、"
        "date_label/time/batt、stage_*、pass_changed、peek_arc 全填）\n"
        "- clues（3 條，規劃它們分別會出現在哪個 App——即使那個 App 屬於付費層，也先寫清 text）\n"
        "- truth（真相卡）\n"
        "- apps：只做 talk（메신저/카톡，最重要——聊天列表是最快的資訊露出）、"
        "feed（推薦流）、footprints（動線）、sns（只做大號 main，小號留給付費層）\n"
        "特別強調 talk：聊天室 4-6 個，備註名即人設（『❤️老公』『別接』『張總(勿删)』『前任(拉黑)』這種），"
        "每個 room 要有 unread（未讀數，可為 0）、置頂/免打擾語感、draft（未發送草稿）、最後訊息時間。",
        "# 這些功能各自要做的事\n" + _app_briefs_for(FREE_APP_IDS + ["sns"]),
        *_material_blocks(persona, chat_lines, feed_lines),
        "# 完整 JSON Schema 參考（鍵名固定，值用繁體中文；schema 裡的韓文只是欄位說明，不要照抄）\n"
        + SCHEMA
        + "\n\n注意：本次 apps 只輸出 talk / feed / footprints / sns 四個鍵（sns 只填 main，alt 留空或省略）。其餘 App 不要輸出。",
        "# 產出要求\n先想清中心秘密（寫進 meta.secret）與 3 條線索的分佈，"
        "再讓免費 App 的內容都指向它。talk 聊天室 4-6、feed 3-5、footprints pins+trips 各 3-5、sns 大號貼文 3-5。",
    ]
    return [{"role": "system", "content": sys},
            {"role": "user", "content": "\n\n".join(blocks)}]


def _build_paid_messages(persona: dict, char_name: str, chat_lines: str,
                         feed_lines: str, secret_bible: str,
                         app_ids: list[str]) -> list[dict]:
    """付費層：照著免費層定下的中心秘密，生成解鎖才可見的 App 內容。"""
    sys = (
        "你是韓國戀愛推理遊戲『查手機』的內容導演。這是【付費層】：玩家花열람권解鎖後才生成的深水區內容。"
        "你必須嚴格服從已定下的『中心秘密聖經』，讓每個 App 都成為那個秘密在不同數據維度的投影，跨 App 能對上。"
        "所有展示文字用繁體中文（可偶爾夾韓文詞調味）。只輸出一個 JSON object，不要 markdown 圍欄，不要解釋。"
    )
    emit_keys = "、".join(app_ids)
    blocks = [
        METHODOLOGY,
        "# 已定下的中心秘密聖經（免費層產出，必須遵守，不可另起爐灶）\n" + (secret_bible or "（未提供，請保守鋪陳）"),
        "# 這次只產出【付費層】這些 App：\n" + "、".join(app_ids)
        + "\n（sns_alt = SNS 小號/부계：主號已在免費層做過，這裡只做小號 alt，"
        "設計原則：大號與小號必須形成戲劇性反差，反差越大付費衝動越強。輸出鍵名用 sns_alt，值是 schema 裡 sns.alt 的結構）",
        "# 這些功能各自要做的事\n" + _app_briefs_for(app_ids),
        *_material_blocks(persona, chat_lines, feed_lines),
        "# 完整 JSON Schema 參考（鍵名固定，值用繁體中文；schema 裡的韓文只是欄位說明，不要照抄）\n"
        + SCHEMA
        + f"\n\n注意：本次只輸出一個 JSON object，頂層鍵是 apps，apps 底下只放這些鍵：{emit_keys}。"
        "不要輸出 meta / clues / truth，也不要輸出未列出的 App。",
        "# 產出要求\n每個 App 都要有料且跨 App 對得上中心秘密。"
        "搜索 5-7、錄音 3-4、相冊每 Tab 2-4、通話 stats+recent、打車/錢包/健康/文件/算命各給足 schema 條目。"
        "把 3 條線索裡屬於這些 App 的那幾條，標在對應 item 的 clue 欄位。",
    ]
    return [{"role": "system", "content": sys},
            {"role": "user", "content": "\n\n".join(blocks)}]
def _gen_path(demo_id: str) -> Path:
    return GEN_DIR / f"{demo_id}.json"


def _persona_name(record: dict) -> str:
    name = (record.get("persona") or {}).get("name")
    if isinstance(name, dict):
        return name.get("zh") or name.get("ko") or next(iter(name.values()), "")
    return str(name or "")


def _cover_url(record: dict, char_id: str) -> str | None:
    cover = pipeline._served_image_url(record.get("cover"))
    if cover and cover.startswith("/img/"):
        if (config.IMAGE_DIR / cover[len("/img/"):]).exists():
            return cover
    elif cover and cover.startswith("http"):
        return cover
    # 退回第一張存在的 feed 配圖
    feed = feed_posts.list_feed_posts(char_id)
    for post in (feed.get("posts") or []):
        img = post.get("image") or {}
        url = pipeline._served_image_url(img) if isinstance(img, dict) else None
        if url and url.startswith("/img/") and (config.IMAGE_DIR / url[len("/img/"):]).exists():
            return url
        if url and url.startswith("http"):
            return url
    return cover


def _write(demo_id: str, result: dict) -> dict:
    result["updated"] = int(time.time())
    _gen_path(demo_id).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _blank_result(demo_id: str, real_id: str, record: dict, char_name: str,
                  chat_used: bool) -> dict:
    return {
        "demo_id": demo_id,
        "char_id": real_id,
        "name": char_name,
        "cover_url": _cover_url(record, real_id),
        "chat_material_used": chat_used,
        "created": int(time.time()),
        "content": {"meta": {}, "clues": [], "truth": {}, "apps": {}},
        "paid_apps": {},   # app_id -> unix ts（已生成的付費 App）
    }


def generate_free(demo_id: str, real_id: str, temperature: float = 0.9) -> dict:
    """免費層：中心秘密 + meta/clues/truth + 免費 App。落盤時保留已生成的付費 App。"""
    record = pipeline.load_character(real_id)
    persona = record.get("persona") or {}
    char_name = _persona_name(record)
    chat_lines = _chat_lines_text(real_id, char_name or real_id)
    feed_lines = _feed_text(real_id)
    messages = _build_free_messages(persona, char_name, chat_lines, feed_lines)
    raw = api_client.chat(messages, model=config.LLM_MODEL,
                          temperature=temperature, max_tokens=24000)
    data = api_client.parse_json_text(raw)
    if not isinstance(data, dict) or "apps" not in data:
        raise ValueError(f"免費層模型未返回合法 JSON：{str(raw)[:300]}")
    # 只保留免費層該有的 App（sns 只留 main），避免模型越界多產。
    free_apps = {}
    for aid in FREE_APP_IDS + ["sns"]:
        if aid in (data.get("apps") or {}):
            free_apps[aid] = data["apps"][aid]
    existing = load_one(demo_id) or {}
    result = existing if existing.get("char_id") == real_id else \
        _blank_result(demo_id, real_id, record, char_name, bool(chat_lines))
    result.setdefault("content", {}).setdefault("apps", {})
    result["name"] = char_name
    result["cover_url"] = _cover_url(record, real_id)
    result["chat_material_used"] = bool(chat_lines)
    result["content"]["meta"] = data.get("meta") or {}
    result["content"]["clues"] = data.get("clues") or []
    result["content"]["truth"] = data.get("truth") or {}
    # 免費 App 覆蓋；付費 App（既有）原樣保留。
    for aid, node in free_apps.items():
        result["content"]["apps"][aid] = node
    return _write(demo_id, result)


def generate_paid(demo_id: str, real_id: str, app_ids: list[str] | None = None,
                  temperature: float = 0.9) -> dict:
    """付費層：解鎖某些 App 時，照免費層定下的中心秘密單次生成並併入落盤。

    app_ids 省略＝一次生成全部付費 App。sns_alt 會併入 apps.sns.alt。
    """
    record = pipeline.load_character(real_id)
    persona = record.get("persona") or {}
    char_name = _persona_name(record)
    chat_lines = _chat_lines_text(real_id, char_name or real_id)
    feed_lines = _feed_text(real_id)
    existing = load_one(demo_id)
    if not existing or existing.get("char_id") != real_id:
        # 付費層依賴免費層的中心秘密；沒有就先跑一次免費層。
        existing = generate_free(demo_id, real_id, temperature)
    secret_bible = _secret_bible(existing)
    wanted = [a for a in (app_ids or PAID_APP_IDS) if a in PAID_APP_IDS]
    if not wanted:
        raise ValueError("no valid paid app ids")
    messages = _build_paid_messages(persona, char_name, chat_lines,
                                    feed_lines, secret_bible, wanted)
    raw = api_client.chat(messages, model=config.LLM_MODEL,
                          temperature=temperature, max_tokens=28000)
    data = api_client.parse_json_text(raw)
    if not isinstance(data, dict):
        raise ValueError(f"付費層模型未返回合法 JSON：{str(raw)[:300]}")
    apps_out = data.get("apps") if isinstance(data.get("apps"), dict) else data
    result = existing
    result.setdefault("content", {}).setdefault("apps", {})
    result.setdefault("paid_apps", {})
    now = int(time.time())
    for aid in wanted:
        node = apps_out.get(aid)
        if node is None:
            continue
        if aid == "sns_alt":
            sns = result["content"]["apps"].setdefault("sns", {})
            sns["alt"] = node
        else:
            result["content"]["apps"][aid] = node
        result["paid_apps"][aid] = now
    return _write(demo_id, result)


def _secret_bible(record: dict) -> str:
    content = record.get("content") or {}
    meta = content.get("meta") or {}
    parts = []
    if meta.get("secret"):
        parts.append("中心秘密：" + str(meta["secret"]))
    clues = content.get("clues") or []
    if clues:
        parts.append("三條線索：" + " / ".join(
            f"{c.get('id')}={c.get('text')}" for c in clues if isinstance(c, dict)))
    truth = content.get("truth") or {}
    if truth.get("body"):
        parts.append("真相卡：" + str(truth.get("title") or "") + " — " + str(truth["body"]))
    if meta.get("pass_origin"):
        parts.append("密碼由來：" + str(meta["pass_origin"]))
    return "\n".join(parts)


def generate_one(demo_id: str, real_id: str, temperature: float = 0.9) -> dict:
    """demo 自動觸發：免費層 + 一次性把全部付費層也生成好，保證直接看效果。"""
    generate_free(demo_id, real_id, temperature)
    return generate_paid(demo_id, real_id, None, temperature)


def load_one(demo_id: str) -> dict | None:
    p = _gen_path(demo_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_all() -> dict:
    """前端一次 fetch：所有已生成角色的內容（未生成的角色缺席，前端保留 mock）。"""
    chars = []
    for demo_id in DEMO_CHAR_MAP:
        one = load_one(demo_id)
        if one:
            chars.append(one)
    return {"chars": chars}


def find_by_char_id(char_id: str) -> dict | None:
    """Return a generated phone dossier by real platform character id.

    The phone UI has stable demo ids for its presentation templates, while
    chat/schedule use platform character ids.  Keeping this resolver here
    prevents the two surfaces from inventing separate mappings.
    """
    for demo_id, mapped_id in DEMO_CHAR_MAP.items():
        if mapped_id != char_id:
            continue
        return load_one(demo_id)
    return None


def generate_all(temperature: float = 0.9) -> dict:
    """逐個角色生成免費層+付費層（單個失敗不阻斷其餘）。"""
    out = []
    for demo_id, real_id in DEMO_CHAR_MAP.items():
        try:
            out.append({"demo_id": demo_id, "ok": True,
                        "result": generate_one(demo_id, real_id, temperature)})
        except Exception as e:  # noqa: BLE001
            out.append({"demo_id": demo_id, "ok": False, "error": str(e)})
    return {"results": out}


def tiers() -> dict:
    """前端用：哪些 App 免費、哪些付費鎖。"""
    return {
        "free": FREE_APP_IDS + ["sns"],
        "paid_lock": PAID_LOCK_IDS,
        "paid_apps": PAID_APP_IDS,
    }
