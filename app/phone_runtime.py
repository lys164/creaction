"""Persistent state shared by Phone Peek, character chat, and schedules.

Phone content is a generated dossier.  This module stores only the evolving
interaction state around that dossier: a phone-peek entry, risk traces, and
the latest schedule-derived notifications.  It deliberately does not replace
the phone generator's rich, cross-app story content.
"""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

from . import config, phone_check_gen, schedule_pipeline, storage


# 只有「輸錯密碼」才累積到被抓；翻刪除相冊/開未讀/代發等越界行為不再計入
# 「你是不是動我手機」的觸發（它們仍可留痕，但不會導致被抓）。
RISK_POINTS = {
    "wrong_pin": 3,
    "unread_opened": 0,
    "draft_sent": 0,
    "deleted_album_opened": 0,
}
RISK_LIMIT = 3

# ── 主動試探（B 檔）：玩家讀到重磅 item 後，角色可能給「나」發來一條擦邊訊息 ──
# 鐵律：永遠不承認知道你在看（實錘只走 caught）；一次偷看 session 最多 PROBE_LIMIT 條。
PROBE_LIMIT = 2       # 單次偷看 session 最多主動發幾條
PROBE_PROB = 0.6      # 每次讀到重磅時觸發的機率（間歇性獎勵，非必觸發）
PROBE_MIN_GAP = 25    # 兩條主動訊息之間的最小間隔（秒），避免連環轟炸


def _path(char_id: str) -> Path:
    return config.DATA_DIR / "phone_runtime" / f"{char_id}.json"


def _load(char_id: str) -> dict:
    item = storage.load_json("phone_runtime", char_id, _path(char_id))
    if not isinstance(item, dict):
        item = {}
    return {
        "char_id": char_id,
        "risk": int(item.get("risk") or 0),
        "caught": bool(item.get("caught")),
        "entry_started": int(item.get("entry_started") or 0),
        "chat_turns": int(item.get("chat_turns") or 0),
        "chat_session_id": str(item.get("chat_session_id") or ""),
        "events": list(item.get("events") or [])[-24:],
        "probe_count": int(item.get("probe_count") or 0),
        "probe_last": int(item.get("probe_last") or 0),
        "probed_focus": list(item.get("probed_focus") or [])[-8:],
        "updated": int(item.get("updated") or 0),
    }


def _save(state: dict) -> dict:
    state = dict(state)
    state["updated"] = int(time.time())
    storage.save_json("phone_runtime", state["char_id"], state,
                      _path(state["char_id"]))
    return state


def _txt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("zh") or value.get("ko") or next(iter(value.values()), ""))
    return str(value)


def _latest_schedule(char_id: str) -> tuple[str, dict]:
    """Pick the newest generated schedule day, if the schedule pipeline exists."""
    workspace = schedule_pipeline.load_workspace(char_id)
    weeks = workspace.get("weeks") or {}
    newest: tuple[tuple[int, int], str, dict] | None = None
    for week_no, week in weeks.items():
        try:
            n_week = int(week_no)
        except (TypeError, ValueError):
            n_week = 0
        plans = (week or {}).get("day_plans") or {}
        for day_no, key in enumerate(schedule_pipeline.DAY_KEYS):
            plan = plans.get(key)
            if isinstance(plan, dict) and plan.get("daily_schedule"):
                candidate = ((n_week, day_no), key, plan)
                if newest is None or candidate[0] > newest[0]:
                    newest = candidate
    return (newest[1], newest[2]) if newest else ("", {})


def _schedule_snapshot(char_id: str) -> dict:
    day_key, plan = _latest_schedule(char_id)
    if not plan:
        return {
            "day": "", "summary": "", "notifications": [],
            "app_badges": {}, "phone_state": {"mode": "away", "app": ""},
            "phone_update": {}, "lock_time": "", "date_label": "",
        }
    pu = plan.get("phone_update") if isinstance(plan.get("phone_update"), dict) else {}
    segments = [item for item in (plan.get("daily_schedule") or []) if isinstance(item, dict)]
    selected = segments[-1] if segments else {}

    # ── notifications：優先用日程新增的 phone_update.push；否則退回舊的活動摘要 ──
    notifications: list[dict] = []
    for push in (pu.get("push") or []):
        if isinstance(push, dict) and _txt(push.get("text")):
            notifications.append({
                "icon": push.get("icon") or "🔔",
                "app": _txt(push.get("app")) or "手机",
                "time": _txt(push.get("time")) or "刚刚",
                "text": _txt(push.get("text"))[:88],
                "hot": bool(push.get("hot")),
            })
    if not notifications:
        title = _txt(selected.get("activity_name")) or "日程有更新"
        detail = _txt(selected.get("detail")) or _txt(plan.get("day_summary"))
        notifications.append({
            "icon": "🗓", "app": "今日行程", "time": "刚刚",
            "text": f"{title} · {detail}"[:88],
        })

    # ── app badges（紅點）：聊天新增未讀、動線/推薦有更新 ──
    chat_updates = [u for u in (pu.get("chat_updates") or []) if isinstance(u, dict)]
    talk_unread = 0
    for u in chat_updates:
        try:
            talk_unread += int(u.get("unread") or 0)
        except (TypeError, ValueError):
            pass
    if not chat_updates:
        # 舊行為兼容：mobile_messages 也算一次 talk 更新
        msgs = []
        for segment in segments:
            for m in ((segment.get("mobile_messages") or {}).get("messages") or []):
                if str(m).strip():
                    msgs.append(str(m).strip())
        talk_unread = len(msgs)
    badges = {}
    if talk_unread:
        badges["talk"] = talk_unread
    if pu.get("footprint"):
        badges["footprints"] = 1
    if pu.get("feed_seen"):
        badges["feed"] = 1

    state = str(pu.get("phone_state") or "").strip().lower()
    is_using = state == "using" or (not state and bool(chat_updates))
    cur_app = _txt(pu.get("current_app")) or ("talk" if is_using else "")

    title = _txt(selected.get("activity_name")) or "日程有更新"
    detail = _txt(selected.get("detail")) or _txt(plan.get("day_summary"))
    return {
        "day": day_key,
        "summary": _txt(plan.get("day_summary")),
        "activity": f"{title} · {detail}".strip(" ·"),
        "notifications": notifications,
        "app_badges": badges,
        "lock_time": _txt(pu.get("lock_time")),
        "date_label": _txt(pu.get("date_label")),
        "phone_update": pu,
        "phone_state": {
            "mode": "using" if is_using else "away",
            "app": cur_app,
            "label": "正在玩手机" if is_using else "暂时离开手机",
        },
    }


def apply_schedule_to_dossier(char_id: str) -> dict:
    """把最新日程的 phone_update 併進免費層 dossier 的 talk/footprints/feed，
    並標記新增項目（前端據此加紅點）。缺 dossier 或缺日程時安靜跳過。"""
    from . import phone_check_gen
    dossier = phone_check_gen.find_by_char_id(char_id)
    if not dossier:
        return {"applied": False, "reason": "no dossier"}
    snap = _schedule_snapshot(char_id)
    pu = snap.get("phone_update") or {}
    if not pu:
        return {"applied": False, "reason": "no phone_update"}
    demo_id = dossier.get("demo_id")
    content = dossier.setdefault("content", {})
    apps = content.setdefault("apps", {})
    changed = False

    # talk：把 chat_updates 併進對應 room（依備註名匹配），沒有則新增一條
    talk = apps.get("talk") or {}
    rooms = talk.get("rooms") if isinstance(talk.get("rooms"), list) else []
    for u in (pu.get("chat_updates") or []):
        if not isinstance(u, dict):
            continue
        name = _txt(u.get("room"))
        last = _txt(u.get("last"))
        if not last:
            continue
        room = None
        for r in rooms:
            rn = _txt(r.get("name"))
            if name and (rn == name or (u.get("room") == "나" and r.get("is_user"))):
                room = r
                break
        msg = {"who": "them", "t": last, "time": _txt(u.get("time")), "_new": True}
        if room is None:
            room = {"name": name or "새 대화", "msgs": [], "unread": 0}
            rooms.append(room)
        room.setdefault("msgs", []).append(msg)
        try:
            room["unread"] = int(room.get("unread") or 0) + int(u.get("unread") or 0)
        except (TypeError, ValueError):
            pass
        if _txt(u.get("draft")):
            room["draft"] = _txt(u.get("draft"))
        room["_new"] = True
        changed = True
    if rooms:
        talk["rooms"] = rooms
        apps["talk"] = talk

    # footprints：追加一個 pin/trip
    fp = pu.get("footprint")
    if isinstance(fp, dict) and _txt(fp.get("place")):
        foot = apps.get("footprints") or {}
        trips = foot.get("trips") if isinstance(foot.get("trips"), list) else []
        trips.append({
            "route": _txt(fp.get("place")), "time": _txt(fp.get("time")),
            "s": _txt(fp.get("note")), "hot": bool(fp.get("hot")), "_new": True,
        })
        foot["trips"] = trips
        apps["footprints"] = foot
        changed = True

    # feed：追加一張推薦卡
    seen = _txt(pu.get("feed_seen"))
    if seen:
        feed = apps.get("feed") or {}
        cards = feed.get("cards") if isinstance(feed.get("cards"), list) else []
        cards.insert(0, {"emo": "📺", "src": "오늘", "t": seen,
                         "why": _txt(snap.get("activity")), "hot": True, "_new": True})
        feed["cards"] = cards
        apps["feed"] = feed
        changed = True

    if changed and demo_id:
        phone_check_gen._write(demo_id, dossier)
    return {"applied": changed, "demo_id": demo_id, "badges": snap.get("app_badges")}


def runtime(char_id: str) -> dict:
    dossier = phone_check_gen.find_by_char_id(char_id)
    state = _load(char_id)
    snapshot = _schedule_snapshot(char_id)
    return {
        "available": bool(dossier),
        "demo_id": (dossier or {}).get("demo_id"),
        "char_id": char_id,
        "new_count": len(snapshot["notifications"]),
        "schedule": snapshot,
        "paid_apps": list((dossier or {}).get("paid_apps", {}).keys()),
        "risk": state["risk"],
        "risk_limit": RISK_LIMIT,
        "caught": state["caught"],
        "chat_turns": state["chat_turns"],
    }


def enter(char_id: str, session_id: str = "") -> dict:
    state = _load(char_id)
    state.update({
        "risk": 0, "caught": False, "entry_started": int(time.time()),
        "chat_turns": 0, "chat_session_id": session_id or state.get("chat_session_id", ""),
        "events": [], "probe_count": 0, "probe_last": 0, "probed_focus": [],
    })
    _save(state)
    return runtime(char_id)


def _password_context(char_id: str, state: dict) -> str:
    dossier = phone_check_gen.find_by_char_id(char_id) or {}
    meta = (dossier.get("content") or {}).get("meta") or {}
    pin = str(meta.get("pass") or "")
    origin = _txt(meta.get("pass_origin"))
    hint = _txt(meta.get("hint"))
    turns = state["chat_turns"]
    if not pin:
        return "对方刚尝试查看你的手机。没有可用的手机档案，不要编造密码。"
    gate = (
        "还未完成两轮正常聊天；即使被问到也只给模糊、生活化的方向，绝不说出四位数字。"
        if turns < 2 else
        "已经完成两轮正常聊天；若对方自然问起，可以给出一条能推到答案的间接线索，仍不要直接读出四位数字。"
    )
    return (
        "【手机窥探事件，仅限角色私下记忆】对方刚点开了你的手机入口。"
        f"这是一段虚构手机档案；密码是 {pin}，它的由来是：{origin or '未写明'}。"
        f"锁屏线索是：{hint or '未写明'}。当前已完成 {turns} 轮聊天。{gate}"
    )


def _ambiguity_guard(char_id: str) -> str:
    """把手機裡那條『看著有問題、但解釋得通』的未解暧昧，變成角色在聊天裡
    守口如瓶的點：玩家試探時，角色也保持不實錘，讓暧昧從手機 UI 活到聊天裡。"""
    dossier = phone_check_gen.find_by_char_id(char_id) or {}
    content = dossier.get("content") or {}
    meta = content.get("meta") or {}
    secret = _txt(meta.get("secret"))
    if not secret:
        return ""
    return (
        "【你心裡有一件沒說出口的事，這是你的潛台詞，不是要交代的劇情】"
        f"{secret[:300]} —— 若對方在聊天裡旁敲側擊、試探到這件事，你可以有反應"
        "（迴避、岔開、欲言又止、半真半假），但【絕不主動全盤托出、也不徹底否認】。"
        "保持這份懸而未決：往好想解釋得通，往壞想也說得過去。這份曖昧是你們關係的燃料。"
    )


def chat_context(char_id: str, session_id: str | None = None) -> str:
    state = _load(char_id)
    if not state["entry_started"] or state["caught"]:
        return ""
    if session_id and state["chat_session_id"] and state["chat_session_id"] != session_id:
        return ""
    parts = [_password_context(char_id, state)]
    guard = _ambiguity_guard(char_id)
    if guard:
        parts.append(guard)
    return "\n".join(p for p in parts if p)


def note_chat_turn(char_id: str, session_id: str) -> None:
    state = _load(char_id)
    if not state["entry_started"] or state["caught"]:
        return
    if state["chat_session_id"] and state["chat_session_id"] != session_id:
        return
    state["chat_session_id"] = session_id
    state["chat_turns"] += 1
    _save(state)


def record_event(char_id: str, event: str, detail: str = "") -> dict:
    # 只有「輸錯密碼」才會讓角色主動發消息。peek_hot（讀到重磅線索）不再觸發主動試探，
    # 僅作為無副作用的心理焦點信號返回，避免非 wrong_pin 情況下角色憑空發訊息。
    if event == "peek_hot":
        st = _load(char_id)
        return {
            "risk": st["risk"], "risk_limit": RISK_LIMIT,
            "caught": st["caught"], "caught_now": False,
            "message": "", "probe": {"sent": False, "disabled": True},
        }
    state = _load(char_id)
    points = RISK_POINTS.get(event, 0)
    if event == "clear_trace":
        state["risk"] = max(0, state["risk"] - 1)
    else:
        state["risk"] += points
    state["events"].append({"event": event, "detail": detail[:100], "at": int(time.time())})
    caught_now = not state["caught"] and state["risk"] >= RISK_LIMIT
    message = ""
    if caught_now:
        state["caught"] = True
        message = "你是不是动我手机了？"
        # Keep the proactive message inside the real chat history, so opening
        # chat after a risk event has a durable payoff rather than a toast.
        from . import chat
        chat.append_phone_event(char_id, message, detail or event)
    _save(state)
    return {
        "risk": state["risk"], "risk_limit": RISK_LIMIT,
        "caught": state["caught"], "caught_now": caught_now,
        "message": message,
    }


def _resolve_focus(dossier: dict, focus: str) -> str:
    """把前端上報的 focus（多半是 clue id，如 'h1'，或一段 item 文字）
    還原成『玩家此刻讀到了什麼』的一句話，餵給主動試探的 LLM。"""
    focus = (focus or "").strip()
    if not focus:
        return ""
    content = dossier.get("content") or {}
    for clue in (content.get("clues") or []):
        if isinstance(clue, dict) and str(clue.get("id")) == focus:
            return _txt(clue.get("text"))
    # 不是已知 clue id：當作 item 文字直接用（截斷）
    return focus[:120]


def _probe_messages(char_name: str, persona_brief: str, secret_bible: str,
                    chat_lines: str, focus_desc: str) -> list[dict]:
    sys = (
        f"你是韓國戀愛推理遊戲裡的角色「{char_name}」，正在通訊軟體上跟對方（玩家）聊天。"
        "此刻對方正在偷看你的手機——但【你完全不知道這件事】。"
        "系統會告訴你對方剛看到你手機裡的哪個痕跡，你要『恰好在這個時間點』給對方發一條訊息。"
        "\n# 鐵律（違反即毀掉體驗）\n"
        "1. 你永遠不知道、也絕不能暗示對方在看你手機。不許出現『你在看我手機嗎』『別亂翻』之類的話。\n"
        "2. 訊息內容要和對方剛看到的痕跡【語義相關，但錯開一層】——像是巧合般擦邊，"
        "讓對方後背發涼『他怎麼突然說這個』，但你這句話本身完全解釋得通、是日常會發的。\n"
        "3. 絕不實錘、不點破中心秘密。曖昧要懸而未決，你只是若無其事地生活、順口說一句。\n"
        "4. 就是一條網聊訊息：口語、短、可碎片化，符合你的人設語氣。不寫旁白動作，不用引號。\n"
        "# 輸出\n只輸出一個 JSON object：{\"text\": \"你要發的那條訊息\"}。不要解釋、不要 markdown。"
    )
    blocks = [
        f"# 你的人設\n{persona_brief}",
        f"# 你的中心秘密（只有你自己知道，是這條訊息的潛台詞來源，絕不可說破）\n{secret_bible or '（暫無，保守發揮）'}",
    ]
    if chat_lines:
        blocks.append("# 你和對方最近的聊天（延續語氣與稱呼，只能用這裡出現過的梗指向對方）\n" + chat_lines)
    else:
        blocks.append("# 你和對方最近的聊天\n（還在初期，只發最輕的擦邊，不要編造親密史）")
    blocks.append(
        "# 對方此刻剛在你手機裡看到的痕跡（你不知道這件事，但請讓你的訊息『恰好』擦到它的邊）\n"
        + focus_desc)
    return [{"role": "system", "content": sys},
            {"role": "user", "content": "\n\n".join(blocks)}]


def proactive_probe(char_id: str, focus: str = "") -> dict:
    """玩家讀到重磅 item 時可能觸發：角色『恰好』給玩家發來一條擦邊、不實錘的訊息。

    受單次偷看 session 的條數上限、間隔與機率節流。生成的訊息會被寫進真實的
    normal 聊天會話（durable），讓小手機 talk 的紅點和聊天界面看到的是同一條。
    任何一步失敗都安靜跳過（絕不打斷偷看流程）。
    """
    state = _load(char_id)
    now = int(time.time())
    if not state["entry_started"] or state["caught"]:
        return {"sent": False, "reason": "no active peek"}
    if state["probe_count"] >= PROBE_LIMIT:
        return {"sent": False, "reason": "cap reached"}
    if now - state["probe_last"] < PROBE_MIN_GAP:
        return {"sent": False, "reason": "too soon"}
    if random.random() > PROBE_PROB:
        return {"sent": False, "reason": "no roll"}

    dossier = phone_check_gen.find_by_char_id(char_id)
    if not dossier:
        return {"sent": False, "reason": "no dossier"}
    focus_desc = _resolve_focus(dossier, focus)
    if not focus_desc or focus_desc in state["probed_focus"]:
        return {"sent": False, "reason": "no/repeat focus"}

    try:
        from . import api_client, chat, pipeline
        record = pipeline.load_character(char_id)
        persona = record.get("persona") or {}
        char_name = _txt(persona.get("name")) or char_id
        secret_bible = phone_check_gen._secret_bible(dossier)
        chat_lines = phone_check_gen._chat_lines_text(char_id, char_name)
        messages = _probe_messages(char_name, phone_check_gen._persona_brief(persona),
                                   secret_bible, chat_lines, focus_desc)
        raw = api_client.chat(messages, model=config.CHAT_MODEL,
                              temperature=0.95, max_tokens=400)
        data = api_client.parse_json_text(raw)
        text = ""
        if isinstance(data, dict):
            text = _txt(data.get("text"))
        if not text:
            return {"sent": False, "reason": "empty gen"}
        chat.append_proactive_message(char_id, text)
    except Exception as e:  # noqa: BLE001 主動試探絕不打斷偷看
        return {"sent": False, "reason": f"gen failed: {e}"}

    state["probe_count"] += 1
    state["probe_last"] = now
    state["probed_focus"].append(focus_desc)
    _save(state)
    return {"sent": True, "text": text, "sender": char_name,
            "probe_count": state["probe_count"], "probe_limit": PROBE_LIMIT}
