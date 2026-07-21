# -*- coding: utf-8 -*-
"""全量把線上 source=mengnv 角色的名字改成【音近/形近但不同】的化名（花名）。

做法：
  1. 拉取線上所有 source=mengnv 角色，逐個 GET 詳情並落盤備份（可回滾）。
  2. 按 group_id 聚合（同一虛擬人物的多語言版本在同組）。
  3. 每組呼叫一次 LLM，產出組內一致的化名（同一個人跨語言保持同一新身份，
     保留原語言/書寫系統，聽起來像該語言裡真實存在的名字，但和原名明顯不同）。
  4. 對每個成員的 persona JSON 文字，按組內所有 (舊名->新名) 做整串替換
     （長串優先，避免子串先被替換）；替換後校驗 JSON 合法、名字確有變化。
  5. --apply 才透過 PUT /api/persona 寫回線上；預設 dry-run 只列印計劃。

安全：寫回前每個角色已落盤備份到 data/_mengnv_rename_backup_<ts>/，
可用備份目錄裡的 persona 原樣 PUT 回滾。
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
    """把專案根 .env 讀進 os.environ（不覆蓋已存在的）。手寫解析，避免 shell
    source 破壞 JSON 值。僅支援 KEY=VALUE 行，忽略註釋/空行。"""
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
    "你是命名專家。任務：給一組「同一個虛擬人物」的多語言名字，各自改成一個"
    "【聽起來/看起來相近但明顯不同】的化名，避免與任何真實明星/藝人重名。"
    "嚴格要求：\n"
    "1) 每個名字保留其原本的語言與書寫系統（中文名→中文名，한글→한글，"
    "かな/漢字→かな/漢字，English→English）。\n"
    "2) 同一個人的各語言化名要彼此對應（是同一個新身份的不同語言寫法）。\n"
    "3) 新名要像該語言裡真實存在的自然人名，不要奇怪生造。\n"
    "4) 和原名要有可感知的相似度（音近或形近），但不能等同於原名，也不能等同於"
    "任何知名藝人的本名或藝名。\n"
    "5) 只輸出 JSON，鍵為傳入的 char_id，值為該角色的新名字串。不要解釋。"
)


def _get(url: str, **kw) -> requests.Response:
    # (connect, read) 元組超時：避免個別掛死 socket 永久阻塞 worker 執行緒。
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
    raise RuntimeError(f"GET 失敗 {url}: {last}")


def _new_names_for_group(members: list[dict]) -> dict[str, str]:
    """members: [{char_id, lang, name}] -> {char_id: new_name}."""
    payload = [{"char_id": m["char_id"], "lang": m.get("lang"),
                "name": m.get("name")} for m in members]
    user = (
        "下面是同一個虛擬人物的多語言版本。請給每個 char_id 生成一個化名。\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + '\n\n只輸出形如 {"char_id": "新名", ...} 的 JSON。'
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
    """按 (舊,新) 順序（已按舊串長度降序）整串替換，返回 (新文字, [(舊,新,命中數)])。"""
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
    ap.add_argument("--apply", action="store_true", help="寫回線上（否則只 dry-run）")
    ap.add_argument("--limit-groups", type=int, default=0,
                    help="只處理前 N 組（除錯用，0=全部）")
    ap.add_argument("--reuse-backup", type=str, default="",
                    help="複用已有備份目錄（跳過已下載的角色，僅補齊缺失的）")
    ap.add_argument("--plan-workers", type=int, default=6,
                    help="併發規劃的執行緒數（LLM 出化名階段）")
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
    print(f"線上 mengnv 角色: {len(mv)}；備份目錄: {bak}", flush=True)

    # 拉詳情 + 備份（已存在則直接讀盤，支援斷點續跑/複用備份）
    details: dict[str, dict] = {}

    def _one(c):
        cid = c["char_id"]
        path = os.path.join(bak, f"{cid}.json")
        if os.path.exists(path):
            try:
                return cid, json.load(open(path, encoding="utf-8"))
            except Exception:  # noqa: BLE001 壞檔案則重新拉
                pass
        d = _get(f"{BASE}/api/character/{cid}").json()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return cid, d

    with ThreadPoolExecutor(max_workers=6) as ex:
        for cid, d in ex.map(_one, mv):
            details[cid] = d
    print(f"已備份 {len(details)} 個角色詳情 -> {bak}", flush=True)

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
    print(f"共 {len(gids)} 組待處理（併發規劃 {args.plan_workers}）\n", flush=True)

    plan = []          # (cid, new_persona_dict, name_before, name_after, report)
    skipped = []       # (cid, reason)

    def _plan_group(job):
        """規劃一組：調 LLM 出化名並對每個成員算替換後的 persona。
        返回 (gi, gid, [plan_rows], [skip_rows], [log_lines])。純計算，不寫線上。"""
        gi, gid = job
        members = groups[gid]
        rows, skips, logs = [], [], [f"[組 {gi}/{len(gids)}] {gid}"]
        try:
            mapping = _new_names_for_group(members)
        except Exception as e:  # noqa: BLE001
            logs.append(f"    LLM 失敗，跳過整組: {e}")
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
                logs.append(f"    - {cid} {old_name!r} 無有效新名，跳過")
                continue
            persona_text = json.dumps(d.get("persona", {}), ensure_ascii=False)
            new_text, report = _apply_rules(persona_text, rules)
            try:
                new_persona = json.loads(new_text)
            except Exception as e:  # noqa: BLE001
                skips.append((cid, f"json_invalid: {e}"))
                logs.append(f"    ✗ {cid} 替換後 JSON 非法，跳過: {e}")
                continue
            if new_persona.get("name") == old_name:
                skips.append((cid, "name_field_unchanged"))
                logs.append(f"    ✗ {cid} name 欄位未變（原名可能是他人子串），跳過")
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

    print(f"\n計劃改名 {len(plan)} 個，跳過 {len(skipped)} 個。", flush=True)
    if skipped:
        print("跳過明細（前 20）:")
        for cid, why in skipped[:20]:
            print(f"    {cid}: {why}")

    # 落盤計劃，便於複核/複用
    plan_path = os.path.join(bak, "_rename_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump([{"char_id": c, "name_before": nb, "name_after": na}
                   for c, _p, nb, na, _r in plan], f,
                  ensure_ascii=False, indent=1)
    print(f"計劃已寫入 {plan_path}", flush=True)

    if not args.apply:
        print("\n（dry-run，未寫回線上。加 --apply 才會寫。）")
        return 0

    ok = err = 0
    for cid, persona, _nb, _na, _r in plan:
        try:
            r = requests.put(f"{BASE}/api/persona",
                             json={"char_id": cid, "persona": persona},
                             timeout=(10, 60))
            r.raise_for_status()
            ok += 1
            print(f"  ✓ 寫回 {cid}", flush=True)
        except Exception as e:  # noqa: BLE001
            err += 1
            print(f"  ✗ 寫回失敗 {cid}: {e}", flush=True)
        time.sleep(0.2)
    print(f"\n完成：成功 {ok}，失敗 {err}。備份目錄: {bak}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
