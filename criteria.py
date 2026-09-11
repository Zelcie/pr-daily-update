"""优先级判定标准 —— 提示词和页面的**唯一**来源。

改这里，`analyze.py` 喂给模型的提示词和页面上「优先级判断标准」那一栏会同时变。
分开写两份的话它们一定会漂移：页面上挂着旧标准、模型按新标准判，
而且不会有任何报错。

`version()` 是标准内容的短哈希，会印在页面上 —— 用来回答「这批标签是哪版
标准判出来的」。标准一改哈希就变，历史页面的标签也就不能跟新页面直接比。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

# (档位, 一句话定义, [(小类, 具体表现)])
LEVELS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("P0", "影响**我们的**服务能否被**正确地 serve 起来**，两类同级", [
        ("静默算错",
         "输出损坏、数值错误、精度崩坏、非确定性输出、算子结果错、"
         "状态／位置／索引错位、聚合或路由算错"),
        ("服务不可用",
         "崩溃、卡死、死锁、OOM、启动失败、断言失败、加载或编译失败、接受率崩塌"),
    ]),
    ("P1", "不影响正确性和可用性，但值得跟", [
        ("性能", "性能退化（**退化也是 P1，不是 P0**）、性能优化"),
        ("能力", "功能推进、新能力、新配置支持"),
        ("与我们无关的故障",
         "只在 ROCm / NPU / XPU / CPU 等我们不跑的后端上出现的问题"),
    ]),
    ("P2", "知道就行", [
        ("杂务", "文档、示例、CI、测试、重构、代码归属、日志、命名、依赖升级"),
    ]),
]

# 判级时必须遵守的硬规则，同时也是这套标准最容易被问到的几个点
RULES: list[tuple[str, str]] = [
    ("只看后果，不看别的",
     "不看提交者、不看代码量、不看在哪张卡上。唯一的问题是「对我们的服务，"
     "后果是什么」。"),
    ("静默算错和崩溃同级",
     "崩溃会被健康检查抓到、会自动重启，损失的是可用性；静默算错监控看不见，"
     "用户拿到错答案可能几周后才有人发现。两者都必须当天看。"),
    ("卡型只做标注，不参与判级",
     "SM80 / SM90 / SM100 / H20 / Blackwell 只是标签。不同卡型是并行推进的"
     "几条线，A100 上起不来和 B200 上起不来同样是 P0。"),
    ("厂商后端封顶 P1",
     "ROCm / NPU / XPU / CPU 不在我们的技术栈里，那上面的崩溃不影响我们的服务。"
     "这与上一条不冲突：卡型是我们内部并行推进的分支，厂商后端是别人的路。"),
    ("不用任何数量或比例约束",
     "早期项目 PR 多本身是信号，不该被压。模型每次只看到全体的一小部分，"
     "按比例控制必然失真 —— 会在糟糕的日子藏掉 P0、在安静的日子提拔垃圾凑数。"),
]

# 校准样例。比任何形容词都管用 —— 光写「拿不准往低了报」模型不会照做。
EXAMPLES: list[tuple[str, str, str]] = [
    ("P0", "Fix DeepSeek-V4 routing: sqrtsoftplus underflow and unfloored renorm",
     "数值下溢，路由权重算错"),
    ("P0", "[Bug] on 2x H200: progressive output corruption under concurrency",
     "输出损坏"),
    ("P0", "[Spec] Budget the DFLASH/DSPARK draft KV pool by attn_tp_size",
     "预算算错导致 OOM"),
    ("P0", "[Fix] Key DSpark compact ragged CUDA graphs by request-slot geometry",
     "首次回放非法访存崩溃"),
    ("P0", "Fix/dsv4 pre sm90 sparse mla omnibus",
     "SM80 上起不来；卡型不降级"),
    ("P0", "[Bugfix][Parser] Fix DeepSeek V4 tool argument streaming",
     "对外接口输出错乱，等同算错"),
    ("P1", "[Perf][DSpark] KV-only context insert and fused kv_norm", "优化"),
    ("P1", "[Perf] v0.28.0 needs ~4 GiB/GPU more non-KV memory than v0.27",
     "性能／显存退化，退化是 P1"),
    ("P1", "[Model] Support DeepSeek-V4.1-Flash", "新能力"),
    ("P1", "refactor: streamline DeepSeek V4 mHC warmup", "重构且无故障描述"),
    ("P1", "[ROCm] Fixing GLM-5.1, DeepSeek-V3.2, DeepSeek-V4 on gfx942/gfx950",
     "只在 AMD 上崩，我们不跑"),
    ("P1", "[NPU] Support DFlash speculative decoding for MiMo-V2.5-Pro",
     "昇腾专属，不影响我们"),
    ("P2", "chore: add HiSparse coordinator and allocator code owners", "杂务"),
    ("P2", "[CI][Ascend] Add debug-only nightly perf suite", "CI"),
]


def prompt_block() -> str:
    """渲染进 system prompt 的那一段。"""
    out = ["给每条 GitHub PR 或 issue 评一个优先级。判据是「**对我们的服务**"
           "后果是什么」——注意主语是我们。\n"]
    for lvl, one_liner, groups in LEVELS:
        out.append(f"{lvl} —— {one_liner}：")
        for name, detail in groups:
            out.append(f"      · {name} —— {detail}")
        out.append("")
    out.append("硬规则：\n")
    for i, (title, body) in enumerate(RULES, 1):
        out.append(f"{i}. **{title}。** {body}")
    out.append("\n只有标题和正文摘要，看不到 diff。后果拿不准就往低了报。\n")
    out.append("校准样例：\n")
    for lvl, title, why in EXAMPLES:
        out.append(f"{lvl}  {title}\n    → {why}")
    return "\n".join(out)


def version() -> str:
    """标准内容的短哈希。改了标准这个值就变。"""
    return hashlib.sha256(prompt_block().encode()).hexdigest()[:8]


# ---------------------------------------------------------------- 可编辑来源

REPO = os.environ.get("GITHUB_REPOSITORY", "Zelcie/pr-daily-update")


def _fetch_issue(number: str, token: str | None) -> str:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/issues/{number}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "pr-daily-update")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return (json.load(resp).get("body") or "").strip()


_cache: dict | None = None


def active(token: str | None = None) -> dict:
    """当前生效的判定标准。

    优先用 CRITERIA_ISSUE 指向的那个 issue 的正文 —— 那就是「输入栏」：在浏览器里
    改完保存，下次运行就生效，不用碰代码。取不到就退回本文件里的内置版本。

    注意 issue 正文会整段进 system prompt。只有仓库协作者能编辑正文，但这仍然是
    一条能改变判级行为的通道，别对陌生人开放写权限。
    """
    global _cache
    if _cache is not None:
        return _cache

    num = os.environ.get("CRITERIA_ISSUE", "").strip().lstrip("#")
    text, source, url = prompt_block(), "内置（criteria.py）", (
        f"https://github.com/{REPO}/edit/main/criteria.py")
    if num:
        try:
            body = _fetch_issue(num, token)
            if body:
                text = body
                source = f"Issue #{num}"
                url = f"https://github.com/{REPO}/issues/{num}"
            else:
                print(f"criteria: issue #{num} 正文为空，用内置标准",
                      file=sys.stderr)
        except Exception as e:
            print(f"criteria: 读不到 issue #{num}（{e}），用内置标准",
                  file=sys.stderr)

    _cache = {
        "text": text,
        "source": source,
        "url": url,
        "version": hashlib.sha256(text.encode()).hexdigest()[:8],
        "file_url": f"https://github.com/{REPO}/edit/main/criteria.py",
    }
    return _cache
