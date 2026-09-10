#!/usr/bin/env python3
"""每日把 vLLM / SGLang 上跟 DeepSeek-V4.1 有关的 PR 动态推到飞书群。

产出两样东西：
  1. site/index.html —— 组件 × 类型的全量明细页，每条 PR 带链接和摘要
  2. 一张飞书卡片 —— 只放矩阵，每个数字链到页面对应锚点

无状态：只查「最近 WINDOW_HOURS 小时内有更新」的 PR，不需要 state 文件或 cache，
重跑幂等。连续几天在迭代的 PR 会连续出现 —— 这是特性，说明它还在动。

环境变量:
  LARK_WEBHOOK      发卡片时必填；只生成页面（PAGE_ONLY=1）时不需要
  LARK_SECRET       群里开了「签名校验」就必填
  LARK_KEYWORD      群里开了「自定义关键词」就必填，这个词会被织进卡片正文；
                    不填而群里又开了关键词，飞书会以 19024 Key Words Not Found 拒收
  GITHUB_TOKEN      强烈建议，未认证的 search API 只有 10 次/分钟
  PAGE_URL          明细页地址，卡片里的锚点链接以此为前缀
  WINDOW_HOURS      回看窗口，默认 25（比 24 多 1 小时，避免 cron 抖动漏掉）
  REPORT_TZ         展示时区，默认 America/Los_Angeles
  OUT_DIR           页面输出目录，默认 site
  SEND_WHEN_EMPTY   一条都没有时是否照发，默认 0
  PAGE_ONLY         1 = 只生成页面不发卡片
  DRY_RUN           1 = 生成页面并打印卡片 JSON，但不发送
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import page  # noqa: E402
from common import (COMPONENT_LABELS, COMPONENT_ORDER, KIND_LABELS,  # noqa: E402
                    KIND_ORDER, collect, component_of, is_v41, kind_of)

DEFAULT_PAGE = "https://zelcie.github.io/pr-daily-update/"


def _get(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing required env var: {name}")
    return v


def build_card(items: list[dict], since: datetime, tz: ZoneInfo,
               page_url: str, keyword: str | None = None) -> dict:
    grid: dict[str, Counter] = defaultdict(Counter)
    for it in items:
        grid[component_of(it["title"])][kind_of(it["title"])] += 1

    now = datetime.now(tz)
    v41 = sum(1 for i in items if is_v41(i["title"]))
    base = page_url.rstrip("/") + "/"
    # 关键词校验只扫 elements，不扫 header —— 早期版本正文里铺着 PR 标题、
    # 自带关键词，改成纯矩阵后就撞上了 19024，所以这里显式织一遍。
    lead = f"共 **{len(items)}** 条，其中标题直指 V4.1 **{v41}** 条 · [看全部明细]({base})"
    if keyword and keyword.lower() not in lead.lower():
        lead = f"{keyword} · {lead}"
    els: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": lead}},
                       {"tag": "hr"}]

    for c in COMPONENT_ORDER:
        row = grid.get(c)
        if not row:
            continue
        # 每个数字都是链接，点进去落到明细页对应小节
        cells = [f"[{KIND_LABELS[k]} {row[k]}]({base}#{c}-{k})"
                 for k in KIND_ORDER if row[k]]
        els.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"**{COMPONENT_LABELS[c]}** [{sum(row.values())} 条]({base}#{c})\n"
            f"{' · '.join(cells)}"}})

    els.append({"tag": "hr"})
    els.append({"tag": "note", "elements": [{"tag": "lark_md", "content":
        f"窗口 {since.astimezone(tz):%m-%d %H:%M} → {now:%m-%d %H:%M} · "
        f"组件和类型都取第一个命中的分类"}]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text",
                   "content": f"DeepSeek-V4.1 · vLLM / SGLang 日报 {now:%m-%d}"}},
        "elements": els,
    }


def post(webhook: str, secret: str | None, card: dict) -> None:
    body: dict = {"msg_type": "interactive", "card": card}
    if secret:
        ts = str(int(time.time()))
        # 飞书签名很反直觉：待签名串当 key，被签名的消息体是空的。
        digest = hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()
        body["timestamp"], body["sign"] = ts, base64.b64encode(digest).decode()

    raw = json.dumps(body, ensure_ascii=False).encode()
    if len(raw) > 20 * 1024:
        sys.exit(f"card body {len(raw)}B exceeds the 20KB webhook limit")
    req = urllib.request.Request(webhook, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.load(resp)
    if out.get("code", out.get("StatusCode")) != 0:
        sys.exit(f"lark rejected the message: {out}")
    print(f"card sent ok ({len(raw)}B)")


def main() -> None:
    dry = os.environ.get("DRY_RUN") == "1"
    page_only = os.environ.get("PAGE_ONLY") == "1"
    webhook = "" if (dry or page_only) else _get("LARK_WEBHOOK")
    tz = ZoneInfo(os.environ.get("REPORT_TZ", "America/Los_Angeles"))
    page_url = os.environ.get("PAGE_URL", DEFAULT_PAGE)
    since = datetime.now(timezone.utc) - timedelta(
        hours=float(os.environ.get("WINDOW_HOURS", "25")))

    items = collect(since, os.environ.get("GITHUB_TOKEN"))
    dist = Counter(f"{component_of(i['title'])}/{kind_of(i['title'])}" for i in items)
    print(f"{len(items)} PRs in window, "
          f"{sum(1 for i in items if is_v41(i['title']))} match V4.1")
    print(f"  top cells: {dist.most_common(6)}")

    out_dir = pathlib.Path(os.environ.get("OUT_DIR", "site"))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "index.html"
    target.write_text(page.render(items, since, tz), encoding="utf-8")
    print(f"page written: {target} ({target.stat().st_size}B)")

    if page_only:
        return
    if not items and os.environ.get("SEND_WHEN_EMPTY", "0") != "1":
        print("nothing in window, skipping card")
        return

    card = build_card(items, since, tz, page_url,
                      os.environ.get("LARK_KEYWORD") or None)
    if dry:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return
    post(webhook, os.environ.get("LARK_SECRET") or None, card)


if __name__ == "__main__":
    main()
