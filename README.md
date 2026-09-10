# pr-daily-update

每天把 vLLM / SGLang 上跟 DeepSeek-V4.1 有关的 PR 动态推到飞书群，并生成一页
**组件 × 类型** 的全量明细。卡片上每个数字都是链接，点进去落到明细页的对应小节。

要盯别的仓库或别的模型，改 `common.py` 顶部的 `REPOS` 和 `SEARCH_TERMS`，
再把 `COMPONENTS` 换成那个项目的组件代号即可 —— 其余部分与模型无关。

```
common.py   抓取 + 分类（组件、类型、V4.1 判定、摘要提取）
page.py     渲染 site/index.html
watch.py    入口：抓 → 出页面 → 发卡片
```

## 分类

**两个维度，各自取第一个命中的类，顺序即优先级。** 一条 PR 只归一个组件、一个类型，
否则矩阵会重复计数、合计对不上。

组件（`common.COMPONENTS`）：
`🧬 Engram → 🚀 投机解码 → 💾 KV Cache → 🎯 稀疏注意力/MLA → 🧠 MoE/路由 →
🔢 量化 → 🔀 并行/PD → 🗣 前端/解析 → ⚙️ Kernel/融合 → 🧱 模型接入 → 📦 其他`

越具体的排越前。两处刻意的安排：**KV Cache 在稀疏注意力前面**，因为
`Fix HiSparse slot translation in the fused MLA KV writer` 主体是 HiSparse 不是 MLA；
**模型接入几乎垫底**，因为 `[Model]` 标签太常见，放前面会把所有东西吸走。

类型（`common.KINDS`）：`🛠 修正 → ⚡ 性能 → 🔧 工程 → 🔌 硬件适配 → ✨ 功能 → · 其他`

顺序同样不是随手排的：`[AMD] Fix HiSparse slot translation` 该算修正而不是硬件适配，
`[AMD][DI][CI] Add nightly recipes` 该算工程而不是硬件适配 ——「在干什么」比
「在哪个后端干」更能说明上游的投入方向。硬件适配单独成类，是因为 `[NPU] Support X`
语义上是「往新后端搬」，跟「加了个新能力」是完全不同的信号，混进功能里会让你误判
上游在扩能力。

匹配时方括号标签优先（`[Bugfix]` `[Perf]` `[CI]`），没打标签的才看正文关键词。
两个维度都留了兜底桶，不硬塞 —— 实测各残留 5% 左右，都是真难判的。

### 改正则时注意词边界

用 `common._w()` 造正则，**不要直接写 `\b`**。`\b` 认为下划线是单词字符，于是
`\bmoe\b` 匹配不上 `fused_moe_triton`、`\byarn\b` 匹配不上 `deepseek_yarn` ——
这两条实测都因此掉进过兜底桶。`_w()` 用 `(?<![a-z0-9])…(?![a-z0-9])` 代替。

## 为什么是现在这个形状

**只搜标题，不搜正文。** `deepseek in:title,body` 七天回来 717 条，绝大多数是 PR
模板/checklist 里顺口提一句 deepseek；还会制造假阳性，比如
`[HiCache] Keep the file backend temp file name within NAME_MAX` 被判成 V4.1 命中。
换成 `in:title` 七天 177 条，噪声基本消失。

**多关键词分别搜再合并。** GitHub 的 issue search 把词按 AND 处理，不支持 OR，
所以 `SEARCH_TERMS` 里每个词发一次请求，再按 PR id 去重。

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
| `GITHUB_TOKEN` | 强烈建议。未认证的 search API 只有 10 次/分钟，认证后 30 次 |
| `PAGE_URL` | 明细页地址，卡片锚点链接的前缀 |
| `WINDOW_HOURS` | 回看窗口，默认 25（比 24 多 1 小时，避免 cron 抖动漏掉） |
| `REPORT_TZ` | 展示时区，默认 `America/Los_Angeles` |
| `OUT_DIR` | 页面输出目录，默认 `site` |
| `SEND_WHEN_EMPTY` | 一条都没有时是否照发，默认 `0` |
| `PAGE_ONLY` / `DRY_RUN` | `1` = 只出页面 / 只打印不发 |

## 部署

`.github/workflows/daily.yml`，每天 09:07 PDT 跑一次，也可手动触发。

**需要三处一次性配置：**

1. **Settings → Pages → Source 选 `GitHub Actions`**。不点这个，deploy 那步会失败。
2. **Settings → Secrets and variables → Actions → Secrets** 加 `LARK_WEBHOOK`；
   开了签名校验再加 `LARK_SECRET`。
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
  铺着 PR 标题、自带关键词，改成纯矩阵后正文里就没那个词了，飞书直接以
  `19024 Key Words Not Found` 拒收。**关键词校验只扫 `elements`，不扫 `header`** ——
  标题里有词不算数。真要用就把词填进 `LARK_KEYWORD`，脚本会织进正文第一行。
- **签名校验** —— 不限制内容，又真正防盗用。就选这个。

cron 特意避开了整点和半点：飞书文档明确提醒那两个时刻全公司的定时任务一起打过来，
容易撞限流（单租户单机器人 100 次/分钟、5 次/秒，body ≤ 20KB）。

## 两个会过期的假设

1. **命名。** `RE_V41` 现在覆盖 `v4.1` / `v4_1` / `v41` / `dsv41`。上游要是换了写法，
   V4.1 计数会悄悄掉到 0 而不报错 —— 卡片上那个数连着几天是 0 就该来查这里。
2. **组件代号。** Engram / DSpark / HiSparse / DFlash 是 2026-09 的观察，其中只有
   Engram 是 V4.1 专属，其余三个跨模型。V4.2 换一批代号的话 `COMPONENTS` 要跟着改。
