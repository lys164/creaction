# -*- coding: utf-8 -*-
"""Fill remaining zh -> Traditional Chinese localization by pulling FULL data from
the arca storage hub (not just local cache).

Reuses the translation logic in translate_zh_to_hant_localized.py as a library.

Scope:
- personas (remote query_all): records with lang == "zh" whose editable text still
  contains simplified-only characters.
- ig_batches (remote query_all): batches with lang == "zh"; translate each post
  content that still contains simplified-only characters.

Safety: same as the base script (name protected & restored exactly, {user} preserved,
IDs/enums untouched, per-record backup, resumable state).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env (no secret printing).
_envp = ROOT / ".env"
if _envp.exists():
    for _raw in _envp.read_text(encoding="utf-8").splitlines():
        _l = _raw.strip()
        if not _l or _l.startswith("#") or "=" not in _l:
            continue
        _k, _v = _l.split("=", 1)
        _k = _k.strip(); _v = _v.strip()
        if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ("'", '"'):
            _v = _v[1:-1]
        os.environ.setdefault(_k, _v)

from app import config, storage  # noqa: E402
import scripts.translate_zh_to_hant_localized as base  # noqa: E402

DATA_DIR = config.DATA_DIR
STATE_PATH = DATA_DIR / "translate_zh_to_hant_online_fill_state.json"
_LOCK = threading.Lock()

# Simplified-only characters (map to a different traditional form). Reused from the
# verification pass. Presence => text still needs conversion/localization.
SIMP = set(
    "這們為來時說沒過還讓發見間體國門頭話氣樂電車東風雲鳥魚龍雞鴨鵝麥黃綠紅藍麗萬與專業叢絲嚴臨舉義烏喬習鄉書買亂爭虧亞產親從眾優會傘偉傳傷倫偽俠侶傾償儲兒黨蘭關興養獸內寫軍農沖決況凍淨準涼減湊鳳擊鑿劃劉則剛創刪別劑劍劇勸辦務動勵勞勢勳勻區醫華協單賣盧滷臥衛卻廠廳歷厲壓厭縣叄參雙變敘疊葉號嘆聽啟嗎響團園圍圖圓聖場壞塊堅壇壩墳墜壟墊牆壯聲殼壺處備復夠夾奪奮獎奧妝婦媽嬌娛孫學寧寶實寵審寬賓寢對尋導壽將爾塵嘗層歲嶺島峽嶄巔幣帥師帳簾幟帶幫莊慶庫應廟廢廣開異棄張彌彎彈強歸當錄徹徑憶憂懷態憐總戀恆懇惡惱悅驚懼慘懲慣願懶戲戰戶撲執擴掃揚擾撫拋搶護報擔擬攏揀擁攔擰撥擇掛摯撓擋掙擠揮撈損撿換搗據擄擲摻攬擱摟攪攜攝擺搖攤撐攆攢敵斂數齋斬斷無舊曠顯曬曉暈暉暫術機殺雜權條楊極構棗槍楓櫃標棧棟欄樹樣檔橋樺槳樁夢檢樓橫櫻歡歐殘毆毀畢斃匯漢汙湯溝滄濘淚潑澤潔灑淺漿澆濁測濟瀏渾濃塗濤澇漣渦渙滌潤澗漲澀淵漁滲溫灣溼潰濺滾滯滿濾濫濱灘瀟潛瀾滅燈靈災燦爐燉點煉爛燭煙煩燒燙熱愛爺牽狀獨獅獄獵豬貓獻瑪環現瓏瑣瓊畫暢疇療瘡瘋癢瘓癇癱癮皺盞鹽監蓋盜盤睜睞瞞矯礦碼磚礎碩確礙鹼禮禱禍禪離禿種積稱穩窮竅窯竄窩窺競篤筍筆籠築篩籌籤簡籃類糧緊紅約級紀線練組細織終經綁結繞給絕統繡繼績緒續維綿綜綻綠綴緬纜緝緩編緣縛縫纏縮繳網羅罰罷羈翹聳恥聾職聯聰肅腸膚腎腫脹脅膽勝膠脈髒腦膿腳脫臉臘膩騰艦艙艱豔藝節蕪葦蒼蘇蘋莖薦榮葷藥萊蓮獲瑩營蕭薩藍薔虜慮虛雖蝦蟻螞蠶蠻蠅蟬釁銜補襯襖襲裝褲見觀規視覽覺觸譽計訂認討讓訓議訊記講諱訝許論設訪訣證評識訴診詞試詩誠誕詢該詳語誤誘說請諸諾讀課誰調談謀謊謂謎謙謹譜謝謠謬貝負財賢敗賬貨質販貧購貯貫貼貴貸費賀賊賈貿資賭賞賠賴賺賽贊贈贏趕趨躍踐蹤軀轉輪軟轟輕載較輔輛輩輝輯輸轅轄輾辭辯邊遼達遷過運還這進遠違連遲適選遜遞邏遺遙鄧郵鄰鄭醬釀釋鑑針鍾鋼錢鐵鈴銀鋪銷鎖錯錨錫錦鍵鏡長閃閉問闖閒悶鬧聞閱闊隊陽陰陣階際陸陳險隨隱難霧靜韻頁頂項順須顧頓領頸頰頻題顏額風飛飯飲飾飽館饞馬馳驅駛駝駕罵驗騎騙驟鮮齡龜"
)

# a few common multi-char words for cleaner detection (optional; single-char set already covers).


def has_simplified(text: str, name: str = "") -> bool:
    s = text.replace(name, "") if name else text
    return any(ch in SIMP for ch in s)


def record_needs(persona: dict) -> bool:
    name = persona.get("name", "") if isinstance(persona, dict) else ""

    def walk(x) -> bool:
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "name":
                    continue
                if walk(v):
                    return True
            return False
        if isinstance(x, list):
            return any(walk(v) for v in x)
        if isinstance(x, str):
            return has_simplified(x, name)
        return False

    return walk(persona)


def load_state() -> dict:
    obj = base.load_json(STATE_PATH) or {}
    obj.setdefault("done_personas", [])
    obj.setdefault("done_ig", [])
    obj.setdefault("failed", [])
    return obj


def save_state(state: dict) -> None:
    with _LOCK:
        base.atomic_write_json(STATE_PATH, state)


def mark(state: dict, bucket: str, key: str) -> None:
    with _LOCK:
        arr = state.setdefault(bucket, [])
        if key not in arr:
            arr.append(key)
        base.atomic_write_json(STATE_PATH, state)


def fail(state: dict, item: str, err: Exception) -> None:
    with _LOCK:
        state.setdefault("failed", []).append({"item": item, "error": str(err)[:400], "ts": int(time.time())})
        base.atomic_write_json(STATE_PATH, state)


def translate_persona_record(record: dict, model: str, temperature: float, retries: int) -> dict:
    persona = record["persona"]
    name = persona.get("name")
    if not isinstance(name, str):
        raise base.TranslationError("persona.name not string")
    editable = copy.deepcopy(persona)
    editable.pop("name", None)
    payload = base.prepare_payload(editable, name)
    translated = base.translate_json(payload, "persona_json_without_name", model, temperature, retries)
    translated = base.restore_payload(translated, name)
    errors = base.same_shape(editable, translated)
    if errors:
        raise base.TranslationError("shape mismatch: " + "; ".join(errors[:5]))
    ordered = {}
    for k in persona.keys():
        if k == "name":
            ordered[k] = name
        elif k in translated:
            ordered[k] = translated[k]
    for k, v in translated.items():
        if k not in ordered:
            ordered[k] = v
    ordered["name"] = name
    new_rec = copy.deepcopy(record)
    new_rec["persona"] = ordered
    if new_rec["persona"].get("name") != name:
        raise base.TranslationError("name changed")
    return new_rec


def translate_ig_batch(batch: dict, name: str, model: str, temperature: float, retries: int) -> tuple[dict, int]:
    posts = batch.get("posts") or []
    payload_posts = []
    ids = set()
    for post in posts:
        if not isinstance(post, dict):
            continue
        pid = post.get("post_id"); content = post.get("content")
        if not isinstance(pid, str) or not isinstance(content, str):
            continue
        if not has_simplified(content, name):
            continue  # already traditional; skip to save tokens
        ids.add(pid)
        payload_posts.append({"post_id": pid, "content": base.mask_text(content, name)})
    if not payload_posts:
        return batch, 0
    payload = {"posts": payload_posts}
    translated = base.translate_json(payload, "post_content_list", model, temperature, retries)
    errors = base.same_shape(payload, translated)
    if errors:
        raise base.TranslationError("post shape mismatch: " + "; ".join(errors[:5]))
    by_id = {p.get("post_id"): p.get("content") for p in translated.get("posts", []) if isinstance(p, dict)}
    if set(by_id.keys()) != ids:
        raise base.TranslationError("post_id set changed")
    new_batch = copy.deepcopy(batch)
    n = 0
    for post in new_batch.get("posts", []):
        pid = post.get("post_id") if isinstance(post, dict) else None
        if pid in by_id:
            post["content"] = base.unmask_text(str(by_id[pid]), name)
            n += 1
    return new_batch, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--model", default="gemini-3.5-flash")
    ap.add_argument("--temperature", type=float, default=0.35)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--limit-personas", type=int, default=0)
    ap.add_argument("--limit-ig", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    dry = not args.apply
    if not storage.arca_storage.enabled():
        print("ERROR: arca storage not enabled; cannot pull remote full data.")
        return 2

    state = {"done_personas": [], "done_ig": [], "failed": []} if args.force else load_state()
    done_p = set(state.get("done_personas", []))
    done_ig = set(state.get("done_ig", []))

    print("pulling remote personas ...", flush=True)
    persona_rows = storage.query_all("personas")
    print("pulling remote ig_batches ...", flush=True)
    ig_rows = storage.query_all("ig_batches")

    # Build persona name map (for post masking) from remote personas.
    name_by_char = {}
    for r in persona_rows:
        d = r.get("data") or {}
        cid = d.get("char_id") or r.get("key")
        nm = (d.get("persona") or {}).get("name")
        if isinstance(cid, str) and isinstance(nm, str):
            name_by_char[cid] = nm

    persona_targets = []
    for r in persona_rows:
        d = r.get("data") or {}
        if d.get("lang") != "zh" or not isinstance(d.get("persona"), dict):
            continue
        cid = d.get("char_id") or r.get("key")
        if not args.force and cid in done_p:
            continue
        if record_needs(d["persona"]):
            persona_targets.append((cid, d))
    if args.limit_personas:
        persona_targets = persona_targets[: args.limit_personas]

    ig_targets = []
    for r in ig_rows:
        d = r.get("data") or {}
        if d.get("lang") != "zh" or not isinstance(d.get("posts"), list):
            continue
        cid = d.get("char_id") or r.get("key")
        if not args.force and cid in done_ig:
            continue
        name = name_by_char.get(cid, "")
        if any(isinstance(p, dict) and isinstance(p.get("content"), str) and has_simplified(p["content"], name)
               for p in d.get("posts", [])):
            ig_targets.append((cid, d))
    if args.limit_ig:
        ig_targets = ig_targets[: args.limit_ig]

    remaining_posts = sum(
        sum(1 for p in d.get("posts", []) if isinstance(p, dict) and isinstance(p.get("content"), str)
            and has_simplified(p["content"], name_by_char.get(cid, "")))
        for cid, d in ig_targets)

    print(f"remote totals: personas={len(persona_rows)} ig_batches={len(ig_rows)}")
    print(f"TO TRANSLATE: personas={len(persona_targets)} | ig_batches={len(ig_targets)} "
          f"(~{remaining_posts} post contents) | dry_run={dry} | model={args.model}")

    if dry:
        for cid, d in persona_targets[:8]:
            print("  persona todo", cid, (d.get("persona") or {}).get("name"))
        for cid, d in ig_targets[:8]:
            print("  ig todo", cid, "posts=", len(d.get("posts", [])))
        return 0

    backup = DATA_DIR / f"_backup_zh_hant_online_{time.strftime('%Y%m%d_%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "manifest.json").write_text(json.dumps({
        "created": int(time.time()),
        "personas_to_translate": len(persona_targets),
        "ig_to_translate": len(ig_targets),
        "post_contents": remaining_posts,
        "model": args.model,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    def backup_remote(coll: str, key: str, data: dict) -> None:
        d = backup / coll
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{key}.json"
        if not f.exists():
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    errors = 0

    def do_persona(cid_data):
        cid, d = cid_data
        try:
            new_rec = translate_persona_record(d, args.model, args.temperature, args.retries)
            backup_remote("personas", cid, d)
            local = config.PERSONA_DIR / f"{cid}.json"
            storage.save_json("personas", new_rec["char_id"], new_rec, local)
            return (cid, (new_rec.get("persona") or {}).get("profile", "")[:60], None)
        except Exception as e:  # noqa: BLE001
            return (cid, None, e)

    def do_ig(cid_data):
        cid, d = cid_data
        name = name_by_char.get(cid, "")
        try:
            new_batch, n = translate_ig_batch(d, name, args.model, args.temperature, args.retries)
            backup_remote("ig_batches", cid, d)
            local = config.POST_DIR / cid / "ig_latest.json"
            storage.save_json("ig_batches", str(cid), new_batch, local)
            return (cid, n, None)
        except Exception as e:  # noqa: BLE001
            return (cid, None, e)

    print(f"translating {len(persona_targets)} personas ...", flush=True)
    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as ex:
        futs = [ex.submit(do_persona, t) for t in persona_targets]
        for i, fut in enumerate(as_completed(futs), 1):
            cid, sample, err = fut.result()
            if err:
                errors += 1; fail(state, f"persona:{cid}", err)
                print(f"  ✗ persona {cid}: {err}", flush=True)
            else:
                mark(state, "done_personas", cid)
                print(f"  ✓ persona {i}/{len(persona_targets)} {cid} :: {sample}", flush=True)

    print(f"translating {len(ig_targets)} ig_batches ...", flush=True)
    with ThreadPoolExecutor(max_workers=max(args.concurrency, 1)) as ex:
        futs = [ex.submit(do_ig, t) for t in ig_targets]
        for i, fut in enumerate(as_completed(futs), 1):
            cid, n, err = fut.result()
            if err:
                errors += 1; fail(state, f"ig:{cid}", err)
                print(f"  ✗ ig {cid}: {err}", flush=True)
            else:
                mark(state, "done_ig", cid)
                print(f"  ✓ ig {i}/{len(ig_targets)} {cid} :: {n} posts", flush=True)

    print(f"DONE. errors={errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
