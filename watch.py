#!/usr/bin/env python3
"""每天把 vLLM / SGLang 上跟 DeepSeek-V4.1 有关的进展推到飞书群。

产出两样：
  1. site/index.html —— 按 P0/P1/P2 分组的朴素链接列表，每条带 AI 评级理由
  2. 一张飞书卡片 —— P0 全列（加粗），P1 列标题，P2 只给数

三道处理：抓 → 只留窗口内有实质进展的 → 交给 Claude 评 P0/P1/P2。

环境变量:
  LARK_WEBHOOK       发卡片时必填
  LARK_SECRET        群里开了「签名校验」就必填
  LARK_KEYWORD       群里开了「自定义关键词」就必填
  GITHUB_TOKEN       强烈建议。没有它 search 只有 10 次/分钟，
                     且有效更新过滤会整个跳过（timeline 请求打不动）
  LLM_API_KEY        公司网关的 key。没有就降级成规则打分，页面上会标明未经评估
  LLM_BASE_URL       默认 https://f7xnt9mg.fn.bytedance.net/v1
  LLM_MODEL          默认 gpt-6-astra
  PAGE_URL           明细页地址
  WINDOW_HOURS       回看窗口，默认 25（比 24 多 1 小时，避免 cron 抖动漏掉）
  REPORT_TZ          展示时区，默认 America/Los_Angeles
  OUT_DIR            页面输出目录，默认 site
  SEND_WHEN_EMPTY    一条都没有时是否照发，默认 0
  PAGE_ONLY / DRY_RUN / NO_AI   只出页面 / 不发送 / 跳过 AI
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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import criteria  # noqa: E402
import page  # noqa: E402
from analyze import _describe, key_of, triage  # noqa: E402
from common import (COMPONENT_LABELS, COMPONENT_ORDER, card_of,  # noqa: E402
                    collect, component_of, filter_effective, is_new,
                    keep_issue, state_of)

DEFAULT_PAGE = "https://zelcie.github.io/pr-daily-update/"
CARD_P0_CAP = 20      # 单次卡片最多列这么多 P0，其余给个跳转
CARD_LIMIT = 20 * 1024
CARD_SOFT = 18 * 1024  # 逼近上限就先砍理由，别等 post() 里硬退出


def _get(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing required env var: {name}")
    return v


def build_card(items: list[dict], verdicts: dict, since: datetime, tz: ZoneInfo,
               page_url: str, ai_on: bool, keyword: str | None = None) -> dict:
    """卡片跟页面同构：先大类、类内再分档。P0 全列且加粗，P1/P2 只给数。"""
    grid: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for it in items:
        lvl = verdicts.get(key_of(it), {}).get("level", "P2")
        grid[component_of(it["title"])][lvl].append(it)
    p0_total = sum(len(grid[c]["P0"]) for c in grid)
    now = datetime.now(tz)
    base = page_url.rstrip("/") + "/"

    fresh = sum(1 for i in items if is_new(i, since))
    p0_fresh = sum(1 for c in grid for i in grid[c]["P0"] if is_new(i, since))
    # 「今日 N 条」会被读成「今天新出 N 条」，但窗口卡的是 updated_at，
    # 实测四成条目超过一周。新增和存量的行动完全不同，必须分开写。
    lead = (f"**P0 {p0_total}**"
            + (f"（今日新增 **{p0_fresh}**）" if p0_fresh else "（均为存量）")
            + f" · 今日有进展 {len(items)} 条，其中新建 {fresh} 条"
            f" · [看全部]({base})")
    if keyword and keyword.lower() not in lead.lower():
        lead = f"{keyword} · {lead}"
    els: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": lead}}]
    if not ai_on:
        els.append({"tag": "div", "text": {"tag": "lark_md", "content":
            "<font color='grey'>⚠️ 等级为规则打分，未经 AI 评估</font>"}})

    # P0 按大类分段列出。判级松紧会波动，P0 一多卡片就会超 20KB 被飞书拒收 ——
    # 那天就一条都发不出去，所以这里必须有硬上限，不能赌「P0 应该不多」。
    shown = 0
    for c in COMPONENT_ORDER:
        p0 = grid[c]["P0"]
        if not p0 or shown >= CARD_P0_CAP:
            continue
        p0 = sorted(p0, key=lambda i: not is_new(i, since))
        take = p0[:CARD_P0_CAP - shown]
        shown += len(take)
        els.append({"tag": "hr"})
        els.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"**{COMPONENT_LABELS[c]}** · P0 {len(p0)} 条"
            + (f"（列出 {len(take)} 条）" if len(take) < len(p0) else "")}})
        for it in take:
            v = verdicts.get(key_of(it), {})
            _, st = state_of(it)
            card = card_of(it["title"])
            meta = " · ".join(x for x in (st, card) if x)
            tag = "🆕 " if is_new(it, since) else ""
            els.append({"tag": "div", "text": {"tag": "lark_md", "content":
                f"**{tag}{'🐞 ' if 'pull_request' not in it else ''}"
                f"[{it['title']}]({it['html_url']})**\n"
                f"<font color='grey'>{v.get('reason', '')} · {meta}</font>"}})
    if p0_total > shown:
        els.append({"tag": "div", "text": {"tag": "lark_md", "content":
            f"<font color='grey'>…还有 {p0_total - shown} 条 P0 未列出，"
            f"[在明细页查看]({base})</font>"}})

    # P1 / P2 只给每个大类的条数，点进去看
    rest = []
    for c in COMPONENT_ORDER:
        n1, n2 = len(grid[c]["P1"]), len(grid[c]["P2"])
        if n1 or n2:
            parts = " ".join(filter(None, [
                f"[P1 {n1}]({base}#{c}-p1)" if n1 else "",
                f"[P2 {n2}]({base}#{c}-p2)" if n2 else ""]))
            rest.append(f"{COMPONENT_LABELS[c]} {parts}")
    if rest:
        els.append({"tag": "hr"})
        els.append({"tag": "div", "text": {"tag": "lark_md",
                    "content": "**其余**\n" + "\n".join(rest)}})

    els.append({"tag": "note", "elements": [{"tag": "lark_md", "content":
        f"{since.astimezone(tz):%m-%d %H:%M} → {now:%m-%d %H:%M} · "
        f"窗口卡的是「有进展」不是「新建」·「🆕 今日新增」才是今天才出现的 · "
        f"[判定标准 {criteria.active()['version']}]({base}#criteria)"}]})

    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "red" if p0_total else "blue",
                       "title": {"tag": "plain_text",
                                 "content": f"DeepSeek-V4.1 上游日报 {now:%m-%d}"}},
            "elements": els}

    # 兜底：还是逼近上限就把灰色理由行剥掉，标题和链接优先保住
    if len(json.dumps(card, ensure_ascii=False).encode()) > CARD_SOFT:
        for e in card["elements"]:
            t = (e.get("text") or {}).get("content", "")
            if "<font color='grey'>" in t and t.startswith("**"):
                e["text"]["content"] = t.split("\n<font color='grey'>")[0]
        print("card: 逼近 20KB，已剥掉理由行", file=sys.stderr)
    return card


def post(webhook: str, secret: str | None, card: dict) -> None:
    body: dict = {"msg_type": "interactive", "card": card}
    if secret:
        ts = str(int(time.time()))
        # 飞书签名很反直觉：待签名串当 key，被签名的消息体是空的。
        digest = hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()
        body["timestamp"], body["sign"] = ts, base64.b64encode(digest).decode()
    raw = json.dumps(body, ensure_ascii=False).encode()
    if len(raw) > CARD_LIMIT:
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
    token = os.environ.get("GITHUB_TOKEN")
    since = datetime.now(timezone.utc) - timedelta(
        hours=float(os.environ.get("WINDOW_HOURS", "25")))

    prs = collect(since, token)
    # issue 用同一个窗口：这里要的是「今天动了什么」，不是长期风险板
    issues = [i for i in collect(since, token, kind="issue") if keep_issue(i)]
    raw = prs + issues
    items = filter_effective(raw, since, token)
    filtered = bool(token)
    print(f"{len(raw)} touched → {len(items)} with real progress"
          f" ({len(prs)} PRs / {len(issues)} issues before filter)"
          + ("" if filtered else "  [未过滤：无 GITHUB_TOKEN]"))

    ai_on = False
    verdicts: dict[str, dict] = {}
    if items and os.environ.get("NO_AI") != "1":
        verdicts = triage(items)
        ai_on = any(v.get("ai") for v in verdicts.values())
    elif items:
        from analyze import _fallback
        verdicts = _fallback(items)
    _c = criteria.active(token)
    lv = defaultdict(int)
    for v in verdicts.values():
        lv[v["level"]] += 1
    print(f"triage: P0 {lv['P0']} / P1 {lv['P1']} / P2 {lv['P2']}"
          f"  (ai={'on' if ai_on else 'off'}, model={_describe()}, "
          f"criteria={_c['version']} [{_c['source']}])")
    # 两处降级都不会让任务失败，只会让日报悄悄变成噪声 —— 在日志里喊出来
    if not ai_on:
        print("::warning::LLM_API_KEY 未配置，等级为规则打分，未经 AI 评估")
    if not filtered:
        print("::warning::GITHUB_TOKEN 未配置，未做有效更新过滤")

    out_dir = pathlib.Path(os.environ.get("OUT_DIR", "site"))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "index.html"
    target.write_text(page.render(items, verdicts, since, tz, filtered, ai_on),
                      encoding="utf-8")
    print(f"page written: {target} ({target.stat().st_size}B)")

    if page_only:
        return
    if not items and os.environ.get("SEND_WHEN_EMPTY", "0") != "1":
        print("nothing with real progress, skipping card")
        return
    card = build_card(items, verdicts, since, tz, page_url, ai_on,
                      os.environ.get("LARK_KEYWORD") or None)
    if dry:
        print(json.dumps(card, ensure_ascii=False, indent=2)[:3000])
        return
    post(webhook, os.environ.get("LARK_SECRET") or None, card)


if __name__ == "__main__":
    main()
