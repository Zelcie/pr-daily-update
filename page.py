"""把分好类的 PR 渲染成一页静态 HTML，供 GitHub Pages 托管。

卡片上的每个「组件 × 类型」格子都链到这里的锚点，锚点 id 形如 `spec-fix`。
"""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from common import (COMPONENT_LABELS, COMPONENT_ORDER, KIND_LABELS, KIND_ORDER,
                    component_of, is_v41, kind_of, repo_of, state_of, summarize)

CSS = """
:root{
  --bg:#fbfaf8; --panel:#fff; --ink:#1c1b19; --muted:#6b6862; --line:#e5e1da;
  --accent:#2d6ae0; --v41:#7c3aed; --v41-bg:#f3ebff;
  --open:#1a7f37; --draft:#8b8680; --merged:#8250df; --closed:#cf222e;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#16151a; --panel:#1e1d24; --ink:#e8e6e3; --muted:#9a958d; --line:#312f38;
  --accent:#6f9bff; --v41:#b18aff; --v41-bg:#2a2140;
  --open:#3fb950; --draft:#8b8680; --merged:#a371f7; --closed:#f85149;
}}
:root[data-theme="dark"]{
  --bg:#16151a; --panel:#1e1d24; --ink:#e8e6e3; --muted:#9a958d; --line:#312f38;
  --accent:#6f9bff; --v41:#b18aff; --v41-bg:#2a2140;
  --open:#3fb950; --draft:#8b8680; --merged:#a371f7; --closed:#f85149;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",system-ui,
  "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin-bottom:28px}
.matrix{overflow-x:auto;margin-bottom:36px;border:1px solid var(--line);
  border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;min-width:660px;font-size:13px}
th,td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--line);
  white-space:nowrap}
th:first-child,td:first-child{text-align:left;font-weight:600}
thead th{color:var(--muted);font-weight:500;position:sticky;top:0;
  background:var(--panel)}
tbody tr:last-child td{border-bottom:none}
tfoot td{font-weight:600;border-top:2px solid var(--line);border-bottom:none}
td a{color:var(--accent);text-decoration:none;font-variant-numeric:tabular-nums;
  display:inline-block;min-width:1.4em}
td a:hover{text-decoration:underline}
td .zero{color:var(--line)}
h2{font-size:19px;margin:40px 0 2px;padding-top:12px;border-top:1px solid var(--line)}
h3{font-size:15px;margin:22px 0 10px;color:var(--muted);font-weight:600;
  scroll-margin-top:16px}
.pr{background:var(--panel);border:1px solid var(--line);border-radius:9px;
  padding:12px 14px;margin-bottom:9px}
.pr.v41{border-left:3px solid var(--v41)}
.pr-head{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;margin-bottom:3px}
.pr-head a{color:var(--ink);text-decoration:none;font-weight:600}
.pr-head a:hover{color:var(--accent);text-decoration:underline}
.tag{font-size:11px;padding:1px 7px;border-radius:20px;border:1px solid var(--line);
  color:var(--muted);white-space:nowrap}
.tag.v41{background:var(--v41-bg);color:var(--v41);border-color:transparent;
  font-weight:600}
.st-open{color:var(--open)} .st-draft{color:var(--draft)}
.st-merged{color:var(--merged)} .st-closed{color:var(--closed)}
.meta{font-size:12px;color:var(--muted)}
.sum{font-size:13px;color:var(--muted);margin-top:6px;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.empty{color:var(--muted);font-size:13px;font-style:italic}
footer{margin-top:56px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12px}
"""


def _cell(comp: str, kind: str, n: int) -> str:
    if not n:
        return '<span class="zero">·</span>'
    return f'<a href="#{comp}-{kind}">{n}</a>'


def render(items: list[dict], since: datetime, tz: ZoneInfo) -> str:
    grid: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for it in items:
        grid[component_of(it["title"])][kind_of(it["title"])].append(it)

    now = datetime.now(tz)
    v41_total = sum(1 for i in items if is_v41(i["title"]))
    out: list[str] = []

    out.append('<div class="wrap">')
    out.append("<h1>DeepSeek-V4.1 · vLLM / SGLang PR 追踪</h1>")
    out.append(f'<div class="sub">窗口 {since.astimezone(tz):%Y-%m-%d %H:%M} → '
               f'{now:%Y-%m-%d %H:%M} ({tz.key}) · 共 {len(items)} 条，'
               f'其中标题直指 V4.1 <strong>{v41_total}</strong> 条 · '
               f'点表格里的数字跳到对应小节</div>')

    # ---- 汇总矩阵
    out.append('<div class="matrix"><table><thead><tr><th>组件</th>')
    for k in KIND_ORDER:
        out.append(f"<th>{html.escape(KIND_LABELS[k])}</th>")
    out.append("<th>合计</th></tr></thead><tbody>")
    totals: Counter = Counter()
    for c in COMPONENT_ORDER:
        if not grid[c]:
            continue
        row = {k: len(grid[c][k]) for k in KIND_ORDER}
        totals.update(row)
        out.append(f"<tr><td>{html.escape(COMPONENT_LABELS[c])}</td>")
        out += [f"<td>{_cell(c, k, row[k])}</td>" for k in KIND_ORDER]
        out.append(f"<td>{sum(row.values())}</td></tr>")
    out.append("</tbody><tfoot><tr><td>合计</td>")
    out += [f"<td>{totals[k]}</td>" for k in KIND_ORDER]
    out.append(f"<td>{sum(totals.values())}</td></tr></tfoot></table></div>")

    # ---- 明细
    for c in COMPONENT_ORDER:
        if not grid[c]:
            continue
        out.append(f'<h2 id="{c}">{html.escape(COMPONENT_LABELS[c])}</h2>')
        for k in KIND_ORDER:
            bucket = grid[c][k]
            if not bucket:
                continue
            out.append(f'<h3 id="{c}-{k}">{html.escape(KIND_LABELS[k])} · '
                       f"{len(bucket)} 条</h3>")
            for it in bucket:
                emoji, st = state_of(it)
                v41 = is_v41(it["title"])
                updated = datetime.fromisoformat(
                    it["updated_at"].replace("Z", "+00:00")).astimezone(tz)
                out.append(f'<div class="pr{" v41" if v41 else ""}">')
                out.append('<div class="pr-head">')
                out.append(f'<a href="{html.escape(it["html_url"])}" '
                           f'target="_blank" rel="noopener">'
                           f'{html.escape(repo_of(it))} #{it["number"]} · '
                           f'{html.escape(it["title"])}</a>')
                out.append(f'<span class="tag st-{st}">{emoji} {st}</span>')
                if v41:
                    out.append('<span class="tag v41">V4.1</span>')
                out.append("</div>")
                out.append(f'<div class="meta">@{html.escape(it["user"]["login"])} · '
                           f"更新于 {updated:%m-%d %H:%M}</div>")
                if (s := summarize(it.get("body"))):
                    out.append(f'<div class="sum">{html.escape(s)}</div>')
                out.append("</div>")

    out.append(f"<footer>由 <code>dsv41-watch</code> 每日生成 · "
               f"生成于 {now:%Y-%m-%d %H:%M %Z}<br>"
               f"分类优先级：组件按 Engram → 投机解码 → KV → MLA → MoE → 量化 → "
               f"并行 → 前端 → Kernel → 模型接入 排序，"
               f"类型按 修正 → 性能 → 工程 → 硬件 → 功能 排序，第一个命中的赢。"
               f"</footer></div>")

    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>DeepSeek-V4.1 PR 追踪</title>"
            f"<style>{CSS}</style></head><body>" + "".join(out) + "</body></html>")
