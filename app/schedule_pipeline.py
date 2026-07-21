"""Four-stage schedule production pipeline.

This module is intentionally separate from ``daily.py``.  ``daily.py`` is the
older one-click *daily run* demo; this module implements the hand-book demo's
actual production order: month -> week -> selected days -> notebook pages.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from . import api_client, config, pipeline, storage


DAY_KEYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_LABELS = dict(zip(DAY_KEYS, ("周一", "周二", "周三", "周四", "周五", "周六", "周日")))


def _path(char_id: str) -> Path:
    return config.DATA_DIR / "schedule_pipeline" / f"{char_id}.json"


def empty_workspace(char_id: str) -> dict:
    return {
        "char_id": char_id,
        "env": {},
        "monthly_plan": None,
        "current_week_no": 1,
        "weeks": {},
        "updated_at": 0,
    }


def load_workspace(char_id: str) -> dict:
    saved = storage.load_json("schedule_pipeline", char_id, _path(char_id))
    if not isinstance(saved, dict):
        return empty_workspace(char_id)
    base = empty_workspace(char_id)
    base.update(saved)
    base["char_id"] = char_id
    base["weeks"] = base["weeks"] if isinstance(base.get("weeks"), dict) else {}
    return base


def save_workspace(workspace: dict) -> dict:
    workspace = copy.deepcopy(workspace)
    workspace["updated_at"] = int(time.time())
    storage.save_json("schedule_pipeline", workspace["char_id"], workspace,
                      _path(workspace["char_id"]))
    return workspace


def _txt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("zh") or value.get("ko") or next(iter(value.values()), ""))
    if isinstance(value, list):
        return "、".join(_txt(item) for item in value if _txt(item))
    return str(value)


def _persona_block(persona: dict) -> str:
    fields = {
        "名称": persona.get("name"), "性别": persona.get("gender"),
        "物种": persona.get("species"), "简介": persona.get("profile"),
        "性格": persona.get("personality"), "特征": persona.get("key_features"),
        "背景": persona.get("backstory"), "技能": persona.get("skills"),
        "人生目标": persona.get("life_goal"), "遗憾": persona.get("life_regret"),
        "社交模式": persona.get("social_mode"), "喜欢": persona.get("likes"),
        "恐惧": persona.get("fears"), "核心矛盾": persona.get("contradiction"),
        "愿望清单": persona.get("plan"),
    }
    return "\n".join(f"- {name}：{_txt(value)}" for name, value in fields.items() if _txt(value))


def _parse_json(raw: str, stage: str) -> dict:
    try:
        parsed = api_client.parse_json_text(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{stage}没有返回有效 JSON：{raw[:500]}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{stage}没有返回 JSON object")
    return parsed


MONTH_SYSTEM = """你是角色生活规划师。为虚拟角色规划其独自生活，用户绝不在场。
你必须把抽象目标改成可观察、可执行的事件；不要写空泛情绪或文学比喻。
只输出严格 JSON，不要 markdown。"""

MONTH_SCHEMA = """{
 "month_label":"月份标签", "month_theme":"本月会发生什么的具体一句话",
 "goal_threads":[{"id":"stable_id","name":"可验证完成态的目标","type":"main|side|maintain|shelved","domain":"事业/健康/关系/自我成长/生活项目","progress_pct":"月初进度","expected_months":1,"note":"当前具体状态"}],
 "thread_arc":[{"id":"stable_id","target_pct":"月末进度/状态","arc":"具体推进或代价"}],
 "month_milestones":["第几周的具体里程碑"], "month_events":["第几周的季节/社会事件"],
 "exploration_pool":[{"name":"可能冒头的新兴趣","domain":"领域","reason":"为什么适合","suggested_week":"1-4"}],
 "weeks":[{"week_no":1,"focus_thread":["目标名"],"pace":"build|push|climax|recover","week_brief":"本周做什么","expected_milestone":"可判断完成的里程碑"}],
 "first_week_plan":{"weekly_goal":"具体目标","sub_goals":["子目标"],"milestone":"里程碑","daily_themes":{"Mon":{"theme":"10字内主题","type":"workday|restday|special","focus":"这天的方向"},"Tue":{},"Wed":{},"Thu":{},"Fri":{},"Sat":{},"Sun":{}},"exploration_suggestions":{"new_interest":[{"name":"兴趣","reason":"理由"}]}}
}"""

WEEK_SYSTEM = """你是角色周规划师。把月规划中的指定一周展开成可被日程执行的安排。
角色独自生活，用户绝不在场。只输出严格 JSON。"""

WEEK_SCHEMA = """{
 "weekly_goal":"本周可执行目标", "sub_goals":["2-4个具体子目标"], "milestone":"本周可判断里程碑",
 "daily_themes":{"Mon":{"theme":"10字内主题","type":"workday|restday|special","focus":"当天方向"},"Tue":{},"Wed":{},"Thu":{},"Fri":{},"Sat":{},"Sun":{}},
 "exploration_suggestions":{"new_interest":[{"name":"新兴趣","reason":"为何本周适合"}]}
}"""

DAY_SYSTEM = """你是角色日程导演。根据周规划生成角色独自度过的一天。
用户不在场：用户只能以真实聊天留下的情绪回声出现，不能被排进活动。
日程必须从早到晚 4-7 段，包含目标推进和生活毛边；每条都要可拍摄、可验证。

除了日程，你还要产出 phone_update：这一天在角色手机上留下的、可被偷看的免费层痕迹
（会被填进「查手机」demo 的锁屏推送、聊天列表、动线、推荐流，并触发红点提示）。
phone_update 必须与当天日程一致（同一件事在日程和手机上要对得上），语气用繁体中文。
只输出严格 JSON。"""

DAY_SCHEMA = """{
 "opening_state":{"wake_time":"","sleep":"","body":"","mood_carryover":"","energy":""},
 "state_update":{"daily_theme":"","advanced_threads":[""],"thread_progress":[{"id":"","progress_pct":""}],"closing_state":""},
 "daily_schedule":[{"start_time":"09:00","end_time":"10:00","activity_name":"","location":"","status":"","detail":"","emotion":"","echo":"","mobile_messages":{"intent":"","is_push":false,"messages":[""]}}],
 "todo_tracking":{"completed_todo":[""],"todo":[""]},
 "moments":[{"post_time":"","post_type":"life|mood|peacock","format":"text","content":""}],
 "third_party_posts":[{"author":"","post_time":"","format":"text","content":""}],
 "phone_update":{
   "lock_time":"HH:MM(锁屏时间，取当天最后一段活动的时刻)",
   "date_label":"锁屏日期标签(如 12월 6일 금요일)",
   "phone_state":"using|away(此刻在玩手机还是暂时离开)",
   "current_app":"若 using，此刻停在哪个界面(talk/feed/footprints 之一) 否则空",
   "push":[{"icon":"이모지","app":"来源App(톡/배민/지도 等)","time":"刚刚/HH:MM","text":"锁屏推送一行(引导玩家想偷看)","hot":false}],
   "chat_updates":[{"room":"聊天室对象备注名(与免费层 talk 的 room 对得上，'나'=玩家)","last":"这一天该聊天室的最新一条消息","time":"HH:MM","unread":"新增未读数(整数)","draft":"若这条是没发出去的草稿则填内容 否则空"}],
   "footprint":{"place":"今天去过的一个地点","time":"HH:MM","note":"一行说明(可疑或日常)","hot":false},
   "feed_seen":"今天算法推给他/他自己看的一条内容(暴露心事) 或空"
 },
 "day_summary":"当天的事实总结"
}"""


def _request(messages: list[dict], stage: str, max_tokens: int = 10000) -> dict:
    raw = api_client.chat(messages, model=config.LLM_MODEL, temperature=0.85,
                          max_tokens=max_tokens)
    return _parse_json(raw, stage)


def _week_info(monthly: dict, week_no: int) -> dict:
    for item in monthly.get("weeks") or []:
        if isinstance(item, dict) and int(item.get("week_no") or 0) == week_no:
            return item
    return {"week_no": week_no}


def generate_month(char_id: str, env: dict, continue_month: bool = False) -> dict:
    record = pipeline.load_character(char_id)
    persona = record.get("persona") or {}
    workspace = load_workspace(char_id)
    previous = workspace.get("monthly_plan") if continue_month else None
    continuation = ("\n# 上月结果（承接但不重演）\n" + json.dumps(previous, ensure_ascii=False)
                    if previous else "\n# 首次规划：请按人设建立 2-5 条目标线。")
    user = f"""# 角色设定
{_persona_block(persona)}
# 环境
- 季节：{env.get('season') or '未设定'}
- 城市：{env.get('city') or '未设定'}
- 本月起始日期（周一）：{env.get('month_start_date') or '未设定'}
- 天气倾向：{env.get('weather') or '未设定'}
- 最近对话／经历（仅影响角色自己的心境）：{env.get('dialogue') or '无'}
{continuation}

{MONTH_SCHEMA}"""
    monthly = _request([{"role": "system", "content": MONTH_SYSTEM},
                        {"role": "user", "content": user}], "月规划", 12000)
    first = monthly.pop("first_week_plan", None)
    if not isinstance(first, dict):
        first = {}
    workspace = empty_workspace(char_id)
    workspace["env"] = env
    workspace["monthly_plan"] = monthly
    workspace["current_week_no"] = 1
    workspace["weeks"] = {"1": {"weekly_plan": first, "day_plans": {}, "journals": {}}}
    return save_workspace(workspace)


def generate_week(char_id: str, week_no: int) -> dict:
    workspace = load_workspace(char_id)
    monthly = workspace.get("monthly_plan")
    if not isinstance(monthly, dict):
        raise ValueError("请先生成月规划")
    record = pipeline.load_character(char_id)
    persona = record.get("persona") or {}
    summary = json.dumps(monthly, ensure_ascii=False)
    prior = workspace.get("weeks", {}).get(str(week_no - 1), {}).get("settlement")
    user = f"""# 角色设定
{_persona_block(persona)}
# 本月规划
{summary}
# 要展开的第 {week_no} 周
{json.dumps(_week_info(monthly, week_no), ensure_ascii=False)}
# 上周结算
{json.dumps(prior, ensure_ascii=False) if prior else '首次周或尚无结算'}
# 环境
{json.dumps(workspace.get('env') or {}, ensure_ascii=False)}
{WEEK_SCHEMA}"""
    week_plan = _request([{"role": "system", "content": WEEK_SYSTEM},
                          {"role": "user", "content": user}], f"第 {week_no} 周规划")
    week = workspace.setdefault("weeks", {}).setdefault(str(week_no), {})
    week["weekly_plan"] = week_plan
    week.setdefault("day_plans", {})
    week.setdefault("journals", {})
    workspace["current_week_no"] = week_no
    return save_workspace(workspace)


def _previous_day_summary(day_plans: dict, day: str) -> str:
    index = DAY_KEYS.index(day)
    for key in reversed(DAY_KEYS[:index]):
        plan = day_plans.get(key)
        if isinstance(plan, dict) and plan.get("day_summary"):
            return _txt(plan.get("day_summary"))
    return "无（本周第一天）"


def _settlement(day_plans: dict, weekly_plan: dict) -> dict:
    complete = []
    for key in DAY_KEYS:
        summary = _txt((day_plans.get(key) or {}).get("day_summary"))
        if summary:
            complete.append(f"{DAY_LABELS[key]}：{summary}")
    return {
        "completed": complete[-4:],
        "progress_changes": [],
        "activated_side": "",
        "carry_to_next": list(weekly_plan.get("sub_goals") or [])[-2:],
        "avoid_repeat": [],
    }


def generate_days(char_id: str, week_no: int, days: list[str]) -> dict:
    wanted = [day for day in DAY_KEYS if day in days]
    if not wanted:
        raise ValueError("至少选择一天日程")
    workspace = load_workspace(char_id)
    week = workspace.get("weeks", {}).get(str(week_no)) or {}
    weekly = week.get("weekly_plan")
    if not isinstance(weekly, dict):
        raise ValueError("请先生成本周规划")
    record = pipeline.load_character(char_id)
    persona = record.get("persona") or {}
    day_plans = week.setdefault("day_plans", {})
    env = workspace.get("env") or {}
    month = workspace.get("monthly_plan") or {}
    for day in wanted:
        theme = (weekly.get("daily_themes") or {}).get(day) or {}
        user = f"""# 角色设定
{_persona_block(persona)}
# 环境
{json.dumps(env, ensure_ascii=False)}
# 月目标线
{json.dumps(month.get('goal_threads') or [], ensure_ascii=False)}
# 本周规划
{json.dumps(weekly, ensure_ascii=False)}
# 今天
- 星期：{DAY_LABELS[day]}（{day}）
- 今日主题：{json.dumps(theme, ensure_ascii=False)}
- 昨日总结：{_previous_day_summary(day_plans, day)}
{DAY_SCHEMA}"""
        day_plans[day] = _request([{"role": "system", "content": DAY_SYSTEM},
                                   {"role": "user", "content": user}],
                                  f"{DAY_LABELS[day]}日程", 9000)
    if all(day_plans.get(key) for key in DAY_KEYS):
        week["settlement"] = _settlement(day_plans, weekly)
    week.setdefault("journals", {})
    workspace["current_week_no"] = week_no
    return save_workspace(workspace)
