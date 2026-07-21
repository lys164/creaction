#!/usr/bin/env python3
"""構建可拖動排序的多 tab feed 預覽頁。

- 三個列表（list1=64、list2=98去重、list3=59）作為 tab。
- 掃描一次線上 ig_batches，建立 post_id 索引。
- 截斷的 ID（後綴 <6 位）按前綴在線上唯一匹配後自動補全。
- 圖片走線上 /img/{char_key}_{post_id}.png。
- 資料內嵌進 HTML，雙擊即可用；前端可拖動、可重新導出。

輸出: data/feed_reorder.html
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://api.popop.dev"
KEY = "sk_5c581cae262b4f54b838246942dd30de3375f9d3f283df24424d9f09502615cb"
IMG_BASE = "http://popop-pipeline.internal-app.imaginewithu.com"

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "scripts" / "feed_reorder_template.html"
OUT = ROOT / "data" / "feed_reorder.html"

# ---- 三個列表（保持使用者給的原始順序）----
LIST1 = """
ig_1783713634_bc329e ig_1783705276_d72432 ig_1783704215_e59c35 ig_1783709334_0aecde
ig_1783671552_4ba092 ig_1783704282_8404a1 ig_1783709882_4e4965 ig_1783710553_ad1757
ig_1783705902_e71e6a ig_1783671852_d12e8d ig_1783703598_5bf24b ig_1783710624_5f4405
ig_1783709502_d7fc81 ig_1783703814_1d411c ig_1783660597_d996d4 ig_1783681083_61942b
ig_1783710060_f4a3dc ig_1783708752_add ig_1783704878_5e2f34 ig_1783704983_2b83a7
ig_1783713639_5a685d ig_1783709880_a138a6 ig_1783711861_a4fa10 ig_1783711717_14aa03
ig_1783681820_a6c453 ig_1783571699_ec55b6 ig_1783709614_3f274b ig_1783677986_43e303
ig_1783710291_71a326 ig_1783710811_9ceb37 ig_1783707509_e837b0 ig_1783681177_f88084
ig_1783711766_7399b2 ig_1783711146_87b08f ig_1783711599_73c1b2 ig_1783709516_8de174
ig_1783707744_0ad88f ig_1783684028_2c68a4 ig_1783711421_b21183 ig_1783705893_8016ff
ig_1783709566_ac93f9 ig_1783712492_7bca15 ig_1783705902_c90182 ig_1783705890_eea80e
ig_1783707841_b7 ig_1783670470_527f98 ig_1783703974_50bb33 ig_1783684028_f3005c
ig_1783693790_ccb44a ig_1783703102_6c9d81 ig_1783703531_d4dfb5 ig_1783702530_4e7410
ig_1783707744_d07a67 ig_1783687176_8f09aa ig_1783704036_4b7961 ig_1783708746_f5e33c
ig_1783686584_55044b ig_1783708746_a68936 ig_1783710720_1234e0 ig_1783705051_d44018
ig_1783704019_37098e ig_1783712066_7f0e0e ig_1783690105_597b5f ig_1783708752_328652
"""

LIST2 = """
ig_1782701060_600023 ig_1782701300_7ce5be ig_1782701319_eee16f ig_1783708631_591780
ig_1783676783_90014b ig_1783695302_1298fa ig_1783708305_e42d3d ig_1783705829_c8e2ae
ig_1783709752_6b86ba ig_1783710226_227485 ig_1783694324_0e3ba4 ig_1783710988_05eea0
ig_1783710070_4d5327 ig_1783687896_64fb7e ig_1783687179_8c8c7f ig_1782701638_5fabcc
ig_1783695345_bd3e6d ig_1783687179_719b86 ig_1783708305_21f58a ig_1783676783_ab7a4a
ig_1783711683_6ba77b ig_1783710784_4b837a ig_1783708949_98e12e ig_1783705718_7fb925
ig_1783701242_205056 ig_1783710988_565d05 ig_1783710226_26f963 ig_1783710784_224998
ig_1783695121_4b1b3c ig_1783709494_d3b5aa ig_1783709777_2e8cbb ig_1783709752_a5005d
ig_1783712335_59c751 ig_1783712147_667233 ig_1783693882_7a71d3 ig_1783709934_06d489
ig_1783712422_28f794 ig_1783714854_afe4e5 ig_1783708456_1e5452 ig_1783687896_6cb168
ig_1783711660_88abdf ig_1783712477_b88e3a ig_1783711714_16dd9e ig_1783701827_48424a
ig_1783710086_26b61c ig_1783714854_f36b97 ig_1783712663_f7ed06 ig_1783687896_e6912a
ig_1783714854_f36b97 ig_1783703626_dde577 ig_1783709494_121bfe ig_1783701667_900690
ig_1783676288_750d96 ig_1783709777_67bb98 ig_1783703626_970b95 ig_1783703089_bdb537
ig_1783703626_3d861c ig_1783702134_8492e1 ig_1783711737_4bcb00 ig_1783704671_55e318
ig_1783711769_0dbfd8 ig_1783709466_6d66fe ig_1783704671_69124b ig_1783712860_3a8a5c
ig_1783705371_5b7a5d ig_1783710070_7363ba ig_1783701396_f5d486 ig_1783708667_5bd427
ig_1783704307_af8a7e ig_1783704616_0ee523 ig_1783709534_ea38e3 ig_1783712127_10f573
ig_1783676783_90014b ig_1783708805_d95c53 ig_1783687179_744a26 ig_1783702453_3eb3a4
ig_1783711769_ea778d ig_1783710731_cee7d6 ig_1783703166_3daef6 ig_1783704616_9d090f
ig_1783702820_12d54b ig_1783689171_737076 ig_1783713978_ceea44 ig_1783704671_49636e
ig_1783709466_54a1a4 ig_1783701901_4357e4 ig_1783703038_ec9b66 ig_1783705025_7d1ea4
ig_1783570504_87adfd ig_1783709534_ba093e ig_1783670417_03ca7d ig_1783712127_cfbb45
ig_1782701300_6bd51d ig_1783713649_21e945 ig_1783713978_7c5473 ig_1783710070_c49c7b
ig_1783767293_86173a ig_1783704632_4d3a12 ig_1783704269_7e7907 ig_1783705025_0ecd93
"""

LIST3 = """
ig_1783710071_ae3365 ig_1783701268_239926 ig_1783709133_d0272d ig_1783660577_2b2443
ig_1783767292_e0ccf3 ig_1783708689_28c69b ig_1783707892_df9f0a ig_1783673167_56d5cc
ig_1783711093_aec558 ig_1783685647_57e1cf ig_1783704048_fa83ec ig_1783957074_0f39ab
ig_1783708733_07fe23 ig_1783712377_f77e7d ig_1784050919_b7c567 ig_1783709555_b531cc
ig_1783711251_59b483 ig_1783712817_72e128 ig_1783709780_3d1c4a ig_1783705980_76af37
ig_1783707572_cfd862 ig_1783665045_2e3834 ig_1783964720_a82b0e ig_1783712846_d498c1
ig_1783705043_6189da ig_1783707616_5daad5 ig_1783707247_b6ef6a ig_1783701947_6dcbc2
ig_1783701466_1108e ig_1783767298_d2c652 ig_1783767298_76d836 ig_1782701509_b6e5a2
ig_1783693718_e80444 ig_1783709007_0726bc ig_1783701290_cd1214 ig_1783677780_6928a8
ig_1783767283_486e4e ig_1783707295_535eb4 ig_1783707326_74a62a ig_1783710085_b71272
ig_1783951304_1da9fc ig_1783714554_9511a8 ig_1783709134_8ee84f ig_1783710080_51a22e
ig_1783708067_4c8240 ig_1783711733_1ed6dc ig_1783680926_531ea2 ig_1784040959_e60cff
ig_1783702607_8be061 ig_1783708960_036715 ig_1783708521_92fc88 ig_1783710525_4ce4cb
ig_1783710054_352c34 ig_1783695154_eae838 ig_1783702510_088ffd ig_1783705986_2e2d2e
ig_1783663003_81ea12 ig_1783708307_0d21e5 ig_1783767720_b7e852
"""


def parse(s):
    return [t for t in s.split() if t.startswith("ig_")]


def dedup(ids):
    seen = set()
    out = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def is_truncated(pid):
    m = re.match(r"^ig_\d+_([0-9a-f]+)$", pid)
    return bool(m) and len(m.group(1)) < 6


def content_text(post):
    c = post.get("content")
    if isinstance(c, dict):
        return c.get("ko") or c.get("zh") or c.get("en") or c.get("ja") or ""
    return c or ""


def content_zh(post):
    c = post.get("content")
    if isinstance(c, dict):
        return c.get("zh") or ""
    return ""


def scan(wanted_exact, truncated_prefixes):
    """掃描全部記錄；返回 exact 索引 + 截斷前綴命中。"""
    index = {}
    trunc_hits = {p: [] for p in truncated_prefixes}
    offset = 0
    limit = 200
    scanned = 0
    while True:
        r = requests.post(f"{BASE}/storage/records/query",
                          headers={"X-Storage-Key": KEY,
                                   "Content-Type": "application/json"},
                          json={"collection": "ig_batches", "limit": limit,
                                "offset": offset}, timeout=60)
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            break
        for it in items:
            key = it.get("key")
            data = it.get("data") or {}
            lang = data.get("lang")
            for p in data.get("posts", []) or []:
                pid = p.get("post_id")
                if not pid:
                    continue
                if pid in wanted_exact and pid not in index:
                    index[pid] = {"char_key": key, "lang": lang, "post": p}
                for pref in truncated_prefixes:
                    if pid.startswith(pref):
                        trunc_hits[pref].append(
                            {"pid": pid, "char_key": key, "lang": lang,
                             "post": p})
        scanned += len(items)
        print(f"  scanned {scanned}, exact {len(index)}", file=sys.stderr)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.05)
    return index, trunc_hits


def main():
    lists = [
        ("列表1", parse(LIST1)),
        ("列表2(去重)", dedup(parse(LIST2))),
        ("列表3", parse(LIST3)),
    ]
    all_ids = []
    for _, ids in lists:
        all_ids += ids
    exact = set(x for x in all_ids if not is_truncated(x))
    trunc = sorted(set(x for x in all_ids if is_truncated(x)))
    print(f"總 ID {len(all_ids)}，exact {len(exact)}，截斷 {len(trunc)}: {trunc}",
          file=sys.stderr)

    index, trunc_hits = scan(exact, trunc)

    # 補全截斷 ID：唯一命中才替換
    resolve = {}
    for pref, hits in trunc_hits.items():
        uniq = {h["pid"]: h for h in hits}
        if len(uniq) == 1:
            h = next(iter(uniq.values()))
            resolve[pref] = h["pid"]
            index[h["pid"]] = {"char_key": h["char_key"], "lang": h["lang"],
                               "post": h["post"]}
            print(f"  補全 {pref} -> {h['pid']}", file=sys.stderr)
        else:
            print(f"  截斷 {pref} 命中 {len(uniq)} 個，保留原樣", file=sys.stderr)

    def fix(ids):
        return [resolve.get(x, x) for x in ids]

    # 組裝 posts 字典
    posts = {}
    for _, ids in lists:
        for pid in fix(ids):
            if pid in posts:
                continue
            rec = index.get(pid)
            if not rec:
                posts[pid] = {"missing": True}
                continue
            p = rec["post"]
            ck = rec["char_key"]
            posts[pid] = {
                "char_key": ck,
                "lang": rec.get("lang") or "",
                "text": content_text(p),
                "zh": content_zh(p),
                "ptype": p.get("post_type_name") or p.get("post_type") or "",
                "img": f"{ck}_{pid}.png",
            }

    data = {
        "img_base": IMG_BASE,
        "lists": [{"name": n, "ids": fix(ids)} for n, ids in lists],
        "posts": posts,
    }

    tpl = TPL.read_text(encoding="utf-8")
    html = tpl.replace("/*__DATA__*/",
                       json.dumps(data, ensure_ascii=False))
    OUT.write_text(html, encoding="utf-8")

    total = sum(len(l["ids"]) for l in data["lists"])
    miss = sum(1 for p in posts.values() if p.get("missing"))
    print(f"寫入 {OUT}", file=sys.stderr)
    print(f"三個 tab 共 {total} 卡片，唯一帖子 {len(posts)}，缺失 {miss}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
