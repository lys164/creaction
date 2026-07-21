# -*- coding: utf-8 -*-
"""批次用「真實人設(real track)」鏈路跑角色：人設(中日韓英) → 帖子。

規則（對應需求）：
- 人物圖各是一個角色；50% 機率再從【同性別】非人物相簿挑一張拼成 2 張輸入。
- 圖片單次領料：選過的圖（人物圖/非人物圖）就不再被選；同性別非人物圖用盡則不拼。
- 生成人設時拼入職業庫 + 性格庫：全庫可用，但每條只用一次（拼過就不拼），
  每個角色組發一手（性格 1 + 職業 1），4 語言共用同一手；領到即銷賬（不再發放）。
- source 寫 "image"；中日韓英四語言各生成一個本土化角色記錄。
- 帖子預設只出文案（with_images=False，控成本）；跑完全部落線上(arca)。

斷點續跑：進度寫 data/batch_real_track_state.json，記錄已完成人物圖、已消耗
非人物圖、已消耗靈感庫條目。重跑自動跳過已完成、繼續未消耗資源。

用法：
  PYTHONPATH=. python3 scripts/batch_real_track.py [--limit N] [--with-post-images]
  PYTHONPATH=. python3 scripts/batch_real_track.py --dry-run     # 只校驗，不調 API
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import traceback
from pathlib import Path

from app import config, library, pipeline

DL = Path.home() / "Downloads"
# (資料夾, 性別, 是否人物圖)
FOLDERS = [
    (DL / "女人設40", "female", True),
    (DL / "男人設60", "male", True),
    (DL / "男人設60-非人物圖", "male", False),
    (DL / " 女人設40 非人物圖", "female", False),
]
LANGS = ["zh", "ja", "ko", "en"]
SOURCE = "image"
COMBINE_PROB = 0.5
STATE_PATH = config.DATA_DIR / "batch_real_track_state.json"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _list_images(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    return sorted(
        str(p) for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMG_EXTS and not p.name.startswith(".")
    )


def load_state() -> dict:
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            s.setdefault("done_person", [])
            s.setdefault("used_nonperson", [])
            s.setdefault("used_seed_ids", [])
            s.setdefault("groups", [])
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"done_person": [], "used_nonperson": [], "used_seed_ids": [], "groups": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")


# ---- 靈感庫：全庫單次領料（發即銷賬，拼過不再拼）---------------------------
class SeedDealer:
    """從性格庫/職業庫無放回發牌：每組發 (性格1 + 職業1)，領到即消耗。"""

    def __init__(self, used_ids: list[str], per_dim: int = 1):
        self.per_dim = per_dim
        self.used = set(used_ids)
        self.pools: dict[str, list[dict]] = {}
        for dim, entries in library._ENTRIES.items():
            avail = [e for e in entries if e["id"] not in self.used]
            random.shuffle(avail)
            self.pools[dim] = avail
        self.current_hand: list[dict] = []

    def deal_group(self) -> list[dict]:
        hand: list[dict] = []
        for dim in ("personality", "occupation"):
            pool = self.pools.get(dim, [])
            for _ in range(self.per_dim):
                if not pool:
                    break
                e = pool.pop()
                self.used.add(e["id"])
                hand.append(e)
        self.current_hand = hand
        return hand


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多跑多少個角色組")
    ap.add_argument("--with-post-images", action="store_true",
                    help="帖子同時出配圖（極慢極貴；預設只出文案）")
    ap.add_argument("--dry-run", action="store_true", help="只校驗，不呼叫 API")
    ap.add_argument("--seed", type=int, default=None, help="隨機種子（復現拼圖/發牌）")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    # 列舉相簿
    person: dict[str, list[str]] = {"female": [], "male": []}
    nonperson: dict[str, list[str]] = {"female": [], "male": []}
    for folder, gender, is_person in FOLDERS:
        imgs = _list_images(folder)
        (person if is_person else nonperson)[gender].extend(imgs)
        print(f"  {folder.name}: {len(imgs)} 張 ({gender}, {'人物' if is_person else '非人物'})")

    state = load_state()
    done = set(state["done_person"])
    used_np = set(state["used_nonperson"])
    # 每性別可用非人物圖（去掉已消耗），洗牌
    np_avail = {g: [p for p in nonperson[g] if p not in used_np] for g in nonperson}
    for g in np_avail:
        random.shuffle(np_avail[g])

    dealer = SeedDealer(state["used_seed_ids"])
    print(f"靈感庫可用：性格 {len(dealer.pools.get('personality', []))} / "
          f"職業 {len(dealer.pools.get('occupation', []))}"
          f"（已消耗 {len(dealer.used)}）")

    # 待跑人物圖（跳過已完成），保持穩定順序後洗牌以增加多樣性
    todo: list[tuple[str, str]] = []
    for gender in ("female", "male"):
        for img in person[gender]:
            if img not in done:
                todo.append((img, gender))
    random.shuffle(todo)
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    print(f"\narca 線上儲存：{'開啟' if _arca_on() else '未開啟(僅本地)'}")
    print(f"待跑角色組：{len(todo)}（已完成 {len(done)}）；"
          f"帖子配圖：{'開' if args.with_post_images else '關(僅文案)'}\n")
    if args.dry_run:
        _dry_preview(todo, np_avail, dealer)
        return 0

    # 猴補 library：每組發一手無放回牌，4 語言共用；commit 變 no-op（發即銷賬）
    _orig_checkout, _orig_commit = library.checkout, library.commit
    library.checkout = lambda *a, **k: list(dealer.current_hand)
    library.commit = lambda used_ids, *a, **k: list(used_ids)

    ok = err = 0
    try:
        for idx, (img, gender) in enumerate(todo, 1):
            imgs = [img]
            picked_np = None
            if random.random() < COMBINE_PROB and np_avail.get(gender):
                picked_np = np_avail[gender].pop()
                imgs.append(picked_np)
            dealer.deal_group()
            hand_desc = ", ".join(e["id"] for e in dealer.current_hand) or "(庫已空)"
            print(f"[{idx}/{len(todo)}] {gender} {Path(img).name}"
                  f"{' +非人物' if picked_np else ''} | 靈感:{hand_desc}", flush=True)
            try:
                records = pipeline.create_personas_from_images(
                    imgs, LANGS, user_hint="", track="real", source=SOURCE)
                for rec in records:
                    pipeline.generate_instagram_posts(
                        rec["char_id"], track="real",
                        with_images=args.with_post_images)
                ok += 1
                state["done_person"].append(img)
                if picked_np:
                    state["used_nonperson"].append(picked_np)
                state["used_seed_ids"] = sorted(dealer.used)
                state["groups"].append({
                    "person_image": img, "nonperson_image": picked_np,
                    "gender": gender, "group_id": records[0].get("group_id"),
                    "char_ids": [r["char_id"] for r in records],
                    "seeds": [e["id"] for e in dealer.current_hand],
                    "ts": int(time.time()),
                })
                save_state(state)
            except Exception as e:  # noqa: BLE001 單組失敗不中斷整批
                err += 1
                # 組失敗：把本組預挑的非人物圖退回池，避免白白消耗
                if picked_np:
                    np_avail[gender].append(picked_np)
                print(f"    ✗ 失敗：{e}", flush=True)
                traceback.print_exc()
    finally:
        library.checkout, library.commit = _orig_checkout, _orig_commit

    print(f"\n完成：成功 {ok} 組，失敗 {err} 組。累計完成 {len(state['done_person'])} 組。")
    return 0 if err == 0 else 1


def _arca_on() -> bool:
    from app import arca_storage
    return arca_storage.enabled()


def _dry_preview(todo, np_avail, dealer) -> None:
    print("=== DRY RUN 預覽（不呼叫 API）===")
    np_left = {g: list(v) for g, v in np_avail.items()}
    combine = 0
    for img, gender in todo:
        if random.random() < COMBINE_PROB and np_left.get(gender):
            np_left[gender].pop()
            combine += 1
    print(f"計劃跑 {len(todo)} 組，其中約 {combine} 組會拼第二張同性別非人物圖。")
    print(f"每組發牌：性格1 + 職業1（無放回）；庫存夠跑 "
          f"{min(len(dealer.pools.get('personality', [])), len(dealer.pools.get('occupation', [])))} 組不重複。")
    for img, gender in todo[:3]:
        print(f"  樣例：{gender} {Path(img).name}")


if __name__ == "__main__":
    sys.exit(main())
