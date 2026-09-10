# pr-daily-update

每天把 vLLM / SGLang 上跟 DeepSeek-V4.1 有关的 PR 动态推到飞书群，并生成一页
按 **P0 / P1 / P2** 分组的朴素链接列表。卡片里 P0 全列且加粗，P1 列标题，P2 只给数。

要盯别的仓库或别的模型，改 `common.py` 顶部的 `REPOS` 和 `SEARCH_TERMS`，
再把 `COMPONENTS` 换成那个项目的组件代号即可 —— 其余部分与模型无关。

```
common.py   抓取 + 分类（组件、类型、V4.1 判定、摘要提取）
page.py     渲染 site/index.html
watch.py    入口：抓 → 出页面 → 发卡片
```

## 三道处理

**一、抓。** 两个仓库，`SEARCH_TERMS` 里每个词单独搜一次再按 id 去重（GitHub 的
issue search 把词按 AND 处理，不支持 OR）。只匹配标题：实测 `deepseek in:title,body`
七天回来 717 条，绝大多数是 PR 模板里顺口提一句，还会把
`[HiCache] ... NAME_MAX` 误判成 V4.1 命中；换成 `in:title` 七天 177 条。
issue 另外过一道硬筛：只留未关闭、非 `stale`/`inactive`、标题前缀是
`[Bug]`/`[Perf]` 的（Feature 和 RFC 是路线图不是风险）。

**二、只留有实质进展的。** Search API 的 `updated_at` 被任何活动顶起来 —— 机器人
评论、加个标签、点个赞都算。所以逐条查 timeline，只留窗口内发生过
**新提交 / 强推 / review / 合并 / 关闭 / 重开 / 转正式 / 人写的评论** 的，
并按 actor 名字滤掉 `[bot]`、`github-actions`、`codecov`、`dependabot` 之流。

> timeline 是**时间升序**返回的，长 PR 的近期事件都在最后一页 —— 只读第一页会把
> 活跃 PR 全判成「没进展」，方向正好反。所以要先从 `Link` 头挖出末页再读。

没有 `GITHUB_TOKEN` 时这一步整个跳过（未认证 60 次/小时，一轮上百个 timeline
请求必然 403），页面顶部会标明「未做有效更新过滤」。

**三、评级。** 交给公司网关的模型评 P0 / P1 / P2，每条给一句中文理由。
40 条一批，单批失败只降级那一批。

## P0 / P1 / P2

| 档 | 含义 |
|---|---|
| **P0** | 会直接咬到线上：算错、非确定性、卡死、崩溃、显存爆掉、明显退化，**且**落在我们跑的路径上（NVIDIA、DeepSeek-V4.x、投机解码、稀疏 MLA、KV cache、DP/EP/PD） |
| P1 | 值得本周看：相关路径上的功能推进、有意义的优化、影响面大但不紧急的缺陷 |
| P2 | 知道就行：其他后端/其他模型专属、文档、CI、重构、小修小补 |

**这只是初筛。** 模型只看得到标题和正文摘要，看不到 diff，也不知道我们线上跑的是
哪个配置。P0 的意思是「今天先看这几条」，不是「这几条一定出事」。提示词里明确要求
拿不准往低了报 —— P0 泛滥等于没有 P0。

### 网关的一个坑

**不要用 `response_format: {"type":"json_schema"}`。** 实测这个网关会**静默忽略**它，
照样返回 Markdown 表格且不报错 —— 照着 OpenAI 文档写代码会炸在 JSON 解析上，
而且是运行时才炸。只有 `{"type":"json_object"}` 是认的，所以输出形状靠提示词约定
加 `_extract_json()` 兜底解析（剥代码围栏、括号配对挖最外层对象），不能靠 schema 保证。

模型漏条目或抄错 id 也见过，所以最后会对账补齐，不让页面出现没有等级的行。

## 为什么是现在这个形状

**无状态。** 只查「最近 WINDOW_HOURS 小时内有更新」，不需要 state 文件或 cache，
重跑幂等。连续几天在迭代的 PR 会连续出现 —— 这是特性，说明它还在动。

**摘要是 PR 正文的第一段真话**，不是 LLM 生成的。`common.summarize()` 先剥掉
HTML 注释、代码块、勾选框、`## Purpose` 之类的模板小标题和 `Signed-off-by`，
再取前 260 字并断在句子边界。想要真正的语义摘要得接模型，现在没接。

## 用法

```bash
# 只生成页面，不发卡片
PAGE_ONLY=1 OUT_DIR=/tmp/site python3 watch.py

# 生成页面 + 打印卡片 JSON，不发送
DRY_RUN=1 python3 watch.py

# 真发
LARK_WEBHOOK='https://open.larkoffice.com/open-apis/bot/v2/hook/xxx' \
  python3 watch.py
```

| 环境变量 | 说明 |
|---|---|
| `LARK_WEBHOOK` | 发卡片时必填 |
| `LARK_SECRET` | 群里开了「签名校验」就必填 |
| `LARK_KEYWORD` | 群里开了「自定义关键词」就必填，见下 |
| `LLM_API_KEY` | 公司网关 key。缺失则降级成规则打分 |
| `LLM_BASE_URL` | 默认 `https://f7xnt9mg.fn.bytedance.net/v1` |
| `LLM_MODEL` | 默认 `gpt-6-astra` |
| `GITHUB_TOKEN` | 强烈建议。未认证的 search API 只有 10 次/分钟，认证后 30 次 |
| `PAGE_URL` | 明细页地址，卡片锚点链接的前缀 |
| `WINDOW_HOURS` | PR 回看窗口，默认 25（比 24 多 1 小时，避免 cron 抖动漏掉） |
| `ISSUE_WINDOW_DAYS` | 待修问题回看天数，默认 7 |
| `REPORT_TZ` | 展示时区，默认 `America/Los_Angeles` |
| `OUT_DIR` | 页面输出目录，默认 `site` |
| `SEND_WHEN_EMPTY` | 一条都没有时是否照发，默认 `0` |
| `PAGE_ONLY` / `DRY_RUN` / `NO_AI` | `1` = 只出页面 / 只打印不发 / 跳过评级 |

## 部署

`.github/workflows/daily.yml`，每天 09:07 PDT 跑一次，也可手动触发。

**需要三处一次性配置：**

1. **Settings → Pages → Source 选 `GitHub Actions`**。不点这个，deploy 那步会失败。
2. **Settings → Secrets and variables → Actions → Secrets** 加 `LARK_WEBHOOK`
   和 `LLM_API_KEY`；开了签名校验再加 `LARK_SECRET`。
3. 页面地址如果不是默认的 `https://<owner>.github.io/<repo>/`，在同一页的
   **Variables** 里加 `PAGE_URL` 覆盖。

`GITHUB_TOKEN` 是 Actions 自带的，不用配。

**别把 webhook 地址硬编码进 yml。** 这是 public 仓库；地址泄露后任何人都能往你的群里
发垃圾。Actions 本身是安全的 —— GitHub 不会把 secrets 下发给来自 fork 的 PR
workflow，`schedule` 也只在默认分支上跑。注意 **Pages 页面是公开的**，不过上面只有
公开仓库的 PR 信息，没有额外泄露。

### 安全设置选哪个

**推荐只开签名校验。** 三个选项里：

- **IP 白名单** —— GitHub Actions 的托管 runner 用 Azure 大段动态 IP，`api.github.com/meta`
  的 `actions` 字段有上千条 CIDR 且会变，飞书白名单填不下也跟不上。排除。
- **自定义关键词** —— 能用，但它把消息内容和安全策略绑死了。踩过一次：早期卡片正文
  铺着 PR 标题、自带关键词，改版后正文里就没那个词了，飞书直接以
  `19024 Key Words Not Found` 拒收。**关键词校验只扫 `elements`，不扫 `header`** ——
  标题里有词不算数。真要用就把词填进 `LARK_KEYWORD`，脚本会织进正文第一行。
- **签名校验** —— 不限制内容，又真正防盗用。就选这个。

cron 特意避开了整点和半点：飞书文档明确提醒那两个时刻全公司的定时任务一起打过来，
容易撞限流（单租户单机器人 100 次/分钟、5 次/秒，body ≤ 20KB）。

## 两个会过期的假设

1. **命名。** `RE_V41` 现在覆盖 `v4.1` / `v4_1` / `v41` / `dsv41`。上游要是换了写法，
   V4.1 计数会悄悄掉到 0 而不报错 —— 卡片上那个数连着几天是 0 就该来查这里。
2. **状态不等于结果。** 一条「修正」PR 出现在列表里，只说明有人在修，不说明修好了。
   实测某天「修正」桶里 merged 只占 6%、79% 还开着，所以每行都带 open/merged/closed
   状态标签 —— 「今天有 33 条在修 bug」和「今天修好了 33 个 bug」差着一个数量级。
3. **组件代号。** Engram / DSpark / HiSparse / DFlash 是 2026-09 的观察，其中只有
   Engram 是 V4.1 专属，其余三个跨模型。V4.2 换一批代号的话 `COMPONENTS` 要跟着改。
