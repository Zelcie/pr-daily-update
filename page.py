"""把 PR / issue 渲染成一页朴素的链接列表：先按大类分，再在大类下按 P0/P1/P2 分。

大类的先后按暴露面排 —— EP/DP/PP 相关的靠前，TP/CP 靠后，非 NVIDIA 后端沉底。
这只影响阅读顺序，不影响判级：判级只看后果（能不能被正确 serve 起来）。
卡型（SM80/SM90/SM100…）只做行内标注，同样不参与判级 —— 不同卡型是并行推进的。
"""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from common import (COMPONENT_LABELS, COMPONENT_ORDER, card_of, component_of,
                    repo_of, state_of, summarize)

LEVELS = ["P0", "P1", "P2"]
LEVEL_DESC = {
    "P0": "会直接咬到线上：算错、卡死、崩溃、显存爆掉、明显退化，且落在我们跑的路径上",
    "P1": "值得本周看：相关路径上的功能推进、有意义的优化、影响面大但不紧急的缺陷",
    "P2": "知道就行：其他后端/其他模型专属、文档、CI、重构、小修小补",
}

CSS = """
:root{
  --bg:#fbfaf8; --panel:#fff; --ink:#1c1b19; --muted:#6b6862; --line:#e5e1da;
  --accent:#2d6ae0; --p0:#c0392b; --p0-bg:#fdf0ee; --p1:#b06a00; --p2:#6b6862;
  --open:#1a7f37; --draft:#8b8680; --merged:#8250df; --closed:#cf222e;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#16151a; --panel:#1e1d24; --ink:#e8e6e3; --muted:#9a958d; --line:#312f38;
  --accent:#6f9bff; --p0:#ff6b5a; --p0-bg:#331c19; --p1:#e0a049; --p2:#9a958d;
  --open:#3fb950; --draft:#8b8680; --merged:#a371f7; --closed:#f85149;
}}
:root[data-theme="dark"]{
  --bg:#16151a; --panel:#1e1d24; --ink:#e8e6e3; --muted:#9a958d; --line:#312f38;
  --accent:#6f9bff; --p0:#ff6b5a; --p0-bg:#331c19; --p1:#e0a049; --p2:#9a958d;
  --open:#3fb950; --draft:#8b8680; --merged:#a371f7; --closed:#f85149;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.65 ui-sans-serif,-apple-system,"Segoe UI",system-ui,
  "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin-bottom:30px}
h2{font-size:17px;margin:34px 0 2px;padding-top:14px;border-top:1px solid var(--line)}
h2 .n{color:var(--muted);font-weight:400;font-size:14px}
.desc{font-size:12px;color:var(--muted);margin:0 0 14px}
ul{list-style:none;margin:0;padding:0}
li{padding:9px 0 9px 13px;border-left:2px solid var(--line);margin-bottom:2px}
li.p0{border-left-color:var(--p0);background:var(--p0-bg);
  padding:11px 13px;border-radius:0 6px 6px 0}
a.t{color:var(--ink);text-decoration:none}
a.t:hover{color:var(--accent);text-decoration:underline}
li.p0 a.t{font-weight:700}
.tags{font-size:11px;color:var(--muted);margin-left:6px;white-space:nowrap}
.st-open{color:var(--open)} .st-draft{color:var(--draft)}
.st-merged{color:var(--merged)} .st-closed{color:var(--closed)}
.why{font-size:12.5px;color:var(--muted);margin-top:3px}
.why.ai::before{content:"AI ";font-size:10px;letter-spacing:.06em;
  color:var(--accent);font-weight:600}
.ev{font-size:11px;color:var(--muted)}
.toc{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 8px}
.toc a{display:flex;align-items:baseline;gap:7px;font-size:12.5px;
  text-decoration:none;color:var(--ink);background:var(--panel);
  border:1px solid var(--line);border-radius:7px;padding:5px 10px}
.toc a:hover{border-color:var(--accent)}
.toc .c{font-size:11px;font-weight:400}
.toc b{font-weight:600;margin-left:4px}
b.p0,h3.p0{color:var(--p0)} b.p1,h3.p1{color:var(--p1)} b.p2,h3.p2{color:var(--p2)}
h3{font-size:13px;margin:16px 0 7px;font-weight:700;letter-spacing:.02em}
h2 .n{color:var(--muted);font-weight:400;font-size:13px}
.card{border:1px solid var(--line);border-radius:4px;padding:0 4px}
.note{font-size:12px;color:var(--muted);background:var(--panel);
  border:1px solid var(--line);border-radius:8px;padding:11px 13px;margin:0 0 26px}
footer{margin-top:52px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12px}
"""

EV_LABEL = {"committed": "新提交", "head_ref_force_pushed": "强推",
            "reviewed": "review", "merged": "合并", "closed": "关闭",
            "reopened": "重开", "ready_for_review": "转正式",
            "convert_to_draft": "转草稿", "commented": "讨论",
            "review_requested": "求 review"}


def _line(it: dict, verdict: dict, tz: ZoneInfo) -> str:
    lvl = verdict.get("level", "P2")
    emoji, st = state_of(it)
    is_issue = "pull_request" not in it
    updated = datetime.fromisoformat(
        it["updated_at"].replace("Z", "+00:00")).astimezone(tz)
    bits = [f'<span class="tags">{html.escape(COMPONENT_LABELS[component_of(it["title"])])}'
            f' · <span class="st-{st}">{emoji} {st}</span>'
            f' · @{html.escape(it["user"]["login"])} · {updated:%m-%d %H:%M}']
    evs = [EV_LABEL.get(e, e) for e in dict.fromkeys(it.get("_events", []))]
    if evs:
        bits.append(f' · <span class="ev">今日 {"/".join(evs[:3])}</span>')
    bits.append("</span>")

    out = [f'<li class="{lvl.lower()}">']
    out.append(f'<a class="t" href="{html.escape(it["html_url"])}" target="_blank" '
               f'rel="noopener">{"🐞 " if is_issue else ""}'
               f'{html.escape(repo_of(it))} #{it["number"]} · '
               f'{html.escape(it["title"])}</a>')
    out += bits
    if (why := verdict.get("reason")):
        cls = "why ai" if verdict.get("ai") else "why"
        out.append(f'<div class="{cls}">{html.escape(why)}</div>')
    out.append("</li>")
    return "".join(out)


def render(items: list[dict], verdicts: dict[str, dict], since: datetime,
           tz: ZoneInfo, filtered: bool, ai_on: bool) -> str:
    from analyze import key_of
    grid: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    buckets: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        lvl = verdicts.get(key_of(it), {}).get("level", "P2")
        grid[component_of(it["title"])][lvl].append(it)
        buckets[lvl].append(it)

    now = datetime.now(tz)
    out = ['<div class="wrap">',
           "<h1>DeepSeek-V4.1 · vLLM / SGLang 每日进展</h1>"]
    out.append(f'<div class="sub">{since.astimezone(tz):%m-%d %H:%M} → '
               f'{now:%m-%d %H:%M} ({tz.key}) · 共 {len(items)} 条 · '
               + " · ".join(f"{l} {len(buckets[l])}" for l in LEVELS) + "</div>")

    notes = []
    if not ai_on:
        notes.append("⚠️ <strong>本页等级是规则打分，未经 AI 评估</strong>"
                     "（缺少 Anthropic 凭据）。")
    if not filtered:
        notes.append("⚠️ 未做有效更新过滤（缺少 GitHub token），"
                     "列表里可能混着只被机器人顶了一下、没有实质进展的条目。")
    notes.append("<strong>P0</strong> = 影响服务能否被正确 serve 起来"
                 "（静默算错 或 崩溃/卡死/OOM/起不来，两类同级）；"
                 "<strong>P1</strong> = 性能退化或优化、功能推进；"
                 "<strong>P2</strong> = 文档/CI/重构等杂务。"
                 "卡型只做标注，不参与判级。")
    notes.append("这是<strong>初筛</strong>：模型只看得到标题和正文摘要，看不到 diff。"
                 "P0 的意思是「今天先看这几条」，不是「这几条一定出事」。")
    out.append('<div class="note">' + "<br>".join(notes) + "</div>")

    # 目录：大类 + 各档条数，P0 数字标红
    out.append('<div class="toc">')
    for c in COMPONENT_ORDER:
        if not grid[c]:
            continue
        n = {l: len(grid[c][l]) for l in LEVELS}
        cnt = " ".join(f'<b class="{l.lower()}">{l} {n[l]}</b>' for l in LEVELS if n[l])
        out.append(f'<a href="#{c}">{html.escape(COMPONENT_LABELS[c])}'
                   f'<span class="c">{cnt}</span></a>')
    out.append("</div>")

    for c in COMPONENT_ORDER:
        if not grid[c]:
            continue
        total = sum(len(grid[c][l]) for l in LEVELS)
        out.append(f'<h2 id="{c}">{html.escape(COMPONENT_LABELS[c])} '
                   f'<span class="n">· {total} 条</span></h2>')
        for lvl in LEVELS:
            rows = grid[c][lvl]
            if not rows:
                continue
            out.append(f'<h3 id="{c}-{lvl.lower()}" class="{lvl.lower()}">{lvl} '
                       f'<span class="n">· {len(rows)} 条</span></h3>')
            out.append("<ul>")
            out += [_line(it, verdicts.get(key_of(it), {}), tz) for it in rows]
            out.append("</ul>")

    out.append(f"<footer>由 <code>pr-daily-update</code> 每日生成 · "
               f"{now:%Y-%m-%d %H:%M %Z}</footer></div>")
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>DeepSeek-V4.1 每日进展</title>"
            f"<style>{CSS}</style></head><body>" + "".join(out) + "</body></html>")
