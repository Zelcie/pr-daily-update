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


# 顺序即优先级，第一个命中的赢。一条 PR 只归一个组件，否则矩阵会重复计数。
# 越具体的排越前：KV Cache 在稀疏注意力前面，因为 `Fix HiSparse slot translation
# in the fused MLA KV writer` 主体是 HiSparse 不是 MLA；「模型接入」几乎垫底，
# 因为 [Model] 标签太常见，放前面会把所有东西吸走。
COMPONENTS: list[tuple[str, str, re.Pattern]] = [
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
        "dsa", "mhc", "swa", "yarn", "nope", "attention")),
    ("moe", "🧠 MoE / 路由", _w(
        "moe", "expert", "experts", "deepep", "router", "routing", "gemm")),
    ("quant", "🔢 量化", _w(
        "fp8", "fp4", "nvfp4", "mxfp4", "gptq", "autoround", "quantized",
        "quantization", "quantize", "wna16", "int8", "awq")),
    ("parallel", "🔀 并行 / PD 分离", _w(
        "pp", "dp", "tp", "pcp", "dcp", "ep", "pipeline[ _-]?parallel",
        "sequence[ _-]?parallel", "tensor[ _-]?parallel", "data[ _-]?parallel",
        "disaggregation", "disaggregated", "disagg", "pdmux", r"\d+p\d+d", "pd")),
    ("frontend", "🗣 前端 / 解析", _w(
        "parser", "parse", "parsing", "tool[ _-]?call", "tool", "chat",
        "encoder", "frontend", "renderer", "structural[ _-]?tag", "reasoning",
        "tokenizer", "dsml", "responses", "streaming", "api", "router")),
    ("kernel", "⚙️ Kernel / 融合", _w(
        "kernel", "kernels", "triton", "cutlass", "fusion", "fusions", "fused",
        "csa", "aiter", "sgl[ _-]?kernel", "cuda[ _-]?graph")),
    ("model", "🧱 模型接入", _w(
        "model", "models", "checkpoint", "checkpoints", "multimodal", "vision",
        "definitions", "backend")),
]
COMPONENT_FALLBACK = ("misc", "📦 其他")


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

def _search(term: str, since: str, token: str | None) -> list[dict]:
    q = " ".join([*(f"repo:{r}" for r in REPOS), "is:pr",
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


def collect(since: datetime, token: str | None) -> list[dict]:
    """每个关键词单独搜一次再按 PR id 合并去重。"""
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    merged: dict[int, dict] = {}
    for i, term in enumerate(SEARCH_TERMS):
        if i and not token:
            time.sleep(7)  # 未认证时只有 10 次/分钟，隔开一点免得吃 403
        for item in _search(term, stamp, token):
            merged[item["id"]] = item
    return sorted(merged.values(), key=lambda x: x["updated_at"], reverse=True)


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
