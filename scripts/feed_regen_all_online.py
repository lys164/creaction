#!/usr/bin/env python3
"""對線上所有 feed 策展角色批量生成 T1 / T2 / 熱搜事件，寫入線上資料。

用改後鏈路重新生成，供在 feed.html 上和舊資料對比效果。
- T1：平臺媒體號論壇體宣傳帖
- T2：角色綁定帖（subtype=auto，自動讀最近一次聊天作素材）
- event：熱搜事件（可能返回 abstain，屬正常結果）

每個生成走後臺任務 + 輪詢；併發受限，避免壓垮服務。
"""
import concurrent.futures as cf
import json
import sys
import time
import urllib.request

BASE = "http://popop-pipeline.internal-app.imaginewithu.com"
TIMEOUT = 30
POLL_INTERVAL = 4
POLL_MAX = 180  # 每個任務最多輪詢 180*4=720s


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def _poll(task_id: str) -> dict:
    for _ in range(POLL_MAX):
        t = _get(f"/api/tasks/{task_id}")
        if t.get("status") == "done":
            return {"ok": True, "result": t.get("result")}
        if t.get("status") == "error":
            return {"ok": False, "error": t.get("error") or "unknown"}
        time.sleep(POLL_INTERVAL)
    return {"ok": False, "error": "poll timeout"}


def gen_t1(char_id: str) -> dict:
    r = _post("/api/feed_posts", {"char_id": char_id, "kind": "t1",
                                  "with_images": True})
    return _poll(r["task_id"])


def gen_t2(char_id: str) -> dict:
    r = _post("/api/feed_posts", {"char_id": char_id, "kind": "t2",
                                  "subtype": "auto", "with_images": True})
    return _poll(r["task_id"])


def gen_event(char_id: str) -> dict:
    r = _post("/api/feed_events", {"char_id": char_id, "with_images": True})
    return _poll(r["task_id"])


def run_one(kind: str, char_id: str, name: str) -> dict:
    fn = {"T1": gen_t1, "T2": gen_t2, "event": gen_event}[kind]
    t0 = time.time()
    try:
        out = fn(char_id)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    dt = round(time.time() - t0)
    status = "OK"
    detail = ""
    if not out.get("ok"):
        status = "FAIL"
        detail = str(out.get("error"))[:160]
    else:
        res = out.get("result") or {}
        if isinstance(res, dict) and res.get("abstain"):
            status = "ABSTAIN"
            detail = (res.get("reason") or "")[:160]
    line = f"[{status:7}] {kind:5} {name}({char_id}) {dt}s {detail}"
    print(line, flush=True)
    return {"kind": kind, "char_id": char_id, "name": name,
            "status": status, "detail": detail, "seconds": dt}


def main() -> None:
    retry = "--retry" in sys.argv
    workers = 2 if retry else 4
    if retry:
        with open("scripts/feed_regen_result.json", encoding="utf-8") as fh:
            prev = json.load(fh)
        jobs = [(r["kind"], r["char_id"], r["name"])
                for r in prev if r["status"] == "FAIL"]
        print(f"重跑 {len(jobs)} 個失敗任務（併發 {workers}）", flush=True)
    else:
        chars = _get("/api/feed_characters")
        jobs = [(kind, c["char_id"], c.get("name", ""))
                for c in chars for kind in ("T1", "T2", "event")]
        print(f"角色 {len(chars)} 個 × 3 類 = {len(jobs)} 個生成任務", flush=True)
    results = []
    # 併發受限：每個任務內部是同步 LLM 呼叫，服務端 4 workers，別壓太狠
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_one, *j) for j in jobs]
        for f in cf.as_completed(futs):
            results.append(f.result())

    print("\n==== 匯總 ====", flush=True)
    for st in ("OK", "ABSTAIN", "FAIL"):
        sub = [r for r in results if r["status"] == st]
        print(f"{st}: {len(sub)}", flush=True)
        for r in sub:
            print(f"  {r['kind']:5} {r['name']} {r['detail']}", flush=True)
    with open("scripts/feed_regen_result.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    fails = [r for r in results if r["status"] == "FAIL"]
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
