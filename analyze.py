"""给每条 PR / issue 评一个初步的 P0 / P1 / P2。

走公司网关（OpenAI 兼容的 chat/completions）。

**不要用 `response_format: json_schema`** —— 实测这个网关会静默忽略它，照样返回
Markdown 表格，且不报错。只有 `{"type":"json_object"}` 是认的，所以形状靠提示词
约定 + 下面的兜底解析，不能靠 schema 保证。

评级只是初筛：模型只看得到标题和正文摘要，看不到 diff，也不知道我们线上跑的是
哪个配置。P0 的作用是「今天先看这几条」，不是「这几条一定出事」。

没配 LLM_API_KEY 时整个降级成规则打分，并在页面上标明「未经 AI 评估」，绝不假装。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

from common import is_severe, kind_of, repo_of, summarize

BASE_URL = os.environ.get("LLM_BASE_URL", "https://f7xnt9mg.fn.bytedance.net/v1")
MODEL = os.environ.get("LLM_MODEL", "gpt-6-astra")

SYSTEM = """\
你在为一支自建大模型推理服务的性能团队做上游情报初筛。他们在 NVIDIA GPU 上跑
DeepSeek-V4.1 系列的在线推理，主力并行形态是 EP / DP / PP 加 PD 分离，
默认开 CUDA Graph FULL，涉及投机解码、稀疏 MLA、KV cache、MoE 路由等路径。

给每条 GitHub PR 或 issue 评一个优先级。判据是「**对我们的服务**后果是什么」——
注意主语是我们：只在 ROCm / NPU / XPU / CPU 等我们不跑的后端上出现的故障，
不影响我们的服务，最高只给 P1。

P0 —— 影响**我们的**服务能否被**正确地 serve 起来**，两类都算，同级：
      · 静默算错 —— 输出损坏、数值错误、精度崩坏、非确定性输出、算子结果错、
        状态/位置/索引错位、聚合或路由算错
      · 服务不可用 —— 崩溃、卡死、死锁、OOM、启动失败、断言失败、
        加载或编译失败、接受率崩塌

P1 —— 不影响正确性和可用性，但值得跟：
      · 性能退化（**退化也是 P1，不是 P0**）、性能优化
      · 功能推进、新能力、新配置支持

P2 —— 文档、示例、CI、测试、重构、代码归属、日志、命名、依赖升级等杂务。

三条硬规则：

1. **不要因为「这条是某张 NVIDIA 卡专属」而升降级。** SM80 / SM90 / SM100 /
   H20 / Blackwell 只是标注，不同卡型是并行推进的，A100 上起不来和 B200 上
   起不来一样是 P0。这跟上面说的「非 NVIDIA 后端」是两回事：卡型是我们内部
   并行推进的几条线，ROCm / NPU / XPU 则完全不在我们的技术栈里。
2. **不要考虑这一批里有多少条 P0。** 你每次只看到全体的一小部分，按数量或
   比例控制必然失真。后果够格就是 P0。
3. 只有标题和正文摘要，看不到 diff。后果拿不准就往低了报。

校准样例：

P0  Fix DeepSeek-V4 routing: sqrtsoftplus underflow and unfloored renorm   → 数值下溢，路由权重算错
P0  [Bug] on 2x H200: progressive output corruption under concurrency      → 输出损坏
P0  [Spec] Budget the DFLASH/DSPARK draft KV pool by attn_tp_size          → 预算算错导致 OOM
P0  [Fix] Key DSpark compact ragged CUDA graphs by request-slot geometry   → 首次回放非法访存崩溃
P0  Fix/dsv4 pre sm90 sparse mla omnibus                                   → SM80 上起不来；卡型不降级
P0  [Bugfix][Parser] Fix DeepSeek V4 tool argument streaming               → 对外接口输出错乱，等同算错

P1  [ROCm] Fixing GLM-5.1, DeepSeek-V3.2, DeepSeek-V4 on gfx942 and gfx950 → 只在 AMD 上崩，我们不跑
P1  [NPU] Support DFlash speculative decoding for MiMo-V2.5-Pro            → 昇腾专属，不影响我们

P1  [Perf][DSpark] KV-only context insert and fused kv_norm                → 优化
P1  [Perf] v0.28.0 needs ~4 GiB/GPU more non-KV memory than v0.27          → 性能/显存退化，退化是 P1
P1  [Model] Support DeepSeek-V4.1-Flash                                    → 新能力
P1  refactor: streamline DeepSeek V4 mHC warmup and remove token-size cap  → 重构且无故障描述

P2  chore: add HiSparse coordinator and allocator code owners              → 杂务
P2  [CI][Ascend] Add debug-only nightly perf suite                         → CI

只输出 JSON，不要解释、不要代码围栏，形状固定为：
{"items":[{"id":"原样抄回输入里的 id","level":"P0|P1|P2","reason":"一句中文，说清后果是什么，不要复述标题"}]}
输入里的每一条都必须在 items 里出现一次。"""


def key_of(item: dict) -> str:
    return f"{repo_of(item)}#{item['number']}"


def _fallback(items: list[dict]) -> dict[str, dict]:
    """没凭据 / 调用失败时的规则打分。诚实地标成未经评估。"""
    out = {}
    for it in items:
        if "pull_request" not in it and is_severe(it):
            lvl, why = "P1", "标题命中高危词（规则判定，未经 AI 评估）"
        elif kind_of(it["title"]) == "fix":
            lvl, why = "P1", "缺陷修复（规则判定，未经 AI 评估）"
        else:
            lvl, why = "P2", "规则判定，未经 AI 评估"
        out[key_of(it)] = {"level": lvl, "reason": why, "ai": False}
    return out


def _extract_json(text: str) -> dict:
    """网关不保证纯 JSON，围栏和前后缀都见过，挖出最外层对象再解析。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, depth = text.find("{"), 0
    if start < 0:
        raise ValueError("no JSON object in response")
    for i in range(start, len(text)):
        depth += (text[i] == "{") - (text[i] == "}")
        if depth == 0:
            return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in response")


def _call(payload: list[dict], api_key: str, timeout: int = 180) -> dict:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content":
             f"给下面 {len(payload)} 条评级：\n"
             + json.dumps(payload, ensure_ascii=False)},
        ],
        # 只有 json_object 被这个网关认；json_schema 会被静默忽略
        "response_format": {"type": "json_object"},
        "max_tokens": 8000,
    }).encode()
    req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=body,
                                 method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return _extract_json(data["choices"][0]["message"]["content"])


def triage(items: list[dict], batch: int = 40) -> dict[str, dict]:
    """返回 {repo#num: {level, reason, ai}}。凭据缺失或调用失败即降级。"""
    if not items:
        return {}
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        print("analyze: 无 LLM_API_KEY，降级到规则打分", file=sys.stderr)
        return _fallback(items)

    valid = {"P0", "P1", "P2"}
    out: dict[str, dict] = {}
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        payload = [{"id": key_of(it),
                    "type": "issue" if "pull_request" not in it else "pr",
                    "title": it["title"],
                    "summary": summarize(it.get("body"), 400)} for it in chunk]
        try:
            rows = _call(payload, api_key).get("items", [])
        except Exception as e:
            print(f"analyze: 第 {i // batch + 1} 批失败（{e}），该批降级",
                  file=sys.stderr)
            out.update(_fallback(chunk))
            continue
        for row in rows:
            lvl = str(row.get("level", "")).upper()
            if lvl in valid and row.get("id"):
                out[row["id"]] = {"level": lvl,
                                  "reason": str(row.get("reason", "")).strip(),
                                  "ai": True}

    # 模型漏条目或抄错 id 都见过，补齐，不让页面出现没有等级的行
    missing = [it for it in items if key_of(it) not in out]
    if missing:
        print(f"analyze: {len(missing)} 条未被模型返回，补规则分", file=sys.stderr)
        out.update(_fallback(missing))
    return out
