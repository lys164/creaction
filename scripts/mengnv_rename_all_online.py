# -*- coding: utf-8 -*-
"""全量把线上 source=mengnv 角色的名字改成【音近/形近但不同】的化名（花名）。

做法：
  1. 拉取线上所有 source=mengnv 角色，逐个 GET 详情并落盘备份（可回滚）。
  2. 按 group_id 聚合（同一虚拟人物的多语言版本在同组）。
  3. 每组调用一次 LLM，产出组内一致的化名（同一个人跨语言保持同一新身份，
     保留原语言/书写系统，听起来像该语言里真实存在的名字，但和原名明显不同）。
  4. 对每个成员的 persona JSON 文本，按组内所有 (旧名->新名) 做整串替换
     （长串优先，避免子串先被替换）；替换后校验 JSON 合法、名字确有变化。
  5. --apply 才通过 PUT /api/persona 写回线上；默认 dry-run 只打印计划。

安全：写回前每个角色已落盘备份到 data/_mengnv_rename_backup_<ts>/，
可用备份目录里的 persona 原样 PUT 回滚。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _load_dotenv() -> None:
    """把项目根 .env 读进 os.environ（不覆盖已存在的）。手写解析，避免 shell
    source 破坏 JSON 值。仅支持 KEY=VALUE 行，忽略注释/空行。"""
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        os.environ.setdefault(k, v)


_load_dotenv()
from app import api_client  # noqa: E402

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
ROOT = os.path.join(os.path.dirname(__file__), "..", "data")

SYS = (
    "你是命名专家。任务：给一组「同一个虚拟人物」的多语言名字，各自改成一个"
    "【听起来/看起来相近但明显不同】的化名，避免与任何真实明星/艺人重名。"
    "严格要求：\n"
    "1) 每个名字保留其原本的语言与书写系统（中文名→中文名，한글→한글，"
    "かな/漢字→かな/漢字，English→English）。\n"
    "2) 同一个人的各语言化名要彼此对应（是同一个新身份的不同语言写法）。\n"
    "3) 新名要像该语言里真实存在的自然人名，不要奇怪生造。\n"
    "4) 和原名要有可感知的相似度（音近或形近），但不能等同于原名，也不能等同于"
    "任何知名艺人的本名或艺名。\n"
    "5) 只输出 JSON，键为传入的 char_id，值为该角色的新名字符串。不要解释。"
)


def _get(url: str, **kw) -> requests.Response:
    # (connect, read) 元组超时：避免个别挂死 socket 永久阻塞 worker 线程。
    kw.setdefault("timeout", (10, 30))
    last = None
    for i in range(5):
        try:
            r = requests.get(url, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(min(3 * (i + 1), 15))
    raise RuntimeError(f"GET 失败 {url}: {last}")


def _new_names_for_group(members: list[dict]) -> dict[str, str]:
    """members: [{char_id, lang, name}] -> {char_id: new_name}."""
    payload = [{"char_id": m["char_id"], "lang": m.get("lang"),
                "name": m.get("name")} for m in members]
    user = (
        "下面是同一个虚拟人物的多语言版本。请给每个 char_id 生成一个化名。\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + '\n\n只输出形如 {"char_id": "新名", ...} 的 JSON。'
    )
    messages = [
        {"role": "system", "content": SYS},
        {"role": "user", "content": user},
    ]
    out = api_client.chat_json(messages, temperature=0.7)
    if not isinstance(out, dict):
        raise ValueError(f"LLM 返回非 dict: {out!r}")
    return {str(k): str(v) for k, v in out.items()}


def _apply_rules(persona_text: str, rules: list[tuple[str, str]]) -> tuple[str, list]:
    """按 (旧,新) 顺序（已按旧串长度降序）整串替换，返回 (新文本, [(旧,新,命中数)])。"""
    new = persona_text
    report = []
    for old, nw in rules:
        cnt = new.count(old)
        report.append((old, nw, cnt))
        if cnt:
            new = new.replace(old, nw)
    return new, report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回线上（否则只 dry-run）")
    ap.add_argument("--limit-groups", type=int, default=0,
                    help="只处理前 N 组（调试用，0=全部）")
    ap.add_argument("--reuse-backup", type=str, default="",
                    help="复用已有备份目录（跳过已下载的角色，仅补齐缺失的）")
    ap.add_argument("--plan-workers", type=int, default=6,
                    help="并发规划的线程数（LLM 出化名阶段）")
    args = ap.parse_args()

    if args.reuse_backup:
        bak = args.reuse_backup if os.path.isabs(args.reuse_backup) \
            else os.path.join(ROOT, args.reuse_backup)
        os.makedirs(bak, exist_ok=True)
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        bak = os.path.join(ROOT, f"_mengnv_rename_backup_{ts}")
        os.makedirs(bak, exist_ok=True)

    chars = _get(f"{BASE}/api/characters").json()
    mv = [c for c in chars if c.get("source") == "mengnv"]
    print(f"线上 mengnv 角色: {len(mv)}；备份目录: {bak}", flush=True)

    # 拉详情 + 备份（已存在则直接读盘，支持断点续跑/复用备份）
    details: dict[str, dict] = {}

    def _one(c):
        cid = c["char_id"]
        path = os.path.join(bak, f"{cid}.json")
        if os.path.exists(path):
            try:
                return cid, json.load(open(path, encoding="utf-8"))
            except Exception:  # noqa: BLE001 坏文件则重新拉
                pass
        d = _get(f"{BASE}/api/character/{cid}").json()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return cid, d

    with ThreadPoolExecutor(max_workers=6) as ex:
        for cid, d in ex.map(_one, mv):
            details[cid] = d
    print(f"已备份 {len(details)} 个角色详情 -> {bak}", flush=True)

    # 按 group 聚合
    groups: dict[str, list[dict]] = defaultdict(list)
    for cid, d in details.items():
        gid = d.get("group_id") or f"_nogroup_{cid}"
        p = d.get("persona") or {}
        groups[gid].append({"char_id": cid, "lang": d.get("lang"),
                            "name": p.get("name")})

    gids = list(groups.keys())
    if args.limit_groups:
        gids = gids[:args.limit_groups]
    print(f"共 {len(gids)} 组待处理（并发规划 {args.plan_workers}）\n", flush=True)

    plan = []          # (cid, new_persona_dict, name_before, name_after, report)
    skipped = []       # (cid, reason)

    def _plan_group(job):
        """规划一组：调 LLM 出化名并对每个成员算替换后的 persona。
        返回 (gi, gid, [plan_rows], [skip_rows], [log_lines])。纯计算，不写线上。"""
        gi, gid = job
        members = groups[gid]
        rows, skips, logs = [], [], [f"[组 {gi}/{len(gids)}] {gid}"]
        try:
            mapping = _new_names_for_group(members)
        except Exception as e:  # noqa: BLE001
            logs.append(f"    LLM 失败，跳过整组: {e}")
            for m in members:
                skips.append((m["char_id"], f"llm_error: {e}"))
            return gi, gid, rows, skips, logs

        pair_map: dict[str, str] = {}
        for m in members:
            old = (m.get("name") or "").strip()
            nw = (mapping.get(m["char_id"]) or "").strip()
            if old and nw and old != nw:
                pair_map[old] = nw
        rules = sorted(pair_map.items(), key=lambda kv: -len(kv[0]))

        for m in members:
            cid = m["char_id"]
            d = details[cid]
            old_name = (m.get("name") or "").strip()
            new_name = (mapping.get(cid) or "").strip()
            if not new_name or new_name == old_name:
                skips.append((cid, "no_new_name"))
                logs.append(f"    - {cid} {old_name!r} 无有效新名，跳过")
                continue
            persona_text = json.dumps(d.get("persona", {}), ensure_ascii=False)
            new_text, report = _apply_rules(persona_text, rules)
            try:
                new_persona = json.loads(new_text)
            except Exception as e:  # noqa: BLE001
                skips.append((cid, f"json_invalid: {e}"))
                logs.append(f"    ✗ {cid} 替换后 JSON 非法，跳过: {e}")
                continue
            if new_persona.get("name") == old_name:
                skips.append((cid, "name_field_unchanged"))
                logs.append(f"    ✗ {cid} name 字段未变（原名可能是他人子串），跳过")
                continue
            hits = ", ".join(f"{o!r}->{n!r}x{c}" for o, n, c in report if c)
            logs.append(f"    ✓ {cid} [{m.get('lang')}] {old_name!r} -> "
                        f"{new_persona.get('name')!r}  ({hits})")
            rows.append((cid, new_persona, old_name, new_persona.get("name"), report))
        return gi, gid, rows, skips, logs

    jobs = list(enumerate(gids, 1))
    with ThreadPoolExecutor(max_workers=max(1, args.plan_workers)) as ex:
        for _gi, _gid, rows, skips, logs in ex.map(_plan_group, jobs):
            for ln in logs:
                print(ln, flush=True)
            plan.extend(rows)
            skipped.extend(skips)

    print(f"\n计划改名 {len(plan)} 个，跳过 {len(skipped)} 个。", flush=True)
    if skipped:
        print("跳过明细（前 20）:")
        for cid, why in skipped[:20]:
            print(f"    {cid}: {why}")

    # 落盘计划，便于复核/复用
    plan_path = os.path.join(bak, "_rename_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump([{"char_id": c, "name_before": nb, "name_after": na}
                   for c, _p, nb, na, _r in plan], f,
                  ensure_ascii=False, indent=1)
    print(f"计划已写入 {plan_path}", flush=True)

    if not args.apply:
        print("\n（dry-run，未写回线上。加 --apply 才会写。）")
        return 0

    ok = err = 0
    for cid, persona, _nb, _na, _r in plan:
        try:
            r = requests.put(f"{BASE}/api/persona",
                             json={"char_id": cid, "persona": persona},
                             timeout=(10, 60))
            r.raise_for_status()
            ok += 1
            print(f"  ✓ 写回 {cid}", flush=True)
        except Exception as e:  # noqa: BLE001
            err += 1
            print(f"  ✗ 写回失败 {cid}: {e}", flush=True)
        time.sleep(0.2)
    print(f"\n完成：成功 {ok}，失败 {err}。备份目录: {bak}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
