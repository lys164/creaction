# -*- coding: utf-8 -*-
"""补跑线上多个 source 缺封面角色的封面 —— 直接打线上服务(HTTP)。

扫描线上指定 source(默认 heermeng,image,mengnv；排除"无来源"/空 source)且
无 cover_url 的角色，调用 /api/characters/batch_cover 逐批补封面。

画风(style_id)：
- 缺封面角色通常 style_id=None（还没出过封面）。按"角色已存画风优先、没存用默认"，
  默认 realistic_portrait，与各 source 已成功封面一致。
- nonhuman/flirt 链路服务器内部忽略 style_id，按自身逻辑出图；real/light 用 realistic_portrait。

断点：每批切走已处理项，避免死循环；上游审核拒绝的重试也未必成功，
可再次运行本脚本重扫兜底。

用法：
  PYTHONPATH=. python3 scripts/backfill_covers_multi.py [--sources a,b,c] [--batch N] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import time

import requests

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
DEFAULT_SOURCES = ["heermeng", "image", "mengnv"]
DEFAULT_STYLE = "realistic_portrait"   # 缺封面角色 style_id=None 时的兜底；nonhuman/flirt 忽略
POLL_INTERVAL = 8
BATCH_TIMEOUT = 3600
DEFAULT_BATCH = 8


def _healthy() -> bool:
    try:
        return requests.get(f"{BASE}/api/languages", timeout=20).status_code == 200
    except requests.RequestException:
        return False


def _wait_healthy(label: str = "") -> None:
    delay, waited = 5, 0
    while not _healthy():
        print(f"   ⏳ 服务器不可用，等待恢复{(' ('+label+')') if label else ''} 已等 {waited}s",
              flush=True)
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, 60)


def _req(method: str, url: str, **kw) -> requests.Response:
    last_err = None
    for attempt in range(6):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                last_err = f"HTTP {r.status_code}"
                _wait_healthy(url.rsplit("/", 1)[-1])
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = str(e)
            _wait_healthy(url.rsplit("/", 1)[-1])
            time.sleep(min(5 * (attempt + 1), 30))
    raise RuntimeError(f"请求多次失败 {method} {url}: {last_err}")


def _missing_by_source(sources: set[str]) -> dict[str, list[str]]:
    """线上扫描指定 source 且无 cover_url 的角色 char_id，按 source 分组。"""
    r = _req("GET", f"{BASE}/api/characters", timeout=60)
    out: dict[str, list[str]] = {s: [] for s in sources}
    for c in r.json():
        s = c.get("source")
        if s in sources and not c.get("cover_url") and c.get("char_id"):
            out[s].append(c["char_id"])
    return out


def _poll(task_id: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        r = _req("GET", f"{BASE}/api/tasks/{task_id}", timeout=30)
        t = r.json()
        if t.get("done_count") != last:
            last = t.get("done_count")
            print(f"   {label} {t.get('done_count')}/{t.get('total')} "
                  f"({t.get('status')})", flush=True)
        if t.get("status") == "done":
            return t.get("result") or {}
        if t.get("status") == "error":
            raise RuntimeError(f"{label} 任务失败: {t.get('error')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{label} 轮询超时 ({timeout}s)")


def _cover_batch(char_ids: list[str], style_id: str,
                 use_reference: bool | None = None) -> dict:
    payload = {"char_ids": char_ids, "style_id": style_id, "mode": "fill_missing"}
    if use_reference is not None:
        payload["use_reference"] = use_reference
    r = _req("POST", f"{BASE}/api/characters/batch_cover", json=payload, timeout=120)
    task_id = r.json()["task_id"]
    return _poll(task_id, BATCH_TIMEOUT, "补封面")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", type=str, default=",".join(DEFAULT_SOURCES))
    ap.add_argument("--style", type=str, default=DEFAULT_STYLE)
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--no-reference", action="store_true",
                    help="不拼原图做 i2i 参考，纯按文本 identity 出图（用于原图触发上游审核时）")
    ap.add_argument("--loop", action="store_true",
                    help="循环重扫：跑完一轮自动重扫线上仍缺的继续补，直到某轮 0 成功才停")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    use_ref = False if args.no_reference else None

    sources = {s.strip() for s in args.sources.split(",") if s.strip()}
    grouped = _missing_by_source(sources)
    total = sum(len(v) for v in grouped.values())
    print(f"线上服务: {BASE}")
    ref_desc = "不拼原图(纯文本identity)" if args.no_reference else "默认(按画风决定i2i)"
    print(f"目标 source: {sorted(sources)}；兜底画风: {args.style}；"
          f"每批 {args.batch} 个；原图参考: {ref_desc}")
    for s in sorted(grouped):
        print(f"  {s}: 缺封面 {len(grouped[s])} 个")
    print(f"合计 {total} 个\n")

    if args.dry_run:
        print(f"[DRY] 将分约 {(total + args.batch - 1)//args.batch} 批补封面。")
        return 0

    def _one_pass(pass_ids: list[str]) -> tuple[int, int]:
        """跑一遍给定 id 列表，返回 (成功数, 失败数)。"""
        ok, err, bn = 0, 0, 0
        while pass_ids:
            batch = pass_ids[:args.batch]
            pass_ids = pass_ids[args.batch:]
            bn += 1
            print(f"[批 {bn}] 补 {len(batch)} 个", flush=True)
            try:
                res = _cover_batch(batch, args.style, use_reference=use_ref)
                ok += len(res.get("covered", []))
                errs = res.get("errors", {})
                err += len(errs)
                if errs:
                    print(f"   ⚠ 本批 {len(errs)} 个仍失败(多为上游审核)", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"   ✗ 本批异常: {e}", flush=True)
        return ok, err

    grand_ok = 0
    round_no = 0
    while True:
        round_no += 1
        ids: list[str] = []
        for s in sorted(grouped):
            ids.extend(grouped[s])
        if not ids:
            print("没有缺封面的角色，无需补。" if round_no == 1 else "已无缺封面，收工。")
            break
        print(f"\n===== 第 {round_no} 轮：待补 {len(ids)} 个 =====", flush=True)
        ok, err = _one_pass(ids)
        grand_ok += ok
        print(f"----- 第 {round_no} 轮完成：成功 {ok}, 失败 {err} -----", flush=True)
        if not args.loop:
            print(f"\n完成: 本次成功补 {ok} 个, 失败 {err} 个。")
            break
        # loop 模式：本轮 0 成功说明剩下全是过不了审的硬骨头，停止避免空转
        if ok == 0:
            print(f"\n本轮 0 成功，剩余均为上游审核拒绝的硬骨头，停止。累计成功 {grand_ok} 个。")
            break
        time.sleep(5)
        # 重新扫描线上，只保留仍缺封面的
        grouped = _missing_by_source(sources)

    print("提示: 失败多为上游内容审核拒绝，稍后可再次运行本脚本重扫重试。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
