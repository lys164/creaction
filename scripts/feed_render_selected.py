#!/usr/bin/env python3
"""按指定 post_id 順序，從線上 ig_batches 儲存拉取帖子並渲染成 feed 卡片 HTML。

用法:
  python3 scripts/feed_render_selected.py            # 讀取內建 ID 列表
輸出:
  data/feed_selected.html
"""
import html
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("ARCA_BASE_URL",
                      "https://api.popop.dev").rstrip("/")
KEY = os.environ.get(
    "ARCA_STORAGE_KEY",
    "sk_5c581cae262b4f54b838246942dd30de3375f9d3f283df24424d9f09502615cb")
IMG_BASE = "http://popop-pipeline.internal-app.imaginewithu.com"

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "feed_selected.html"

# 使用者指定的順序（保持原樣，去掉全形冒號差異）
WANTED = [
    "ig_1783713634_bc329e", "ig_1783705276_d72432", "ig_1783704215_e59c35",
    "ig_1783709334_0aecde", "ig_1783671552_4ba092", "ig_1783704282_8404a1",
    "ig_1783709882_4e4965", "ig_1783710553_ad1757", "ig_1783705902_e71e6a",
    "ig_1783671852_d12e8d", "ig_1783703598_5bf24b", "ig_1783710624_5f4405",
    "ig_1783709502_d7fc81", "ig_1783703814_1d411c", "ig_1783660597_d996d4",
    "ig_1783681083_61942b", "ig_1783710060_f4a3dc", "ig_1783708752_addf3f",
    "ig_1783704878_5e2f34", "ig_1783704983_2b83a7", "ig_1783713639_5a685d",
    "ig_1783709880_a138a6", "ig_1783711861_a4fa10", "ig_1783711717_14aa03",
    "ig_1783681820_a6c453", "ig_1783571699_ec55b6", "ig_1783709614_3f274b",
    "ig_1783677986_43e303", "ig_1783710291_71a326", "ig_1783710811_9ceb37",
    "ig_1783707509_e837b0", "ig_1783681177_f88084", "ig_1783711766_7399b2",
    "ig_1783711146_87b08f", "ig_1783711599_73c1b2", "ig_1783709516_8de174",
    "ig_1783707744_0ad88f", "ig_1783684028_2c68a4", "ig_1783711421_b21183",
    "ig_1783705893_8016ff", "ig_1783709566_ac93f9", "ig_1783712492_7bca15",
    "ig_1783705902_c90182", "ig_1783705890_eea80e", "ig_1783707841_b7dbf2",
    "ig_1783670470_527f98", "ig_1783703974_50bb33", "ig_1783684028_f3005c",
    "ig_1783693790_ccb44a", "ig_1783703102_6c9d81", "ig_1783703531_d4dfb5",
    "ig_1783702530_4e7410", "ig_1783707744_d07a67", "ig_1783687176_8f09aa",
    "ig_1783704036_4b7961", "ig_1783708746_f5e33c", "ig_1783686584_55044b",
    "ig_1783708746_a68936", "ig_1783710720_1234e0", "ig_1783705051_d44018",
    "ig_1783704019_37098e", "ig_1783712066_7f0e0e", "ig_1783690105_597b5f",
    "ig_1783708752_328652",
]


def query_page(offset, limit=200):
    r = requests.post(f"{BASE}/storage/records/query",
                      headers={"X-Storage-Key": KEY,
                               "Content-Type": "application/json"},
                      json={"collection": "ig_batches", "limit": limit,
                            "offset": offset},
                      timeout=60)
    r.raise_for_status()
    return r.json().get("items", [])


def build_index():
    """掃描全部 ig_batches 記錄，建立 post_id -> (char_key, batch, post) 索引。"""
    index = {}
    char_meta = {}
    offset = 0
    limit = 200
    scanned = 0
    while True:
        items = query_page(offset, limit)
        if not items:
            break
        for it in items:
            key = it.get("key")
            data = it.get("data") or {}
            char_meta[key] = {"lang": data.get("lang"),
                              "char_id": data.get("char_id", key)}
            for p in data.get("posts", []) or []:
                pid = p.get("post_id")
                if pid and pid not in index:
                    index[pid] = {"char_key": key, "lang": data.get("lang"),
                                  "post": p}
        scanned += len(items)
        print(f"  scanned {scanned} chars, indexed {len(index)} posts",
              file=sys.stderr)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.1)
    return index, char_meta


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


def img_url(post, char_key=None):
    """優先用線上 pipeline 的 /img/{char}_{post_id}.png（穩定、缺圖自動回源 OSS）。

    數據裡 image.url 常是失效的第三方圖床簽名鏈接（403），不可用。
    """
    img = post.get("image") or {}
    pid = post.get("post_id")
    if char_key and pid:
        return f"{IMG_BASE}/img/{char_key}_{pid}.png"
    u = img.get("url") or ""
    if not u:
        return ""
    if u.startswith("http"):
        return u
    return IMG_BASE + u


def esc(s):
    return html.escape(str(s or ""))


def render(index, char_meta):
    cards = []
    found = 0
    missing = []
    for i, pid in enumerate(WANTED, 1):
        rec = index.get(pid)
        if not rec:
            missing.append(pid)
            cards.append(f'''<div class="card missing">
      <div class="hd"><div class="idx">{i}</div>
        <div class="who"><div class="name">未找到</div>
        <div class="pid">{esc(pid)}</div></div></div>
      <div class="body miss">線上儲存中沒有這個 post_id</div>
    </div>''')
            continue
        found += 1
        post = rec["post"]
        char_key = rec["char_key"]
        lang = rec.get("lang") or ""
        text = content_text(post)
        zh = content_zh(post)
        iu = img_url(post, char_key)
        ptype = post.get("post_type_name") or post.get("post_type") or ""
        img_html = (f'<img class="ph" loading="lazy" src="{esc(iu)}" '
                    f'alt="">' if iu else
                    '<div class="ph noimg">無圖</div>')
        zh_html = (f'<div class="zh">{esc(zh)}</div>' if zh else "")
        cards.append(f'''<div class="card">
      <div class="hd">
        <div class="idx">{i}</div>
        <div class="ava">{esc((char_key or "?")[5:7]).upper()}</div>
        <div class="who">
          <div class="name">{esc(char_key)}</div>
          <div class="pid">{esc(pid)} · {esc(lang)}{(" · " + esc(ptype)) if ptype else ""}</div>
        </div>
      </div>
      {img_html}
      <div class="body">
        <div class="txt">{esc(text)}</div>
        {zh_html}
      </div>
    </div>''')

    stat = (f"共 {len(WANTED)} 個 · 命中 {found} · 缺失 {len(missing)}")
    body = "\n".join(cards)
    return PAGE.replace("{{STAT}}", esc(stat)).replace("{{CARDS}}", body), found, missing


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Feed 卡片預覽</title>
<style>
  :root{--bg:#f0f2f5;--card:#fff;--line:#e4e6eb;--sub:#8a8d91;--txt:#1c1e21;}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);font-family:-apple-system,"PingFang SC",
    "Microsoft YaHei",system-ui,sans-serif;color:var(--txt);}
  header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);
    padding:14px 18px;display:flex;align-items:baseline;gap:12px;}
  header h1{font-size:17px;margin:0;font-weight:700;}
  header .stat{font-size:13px;color:var(--sub);}
  .feed{max-width:520px;margin:0 auto;padding:18px 12px 60px;
    display:flex;flex-direction:column;gap:16px;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
    overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.04);}
  .hd{display:flex;align-items:center;gap:10px;padding:10px 12px;}
  .idx{min-width:26px;height:26px;border-radius:50%;background:#eef1f6;color:#65676b;
    font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center;}
  .ava{width:36px;height:36px;border-radius:50%;
    background:linear-gradient(135deg,#7b8cff,#c86bff);color:#fff;font-weight:700;
    display:flex;align-items:center;justify-content:center;font-size:14px;}
  .who{min-width:0;flex:1;}
  .name{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;}
  .pid{font-size:11px;color:var(--sub);white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;}
  .ph{width:100%;display:block;aspect-ratio:1/1;object-fit:cover;background:#eee;}
  .ph.noimg{aspect-ratio:auto;height:64px;display:flex;align-items:center;
    justify-content:center;color:var(--sub);font-size:13px;}
  .body{padding:10px 14px 14px;}
  .txt{font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word;}
  .zh{margin-top:8px;padding-top:8px;border-top:1px dashed var(--line);
    font-size:12.5px;color:#65676b;line-height:1.5;white-space:pre-wrap;}
  .card.missing{opacity:.7;}
  .body.miss{color:#c0392b;font-size:13px;padding:14px;}
</style></head>
<body>
  <header><h1>Feed 卡片預覽</h1><span class="stat">{{STAT}}</span></header>
  <div class="feed">
    {{CARDS}}
  </div>
</body></html>"""


def main():
    print("掃描線上 ig_batches ...", file=sys.stderr)
    index, char_meta = build_index()
    print(f"索引完成：{len(index)} posts / {len(char_meta)} chars",
          file=sys.stderr)
    out_html, found, missing = render(index, char_meta)
    OUT.write_text(out_html, encoding="utf-8")
    print(f"寫入 {OUT}", file=sys.stderr)
    print(f"命中 {found}/{len(WANTED)}，缺失 {len(missing)}", file=sys.stderr)
    if missing:
        print("缺失：" + ", ".join(missing), file=sys.stderr)


if __name__ == "__main__":
    main()
