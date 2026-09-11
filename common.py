"""抓取 + 分类。watch.py 和 page.py 共用。"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

REPOS = ["vllm-project/vllm", "sgl-project/sglang"]

# GitHub 的 issue search 把多个词按 AND 处理，不支持 OR，所以每个词单独搜一次再合并。
# 只搜标题：实测 `deepseek in:title,body` 七天回来 717 条，绝大多数是 PR 模板里
# 顺口提一句 deepseek，还会把 `[HiCache] ... NAME_MAX` 误判成 V4.1 命中；
# 换成 in:title 七天 177 条，噪声基本消失。
SEARCH_TERMS = ["deepseek", "dsv4", "engram", "dspark", "hisparse", "dflash"]

GITHUB_SEARCH = "https://api.github.com/search/issues"

# ---------------------------------------------------------------- 组件

def _w(*alts: str) -> re.Pattern:
    """词边界匹配，但把下划线当分隔符。

    `\b` 认为下划线是单词字符，于是 `\bmoe\b` 匹配不上 `fused_moe_triton`、
    `\byarn\b` 匹配不上 `deepseek_yarn` —— 这两条实测都因此掉进了兜底桶。
    """
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])", re.I)


# 顺序即优先级，第一个命中的赢。一条 PR 只归一个大类。
# 排序按暴露面：EP / DP / PP 相关的排前面，TP / CP 靠后 —— 这只影响页面上
# 大类的先后，不影响 P0/P1/P2 的判定，那个只看「能不能正确 serve 起来」。
# 非 NVIDIA 后端单独成类沉底：不是说它们不重要，是我们不跑。
COMPONENTS: list[tuple[str, str, re.Pattern]] = [
    ("otherhw", "🔌 非 NVIDIA 后端", _w(
        "rocm", "amd", "npu", "ascend", "cann", "xpu", "tpu", "hpu", "gaudi",
        "gfx\\d+", "maca", "intel", "cpu")),
    ("engram", "🧬 Engram", _w("engram")),
    ("spec", "🚀 投机解码", _w(
        "dspark", "dflash", "spec", "speculative", "mtp", "eagle",
        "draft", "drafts", "drafter", "drafters", "d2t", "domino")),
    ("kv", "💾 KV Cache", _w(
        "hisparse", "hicache", "unified[ _-]?cache", "kv[ _-]?cache",
        "block[ _-]?pool", "kv[ _-]?connector", "page", "paged", "radix",
        "prefix[ _-]?cache", "kv[ _-]?writer", "unified_kv", "kv")),
    ("mla", "🎯 稀疏注意力 / MLA", _w(
        "mla", "sparse[ _-]?attention", "sparse", "indexer", "mqa[ _-]?logits",
        "dsa", "mhc", "swa", "yarn", "nope",
        "sparse[ _-]?mla", "attention[ _-]?metadata", "flashmla")),
    # EP / DP / PP + PD 分离 —— 我们的主力并行形态，排在 TP/CP 前面
    # 缩写后面常直接跟并行度（TP4 / DP16 / EP8），所以每个都带 \d* ——
    # 不带的话 `_w("dp")` 匹配不上 `DP8`，这一类会整片漏掉
    ("edp", "🔀 EP / DP / PP · PD 分离", _w(
        r"ep\d*", r"dp\d*", r"pp\d*", r"dcp\d*", r"pcp\d*", r"edp\d*", "dpa",
        "expert[ _-]?parallel", "data[ _-]?parallel", "pipeline[ _-]?parallel",
        "disaggregation", "disaggregated", "disagg", "pdmux", r"\d+p\d+d", "pd")),
    ("moe", "🧠 MoE / 路由", _w(
        "moe", "expert", "experts", "deepep", "router", "routing", "gemm")),
    ("quant", "🔢 量化", _w(
        "fp8", "fp4", "nvfp4", "mxfp4", "gptq", "autoround", "quantized",
        "quantization", "quantize", "wna16", "int8", "awq")),
    ("kernel", "⚙️ Kernel / 融合", _w(
        "kernel", "kernels", "triton", "cutlass", "fusion", "fusions", "fused",
        "csa", "aiter", "sgl[ _-]?kernel", "cuda[ _-]?graph", "cudagraph")),
    # TP / CP / SP —— 暴露面较低，沉到 EP/DP/PP 后面
    ("tcp", "🔗 TP / CP / SP 并行", _w(
        r"tp\d*", r"cp\d*", r"sp\d*", "tensor[ _-]?parallel",
        "context[ _-]?parallel", "sequence[ _-]?parallel")),
    ("frontend", "🗣 前端 / 解析", _w(
        "parser", "parse", "parsing", "tool[ _-]?call", "tool", "chat",
        "encoder", "frontend", "renderer", "structural[ _-]?tag", "reasoning",
        "tokenizer", "dsml", "responses", "streaming", "api")),
    ("model", "🧱 模型接入", _w(
        "model", "models", "checkpoint", "checkpoints", "multimodal", "vision",
        "definitions", "backend")),
]
COMPONENT_FALLBACK = ("misc", "📦 其他")

# 卡型只做标注，不参与优先级 —— 不同卡型是并行推进的，SM80 上起不来和 SM100 上
# 起不来同样是缺陷，不该因为「我们主力不是那张卡」就降级。
CARDS: list[tuple[str, re.Pattern]] = [
    ("Blackwell/SM100", _w("blackwell", "sm100", "sm10x", "b200", "gb200", "b300")),
    ("Hopper/SM90", _w("hopper", "sm90", "sm9x", "h100", "h200", "h20", "h800")),
    ("Ada/SM89", _w("ada", "sm89", "l40", "l40s", "rtx")),
    ("Ampere/SM80", _w("ampere", "sm80", "sm86", "a100", "a800", "pre[ _-]?sm90")),
    ("SM120", _w("sm120", "sm12x")),
]


def card_of(title: str) -> str:
    hits = [name for name, pat in CARDS if pat.search(title)]
    return " / ".join(hits[:2])


def component_of(title: str) -> str:
    for key, _label, pat in COMPONENTS:
        if pat.search(title):
            return key
    return COMPONENT_FALLBACK[0]


COMPONENT_LABELS = {k: lab for k, lab, _ in COMPONENTS}
COMPONENT_LABELS[COMPONENT_FALLBACK[0]] = COMPONENT_FALLBACK[1]
COMPONENT_ORDER = [k for k, _, _ in COMPONENTS] + [COMPONENT_FALLBACK[0]]

# ---------------------------------------------------------------- 类型

# 顺序即优先级：修正 > 性能 > 工程 > 硬件 > 功能。
# 不是随手排的：`[AMD] Fix HiSparse slot translation` 该算修正而不是硬件适配，
# `[AMD][DI][CI] Add nightly recipes` 该算工程而不是硬件适配 ——「在干什么」比
# 「在哪个后端干」更能说明上游的投入方向。
# 方括号标签最可信，先认标签，没打标签的再看正文关键词。
KINDS: list[tuple[str, str, re.Pattern]] = [
    ("fix", "🛠 修正", re.compile(
        r"\[(bug ?fix|fix|hotfix|bug)\]"
        r"|\b(fix|fixes|fixed|fixing|bug|broken|regression|crash|hang|deadlock"
        r"|correct|incorrect|wrong|mismatch|underflow|overflow)\b", re.I)),
    ("perf", "⚡ 性能", re.compile(
        r"\[(perf|performance|speed)\w*\]"
        r"|\b(perf|optimiz|speed ?up|faster|latency|throughput|overlap"
        r"|accelerat|fusion|fused)\w*\b|\+\d+ ?%", re.I)),
    ("infra", "🔧 工程", re.compile(
        r"\[(ci|test|tests|chore|refactor|docs?|build|bench\w*|cookbook)\]"
        r"|\b(ci|unittest|unit test|nightly|chore|refactor|migrate|migration"
        r"|cleanup|clean up|dedup|rename|docs|documentation|metrics|logging"
        r"|observability|code ?owners|lint|typo|cookbook|example|playground)\b", re.I)),
    ("hw", "🔌 硬件适配", re.compile(
        r"\b(rocm|amd|npu|ascend|cann|xpu|tpu|hpu|gaudi|gfx\d+|aiter|maca"
        r"|cpu|arm64|aarch64|sm\d{2,3}|blackwell|hopper|ampere|ada|intel)\b", re.I)),
    ("feat", "✨ 功能", re.compile(
        r"\bfeat\b|\[(model|frontend|feature)\]"
        r"|\b(support|supports|supported|enable|enables|add|adds|added"
        r"|introduce|integrate|expose|implement|allow)\w*\b", re.I)),
]
KIND_FALLBACK = ("other", "· 其他")


def kind_of(title: str) -> str:
    for key, _label, pat in KINDS:
        if pat.search(title):
            return key
    return KIND_FALLBACK[0]


KIND_LABELS = {k: lab for k, lab, _ in KINDS}
KIND_LABELS[KIND_FALLBACK[0]] = KIND_FALLBACK[1]
KIND_ORDER = [k for k, _, _ in KINDS] + [KIND_FALLBACK[0]]

# ---------------------------------------------------------------- V4.1 相关度

# 命名很乱，v4.1 / v4_1 / v41 / dsv41 都有人写。上游换写法的话这里会静默
# 掉到 0 而不报错 —— 卡片连着几天「无更新」就该来查这条正则。
RE_V41 = re.compile(
    r"deepseek[\s\-_.]*v?4[\s\-_.]*1|\bds[\s\-_.]*v?4[\s\-_.]*1\b"
    r"|\bv4[._]1\b|\bdeepseek_?v41\b", re.I)
# Engram 实测几乎只跟 V4.1 一起出现，专一度够高，算直接相关。
RE_ENGRAM = re.compile(r"\bengram\b", re.I)


def is_v41(title: str) -> bool:
    return bool(RE_V41.search(title) or RE_ENGRAM.search(title))


# ---------------------------------------------------------------- 抓取

def _search(term: str, since: str, token: str | None,
            kind: str = "pr") -> list[dict]:
    q = " ".join([*(f"repo:{r}" for r in REPOS), f"is:{kind}",
                  f"{term} in:title", f"updated:>={since}"])
    params = urllib.parse.urlencode(
        {"q": q, "sort": "updated", "order": "desc", "per_page": "100"})
    req = urllib.request.Request(f"{GITHUB_SEARCH}?{params}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "dsv41-watch")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp).get("items", [])


def collect(since: datetime, token: str | None,
            kind: str = "pr") -> list[dict]:
    """每个关键词单独搜一次再按 id 合并去重。"""
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    merged: dict[int, dict] = {}
    for i, term in enumerate(SEARCH_TERMS):
        if i and not token:
            time.sleep(7)  # 未认证时只有 10 次/分钟，隔开一点免得吃 403
        for item in _search(term, stamp, token, kind=kind):
            merged[item["id"]] = item
    return sorted(merged.values(), key=lambda x: x["updated_at"], reverse=True)


# ---------------------------------------------------------------- issue 筛选

# issue 噪声极大，三层筛。vllm/sglang 的 issue 模板会强制打标题前缀，
# 实测 100 条里 [Bug] 72 / [Feature] 12 / [Perf] 5 / [Usage] 3，前缀够可靠。

# 只留缺陷类。Feature/RFC 是路线图不是风险，Usage 是用法提问。
ISSUE_DEFECT = re.compile(r"^\s*\[(bug|perf|performance)\b", re.I)
# 机器人自动标记的僵尸 issue
ISSUE_DEAD = {"stale", "inactive"}
# 会咬到线上的那一类：算错、卡死、崩、退化。区别于「我想要个功能」。
ISSUE_SEVERE = re.compile(
    r"corrupt|wrong|incorrect|non-?deterministi|mismatch|garbage|degenerate"
    r"|hang|deadlock|stuck|crash|illegal memory|assert|segfault|oom"
    r"|out of memory|\bnan\b|accuracy|regress|leak|fail", re.I)


def keep_issue(item: dict) -> bool:
    if item.get("state") != "open":
        return False
    if {l["name"].lower() for l in item.get("labels", [])} & ISSUE_DEAD:
        return False
    return bool(ISSUE_DEFECT.match(item["title"]))


def issue_score(item: dict) -> int:
    """高危词值 3 分，再叠热度。排序用，不是绝对严重度。"""
    return (3 * bool(ISSUE_SEVERE.search(item["title"]))
            + item.get("comments", 0)
            + 2 * item.get("reactions", {}).get("total_count", 0))


def is_severe(item: dict) -> bool:
    return bool(ISSUE_SEVERE.search(item["title"]))


def collect_issues(since: datetime, token: str | None) -> list[dict]:
    """筛过的未解决缺陷类 issue，按分数降序。

    窗口比 PR 长：一个还开着的缺陷，三天前报的和今天报的一样会咬人，
    不该因为今天没人评论就从风险板上消失。
    """
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    merged: dict[int, dict] = {}
    for i, term in enumerate(SEARCH_TERMS):
        if i and not token:
            time.sleep(7)
        for item in _search(term, stamp, token, kind="issue"):
            if keep_issue(item):
                merged[item["id"]] = item
    return sorted(merged.values(), key=issue_score, reverse=True)


def state_of(item: dict) -> tuple[str, str]:
    """(emoji, 状态词)。search 结果里 merged_at 挂在 pull_request 下，不在顶层。"""
    if (item.get("pull_request") or {}).get("merged_at"):
        return "🟣", "merged"
    if item.get("state") == "closed":
        return "🔴", "closed"
    if item.get("draft"):
        return "⚪", "draft"
    return "🟢", "open"


def repo_of(item: dict) -> str:
    name = item["repository_url"].rsplit("/", 1)[-1]
    return {"vllm": "vLLM", "sglang": "SGLang"}.get(name, name)


# ---------------------------------------------------------------- 摘要

# PR 正文前面通常压着一大坨模板：HTML 注释、Purpose/Test Plan 标题、勾选框、
# Signed-off-by。这些先剥掉，剩下的第一段真话才是摘要。
_STRIP = [
    (re.compile(r"<!--.*?-->", re.S), " "),
    (re.compile(r"^\s*(-|\*)\s*\[[ xX]\].*$", re.M), " "),          # 勾选框
    (re.compile(r"^\s*#{1,6}\s*(purpose|test plan|test result|checklist|"
                r"modifications?|motivation|背景|测试|目的).*$", re.I | re.M), " "),
    (re.compile(r"^\s*(signed-off-by|co-authored-by|cc\s*@).*$", re.I | re.M), " "),
    (re.compile(r"```.*?```", re.S), " "),                            # 代码块
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),                       # 图片
    (re.compile(r"</?[a-z][^>]*>", re.I), " "),                       # 裸 HTML
    # 只吃行首的标题符号 —— 全局替换会把正文里的 issue 引用 `#56022` 也啃成 `56022`
    (re.compile(r"^\s*#{1,6}\s*", re.M), " "),
]

# 模板小标题被上面剥掉标题符号后会剩下光秃秃的词，开头这几个直接丢
_LEAD = re.compile(
    r"^(summary|motivation|description|purpose|overview|背景|摘要|说明)"
    r"[\s:&·、,，-]*", re.I)


def summarize(body: str | None, limit: int = 260) -> str:
    if not body:
        return ""
    s = body
    for pat, rep in _STRIP:
        s = pat.sub(rep, s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(3):                       # "Summary & Motivation" 会叠两层
        s2 = _LEAD.sub("", s, count=1).strip()
        if s2 == s:
            break
        s = s2
    if len(s) <= limit:
        return s
    cut = s[:limit]
    # 尽量断在句子边界，断不了就退回词边界
    for sep in (". ", "。", "; "):
        if (i := cut.rfind(sep)) > limit * 0.5:
            return cut[:i + 1].strip()
    return cut.rsplit(" ", 1)[0].strip() + " …"


# ---------------------------------------------------------------- 有效更新

# Search API 的 updated_at 被任何活动顶起来 —— 机器人评论、加个标签、点个赞
# 都算，所以「今天有更新」里混着大量没实质进展的条目。真正的进展要看 timeline。
# 「有进展」和「有实质进展」是两回事。Search 的 updated_at 被任何活动顶起来，
# 而 timeline 里的活动也大半不是进展 —— 实测人写的评论中位数是 **7 个字符**，
# 内容是 `/ci run`（人手敲的 CI 触发命令，按用户名过滤机器人拦不住它）。
#
# 所以 PR 和 issue 分开判：
#   PR   —— 只认代码动了或状态变了。讨论不算进展。
#   issue —— 没有 commit 可看，评论就是唯一信号，但要求有实质内容。

# 代码真的动了
CODE_EVENTS = {"committed", "head_ref_force_pushed"}
# 状态真的变了
STATE_EVENTS = {"merged", "closed", "reopened", "ready_for_review",
                "convert_to_draft"}
# 有结论的 review 才算；单纯 COMMENTED 的 review 归到讨论里
REVIEW_VERDICTS = {"approved", "changes_requested"}

# 机器人账号
BOT_ACTORS = re.compile(r"\[bot\]$|^(github-actions|codecov|mergify|dependabot"
                        r"|pre-commit-ci|sourcery-ai|coderabbitai)$", re.I)
# 人手敲的机器命令：/ci run、/retest、/lgtm、/approve…
SLASH_CMD = re.compile(r"^\s*/[a-z][\w-]*(\s|$)", re.I)
# 低于这个长度的评论当寒暄。实测评论长度 p25=7、p50=7、p75=74，
# 40 落在那堆 `/ci run` 之上、真正讨论之下。
MIN_COMMENT = 40


def _substantive_comment(ev: dict) -> bool:
    body = (ev.get("body") or "").strip()
    if not body or SLASH_CMD.match(body):
        return False
    return len(body) >= MIN_COMMENT


def _last_page_url(url: str, token: str | None) -> tuple[list, str | None]:
    """取第一页，同时从 Link 头里挖出末页地址。

    timeline 是按时间升序返回的，长 PR 的近期事件都在最后一页 —— 只读第一页
    会把活跃 PR 全判成「没进展」，方向正好反了。
    """
    req = urllib.request.Request(url + ("&" if "?" in url else "?") + "per_page=100")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "dsv41-watch")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
        link = resp.headers.get("Link", "")
    m = re.search(r'<([^>]+)>;\s*rel="last"', link)
    return data, (m.group(1) if m else None)


def _fetch_timeline(item: dict, token: str | None) -> list:
    try:
        events, last = _last_page_url(item["timeline_url"], token)
        if last:
            events2, _ = _last_page_url(last, token)
            return events2
        return events
    except Exception:
        return []      # 单条失败不该拖垮整轮，退化成「按 updated_at 算有更新」


def effective_events(item: dict, since: datetime,
                     token: str | None) -> list[str]:
    """窗口内的**实质**进展。空列表 = 只是被顶了一下。"""
    is_pr = "pull_request" in item
    out = []
    for ev in _fetch_timeline(item, token):
        kind = ev.get("event")
        ts = (ev.get("created_at")
              or (ev.get("committer") or {}).get("date")
              or (ev.get("author") or {}).get("date"))
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < since:
            continue
        actor = ((ev.get("actor") or {}).get("login")
                 or (ev.get("user") or {}).get("login") or "")
        if actor and BOT_ACTORS.search(actor):
            continue

        if kind in CODE_EVENTS or kind in STATE_EVENTS:
            out.append(kind)
        elif kind == "reviewed":
            if str(ev.get("state", "")).lower() in REVIEW_VERDICTS:
                out.append("reviewed")
        elif kind == "commented" and not is_pr and _substantive_comment(ev):
            # issue 没有 commit 可看，一条像样的讨论就是进展；PR 不吃这套
            out.append("discussed")
    return out


def is_new(item: dict, since: datetime) -> bool:
    """这一条是不是窗口内新建的。

    搜索窗口卡的是 updated_at，所以「今天有更新」里混着大量存量 —— 实测 42%
    超过一周、18% 超过一个月，最老的 289 天。「今天新冒出来的 P0」和「挂了三周
    的 P0」行动完全不同：前者立刻看，后者该排期。
    """
    try:
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    return created >= since


def filter_effective(items: list[dict], since: datetime,
                     token: str | None, workers: int = 8) -> list[dict]:
    """只留窗口内**新建的**或**有实质进展的**。

    新建无条件保留 —— 它本身就是最强的进展信号，不该再要求 timeline 证明。
    这一条很要紧：今天刚开的 issue 往往一条评论都没有，timeline 是空的，
    只看事件会把它整个丢掉，而那恰恰是最该看的东西。

    没 token 就不筛 —— 未认证 60 次/小时，一轮上百个 timeline 请求必然 403，
    宁可不筛也别筛出个空列表。
    """
    if not token:
        for it in items:
            it["_events"] = []
            it["_fresh"] = is_new(it, since)
        return items

    from concurrent.futures import ThreadPoolExecutor

    fresh = [is_new(it, since) for it in items]
    # 新建的不必查 timeline，省掉一批请求
    need = [it for it, f in zip(items, fresh) if not f]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        got = dict(zip((id(x) for x in need),
                       pool.map(lambda i: effective_events(i, since, token), need)))

    kept = []
    for it, f in zip(items, fresh):
        it["_fresh"] = f
        it["_events"] = ["created"] if f else got.get(id(it), [])
        if f or it["_events"]:
            kept.append(it)
    return kept
