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
# 單角色 demo：只保留游嶼。
DEMO_CHAR_MAP: dict[str, str] = {
    "yuwi": "char_1783597290_f0d265",
}

# ── App 單一數據源(canonical registry) ────────────────────────────
# apps.config.json 是前後端共享的 App 清單；下方 4 張表全部由它派生。
# 上/下架一個 App = 改 apps.config.json 一項，不用動這裡的邏輯。
# 若檔案缺失/損壞，回退到本檔內建的靜態清單(見 _FALLBACK_*)，保證服務不掛。
_APPS_CONFIG_PATH = Path(__file__).resolve().parent / "apps.config.json"


def _load_apps_config() -> list[dict]:
    try:
        with open(_APPS_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        apps = data.get("apps") or []
        return apps if isinstance(apps, list) and apps else []
    except Exception:
        return []


_APPS = _load_apps_config()


def _derive_from_config() -> dict | None:
    """由 canonical registry 派生後端需要的 4 張表。回傳 None 表示 config 不可用。"""
    if not _APPS:
        return None
    app_order, free_ids, paid_ids, paid_lock, brief = [], [], [], [], {}
    for a in _APPS:
        aid = a.get("id")
        if not aid:
            continue
        app_order.append((aid, a.get("type_backend") or a.get("type") or "list",
                           a.get("icon") or "📱", (a.get("name") or {}).get("ko") or aid))
        gen = a.get("gen") or {}
        layers = gen.get("layers") or []
        pay = a.get("pay")
        # 大小號分離的 App(有 key_paid，如 sns)：免費層由呼叫端以 +["sns"] 單獨帶，
        # 不進 FREE_APP_IDS，避免與 free_messages 裡的 +["sns"] 重複。
        if "free" in layers and not gen.get("key_paid"):
            free_ids.append(aid)
        if "paid" in layers:
            paid_ids.append(gen.get("key_paid") or aid)
            if pay in ("bundle", "granular"):
                paid_lock.append(aid)
        if gen.get("brief"):
            brief[aid] = gen["brief"]
    return {"APP_ORDER": app_order, "FREE": free_ids,
            "PAID": paid_ids, "PAID_LOCK": paid_lock, "BRIEF": brief}


# 20 個功能的固定清單（app_id, 前端渲染器 type, 圖示, 名稱）——每個角色都全帶。
# 靜態 fallback：config 不可用時使用。config 可用時會在本檔末端被派生值覆蓋。
APP_ORDER: list[tuple[str, str, str, str]] = [
    ("talk", "chat", "💬", "메신저"),
    ("memo", "memo", "📝", "메모"),
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
    ("calendar", "calendar", "📅", "캘린더"),
    ("mail", "mail", "✉️", "메일"),
    ("stay", "stay", "🏩", "숙박"),
]

# ── 免費 / 付費 兩層 ──────────────────────────────────────────────
# 免費層：跟著日程更新、有紅點提示、進手機即可看（chat 列表最快露資訊）。
# 付費層：解鎖才生成（單次 LLM 呼叫），生成後落盤，可手動重生成。
FREE_APP_IDS = ["talk", "feed", "footprints"]
# sns 主號（대표）在免費層生成；小號（부계）在付費層以 sns_alt 生成後併入 apps.sns.alt。
PAID_APP_IDS = [
    "memo", "sns_alt", "search", "purchase", "market", "ledger", "voice",
    "health", "folder", "gallery", "delivery", "taxi", "screen",
    "calls", "music", "fortune", "calendar", "mail", "stay",
]
# 前端付費鎖：這些 app 進手機時要花열람권解鎖（sns 主號免費、小號在 app 內再鎖）。
PAID_LOCK_IDS = [
    "memo", "search", "purchase", "market", "ledger", "voice", "health",
    "folder", "gallery", "delivery", "taxi", "screen", "calls",
    "music", "fortune", "calendar", "mail", "stay",
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
        {"id":"main","name":"대표 프로필명","av":"이모지","sub":"공개 페르소나(냉정/프로/과묵 — 대외의 얼굴)","note":"이 프로필의 방 목록 상단 설명"},
        {"id":"ghost","name":"부계정명","av":"이모지","secret":true,"sub":"아무도 모르는 얼굴(감정 과잉/솔직/취약 — main과 정반대)","status":"알림 꺼짐/숨김 등","note":"부계정을 켰을 때의 설명(뒷계정 발견 모먼트). main과의 인물상 반전이 클수록 좋다"}
      ],
      "rooms":[
      {"id":"me","profile":"main","is_user":true,"name":"나","note":"방 설명","msgs":[{"who":"them|me","t":"대사","time":"HH:MM","voice":false}],"draft":"입력창에 쓰다 만 미발신 초안(가장 강한 훅) 또는 \"\"","bookmark":"보관한 메시지 설명 또는 \"\""},
      {"id":"other1","profile":"main","name":"상대 이름(관계 암시)","note":"","msgs":[{"who":"them|other","t":"대사","time":"HH:MM"}]},
      {"id":"self","profile":"ghost","name":"나에게 쓰기","note":"부계정 전용 방","msgs":[{"who":"them","t":"아무도 안 보는 곳에 쓰는 한 줄","time":"HH:MM"}]}
    ]},
    "memo": {"notes":[{"title":"메모 제목(또는 첫 줄)","body":"메모 본문(여러 줄, \\n 허용). 속마음/계획/수집한 정보","time":"작성/수정 시각 라벨","pinned":false,"edited":"수정 흔적 라벨(예:'3번 수정됨') 또는 \"\"","strike":["지웠다 만 흔적: 취소선으로 남은 옛 문장 배열 또는 생략"],"hot":false,"clue":null,"d":"펼치면 나오는 상세(왜 이걸 적었는지) 또는 생략"}]},
    "sns": {"main":{"handle":"@아이디","bio":"공개 페르소나 소개","posts":[{"t":"게시물","time":"","likes":숫자}]},
            "alt":{"handle":"@부계아이디","bio":"뒷계 · 비공개 느낌","posts":[{"t":"0 like 새벽 emo, 그 사람 암시","time":"","likes":0,"hot":true,"clue":"c1|null"}]}},
    "search": {"items":[{"t":"검색어(연속 검색이 심리 궤적)","time":"라벨","hot":false,"clue":null,"d":"탭 시 상세(교차검증/속내) 또는 생략"}],
                "browse":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","t":"방문한 페이지 제목","site":"사이트/앱 종류(지도/블로그/쇼핑 등)","visits":"체류시간/재방문 횟수 라벨 또는 \"\"","time":"라벨","hot":false,"clue":null,"d":"행동이 폭로하는 속내(끝까지 읽음/반복 방문/저장) 또는 생략"}]},
    "feed": {"note":"알고리즘이 밀어주는 것 = 관심사 폭로, 한 줄","cards":[{"emo":"이모지","src":"유튜브/지도/쇼핑 추천 등","t":"추천 콘텐츠","why":"왜 이게 뜨는지(그의 관심사 폭로)","hot":false,"d":"상세 또는 생략"}]},
    "footprints": {"pins":[{"x":0-100,"y":0-100,"lbl":"장소","hot":false}],"trips":[{"route":"동선","time":"라벨","s":"부가","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "purchase": {"orders":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","t":"상품명","price":"₩가격","date":"주문일 라벨","step":"0결제|1상품준비|2배송중|3배송완료 정수","addr":"배송지(누구·어디 폭로) 또는 \"\"","hot":false,"clue":null,"d":"상세(수령일=상대 생일 전날 등 의도 폭로) 또는 생략"}],
                 "cart":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","t":"장바구니에 담아둔 미결제 상품(실행 못한 마음이 더 셈)","price":"₩가격","note":"담아둔 기간/망설임 라벨(예:'3개월째 고민 중')","hot":false,"clue":null,"d":"왜 아직 못 샀는지 속내 또는 생략"}]},
    "market": {"items":[{"emo":"이모지","dir":"buy|sell","t":"상품명","price":"가격 라벨","s":"동네인증/거래 후기(대타인 말투 대비)","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "ledger": {"total_balance":"₩총자산(모든 계좌 합)","balance_note":"재력/궁핍 서사 한 줄(있어서 쓴다 / 없어도 쓴다)",
               "accounts":[{"name":"계좌명(입출금/적금/증권/페이 등)","balance":"₩잔액","note":"부가(예:'월급통장'/'그녀 몰래 만든 적금') 또는 \"\"","hot":false}],
               "month":"이번 달 라벨(예:6월)","total":"₩이번 달 총지출","income":"₩이번 달 총수입(+부호, 예:+₩1,180,000)","note":"총평 한 줄 또는 \"\"",
               "incomes":[{"emo":"카테고리 이모지","t":"수입 항목(외주 정산/저작권료/환불 등)","amt":"+₩금액","time":"라벨","memo":"메모=돈의 쓰임/속내(예:'받자마자 대부분 적금으로'/'같이 가려던 공연, 결국 1장만')","hot":false}],
               "txns":[{"emo":"카테고리 이모지","t":"항목명(수취인/상점)","amt":"±₩금액","memo":"손글씨 메모=속내(금액보다 중요: '그녀 오늘 밥 못 먹었을 듯'/'그녀에게 말하지 마'/'6월12일'/공백)","time":"라벨","hot":false,"clue":null}],
               "pending":[{"t":"받을 돈/미수금 항목","amt":"₩금액","s":"부가(누구에게/왜) 또는 \"\"","hot":false} ],
               "refunds":[{"t":"환불 항목","amt":"₩금액","s":"환불 사유(취소한 소비=마음의 변화) 또는 \"\"","hot":false}],
               "autopay":[{"t":"자동이체/구독 대상","amt":"₩금액","cycle":"매월 N일/매주 등","memo":"수취인 모호 or 목표 라벨(예:'결혼자금')/공백=미스터리","hot":false,"clue":null,"d":"상세(장기 미스터리, 교차검증) 또는 생략"}],
               "installments":[{"t":"할부 상품(대화에서 선물한 것이 실은 할부일 수 있음)","total":"₩총액","monthly":"₩월 납부","remain":"N개월 남음","memo":"메모/공백","hot":false}],
               "stocks":[{"emo":"카테고리 이모지(📈/☕/💸 등)","t":"종목명(한국 종목/ETF/코인)","qty":"보유 라벨(예:'3주 · 평단 ₩84,000' 또는 '매월 자동 매수 ₩50,000')","pl":"손익률(+N%/−N%)","plHot":false,"memo":"왜 이 종목을 샀는지=속내(예:'매일 가는 편의점 모회사라 주주라도 되자고'/'외주 준 제작사라'/'한 달 끊김—그 주에 선물 할부가 나가서')"}]},
    "voice": {"locked":false,"memos":[{"name":"파일명.wav","len":"m:ss 또는 h:mm:ss","meta":"녹음 시각/편집횟수 등 메타","wave":"calm|mid|burst","hot":false,"clue":null,"script":"재생 시 나오는 내용/전사(감정 농도 최대)"}]},
    "health": {"summary":"이 사람의 몸 상태를 한 줄로 요약(지표 전체를 관통하는 서사)",
                "sleep":{"hours":"오늘 수면 시간 숫자문자열","avg":"주 평균 숫자 또는 생략","note":"수면 급변이 감정 폭로 한 줄","delta_note":"전주 대비 변화 라벨(예:'-40%') 또는 생략","bars":[7개 0-100 숫자(최근 7일 수면)],"stages":{"deep":"깊은 수면 %(숫자)","light":"얕은 수면 %","rem":"REM %"},"inbed":"취침 시각 HH:MM","wake":"기상 시각 HH:MM"},
                "week_labels":["요일 라벨 7개 또는 생략(기본 월~일)"],
                "metrics":[{"icon":"이모지","name":"지표명(심박수/걸음 수/스트레스/혈중산소 등)","val":"대표 수치","unit":"단위/시각 라벨","note":"짧은 부가","trend":[7개 숫자(최근 7일 추이)],"hot":false,"clue":null,"d":"다른 App과 교차검증되는 속내(어느 시각에 왜 튀었는지) 또는 생략"}],
                "hr_events":[{"time":"HH:MM","bpm":"급변 심박 숫자(예:113)","base":"평소 심박 숫자(예:72)","trigger":"이 순간 무슨 일이 있었나(채팅/알림/장소와 교차, 예:'‘너 지금 어디야’ 메시지 받음')","hot":false,"clue":null,"d":"상세: 심박이 왜 튀었는지, 다른 App과 대조되는 속내. 또는 생략"}],
                "dreams":[{"date":"날짜 라벨(여러 날에 걸쳐)","t":"꿈 일기 한 줄","hot":false,"d":"상세 또는 생략"}],
                "alarms":[{"time":"HH:MM","label":"알람 라벨(설정자의 의도가 드러나는 한 줄) 또는 \"\"(라벨 없음=더 수상함)","repeat":"매일/평일/금요일마다 등","hot":false,"d":"상세 또는 생략"}]},
    "folder": {"folders":[{"icon":"📁","name":"폴더명","count":"항목 수 라벨","pass":"4자리 또는 null(2차 비번)","pass_hint":"2차 비번 힌트 또는 생략","clue":null,"docs":[{"t":"문서명","s":"부가","time":"라벨","hot":false,"d":"상세 또는 생략"}]}]},
    "gallery": {"tabs":[{"id":"recent","name":"최근"},{"id":"deleted","name":"최근 삭제","note":"30일 후 영구삭제"}],
                "photos":{"recent":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","cap":"캡션","c1":"제목","c2":"라이트박스 설명","hot":false}],
                          "deleted":[{"emo":"이모지","grad":"...","cap":"캡션","countdown":"N일 남음","locked":true,"c1":"제목","c2":"설명","hot":false,"clue":null}]}},
    "delivery": {"orders":[{"emo":"음식 이모지","shop":"가게명","serv":"N인분(1인분인데 2인분=누가 있었다)","items":"메뉴 요약","addr":"배송지(집 아닌 곳=행적 폭로) 또는 \"\"","memo":"요청사항=미니 손글씨(안 매운맛인데 매운맛 등 이상신호)","price":"₩금액","time":"라벨","warn":"serv가 이상하면 true 아니면 생략","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "taxi": {"trips":[{"from":"출발지","to":"도착지","way":"중간 경유지(길에서 누굴 태웠다) 또는 \"\"","time":"심야 시각 라벨(행적 폭로)","fare":"₩요금","pay":"결제수단(평소 안 쓰던 카드=이상)","oddPay":"결제수단이 이상하면 true 아니면 생략","hot":false,"clue":null,"d":"상세: 고가치 시나리오를 반드시 하나 이상 심어라(유저 근처까지 왔지만 연락 안 함/심야에 낯선 주소/유저 주소 입력 후 취소/싸운 뒤 첫 만남 장소로/이전 진술과 모순되는 동선/출발 후 목적지 급변경). 또는 생략"}]},
    "screen": {"total":"Nh Nm","total_note":"부가",
               "pickups":"오늘 든 횟수(숫자)","pickups_note":"부가(예:'평소보다 3배')","notis":"받은 알림 수(숫자)","first_use":"첫 사용 시각","last_use":"마지막 사용 시각(늦을수록 수상)",
               "bars":[{"app":"앱명","val":"Nh Nm","pct":0-100,"note":"↑/↓% 또는 생략"}],
               "hourly":[24개 0-100 숫자(0시~23시 시간대별 사용량, 심야 급증이 폭로)],
               "timeline":[{"time":"HH:MM","app":"앱명","act":"행동(열었다/껐다/검색함/N분 머물렀다 등)","hot":false,"clue":null,"d":"이 순간의 속내(예:'23:14 채팅 열고 23:15 바로 껐다=쓰다 지웠다') 또는 생략"}],
               "insights":[{"t":"행동패턴 폭로 한 줄","hot":false,"clue":null,"d":"상세 또는 생략"}],
               "downloads":[{"icon":"이모지","app":"최근 설치/삭제한 앱","action":"설치함|삭제함|설치 후 N분 만에 삭제 등","time":"라벨","hot":false,"clue":null,"d":"왜 이 앱을 설치했는지 드러나는 상세 또는 생략"}]},
    "calls": {"stats":[{"name":"통화 상대","pct":0-100,"dur":"N회 · N분(횟수 반드시 포함)","hot":false,"clue":null,"zero_ic":"0회일 때 아이콘(예: ⤴) 또는 생략","zero_sub":"0회일 때 부제(예: '통화 0회 · 발신 취소 5회') 또는 생략","zero_tag":"0회일 때 태그(예: '걸었다 끊음') 또는 생략","d":"상세 또는 생략. 0회인 상대는 랭킹에서 빠지고 '통화한 적 없음' 구역에 따로 표시된다. 0회는 '무관심'이 아니라 서사의 핵심일 수 있으니(걸었다 끊음/발신 취소 N건), zero_sub·zero_tag·d로 왜 0회인지 반드시 표현하라"}],
              "recent":[{"dir":"in|out|miss|cancel","name":"상대","s":"통화 길이/부가(발신취소면 '신호 전에 끊음' 등)","time":"라벨","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "music": {"playlists":[{"emo":"이모지","grad":"linear-gradient(135deg,#색,#색)","title":"재생목록 제목(날짜/한 줄 감정, 한국인은 이렇게 이름 짓는다)","sub":"곡 수/업데이트 빈도 등 메타","collab":"함께 듣기 상대 아이디 또는 생략(공유 플레이리스트일 때만)","hot":false,"clue":null,"d":"상세 또는 생략"}],
              "recent":[{"title":"곡명","artist":"아티스트","replay":"반복 재생 횟수 라벨 또는 \"\"(노이즈 곡)","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "fortune": {"today":{"date":"오늘 날짜 라벨","score":"오늘의 총운 점수(0-100)","summary":"오늘 운세 한 줄"},
                "sections":[{"key":"total|luck|work|love","icon":"이모지","title":"섹션명(총운/대운/사업·재물/애정·인연)","body":"이 사람 사주 분석 2-3문장(로맨스 추리에 맞게: 애정 섹션은 반드시 유저와의 관계를 암시)","hot":false,"clue":null,"d":"상세(다른 App과 교차되는 속내) 또는 생략"}],
                "compat":[{"kind":"궁합|사주","names":"조회한 두 사람(생일 조합 암시)","score":"점수 또는 \"—\"","note":"결과 해석 한 줄","count":"조회 횟수 라벨(반복 조회=집착의 증거)","hot":false,"clue":null,"d":"상세 또는 생략"}],
                "logs":[{"q":"질문 내용(대부분은 심심풀이 일일운세, 하나는 반복되는 절실한 질문. 시간순으로 질문이 진화=집착의 궤적)","kind":"일일운세|타로|사주","time":"라벨/반복횟수","hot":false,"clue":null,"d":"상세 또는 생략"}]},
    "calendar": {"month":"이번 달 라벨(예:11월)","today":"오늘 날짜 숫자","events":[{"date":"N일 또는 'N일~M일'","title":"일정 제목(제목 자체가 단서: '그날' / 'D-day' / '병원' / 이름 이니셜만)","time":"HH:MM 또는 종일 또는 \"\"","tag":"분류(기념일/마감/병원/약속 등) 또는 \"\"","repeat":"매년/매월 등 또는 생략(반복되는 기념일=집착 or 미련)","secret":false,"hot":false,"clue":null,"d":"이 일정의 속내(왜 이 날을 표시했나, 다른 App과 교차) 또는 생략"}],
                "countdowns":[{"label":"D-day 라벨(예:'D-14')","title":"무엇을 세고 있나","hot":false,"d":"상세 또는 생략"}]},
    "mail": {"folders":[{"id":"inbox","name":"받은편지함","count":"안읽음 수"},{"id":"draft","name":"임시보관","count":"수"}],
             "mails":[{"folder":"inbox|draft|sent","from":"발신자(이름/도메인이 단서: 병원/변호사/회사/항공사/호텔)","to":"수신자 또는 \"\"","subject":"제목(가장 큰 단서: '[예약 확인]' / '사직서' / '건강검진 결과' / 'Re: 우리')","preview":"본문 미리보기 한 줄","time":"라벨","unread":false,"draft":false,"star":false,"hot":false,"clue":null,"d":"메일을 열면 드러나는 속내(임시보관함의 안 보낸 메일이 가장 위험: 썼지만 못 보낸 진심) 또는 생략"}]},
    "stay": {"note":"모텔/호텔 예약 앱(여기어때/야놀자 풍). 로맨스 추리의 고자극 소재지만 반드시 '설명은 되는' 애매함으로: 실제로는 출장/가족/촬영 로케일 수 있게. 실증 금지",
             "bookings":[{"place":"숙소명(지역+타입, 예:'망원 OO스테이')","area":"지역","date":"체크인 날짜 라벨","nights":"N박","guests":"인원(1명인데 2명=누가 있었다, 또는 1명이라 더 외로움)","price":"₩금액","status":"이용완료|예약취소|노쇼","memo":"요청사항/메모(늦은 체크인/조용한 방 등, 미니 단서) 또는 \"\"","cancelled":false,"hot":false,"clue":null,"d":"상세: 반드시 '나쁘게도 좋게도 읽히는' 애매함을 남겨라(유저 집 근처인데 안 만남/기념일에 혼자 1박/예약 후 바로 취소/촬영 로케이션일 수도). 또는 생략"}]}
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

# 鐵律二：跨 App 互指，讓玩家自己拼出結論（偵探感）
中心秘密要在 3 個以上不同 App 留下能對上的痕跡，且**互相指認**：外賣週二沒單 ↔ 動線
週二出門到某地 ↔ 搜索欄那天在查感冒藥 ↔ 記帳那天有一筆藥局支出 ↔ 健康那晚心率飆高。
同一個日期／金額／地點／時間，要在多個 App 反覆出現，玩家翻到第二、第三個 App 時會「啊——
原來如此」。**不要在任何一個 App 直接把答案寫出來**；讓線索散落，結論由玩家腦補。這比直接
餵結論爽 10 倍。不要做收集進度條或集卡 UI——痕跡就藏在真實的 App 內容裡，越像真手機越好。

# 鐵律三：老虎機情緒配比（70% 噪音 / 25% 曖昧 / 5% 重磅）
每支手機、每個 App 的 item 都要按這個比例鋪，這是回訪的核心引擎：
- **70% 日常噪音**：平庸到無聊（繳費、通勤、跟媽媽的乾巴巴問候、訂閱扣款）。噪音不是廢話，
  它讓有料的那幾條更炸，也讓「翻手機」像真的在大海撈針。噪音 item 不標 hot。
- **25% 曖昧信號**：「看著有點意思但不確定」——這是最大的一檔，是間歇性獎勵的來源。標 hot。
- **5% 重磅線索**：整支手機只有 1-2 條真正的重磅（直指中心秘密的痕跡）。標 hot + 進 clue 對帳。
配比錯了會毀掉節奏：全是重磅 = 廉價；全是噪音 = 無聊。每個 App 至少 1 條非噪音。

# 鐵律三之二：不實錘的曖昧（回訪的核心燃料）
每支手機必須埋 1-2 條「看著有問題、但解釋得通」的內容——它同時支持兩種讀法：
往壞想（他有事瞞我）成立，往好想（其實是在為我準備驚喜／單純的巧合）也成立。
永遠不要在同一支手機裡把它「實錘」。這種懸而未決的曖昧，才是玩家反覆打開、反覆重讀、
反覆在聊天裡試探的燃料。實錘 = 故事結束；不實錘 = 玩家自己養故事。

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

# 鐵律六：本地化（韓國市場）——語言硬性規定
App 皮膚是韓國的，但**所有輸出的展示文字必須是繁體中文，一個韓文字都不許出現**。
這包括：App 名稱、聯絡人名、動作描述（act）、備註、標題、內文——全部繁體中文。
禁止輸出任何韓文句子或韓文動詞（例：不可寫『열었다』『껐다』『머물렀다』『카톡』『삼성노트』，
要寫成『打開了』『關掉了』『停留了』『通訊』『備忘錄』）。品牌一律用中文代稱
（카톡→通訊 / 네이버→搜尋 / 배민→外賣 / 카카오T→叫車 / 당근→二手 / 토스→錢包 / 삼성노트→備忘錄 / 갤러리→相簿 / 카메라→相機 / 시계→鬧鐘）。
金額用韓元（₩/만원）。數字要自洽（通話時長、點讚數、螢幕時間互相成比例）。

# 鐵律七：只認真實素材指向玩家
指向玩家（유저/你）的內容，只能用【真實聊天記錄】裡出現過的梗、事件、稱呼。
沒有聊天記錄支撐時，只鋪「剛開始、還沒挑明」的輕痕跡，不許無中生有親密史。
NPC 全部虛構。角色本人長相/職業/性格嚴格follow人設。"""

_APP_BRIEF = {
    "talk": "메신저(카톡)：4-5 個聊天室。**每個 room 的 msgs 要 4-8 條、像真實對話一來一往推進(who 在 them/me/other 間交替)，嚴禁同一句話重複多遍刷屏、嚴禁整屏只有一種訊息**。每條訊息都要有新信息或情緒推進，讀起來像真的聊天。第一個必為『나(玩家)』房，房裡要能看出關係進展(從試探到親近)，並含一條沒發出去的 draft（最強鉤子）。"
                "\n**profiles 兩個檔案必須人設反差強烈**:①main=對外的臉(專業/客套/話少/表情克制，對客戶用敬語、句子短)；②一個 secret 부계정 小號=沒人看的臉(情緒外露/碎碎念/脆弱/坦白，句子長而濕)。同一個人在兩邊語氣要判若兩人——這個反差本身就是付費爽點。"
                "\n各房要做親疏對比:對玩家的備註名藏著心意(用時間/暗號而非真名)、對某人用反話冷淡、某房被靜音置底。小號裡放一個『나에게 쓰기/寫給自己』的房——每天一句記錄玩家的日常(부계 발견 모먼트)。每個 room 都要標 profile 歸屬(main 或小號 id)。備註名即人設(『❤️老公』『別接』『前任(拉黑)』這種)。",
    "memo": "備忘錄(메모)：角色寫給自己看的東西——內心 OS、計劃、收藏的資訊、待辦。3-6 條。關鍵是**刪改痕跡**：用 edited(改過幾次) 和 strike(劃掉沒刪乾淨的舊句子，用取消線體現他改口/反悔的心路) 製造『他寫了又改』的偷窺感。至少 1 條和玩家/中心秘密相關（例：一份沒發出的計劃、關於某人的碎碎念、一個列到一半的清單）。1 條可置頂(pinned)。備忘錄比聊天更誠實，因為沒人會看。",
    "sns": "大號 vs 小號(부계)：大號=給世界看的人設；小號=真我（深夜 0 讚 emo、疑似提到某個人）。小號至少 1 條標 clue。",
    "search": "搜索記錄(네이버)：items 是搜尋記錄,連續幾條要能連成一個心理過程(他在笨拙補救/在查一件難以啟齒的事),至少1條凌晨的、1條標 clue。另外務必給 browse(瀏覽記錄) 3-4 條——他點開過的頁面,比搜尋更誠實:用 site(地圖/部落格/購物/YouTube…)+visits(停留N分/回看N次/已收藏) 體現『行為』,最有料的標 hot/clue(反覆看同一頁、讀到底、收藏你要搬去的社區)。前端可用關鍵字實際搜尋這些內容。",
    "feed": "推薦 Feed：混合兩種不同性質的卡片，src 要體現差異——①算法被動推送（유튜브 추천/지도 추천，他沒搜過但演算法一直塞，最能暴露潛意識）②他自己的觀看行為（유튜브 시청 기록，同一支影片重複看 2-3 次、進度條每次拉到同一段，這是主動、有意圖的，和①的『連他自己都没意識到』形成對照）。至少各出現 1 張。",
    "footprints": "動線/足跡：大多通勤噪音，偶爾一個陌生地點（嘴上說加班，定位在別處 / 你家樓下停留 40 分鐘），或固定週期反覆出現的同一陌生地址（每週四晚，連續四週）——週期本身就是謎題。pins 給地圖坐標。",
    "purchase": "購買記錄：已購 + 購物車（未執行的念頭比已購更有戲——躺了很久沒下單的那件）。物流狀態可劇透未來（收貨日=你生日前）。",
    "market": "當(당근)二手：收/賣兩個方向（收絕版舊物=在挽回；賣情侶物=在告別）。砍價語氣 vs 對玩家語氣的反差。",
    "ledger": "記帳/錢包(토스)：這是完整的錢包系統，收支兩邊都要清晰。要輸出：①total_balance 總資產 + balance_note（用財力講一個故事：『有錢且願意為她花』or『沒錢卻還在為她省』，二選一並貫穿全 App）②accounts 2-4 個不同帳戶（活期/儲蓄/**證券投資**/Pay，其中可藏一個『她不知道的帳戶』，證券帳戶 note 寫估值損益如『估值損益 -₩61,400 (-12%)』）③month 月份標籤 + income 本月總收入(+號) + total 本月總支出(−號)，讓收支對比一目了然④**incomes 收入明細 2-3 筆**（接案結算/版稅/退款等，memo 體現這筆錢去哪了或心情，例：『到帳當天大半轉進存錢罐』『本來想一起去的演出，最後只留一張自己去的』）⑤txns 本月支出流水 5-7 筆⑥pending 待收款⑦refunds 退款⑧autopay 自動轉帳/訂閱（必埋 1 筆每月同日同額、收款方模糊=長線謎題）⑨installments 分期（送用戶的禮物其實在分期）⑩**stocks 投資持倉 2-3 支**（韓國散戶很愛炒股，體現生活狀態；每支用 memo 暗含線索，例：買『每天去的便利店母公司』當股東、買『給自己接案的製作公司』、定投 ETF『中斷了一個月—那週剛好扣了禮物分期』；pl 是損益率 +/−N%）。**所有線索一律走 memo 手寫備註，不要寫旁白解說**——hot 的 memo 像偷瞄到的字條：『她今天應該沒吃飯』『不要告訴她是我』『6月12日』。金額/日期/備註要和其他 App 對帳（跨 App 互指）。amt 用 +/− 號。錢包要能表現：生活狀態、為誰花錢、如何表達愛、是否在準備某件事、一筆無法解釋的支出。",
    "voice": "錄音：元數據先曝光（檔名+時長+錄音時間就是鉤子——凌晨 3:22 錄了 6 分 40 秒什麼？）。至少 1 條甜向（半成品的歌/練習告白反復 NG/睡前碎碎念）、1 條懸疑向（口袋誤錄的環境音/只錄到後半段）。載體要從角色的職業人設裡長出來（練習室 demo、會議口述、工地對講…），不用通用款。標 clue 的放最有料那條。",
    "health": "健康(삼성헬스)：做得像三星헬스那樣豐富、多天、多指標，內容一定要多、可滑很久，玩家才覺得付費值。① summary 一句總綱(精煉一句，別長篇);② sleep 最近7天曲線(bars)+週均(avg)+變化(delta_note)+**stages 睡眠分期(深/淺/REM 百分比)+inbed 入睡時刻+wake 起床時刻**;③ metrics **5-7個指標**(심박수/걸음 수/스트레스/카페인/호흡/체온/운동 等)各帶 trend 7天推移，每個都短短一句就好，重在數量多;④ **hr_events 心率×消息關聯時間線 3-5 條，這是靈魂**：身體不說謊，某個時刻心率從平常 62 突然飆到 104，同一分鐘他收到你的『你在哪』、看你的頭像、或你剛離開——把 time/bpm/base/trigger 對上聊天或其他 App(跨 App 互指)，讓玩家自己讀出『他嘴上說沒事，身體出賣了他』;⑤ dreams **3-5 條**跨越好幾天的夢境日記(連續幾天同一個人=甜向稀有，其中標 1 條『好夢』)。每條都短，但條目要多。飆高時刻必和聊天/動線/搜尋對得上，用 d 說清。",
    "folder": "文件夾：洋蔥結構（新建文件夾套三層）。其中一個要二級密碼（pass），名字曖昧（『要扔掉的東西』），裡面是最深的秘密（2000 字沒寄出的信）。",
    "gallery": "相冊：兩個 Tab——最近(recent 3-5 張)/最近刪除(deleted **至少 3-4 張**，30일 倒計時)。**鐵則：每一張照片(含 recent)都必須勾住三件事之一——①他的內心真實想法(嘴上不說、照片出賣他) ②和玩家有關(偷拍你、你給的東西、你出現過的地方) ③他的中心秘密。禁止純風景/靜物/食物這種毫無指向的噪音填充**(例:別放『完美的太陽蛋』『昏暗的窗戶』這種空鏡)。cap 標題要能勾起好奇(『捨不得刪的那張』『放大了三次的角落』),c1/c2 用來揭開為什麼拍/為什麼留/為什麼刪。recent 舉例:你落在收銀台的字條(有折痕=隨身帶)、他偷偷截的你的動態、他反覆看的一張你沒注意到自己入鏡的合照。刪除區是靈魂,每張都要有『為什麼刪』的重量:偷拍你的連拍、裁掉另一個人臉的合照、拍了又後悔的截圖、想傳卻沒傳出去的自拍。倒計時(countdown)製造『再不看就永遠消失』的限時壓力,數字要有差異(1일/7일/23일)。刪除區要能和其他 App 對帳(刪除日期 ↔ 記帳/動線)。所有文字繁體中文。",
    "delivery": "外賣(배민)：份數+地址+備註拼出『和誰、在哪、怎麼吃』。口味異常（不吃辣卻點辣=有人在場）。備註是微型手寫。",
    "taxi": "打車(카카오T)：輸出 trips 陣列（3-5 筆）。行動軌跡本身就是謎題。大多是通勤噪音，但**必須埋入至少一個高價值疑點場景**（用 hot+d 講清）：角色到了用戶附近卻沒聯絡用戶／深夜打車去一個陌生地址／輸入用戶地址後又取消／吵架後去了兩人第一次見面的地方／某趟行程與角色此前說法不一致／出發後臨時改變目的地。進階細節：中途多一個上車點（way，路上接了人）、用了平時不用的卡（pay+oddPay=true）、深夜時間點。轨迹要和動線/聊天/記帳對得上。",
    "screen": "螢幕時間(數位健康)：這是統計儀表板，不是行為流水帳——重點是『用量的數字』而非『幾點做了什麼』（那是足跡 App 的事，別跟它撞）。輸出：①total 今日總時長 + pickups 今天拿起手機次數(+pickups_note 如'比平常多3倍')+ notis 收到通知數 + last_use 最後使用時刻(越晚越可疑=失眠/等消息)②bars 各 App 使用時長排行(3-5 個，某個 App 佔比異常高就是沉迷信號，App 名用中文如『通訊/相簿/搜尋』)③hourly 24 個 0-100 數字(逐時使用量，**深夜那幾格突然飆高就是無聲的告白**)④most_contact 最常打開的對話對象(名字+今日打開次數，指向玩家最戳)⑤insights 2-3 條數據洞察句(『唯一開著提示音的對話：你』『和某人的對話一天打開31次』這種**從統計裡讀出的心事**，不要寫成幾點幾分的動作流水)⑥downloads 1-2 條近期安裝/刪除(裝過又刪=衝動的痕跡)。**不要輸出 timeline 那種『23:14 打開→23:15 關掉』的行為時間線**——把那些欲望藏進 bars 佔比、most_contact 次數、hourly 深夜峰值裡，讓玩家從數字自己讀出來。所有文字繁體中文。",
    "calls": "通話統計：時長即親密度。stats 做本月排行榜(Top 5)，dur 一定要含『N회/N次』；『你』這種 0회 的對象會自動被踢出排行、改成『通話 없음』小卡，所以 0회 項目要用 d 說清為什麼 0 次(從沒敢打/只有撥出取消)。recent 是最近通話流水，dir 用 in/out/miss/cancel，要看得出撥號人和時間;備註名的信息量(愛心 emoji/單字母/陌生號)、未接+語音留言是甜向彩蛋。",
    "music": "音樂(멜론)：플레이리스트 標題是韓國人的情緒出口，常用日期或一句話命名（不要用『我的歌單』這種空泛名字，要用『2/3』『그 겨울』這種只有他自己懂的標題）。至少 1 個 playlist 要能對上中心秘密的時間線（創建日=劇情裡的某個關鍵日）。最近播放榜第一名可以是一首玩家沒聽過的老歌，反覆重播——用 replay 次數暴露強迫性。可選：一個『함께 듣기』共享歌單，collab 對象的 ID 模糊或已停更（懸疑向最強單品，跟中心秘密掛鉤時才用，不要每個角色硬塞）。",
    "fortune": "運勢(포스텔러풍)：完整命理 App，不只是查詢記錄。輸出：①today 今日總運(date/score/summary)②**sections 四段命盤分析**(key=total總運/luck大運/work事業·財富/love애정·인연 各一段)，每段 2-3 句像真的算命解讀，**애정·인연 那段必須影射角色與玩家的關係**(例:'今年桃花在身邊很近的人身上，但你遲遲不敢確認')，四段裡至少埋一段能和其他 App 對帳的속내③궁합(合婚)查詢至少 1 條，双方生日的組合要對上中心秘密（一組是玩家，另一組留懸念或指向秘密裡的另一人）。count 用『反覆查詢 N 次』體現『查的不是結果、是想再看一眼』的強迫性。logs 是靈魂——**查詢記錄直接體現角色的願望、恐懼和糾結**。大部分是無意義的『오늘의 운세』日常噪音，但要埋 2-3 條切身問題（타로/사주），問題內容像：『她是不是已經不喜歡我了？』『我應該主動聯繫嗎？』『我們以後會住在一起嗎？』『她身邊是不是有別人？』『如果我現在離開，她會不會難過？』。**體現關係進度感**：同一個問題可能被反覆詢問（第一次問→關係變化後再問→time 標『反覆 N 次』），或問題本身在演化（從『她喜歡我嗎』變成『怎麼讓她安心』）。可用一條標註『이 기록은 삭제되었다(此記錄已刪除)』體現角色想抹掉自己問過的痕跡。不要寫成通用占卜文案。",
    "calendar": "行事曆(캘린더)：**日程標題本身就是線索**。events 5-7 條，大部分是日常噪音（繳費日、催稿、家庭聚餐），但埋 1-2 條可疑的：只寫縮寫或符號的日子（'그날'/'D'/名字縮寫）、反覆每年出現的紀念日（repeat=매년，是集念還是未了）、和其他 App 對得上的日子（記帳 6/12 ↔ 行事曆 6/12 標了個沒說明的事）。secret=true 的日程標題模糊、要點開 d 才知道真相。再給 1-2 條 countdowns（D-day），角色在倒數某件事——搬家、生日、一個只有他知道意義的日子。日期要和健康/記帳/動線互指。",
    "mail": "郵件(메일)：韓國人也用 Gmail/Naver 郵件收正式通知。mails 5-7 封，靠**寄件人網域 + 主旨**當線索：醫院（健檢結果）、律師、公司（onboarding/사직서 확인）、航空/飯店（預訂確認）、前任或曖昧對象的名字。分 inbox/draft/sent 三類。**임시보관함（draft）的未寄出郵件最狠**——寫了卻沒送出的真心話、辭職信、給某人的長信，draft=true 標出。大部分是噪音（電子報、帳單），1-2 封是重磅（和中心秘密對帳）。unread 標未讀。",
    "stay": "住宿預訂(여기어때/야놀자 풍)：羅曼史推理的高刺激素材，但**必須嚴格遵守『不實錘的曖昧』**——每一筆都要能往壞想（他和誰去了）也能往好想（出差/家人/拍攝場勘/一個人躲起來）。bookings 2-4 筆：多為正常出差住宿噪音，埋 1 筆真正可疑的（用戶家附近卻沒約用戶 / 紀念日當天獨自 1 泊 / 預訂後馬上取消 status=예약취소 / 深夜臨時訂房）。guests 人數是關鍵信號（1 名卻更孤獨，或 2 名=有人）。d 一定要留『解釋得通』的空間，禁止寫死成出軌實錘。要和動線(taxi/footprints)、行事曆對得上。",
}

# ── 用 canonical registry 派生值覆蓋上方靜態表 ──────────────────────
# 結構性清單(順序/免費/付費/鎖)一律以 apps.config.json 為準：上下架只改 JSON。
# 例外：_APP_BRIEF 保留本檔的精調 prompt 為權威，JSON 的 brief 只用來「補充」
# 後端還沒寫過 brief 的新 App(如透過 JSON 新增的功能)，避免覆蓋降級既有質量。
_CFG = _derive_from_config()
if _CFG:
    APP_ORDER = _CFG["APP_ORDER"]
    FREE_APP_IDS = _CFG["FREE"]
    PAID_APP_IDS = _CFG["PAID"]
    PAID_LOCK_IDS = _CFG["PAID_LOCK"]
    for _aid, _brief in _CFG["BRIEF"].items():
        _APP_BRIEF.setdefault(_aid, _brief)   # 只補缺，不覆蓋既有精調 brief


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
        "所有展示文字必須是繁體中文，一個韓文字都不許出現（App 名/人名/動作/備註全用中文，品牌用中文代稱）。只輸出一個 JSON object，不要 markdown 圍欄，不要解釋。"
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
        "每個 room 要有 unread（未讀數，可為 0）、置頂/免打擾語感、draft（未發送草稿）、最後訊息時間。"
        "**每個聊天室 msgs 要 4-8 條、一來一往像真對話(who 交替)，絕對禁止同一句重複刷屏、禁止整屏同一種訊息**——那是最沒吸引力的敗筆。"
        "**兩個 profile 人設要判若兩人**:main 對外冷靜克制話少，小號情緒外露坦白脆弱。",
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
        "所有展示文字必須是繁體中文，一個韓文字都不許出現（App 名/人名/動作/備註全用中文，品牌用中文代稱）。只輸出一個 JSON object，不要 markdown 圍欄，不要解釋。"
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
        "# 產出要求\n每個 App 按 70/25/5 配比鋪 item，跨 App 對得上中心秘密。**內容一定要多、可滑很久**——每筆都短，但條目要密，玩家花錢才覺得值。每條要能扣『他的內心/和玩家有關/他的秘密』其中之一。"
        "搜索 items 5-7 + browse 3-4、錄音 memos 4-5、相冊每 Tab 3-5、通話 stats 3+recent 5-6。"
        "錢包(ledger)：total_balance+accounts 2-4(含 1 個證券投資帳戶)+month/income/total 收支對比+incomes 收入 2-3+txns 支出 5-7+stocks 持倉 2-3+autopay/installments/pending/refunds 各 1-2 條。線索全走 memo,不寫旁白。"
        "屏幕時間(screen)：total/pickups/notis/last_use + bars 各 App 用時排行 3-5 + hourly 24 格(深夜要有起伏) + most_contact 最常打開對話 + insights 2-3 條數據洞察(不要行為時間線 timeline)。"
        "健康(health)：metrics 5-7 個 + hr_events 3-5 條心率×消息關聯 + sleep 7 天(含 stages 分期+inbed/wake) + dreams 3-5 條。內容要多、可滑久。"
        "算命(fortune)：today + sections 四段(total/luck/work/love，love 影射玩家) + compat 1-2 + logs 3-4(同一問題反覆問=關係進度)。"
        "相冊(gallery)：recent 4-5 + **deleted 至少 3-4 張**(各有刪除理由 + countdown)。"
        "SNS(sns/sns_alt)：主號 posts 2-3(帶 comments 2-3)、小號 alt posts 4-5(0 讚、越私密越好,和大號反差越大越好)。"
        "購物(purchase)：orders 2-3(禮物/為玩家買的藏線索在 memo/giftcard/recipient) + cart 2-3(反覆猶豫的商品)。"
        "二手(market)：items 3-4(求購/出售各半,線索走 s 描述與 price 變化)。"
        "外賣(delivery)：orders 2-3(至少 1 筆送到『非本人地址』、寄件匿名=偷偷關心)。"
        "移動(taxi)：trips 4-5(高價值疑點:到玩家附近沒聯繫/深夜陌生地址/輸入玩家地址後取消/出發後改目的地)。"
        "足跡(footprints)：pins 3-4 + trips 3-4(每筆停留時間/徘徊本身就是心事)。"
        "音樂(music)：playlists 2-3(含 1 個私密/為玩家建的) + recent 2-3(反覆播的歌對得上其他 App)。"
        "文件(folder)：folders 3-4,每個 folder docs 1-3(檔名+修改時間本身是線索,可埋 1 個二級密碼夾)。"
        "備忘錄(memo)：notes 3-4(至少 2 條有 strike 刪改痕跡=沒刪乾淨的真心)。"
        "行事曆(calendar)：events 5-7(埋 1-2 條標題可疑/反覆紀念日) + countdowns 1-2 條 D-day。"
        "郵件(mail)：mails 5-7(分 inbox/draft/sent，임시보관 draft 至少 1 封未寄真心話)。"
        "住宿(stay)：bookings 2-4(嚴守不實錘的曖昧，每筆 d 都要能好壞兩讀，禁止出軌實錘)。"
        "把重磅線索裡屬於這些 App 的那幾條，標在對應 item 的 clue 欄位。",
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
