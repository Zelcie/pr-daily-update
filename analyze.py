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

import criteria
from common import is_severe, kind_of, repo_of, summarize

# 两个 backend：
#   anthropic —— 官方 SDK，凭据来自 ANTHROPIC_API_KEY 或 `ant auth login` 的
#                profile（裸 Anthropic() 会自己找，不用显式传）
#   openai    —— 任何 OpenAI 兼容网关（火山方舟、内网网关…），走 LLM_API_KEY
PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

# anthropic backend
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
# 判级是批量分类，但判据本身不简单（P0 的线调了五轮才稳），所以给中等 effort
EFFORT = os.environ.get("TRIAGE_EFFORT", "medium")

# openai 兼容 backend。注意方舟的 key 绑区域：这把新加坡的 key 在
# cn-beijing 端点报 "The API key doesn't exist"，看起来像 key 作废。
BASE_URL = os.environ.get(
    "LLM_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3")
MODEL = os.environ.get("LLM_MODEL", "seed-sc-260628")

# 并非所有模型都吃 json_schema：实测 seed-sc 支持，glm-5-2-260710 直接返回
# InvalidParameter。所以先试严格 schema，被拒就退回 json_object，两个都失败
# 还有 _extract_json 兜底。
SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"},
                       "level": {"type": "string", "enum": ["P0", "P1", "P2"]},
                       "reason": {"type": "string"}},
        "required": ["id", "level", "reason"],
        "additionalProperties": False}}},
    "required": ["items"],
    "additionalProperties": False,
}

_PREFACE = """\
你在为一支自建大模型推理服务的性能团队做上游情报初筛。

他们在 NVIDIA GPU（B200 / GB200 / B300 为主）上跑在线推理，在跑的模型是
**DeepSeek-V4.1 系列、GLM-5.x、Kimi-K3**。主力并行形态是 EP / DP / PP 加
PD 分离，默认开 CUDA Graph FULL。

他们盯的推理路径组件：稀疏 MLA 与 indexer、KV cache（含 FP4/FP8 KV）、
投机解码（DSpark / DFlash / MTP）、MoE 路由与 DeepEP、EPLB、CUDA Graph。
所以**一条 PR 只要落在这些组件上就与他们相关，哪怕标题里的模型名不是
他们在跑的那几个** —— 组件是共用的。

"""

_TAIL = """

只输出 JSON，不要解释、不要代码围栏，形状固定为：
{"items":[{"id":"原样抄回输入里的 id","level":"P0|P1|P2","reason":"一句中文，说清后果是什么，不要复述标题"}]}
输入里的每一条都必须在 items 里出现一次。"""


def system_prompt(token=None):
    """运行时拼 —— 中间那段标准可能来自 issue 正文，不是编译期常量。"""
    return _PREFACE + criteria.active(token)["text"] + _TAIL


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


def _post(payload: list[dict], api_key: str, fmt: dict, timeout: int) -> dict:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content":
             f"给下面 {len(payload)} 条评级：\n"
             + json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": fmt,
        "max_tokens": 8000,
    }).encode()
    req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=body,
                                 method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return _extract_json(data["choices"][0]["message"]["content"])


_STRICT = {"type": "json_schema",
           "json_schema": {"name": "triage", "strict": True, "schema": SCHEMA}}
_LOOSE = {"type": "json_object"}


def _call(payload: list[dict], api_key: str, timeout: int = 240) -> dict:
    try:
        return _post(payload, api_key, _STRICT, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        # 模型不支持严格 schema，退回 json_object
        print(f"analyze: {MODEL} 拒绝 json_schema，退回 json_object",
              file=sys.stderr)
        return _post(payload, api_key, _LOOSE, timeout)


def _call_anthropic(payload: list[dict], timeout: int = 600) -> dict:
    """官方 SDK。凭据由 SDK 自己解析：ANTHROPIC_API_KEY，或 `ant auth login`
    存在 ~/.config/anthropic/ 的 profile —— 所以裸 Anthropic() 就够了。"""
    import anthropic

    client = anthropic.Anthropic(timeout=timeout)
    with client.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system_prompt(),
        messages=[{"role": "user", "content":
                   f"给下面 {len(payload)} 条评级：\n"
                   + json.dumps(payload, ensure_ascii=False)}],
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT,
                       "format": {"type": "json_schema", "schema": SCHEMA}},
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"refused: {msg.stop_details}")
    text = next(b.text for b in msg.content if b.type == "text")
    return _extract_json(text)


def _describe() -> str:
    return (f"anthropic/{ANTHROPIC_MODEL}" if PROVIDER == "anthropic"
            else f"openai/{MODEL}")


def triage(items: list[dict], batch: int = 40) -> dict[str, dict]:
    """返回 {repo#num: {level, reason, ai}}。凭据缺失或调用失败即降级。"""
    if not items:
        return {}
    anthropic_mode = PROVIDER == "anthropic"
    api_key = os.environ.get("LLM_API_KEY")
    if anthropic_mode:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            print("analyze: 未安装 anthropic SDK，降级到规则打分", file=sys.stderr)
            return _fallback(items)
    elif not api_key:
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
            raw = (_call_anthropic(payload) if anthropic_mode
                   else _call(payload, api_key))
            rows = raw.get("items", [])
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
