# -*- coding: utf-8 -*-
"""批量用「真实人设(real track)」链路跑角色：人设(中日韩英) → 帖子。

规则（对应需求）：
- 人物图各是一个角色；50% 概率再从【同性别】非人物图库挑一张拼成 2 张输入。
- 图片单次领料：选过的图（人物图/非人物图）就不再被选；同性别非人物图用尽则不拼。
- 生成人设时拼入职业库 + 性格库：全库可用，但每条只用一次（拼过就不拼），
  每个角色组发一手（性格 1 + 职业 1），4 语言共用同一手；领到即销账（不再发放）。
- source 写 "image"；中日韩英四语言各生成一个本土化角色记录。
- 帖子默认只出文案（with_images=False，控成本）；跑完全部落线上(arca)。

断点续跑：进度写 data/batch_real_track_state.json，记录已完成人物图、已消耗
非人物图、已消耗灵感库条目。重跑自动跳过已完成、继续未消耗资源。

用法：
  PYTHONPATH=. python3 scripts/batch_real_track.py [--limit N] [--with-post-images]
  PYTHONPATH=. python3 scripts/batch_real_track.py --dry-run     # 只校验，不调 API
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
# (文件夹, 性别, 是否人物图)
FOLDERS = [
    (DL / "女人设40", "female", True),
    (DL / "男人设60", "male", True),
    (DL / "男人设60-非人物图", "male", False),
    (DL / " 女人设40 非人物图", "female", False),
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


# ---- 灵感库：全库单次领料（发即销账，拼过不再拼）---------------------------
class SeedDealer:
    """从性格库/职业库无放回发牌：每组发 (性格1 + 职业1)，领到即消耗。"""

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
    ap.add_argument("--limit", type=int, default=0, help="最多跑多少个角色组")
    ap.add_argument("--with-post-images", action="store_true",
                    help="帖子同时出配图（极慢极贵；默认只出文案）")
    ap.add_argument("--dry-run", action="store_true", help="只校验，不调用 API")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（复现拼图/发牌）")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    # 枚举图库
    person: dict[str, list[str]] = {"female": [], "male": []}
    nonperson: dict[str, list[str]] = {"female": [], "male": []}
    for folder, gender, is_person in FOLDERS:
        imgs = _list_images(folder)
        (person if is_person else nonperson)[gender].extend(imgs)
        print(f"  {folder.name}: {len(imgs)} 张 ({gender}, {'人物' if is_person else '非人物'})")

    state = load_state()
    done = set(state["done_person"])
    used_np = set(state["used_nonperson"])
    # 每性别可用非人物图（去掉已消耗），洗牌
    np_avail = {g: [p for p in nonperson[g] if p not in used_np] for g in nonperson}
    for g in np_avail:
        random.shuffle(np_avail[g])

    dealer = SeedDealer(state["used_seed_ids"])
    print(f"灵感库可用：性格 {len(dealer.pools.get('personality', []))} / "
          f"职业 {len(dealer.pools.get('occupation', []))}"
          f"（已消耗 {len(dealer.used)}）")

    # 待跑人物图（跳过已完成），保持稳定顺序后洗牌以增加多样性
    todo: list[tuple[str, str]] = []
    for gender in ("female", "male"):
        for img in person[gender]:
            if img not in done:
                todo.append((img, gender))
    random.shuffle(todo)
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    print(f"\narca 线上存储：{'开启' if _arca_on() else '未开启(仅本地)'}")
    print(f"待跑角色组：{len(todo)}（已完成 {len(done)}）；"
          f"帖子配图：{'开' if args.with_post_images else '关(仅文案)'}\n")
    if args.dry_run:
        _dry_preview(todo, np_avail, dealer)
        return 0

    # 猴补 library：每组发一手无放回牌，4 语言共用；commit 变 no-op（发即销账）
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
            hand_desc = ", ".join(e["id"] for e in dealer.current_hand) or "(库已空)"
            print(f"[{idx}/{len(todo)}] {gender} {Path(img).name}"
                  f"{' +非人物' if picked_np else ''} | 灵感:{hand_desc}", flush=True)
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
            except Exception as e:  # noqa: BLE001 单组失败不中断整批
                err += 1
                # 组失败：把本组预挑的非人物图退回池，避免白白消耗
                if picked_np:
                    np_avail[gender].append(picked_np)
                print(f"    ✗ 失败：{e}", flush=True)
                traceback.print_exc()
    finally:
        library.checkout, library.commit = _orig_checkout, _orig_commit

    print(f"\n完成：成功 {ok} 组，失败 {err} 组。累计完成 {len(state['done_person'])} 组。")
    return 0 if err == 0 else 1


def _arca_on() -> bool:
    from app import arca_storage
    return arca_storage.enabled()


def _dry_preview(todo, np_avail, dealer) -> None:
    print("=== DRY RUN 预览（不调用 API）===")
    np_left = {g: list(v) for g, v in np_avail.items()}
    combine = 0
    for img, gender in todo:
        if random.random() < COMBINE_PROB and np_left.get(gender):
            np_left[gender].pop()
            combine += 1
    print(f"计划跑 {len(todo)} 组，其中约 {combine} 组会拼第二张同性别非人物图。")
    print(f"每组发牌：性格1 + 职业1（无放回）；库存够跑 "
          f"{min(len(dealer.pools.get('personality', [])), len(dealer.pools.get('occupation', [])))} 组不重复。")
    for img, gender in todo[:3]:
        print(f"  样例：{gender} {Path(img).name}")


if __name__ == "__main__":
    sys.exit(main())
