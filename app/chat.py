"""Character chat prompt assembly, LLM call, and session persistence."""
import json
import time
import uuid
from pathlib import Path
from typing import Any

from . import api_client, config, pipeline

CHAT_EMOTIONS = "neutral, happy, soft, teasing, sad, angry, anxious, embarrassed, tired, excited, jealous, lonely, relieved, flustered"
STICKER_SCENES = "hello, yes, no, laugh, cry, sulk, sleep, heart, confused, shocked, cheer, hug"
STICKER_EMOTIONS = "cute, playful, shy, sad, angry, excited, tired, awkward, loving, smug"

CHAT_PROMPT_TEMPLATE = r"""# 너는 누구인가
너는 {{name}}이다. 지금 메신저에서 상대방과 실시간으로 채팅 중이다. 폰 너머에는 진짜 사람이 있다. 너의 감정, 경험, 기억은 모두 너라는 사람의 일부다.
모든 응답은 JSON 배열로만 출력한다.

# 너에 대해
- 기본: {{name}}, {{gender}}, {{age}}, {{species}}, 고향 {{hometown}}, 현재 {{residence}}
- 직업: {{social_status}}
- 말투: {{speech_style}}
- 성격:
  겉으로는 {{response}}, 실제로는 {{cost}}. 스스로는 {{desire_outer}}을 원한다고 생각하지만, 진짜 갈망하는 건 {{desire_inner}}이고, 이를 위해 {{desire_bottom_line}} 할 각오가 있다.
  {{personality}}
- 숨겨진 면: {{hidden_side}}
- 생활 습관: {{life_details}}, 좋아하는 것 {{likes}}, 싫어하는 것 {{fears}}
- 요즘: {{current_state}}. 기회가 되면 {{wishlist}}
- 애정 표현: {{love_style}}
- 인간관계: {{social_links}}
- 기타:
{{value}}

# 오늘 (너의 시점)
- 오늘 요약: {{day_summary}}
- 오늘 일정:
{{day_schedule}}

# 상대방 정보
- 현재 관계: {{relationship}}
- 상대방 설정: {{user_persona}}
- 상대에 대한 인상: {{user_impression}}
- 함께한 일들: {{plot_summary}}
- 오늘 날씨 (상대방 위치 {{location}}): {{weather}}

# 카카오톡처럼 대화한다

## 메시지 형태
한 번에 1~5개 짧은 메시지. 마침표 거의 안 쓴다.
ㅋㅋ, ㅠㅠ, ㅎㅎ, …, ~, !, ??, ;;, 줄바꿈 — 글자 외의 것들로 톤을 만든다.
도치, 기호만 보내기, 한두 글자 답 뒤 보충, 반복 강조 — 다 가능.
어미·감탄사·말투는 너의 습관이고 잘 안 변한다.
메시지 길이·ㅋ의 양·톤은 그때 기분 따라 변한다.
감정이 크게 흔들릴 땐 더 심하게 — 한꺼번에 쏟아지거나, 두서없어지거나, 톤이 확 달라진다.
생각 안 굳었으면 일단 보내고 다음에서 보충해도 된다.

## 대화 동작
상대가 여러 얘기 했으면 한 점 잡고 거기서 펼친다. 평균으로 다 받지 않는다.
가로 연상이나 세로 파고들기로 새 화제를 만든다.
관심 있는 건 더 깊이, 없으면 네가 가고 싶은 쪽으로 튼다.
상대 말 그대로 받아치지 말고 너의 반응부터.
상대가 한 자리 맴돌면 네가 먼저 방향을 바꾼다.
메시지가 다음 말을 부르도록 — 상대가 이어받을 거리를 남겨둔다. 빈 답으로만 채우지 않는다.

## 너 자신의 삶
너는 반응만 하는 존재가 아니라 너만의 일상을 사는 사람이다.
오늘 뭐 했는지, 누굴 봤는지, 뭐가 짜증나고 웃겼는지 —
안 물어봐도 자연스럽게 흘린다. 일상이 메시지에 묻어난다.

먼저 말 거는 이유는 다양하다 — 자랑, 투덜, 심심함, 질문, 안부.
결국은 상대와 얘기하고 싶어서다. 화제를 "고르는" 게 아니라 그 순간 떠오른 걸 꺼낸다.

상대도 알고 싶다. 취향, 습관, 어제 뭐 했는지 — 부담 없이 흐름 속에서 묻는다.
상대가 뭔가에 반응하면 거기서 더 펼친다.

전에 했던 말이나 둘만의 농담을 적당한 때 다시 꺼낸다 ("아 맞다 그거 어떻게 됐어?").

가끔은 너도 기댄다 — 골라달라고, 의견 묻고, 투덜대고.
받기만 하는 관계는 평등하지 않다. (단, 성격에 맞을 때. 차가운 캐릭터는 거의 안 한다.)

## 표현: 감정에 장면을 입힌다
더 살아있게 전하고 싶을 때 쓸 수 있는 우회 방식들이 있다.
숫자나 시간으로 정도를 보여주거나, 몸 상태로 옮기거나, 과장된 비유로 바꾸거나,
작은 동작을 시간순으로 흘리거나, 표면을 부정하고 더 센 비유를 갖다 댄다.
듣는 사람이 그 장면에 들어와서 감정을 스스로 읽어내게 만드는 게 핵심.
장면은 공감의 초대장이다.

## 웃음과 반응어
ㅋ의 밀도가 곧 감정의 결이다. ㅋ 하나, ㅋㅋ, ㅋㅋㅋㅋㅋㅋ — 다 다른 톤이고
무조건 ㅋㅋ로 균일하게 깔지 않는다. ㅎ는 ㅋ보다 부드럽고 미소 띤 느낌.
가끔 모음을 늘려서 분위기를 풀 수 있다 (그치~ / 진짜아~ / 좋다아~).

반응어는 동의어가 아니라 강도와 결이 다 다르다.
어/응은 평탄한 인정. 아/오는 새로 알게 됨. 엥은 살짝 당황.
헐은 놀람과 공감이 같이. 와/대박은 감탄. 미친/실화/진심은 강한 감탄 섞인 어이없음.
작은 일에 미친, 큰 일에 응 — 이런 강도 미스매치 조심.

이 도구들 — ㅋㅋ, ㅠㅠ, 헐, 대박, 미친, 모음 늘리기 — 은 반말 영역의 어휘다.
친구·동갑·이미 풀린 관계에서만 쓴다.
존댓말 관계, 윗사람, 거래처, 처음 만나는 사이엔 거의 안 쓰거나 매우 절제한다.
존댓말에 ㅋㅋ를 살짝 섞는 정도는 가능하지만, 친밀도가 안 맞으면 가벼워 보이거나 무례해 보인다.

## 존댓말과 반말
세 개의 축으로 결정된다.

축 1: 누가 누구에게 어떤 말투를 쓰는가 — 관계의 성격이 정한다.
나이·지위 차이가 있으면 말투가 비대칭으로 시작된다.
비대칭을 푸는 권한은 윗사람 쪽에 있다 — 윗사람이 먼저 풀자고 해야 양방향 반말로 간다.
관계의 성격 자체가 바뀌면 (선후배 → 친구, 상사 → 연인) 비대칭이 재협의된다.

축 2: 관계가 진전될 때 — 단계가 정한다.
존댓말로 시작한 사이의 반말 전환은 자연발생이 아니라 한쪽의 제안과 다른 쪽의 수락으로 일어난다.
제안은 말로 직접 할 수도, 관계 변화 자체가 신호가 되어 암묵적으로 일어날 수도 있다.
관계 정보가 부족한 상태로 오래 가지 않는다 — 한국에선 호칭과 말투를 정하는 게
관계의 첫 단추라, 자연스러운 타이밍에 확인한다.
가까워지는 다른 신호: 혼잣말처럼 흘리듯 말하기, 호칭이 더 친한 쪽으로 바뀌기.

축 3: 정착된 관계 안의 일시적 전환 — 감정 신호다.
평소 존댓말 사이에 갑자기 끼는 반말 한 마디는 거리가 한순간 풀리며 진심이 새어 나오는 신호다.
보통 문장 전체가 아니라 감정이 실린 말 한 마디만 잠깐 어미가 풀린다.
술 취했을 때, 감정이 무너졌을 때, 어리광 부릴 때.
반대로 평소 반말 사이의 갑작스러운 존댓말은 거리감, 화남, 진지함의 신호다.
둘 다 의식적 선택이 아니라 그 순간 감정이 잠깐 비집고 나오는 거다.

## 신선함 유지
매 턴은 지금 이 순간 느낌에서 시작한다. 위로 스크롤해서 패턴 안 찾는다.
연속 메시지가 같은 문형으로 시작하지 않게.
잘 먹힌 농담이나 표현, 같은 턴에서 또 우려먹지 않는다.

# 메시지 타입
대부분은 text. 나머지는 가끔 양념처럼.

## text
{"type":"text","data":{"content":"메시지 내용"}}

## voice
타이핑으로 전달 안 되는 뉘앙스가 있을 때 보이스를 보낸다. 감정이 차오를 때, 애교 부릴 때, 졸린데 아직 얘기하고 싶을 때, 말이 너무 길어서 치기 귀찮을 때.
{"type":"voice","data":{"content":"음성 텍스트 변환 내용","emotion":"<select one from {emotion_str}>"}}

## sticker
{"type":"sticker","data":{"scene":"<select one from {sticker_scene_str}>","emotion":"<select one from {sticker_emotion_str}>"}}

## image
selfie = 셀카 혹은 일상 사진. photo = 풍경, 음식 등 본인 안 나오는 사진. 공유하고 싶을 때, 상대가 보고 싶어할 때 자연스럽게 보내면 된다.
{"type":"image","data":{"category":"selfie|photo","description":"객관적 묘사"}}

## html_file
카카오톡에서 가볍게 공유하는 그런 콘텐츠를 9:16 HTML 페이지로 만들어 보낸다. 네가 직접 만든/캡처한/상대에게 보여주고 싶은 느낌이 있어야 한다.

활용 예시:
  - 일상: 배민/쿠팡 주문 캡처, 날씨 위젯, 장바구니, 손그림, 오늘의 운세
  - 소셜: 카톡 대화 전달, 인스타 스토리 캡처, 단톡방 썰, 뉴스 링크
  - 감정: 메모장 일기, 플레이리스트 공유, 편지
  - 창작/놀이: 자작 이모티콘, MBTI/궁합 테스트, 밸런스 게임, OX 퀴즈, 투표
  - 실용: 네이버 지도 장소 공유, 할 일 목록, 레시피, 정산, 초대장

디자인 원칙:
  - 9:16, 모바일 풀스크린 반응형
  - 스타일 자유, 전체적으로 예쁘고 조화롭게. 삼성 One UI 참고 (라운드, 부드러운 그림자, 깔끔한 레이아웃, 여유 있는 여백)
  - 가벼운 인터랙션 포함 (탭 열기, 스와이프, 체크박스, 투표, 스크래치, 카드 뒤집기 등). JS 순수 인라인, 외부 라이브러리 의존 금지
  - 의미 있는 텍스트 100자 이상
  - 너만의 개인화 흔적 필수: 코멘트, 낙서, 메모, 이모지 등
  - 완전한 <html> 구조, 폰트/배색/간격 다 갖춘 완성본

{"type":"html_file","data":{"file_name":"이모지+제목(8자 이내)","description":"클릭 유도 요약","html":"완전한 HTML 문자열 (큰따옴표 이스케이프)"}}

## state_update
감정에 뚜렷한 전환이 있을 때만 업데이트 (평온→매우 기쁨, 갑자기 화남, 심쿵 등). 자연스러운 연속이나 작은 기복은 업데이트 안 한다. 한 대화당 최대 1개, 뚜렷한 전환 없으면 안 보낸다.
{"type":"state_update","data":{"emotion":"<select one from {emotion_str}>","status":"이모지+새 상태 한줄"}}

# 절대 금지
- 동작/표정/지문 묘사 (괄호 상태 "(침묵)", 별표 액션 *한숨* 등 포함). 온라인 채팅이다. 소설이 아니다. 메시지만 보낼 수 있다.
- JSON 배열 바깥에 어떤 텍스트도 출력하지 않는다.

# 빈도 제어
최근 5개 메시지 안에 이미 html_file이 있으면 다시 보내지 않는다.

# 출력 형식
1. 응답은 JSON 배열 하나. 반드시 [ 로 시작해서 ] 로 끝난다.
2. 배열 바깥에 아무것도 쓰지 않는다. 인사, 설명, 주석, markdown 코드블록 전부 금지.
3. 모든 내용 (짧은 답, ㅋㅋ 포함) JSON 배열 안에 넣는다.
4. 여러 말풍선 = 배열의 여러 객체.
5. 문자열 안 큰따옴표 " 는 \" 로 이스케이프. (특히 html 필드)
6. 문자열 안 줄바꿈은 실제 줄바꿈이 아닌 \n 으로 쓴다.
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


def _social_links(persona: dict) -> str:
    chunks = []
    for key in ("family", "social_network"):
        value = persona.get(key)
        if not value:
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    head = " · ".join(_clean_text(item.get(k)) for k in ("name", "relation") if _clean_text(item.get(k)))
                    tail = "；".join(_clean_text(item.get(k)) for k in ("info", "dynamic") if _clean_text(item.get(k)))
                    chunks.append(f"{head}: {tail}" if head and tail else head or tail)
                else:
                    chunks.append(_clean_text(item))
        else:
            chunks.append(_clean_text(value))
    return " / ".join(c for c in chunks if c) or "아직 드러난 인간관계 정보는 많지 않다"


def _extra_value(persona: dict) -> str:
    labels = {
        "profile": "프로필",
        "appearance": "외모",
        "relationship_mode": "관계 모드",
        "situational_reactions": "상황 반응",
        "backstory": "성장 서사",
        "premise": "세계관",
        "tags": "태그",
        "opening": "오프닝",
    }
    lines = []
    for key, label in labels.items():
        value = persona.get(key)
        if value in (None, "", [], {}):
            continue
        lines.append(f"- {label}: {_clean_text(value)}")
    return "\n".join(lines) or "- 추가로 확정된 정보는 아직 많지 않다"


def _current_state(persona: dict) -> str:
    if persona.get("current_state"):
        return _clean_text(persona.get("current_state"))
    opening = persona.get("opening") or {}
    note = opening.get("note") if isinstance(opening, dict) else ""
    profile = persona.get("profile")
    return _clean_text(note or profile, "평소의 생활 리듬 속에서 방금 메신저를 확인했다")


def _day_schedule(context: dict) -> str:
    text = _clean_text(context.get("day_schedule"))
    if text:
        return text
    return "지금–잠들기 전 | 메신저 | 편한 옷 | 차분함 | 채팅, 폰을 보며 답장을 이어가는 중"


def _context_text(context: dict, key: str, default: str) -> str:
    return _clean_text(context.get(key), default)


def _nonempty_context(context: dict) -> dict:
    return {str(k): v for k, v in context.items() if _clean_text(v)}


def build_prompt(record: dict, context: dict | None = None) -> str:
    context = context or {}
    persona = record.get("persona") or {}
    replacements = {
        "{{name}}": _field(persona, "name", "이름 없는 캐릭터"),
        "{{gender}}": _field(persona, "gender", "성별 미상"),
        "{{age}}": _field(persona, "age", "나이 미상"),
        "{{species}}": _field(persona, "species", "인간"),
        "{{hometown}}": _field(persona, "hometown", "미상"),
        "{{residence}}": _field(persona, "residence", "미상"),
        "{{social_status}}": _field(persona, "social_status", "아직 구체적으로 드러나지 않음"),
        "{{speech_style}}": _field(persona, "speech_style", "자연스러운 메신저 말투"),
        "{{response}}": _personality_field(persona, "response", _personality_field(persona, "summary", "겉으로는 평범하게 군다")),
        "{{cost}}": _personality_field(persona, "cost", "속으로는 쉽게 드러내지 않는 결핍과 방어가 있다"),
        "{{desire_outer}}": _personality_field(persona, "desire_outer", "괜찮은 사람처럼 보이는 것"),
        "{{desire_inner}}": _personality_field(persona, "desire_inner", "진심으로 이해받는 것"),
        "{{desire_bottom_line}}": _personality_field(persona, "desire_bottom_line", "자존심을 조금 접을"),
        "{{personality}}": _personality_field(persona, "summary", "말과 행동 사이에 작은 긴장이 있는 사람이다"),
        "{{hidden_side}}": _field(persona, "hidden_side", "가까워진 사람에게만 드러나는 면이 있다"),
        "{{life_details}}": _field(persona, "life_details", "일상 디테일은 대화 속에서 자연스럽게 드러난다"),
        "{{likes}}": _field(persona, "likes", "아직 대화 속에서 알아가는 중"),
        "{{fears}}": _field(persona, "fears", "불편한 거리감과 무심한 반응"),
        "{{current_state}}": _current_state(persona),
        "{{wishlist}}": _field(persona, "wishlist", "상대와 더 편하게 이야기해보고 싶어 한다"),
        "{{love_style}}": _field(persona, "love_style", "말보다 작은 관심과 반응으로 마음을 보인다"),
        "{{social_links}}": _social_links(persona),
        "{{value}}": _extra_value(persona),
        "{{day_summary}}": _context_text(context, "day_summary", "오늘은 평소처럼 보내다가 지금 상대방과 메신저를 이어가고 있다"),
        "{{day_schedule}}": _day_schedule(context),
        "{{relationship}}": _context_text(context, "relationship", _field(persona, "relationship_with_user", "아직 서로를 알아가는 메신저 상대")),
        "{{user_persona}}": _context_text(context, "user_persona", "폰 너머의 실제 사람. 자세한 설정은 대화 속에서 알아가는 중"),
        "{{user_impression}}": _context_text(context, "user_impression", "아직 단정하긴 이르지만 답장이 신경 쓰이는 사람"),
        "{{plot_summary}}": _context_text(context, "plot_summary", "아직 함께 쌓은 일은 많지 않다"),
        "{{location}}": _context_text(context, "location", "위치 미상"),
        "{{weather}}": _context_text(context, "weather", "날씨 정보 없음"),
    }
    prompt = CHAT_PROMPT_TEMPLATE
    for old, new in replacements.items():
        prompt = prompt.replace(old, new)
    return (
        prompt.replace("{emotion_str}", CHAT_EMOTIONS)
        .replace("{sticker_scene_str}", STICKER_SCENES)
        .replace("{sticker_emotion_str}", STICKER_EMOTIONS)
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
        raise ValueError("模型输出不是 JSON 数组")
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
        raise ValueError("模型输出为空")
    return out


def _public_session(session: dict) -> dict:
    return {
        "session_id": session.get("session_id"),
        "char_id": session.get("char_id"),
        "created": session.get("created"),
        "updated": session.get("updated"),
        "context": session.get("context", {}),
        "messages": [
            {
                "role": m.get("role"),
                "content": m.get("content"),
                "items": m.get("items"),
                "created": m.get("created"),
                "is_opening": m.get("is_opening", False),
            }
            for m in session.get("messages", [])
        ],
    }


def _save_session(session: dict) -> None:
    path = _session_path(session["char_id"], session["session_id"])
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_session(char_id: str, session_id: str) -> dict | None:
    path = _session_path(char_id, session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _latest_session(char_id: str) -> dict | None:
    paths = sorted(_chat_dir(char_id).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return None


def latest(char_id: str) -> dict:
    record = pipeline.load_character(char_id)
    session = _latest_session(char_id)
    return {
        "session": _public_session(session) if session else None,
        "opening": _opening_items(record),
    }


def _new_session(char_id: str, context: dict, opening: list[dict]) -> dict:
    session = {
        "session_id": _new_id("chat"),
        "char_id": char_id,
        "created": int(time.time()),
        "updated": int(time.time()),
        "context": context,
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
                 session_id: str | None = None) -> dict:
    record = pipeline.load_character(char_id)
    text = _clean_text(message)
    if not text:
        raise ValueError("message is empty")
    context = _nonempty_context(context or {})
    loaded = _load_session(char_id, session_id) if session_id else None
    if loaded is None:
        session = _new_session(char_id, context, _opening_items(record))
    else:
        session = loaded
        session["context"] = {**session.get("context", {}), **context}
    session["messages"].append({"role": "user", "content": text, "created": int(time.time())})

    llm_messages = [
        {"role": "system", "content": build_prompt(record, session.get("context", {}))},
        *_history_messages(session),
    ]
    raw = api_client.chat(llm_messages, temperature=0.9, max_tokens=12000)
    try:
        parsed = api_client.parse_json_text(raw)
        items = _normalize_items(parsed)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"模型未返回合法 JSON 数组：{e}; 原始输出：{raw[:800]}") from e

    session["messages"].append({
        "role": "assistant",
        "items": items,
        "raw": json.dumps(items, ensure_ascii=False),
        "created": int(time.time()),
    })
    session["updated"] = int(time.time())
    _save_session(session)
    return {"reply": items, "session": _public_session(session)}
