# Agent Reach 评估（2026-10-29）

> 起因：用户转来公众号文章《10万 Star 爆火，又一个顶级 Agent 联网神器，能看 B 站、搜小红书》。
> 本文只记**核实到的事实**与**对本项目的接入口/风险**，不写方案、不执行安装。
> 核实来源：仓库 README 原文、`docs/install.md` 原文、GitHub 仓库页实时数字、腾讯云 10-09 同题文章、本项目盘上代码。

---

## 0. 结论（一句话）

**是真项目，值得接，但接法有讲究：先接"默认 5 个零配置渠道"，登录态渠道先别接——本项目的凭证闸不认识它的 cookie 文件，这是个真缺口。**

另外一条更紧急：本项目 `web_search` 的主通道 **SearxNG 当前是死的**，比装什么都急。

---

## 0.1 【2026-10-29 18:2x 修正】"有了它就不需要 web_search" —— **不能这么理解**

拿到一手源码 `agent_reach/channels/web.py` 后改判。原文见 §9。
**§7 的三档建议中"B 档装整套 Agent Reach"应改为"只搬 Jina Reader 一条后端"**——不装、不引外部工具、不改工具面。

**决定性的一句（Agent Reach 自己写的）**：

> 模块 docstring：*"This channel is a fallback: **agents that already have working built-in web search / fetch should use those first.**"*
> 渠道 description：*"网页搜索与阅读（**Agent 自带工具不可用时的保底**）"*
> Exa 限流时的 hint：*"Exa's hosted search limit was reached. **Use the agent's built-in web search** or wait before retrying."*

⇒ **它的设计前提就是"你自带搜索能用"**，它退居保底。所以：
- **搜索侧不该拿它换 SearxNG**（SearxNG 本机自建、无额度；Exa 托管 MCP、会 429）；
- **阅读侧确实该换**（Jina Reader 给 Markdown 全文，裸正则剥标签给不了）；
- **但 Jina Reader 不需要装 Agent Reach**——`https://r.jina.ai/<url>` 直接调即可。

顺带查出本项目 `fetch_web` 的一个实缺陷（见 §9.3）：**遇到 Cloudflare 验证页会把验证页当正文返回给模型。**

---

## 1. 核实：它到底是什么

| 项 | 核实值 | 取证 |
|---|---|---|
| 仓库 | `Panniantong/Agent-Reach` | GitHub 仓库页实时 |
| **Star** | **105.9k**（Forks 9.4k / Watching 344） | GitHub 页面 `insightStats` 实时取值 |
| 许可 | MIT | 仓库页 `MIT-1-ov-file` |
| 语言/门槛 | Python 3.10+ | README badge |
| 地位 | Trendshift **GitHub Trending #1 of the Day** | README badge |
| 渠道数 | **14 个**：默认 5 / 可选 6 / 实验 3 | README 平台表 |

**默认（装好即用、零配置）**：`bilibili`（搜索/详情/热门/字幕）、`v2ex`、`github`（搜仓库/读 README/读 issue+PR）、
`youtube`（信息/搜索/字幕/语音转文字）、`web`（搜索/阅读，保底）
**可选（需登录或 Key）**：`xiaohongshu`、`xueqiu`、`boss`、`xiaoyuzhou`、`linkedin`、`reddit`
**实验（反爬严，随时可能失效）**：`twitter`、`facebook`、`instagram`

**架构口径（作者原话）**：「**能力层而非封装层**」——每个平台的每个动作都有一条**按顺序尝试的首选+备选工具列表**
（如读 B 站：bili-cli → B 站公开接口；读网页：Jina Reader → Exa）。
**Agent Reach 自己不转发数据**，只负责选型、安装、路由、体检；
真干活的是上游开源工具（OpenCLI、bili-cli、yt-dlp、rdt-cli、twitter-cli、mcporter、gh CLI、Jina Reader、Exa）。

**CLI 面**：
```
agent-reach channels                    # 有哪些渠道、各能做什么
agent-reach <渠道> <动作> [参数]          # agent-reach bilibili search "大模型"
agent-reach read <url>                  # 按链接自动选渠道
agent-reach doctor [--json]             # 真实跑命令探测每条通道通断 + 当前走哪条线
agent-reach setup / update / uninstall
```

**安装**：1.6 起走 uv
```bash
uv tool install https://github.com/Panniantong/Agent-Reach/releases/latest/download/agent-reach.zip
agent-reach setup
```
所有东西落在 `~/.agent-reach/` 一个目录里，不散落。

**安全模型（README 当前原文）**：
- **只读**：只搜索和读取，**不发帖、不评论、不点赞、不私信**。
- **从不替你登录，也不要你的密码**；确实需要重新登录时打开**专用窗口**由本人完成。
- 要登录的平台用 **Agent Reach 自己的专用浏览器**（后台运行、不弹窗、只对本机开放）。
- ~~**第一次用某个网站时，会把你 Chrome 里这个网站的登录复制一份过去**，原浏览器不变，**不读密码库**。~~
  > **【2026-10-29 18:2x 更正】** 见 §10.2：`docs/troubleshooting.md` 原文是
  > **"Personal Chrome sessions are never imported."**（个人 Chrome 会话**永不导入**），
  > 且只有 **LinkedIn / Bilibili preserve existing native sessions**。
  > ⇒ README 那句"复制一份"与 troubleshooting 冲突；**按 troubleshooting 记（更严更安全）**，
  > 代价是**要在 Reach 专用窗口里重新登一次**。
- **建议用小号**（复用过来的通常是主账号，平台可能限制自动访问的账号）。
- Key 不要发在聊天里，用 `agent-reach configure <x>-key` 在自己终端填。

**README 自带防诈段**：明确声明本项目**没有**代币/币/钱包/领取活动，凡借用本项目名或地址的加密项目均为诈骗。

**利益结构（README 赞助商栏，需如实记）**：Bright Data、RapidProxy、BrowserAct、CoreClaw、腾讯云 OpenClaw、优刻得——
**清一色是爬虫/代理/抓取服务商**。MIT 开源不等于没有商业动机：这个项目卖的正是"帮你绕过反爬"。

---

## 2. 文章与仓库当前 README 的三处不一致

| # | 文章说法 | 仓库当前 README |
|---|---|---|
| 1 | 小红书要装「Chrome 浏览器扩展（10 万用户）」，走 OpenCLI | **用 Agent Reach 自己的专用浏览器**；首次用某站时把 Chrome 里该站登录**复制一份**过去，原浏览器不变。机制不同 ⇒ **文章描述的是旧版** |
| 2 | 「Exa 语义搜索，MCP 方式接入，免费不要 Key」当全网搜索 | Exa 放在**读网页的备选后端**（首选 Jina Reader）；搜索归在 `web` 渠道 |
| 3 | 「Cookie/token 只在 `~/.agent-reach/config.yaml`，权限 600」 | README 只说"所有东西都在 `~/.agent-reach/`"；config.yaml/600 这条**未在仓库原文核到** |

另有两条文章说法**未能核实**：「不少产品抓取联网数据都用了 Agent Reach 底层实现」（作者断言，无出处）、
「配置 YAML 权限 600」。

---

## 3. 本项目现状：联网能力实测（2026-10-29）

| 项 | 实测值 |
|---|---|
| 联网工具 | `web_search` / `fetch_web` **两件都在注册表里**（`requests` 可用 ＋ 两个 Key 都已配 ⇒ 满足注册条件） |
| `web_search` 主通道 | **SearxNG `http://localhost:8888` 当前连不上**（Docker daemon 未运行） |
| `web_search` 备用通道 | 阿里云 `enable_search`（`ALIYUN_TOKEN_PLAN_API_KEY` 已配）——返回**模型改写文本而非原始链接**，推演类实测 42s |
| `fetch_web` | **正则剥标签**（`_TAG_RE.sub("\n", html)`）——正是文章批评的"一堆 HTML 标签和导航栏文字" |
| 缺失能力 | YouTube 字幕、B 站、GitHub issue、RSS、V2EX、小红书/X/Reddit —— **全部没有** |

⇒ 结论：**SearxNG 挂着 + `fetch_web` 只做剥标签**，这是本项目联网面的真实水位。

---

## 4. 接入口：本项目**已经有** `bash` / `python`，零改造可用

`src/api/card_tools.py:3255 _h_bash` / `:3270 _h_python`，均挂 `policy.SWITCH_TOOLS_FILE`
（`card_tools.py:284-285`）。⇒ 卡可以直接：
```
agent-reach bilibili search "大模型"
agent-reach read https://www.bilibili.com/video/xxx
```
**不需要新加任何工具**。

若要做成**正式工具面**（让模型不靠 `bash` 就直接调），则要按本项目规矩走三处：
`card_tools.py` 注册表 ＋ `config/permissions.json` 的 perms ＋ `tool_doc.py` 的 ToolDoc
（另受 10-28 起的"工具面收敛"口径影响：工具总数在**主动退役**，不是随便加）。

---

## 5. 风险面（必须先处理的一条）

### 5.1 `bash` 是裸 subprocess，**没有路径边界**

`file_tools` 四件（read/write/list/search）限定 `D:\AI_station`；但 `bash`/`python` **不受此限**，
执行前唯一的闸是 `credential_guard.find_credential_refs(command)`，
判据复用 `file_tools._credential_excluded` 的同一份黑名单：

```python
CREDENTIAL_NAMES = {".env", ".env.local", ".env.production", ".env.development", ".env.test",
                     "credentials.json", "secrets.yaml", "secrets.yml", "id_rsa", "id_ed25519",
                     ".netrc", ".npmrc", ".pypirc", "kubeconfig"}
CREDENTIAL_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".jks")
CREDENTIAL_PREFIXES = ("env.",)
```
（`src/api/file_tools.py:57-63`）

### 5.2 缺口

**`config.yaml` 不在黑名单里**，而 `~/.agent-reach/config.yaml` 正是 cookie/token 的落点。

⇒ 装好并配了登录态渠道后，任何有 `bash` 权的卡执行
`type %USERPROFILE%\.agent-reach\config.yaml` 就能把 cookie 原值拿回 stdout；
而 `credential_guard.redact_output` 只按 `.env`/`.netrc`/yml 里**已知的值**脱敏
（`secret_values()` 的三个来源），**不知道 agent-reach 的 cookie** ⇒ **不会被脱敏**。

这撞上的正是本项目的设计前提：**卡是能自主动手的智能节点**，权限只按 perms 下发。
⇒ 一旦给某类卡 `tools_file` 权，那张卡就等于持有你的社交站登录态。

**修法（两条，选一条）**：
1. **只接零配置 5 渠道**（不产生登录态 ⇒ 没有 cookie 可偷）——**最稳，零改动**。
2. 若要接登录态渠道：把 `~/.agent-reach/` 纳入凭证闸（命令里点名该路径即拒 ＋ 输出侧把该文件里的值纳入脱敏样本）。
   注意误伤：`config.yaml` 是通用名，直接加进 `CREDENTIAL_NAMES` 会误伤正常文件；
   应按**路径**判（`~/.agent-reach` 前缀）而非按文件名。

### 5.3 文章自带的提醒仍然成立

用 cookie 的平台（X/小红书）拿脚本调，**可能被平台检测出来，严重会封号**。README 自己写明的。
⇒ 用**小号**，不用主号。

### 5.4 平台线路会换

文章说「今年 6 月 yt-dlp 被 B 站风控封死，项目把 B 站切到 bili-cli」；
README 的「首选+备选有序列表」就是这个机制的正式表述，`doctor` 能看出当前走哪条。
⇒ 对本项目的含义：**上游线路是会断的**，不能把它当稳定依赖；
真要依赖，**doctor 要进体检**，断了要能降级而不是让卡卡死。

---

## 6. 顺带查出的两个文档/代码缺口（本项目，与 Agent Reach 无关但同期发现）

| # | 缺口 | 证据 |
|---|---|---|
| 1 | README 声称「设 `SEARCH_CHANNEL=searxng\|aliyun` 即钉死通道，钉死后该通道失败会**点名报错、不静默换通道**」——**当前代码没有这个开关**。`src/api/search_tool.py` 是"先试 SearxNG，失败静默回落阿里云"，无 `SEARCH_CHANNEL` 读取、无点名报错 | 全仓 `SEARCH_CHANNEL` 只出现在一次性审计脚本 `_audit_plugin_modes.py` 里，**产品代码零引用** |
| 2 | README 说联网开关读 `SEARXNG_URL`，但 `.env` 里实际键名是 **`S5_M3_SEARXNG_URL`**（21 字符＝`http://localhost:8888`）。当前因代码默认值恰好相同而"能用"，但**改 `.env` 那个键不会生效** | `.env` 实测 ＋ `search_tool.py:27` |

---

## 7. 我的建议（三档，按稳妥度递增）

| 档 | 动作 | 代价 | 收益 |
|---|---|---|---|
| **A** | **只修 SearxNG**（起 docker 或改配置） | 最小 | 主通道复活，`web_search` 恢复真链接列表，不再 42s 改写文本 |
| **A+** | **A ＋ `fetch_web` 后端改 Jina Reader 首选**（**不需要装 Agent Reach**） | 改一处代码，零安装、零外部依赖、零登录态 | 立刻消灭"一堆标签"问题；顺带修掉反爬页当正文的缺陷（§9.3） |
| **B** | A+ ＋ 装 Agent Reach，取 YouTube 字幕/B站/GitHub issue/V2EX | 装一个 uv 工具；**不配登录态 ⇒ 无 cookie 风险** | 补上本项目完全没有的四类能力 |
| **C** | B ＋ 登录态渠道 | **必须先补凭证闸缺口**（§5.2） | 小红书/X/Reddit 真实用户反馈 |

> **【修正】** 上一版把 B 档写成"只接 Agent Reach 默认 5 渠道"。
> 看过 `channels/web.py` 后：**web 渠道不值得为它装整套**——它的搜索只有 Exa 一个后端且会 429，
> 而它的阅读能力（Jina Reader）可以单独摘出来零成本接进 `fetch_web`。
> ⇒ **真正值得搬的是 Jina Reader 这一条后端**；Agent Reach 剩下的价值在它**自己的**渠道
> （YouTube 字幕 / B站 / GitHub issue / V2EX），那些确实只能靠装它，或自己接 yt-dlp / bili-cli。
>
> **另有一条零安装的路**（上一版已写，仍成立）：`fetch_web` 加 Jina Reader 作首选后端，
> 不装任何东西就能把"一堆标签"变成干净正文。⇒ **A+ 与这条是同一件事，应优先做。**

---

## 8. 未确认 / 没做

- **没安装任何东西**，没改本项目任何代码，没跑测试。
- 未核：`~/.agent-reach/` 的实际目录结构与文件权限、文章所称 `config.yaml` 权限 600、
  「Exa 免费不要 Key」、`agent-reach doctor` 的实际输出格式。
- 未核：上游工具（bili-cli / OpenCLI / rdt-cli）的真实可用性与国内网络可达性。
- 未核：`agent-reach read <url>` 的输出是否会被本项目的 payload 裁剪机制
  （`payload_cap.py` / `read_linked` 有界注入）二次截断。

---

## 9. 一手源码核实：`agent_reach/channels/web.py`（2026-10-29 18:2x 补）

> 取证：`https://raw.githubusercontent.com/Panniantong/Agent-Reach/main/agent_reach/channels/web.py`（原文读取）。

### 9.1 渠道定位（原文）

```python
"""Web — search (Exa) and read any page (Jina Reader, Exa fallback).

This channel is a fallback: agents that already have working built-in web
search / fetch should use those first.
"""
class WebChannel(Channel):
    name = "web"
    description = "网页搜索与阅读（Agent 自带工具不可用时的保底）"
    level = "default"
    def can_handle(self, url: str) -> bool:
        return True  # Fallback — handles any URL
```

⇒ **官方定位：保底。** 设计前提是"agent 自带搜索/阅读能用就用自己的"。
⇒ **`can_handle` 恒 True** ⇒ 它抢不走你的 `fetch_web`，但也意味着**你若调它，它什么都收**。

### 9.2 两个动作的后端（**搜索只有 Exa 一个后端，无备选**）

| 动作 | 后端链 | 实现要点 |
|---|---|---|
| `search` | **仅 Exa**（`_exa_search`） | 走 `https://mcp.exa.ai/mcp` 免费托管 MCP，**无 key**；`web_search_exa`，`numResults=limit`（默认 5） |
| `read` | **Jina Reader 首选 → Exa 备选** | Jina＝`https://r.jina.ai/<url>`（`Accept: text/plain`）；Exa 兜底 `web_fetch_exa`，`maxCharacters=20000` |

**Exa 的额度/限流是被代码显式识别的**：
```python
_EXA_LIMIT_RE = re.compile(r"\b429\b|rate[ _-]?limit|too many requests|\bquota\b|..."
                           r"|(?:credit|credits|balance).{0,40}(?:exhaust|deplet|insufficient|exceed|reach)|...", re.IGNORECASE)
# 命中 ⇒ ReachError(RATE_LIMITED, ..., hint="Exa's hosted search limit was reached.
#                                            Use the agent's built-in web search or wait before retrying.")
```
⇒ **它自己都写着"用回 agent 自带的 web search"。** 这是"能不能替代"的直接答案：**不能，它是补位不是替代。**

**两处工程细节（值得抄）**：
- `_exa_search` 解析不出结果时 **不报"零命中"**，抛 `UPSTREAM_BROKEN`（注释：*"Do not report an unrecognized response as a successful zero-hit search"*）
  ⇒ 托管 MCP 没有可验证的空 schema，"没解析出来"≠"搜到 0 条"。
- `read` 有 **5 MB 上限**（`_MAX_RESPONSE_BYTES`）＋超时；Jina 返回超限即 `ValueError`。

### 9.3 本项目 `fetch_web` 的一个实缺陷（Agent Reach 已处理，我们没有）

Agent Reach 有 `_is_antibot_page()`，识别 **Jina/Cloudflare 挑战页**：
```
"title: just a moment..."  |  "## performing security verification"
"title: attention required! | cloudflare"（含 ray id / /cdn-cgi/challenge-platform/）
⇒ 抛 UPSTREAM_BROKEN：「Jina Reader 返回了反爬验证页，未获取到目标内容；
                       请改用站点专用工具或浏览器读取」
```
**即：它宁可报错，也不把验证页当正文交出去。**

而本项目 `src/api/search_tool.py:142-175 fetch_web`：
```python
html = _COMMENT_RE.sub("", r.text)
title_m = _TITLE_RE.search(html)
html = _SCRIPT_RE.sub("", html)
text = _TAG_RE.sub("\n", html)          # ← 只剥标签，不判是不是验证页
```
⇒ 遇到 Cloudflare/「Just a moment...」页面时，**会把验证页剥成文本当正文返回给模型**，
模型对着一个验证页写总结，**且没有任何错误信号**（status 200 ⇒ 正常返回）。
⇒ 这是 `fetch_web` 当前的**真实可靠性缺口**，与换不换 Agent Reach 无关，值得单独修。

### 9.4 由此得到的口径（写给决策用）

1. **不该拿 Agent Reach 换掉 `web_search`**：SearxNG 本机自建、无额度、无速率限制、返回真 URL/标题/摘要；
   Exa 托管 MCP 有额度、会 429。**互补关系，不是二选一。**
2. **该把 `fetch_web` 的后端换成 Jina Reader 首选**——而且**不需要装 Agent Reach**，
   `https://r.jina.ai/<url>` 直接调即可；顺手补上 §9.3 的反爬页识别。
3. **Agent Reach 真正的增量在它自己的渠道**（YouTube 字幕 / B站 / GitHub issue / V2EX），
   那些不是 Jina 能替代的 ⇒ 要么装它，要么自己接 yt-dlp / bili-cli / gh。
4. **若最终决定装它**：先只开零配置渠道；登录态渠道必须先补 §5.2 的凭证闸缺口。

---

## 10. Exa 的"额度"到底怎么回事（2026-10-29 18:2x 补）

> 取证：`https://exa.ai/docs/admin/pricing.md`、`https://exa.ai/docs/admin/billing.md`、
> `https://exa.ai/docs/get-started/exa-mcp`（官方文档原文）。

### 10.1 "额度"是**两件不同的东西**，混起来是根源

| | 是什么 | 判据 | 报错 |
|---|---|---|---|
| **Rate limit（速率限制）** | **QPS**，按**团队整体**计（跨所有 key） | 请求太快 | `429 Too Many Requests` |
| **Credits（余额）** | **预付美元余额**，按次扣 | 余额耗尽 | `402 Payment Required` |

默认限速：`/search`、`/answer`、`/chat/completions` **10 QPS**；`/contents` **100 QPS**；
deep 类（`deep-lite`/`deep`/`deep-reasoning`）**5 QPS**；`/websets/*` 20 QPS；`/agent/runs` 5 QPS ＋ 50 并发。

### 10.2 为什么"免费无 Key"却有额度 —— 官方 429 排障条目原文

> *"The connection is using **Exa's free rate limits**. Sign in with OAuth or add your own API key,
> then reconnect so requests use **your team's plan and limits**."*

⇒ **匿名 MCP 连接走的是共享的"免费速率档"**；带 key / OAuth 后请求走**你自己的 plan 和 limits**。
⇒ **免费额度是有的，但要账号**：Free tier = **每月 $10 credits（上限约 2,500 次 Lite 搜索）**
＋ 注册一次性 $10 onboarding bonus；**不需绑卡**；每月 1 号重置、**不累积**；**只有用户的第一个 team 拿月度 grant**。
⇒ `agent_run` 工具**不能用免费档**，必须登录或配 key 才出现。

**根因**：Exa 背后是真实成本（爬取＋索引＋**额外的 LLM 调用**做摘要）。
"免费不用注册"是产品姿态，"限额"是成本闸——防止匿名用户把端点当白嫖搜索 API。

### 10.3 额度具体花在哪（官方成本表 + 计费口径）

| 端点 | 基础价 | 超出 10 条的结果 | AI 页面摘要 |
|---|---|---|---|
| `/search` (lite) | **$4 / 1k 请求** | $1 / 1k 条 | — |
| `/search` (auto / fast) | $7 / 1k | $1 / 1k 条 | $1 / 1k 页 |
| `/contents` | **$1 / 1k 页**（按内容类型分别计） | — | $1 / 1k 页 |
| `/answer` | $5 / 1k | — | — |
| `/agent` | $0.012（minimal）～ $1.00（xhigh）/ 次 | | |

官方 cost glossary 的三条口径：
- **Request** = 一次 API 调用（"$7/1k ⇒ 每次 $0.007"）
- **Result** = 一条搜索结果；**基础价含前 10 条**，超出的每条 $1/1k。
  原文举例：`numResults: 20` ＝ 基础价 ＋ 10 条额外结果。
- **Page & content type** = 一个 URL 算一页；一个内容类型（`text`/`highlights`/`summary`）算一次，
  **同一页要 text ＋ highlights 就按两页计**
- **AI page summary** = Exa 侧**额外的 LLM 调用**，$1/1k 页

### 10.4 落到 Agent Reach 的代码上 ⇒ **额度基本被 search 吃掉，read 不吃**

| Agent Reach 动作 | 实际调用 | 计费形态 | 花费 |
|---|---|---|---|
| `web search` | `web_search_exa`，`numResults=limit`（**默认 5**） | 1 request；**5 ≤ 10 ⇒ 不触发"超出 10 条"加价** | 每次 ≈ $0.004～0.007 |
| `web read` | **Jina Reader 首选**（`r.jina.ai`）→ Exa 兜底 `web_fetch_exa`(`maxCharacters=20000`) | **只有 Jina 失败才落到 Exa** ⇒ 1 page + 1 content type | 正常 **0**；兜底 ≈ $0.001 |

⇒ **Agent Reach 的默认用法很省**（search 5 条、read 走 Jina）。
⇒ 但 **`web` 渠道的 search 只有 Exa 一个后端、没有备选** ⇒ 撞限额直接失败，无降级路径。

### 10.5 钱到底付给谁（2026-10-29 22:2x 补 · 用户追问后的收敛）

| 付给谁 | 什么时候付 | 什么量级 |
|---|---|---|
| **Exa** | `web search`（唯一后端）；`read` 只有 Jina 失败兜底才走 | $4/1k 请求（lite）＋ 超 10 条每条 $1/1k |
| **Jina AI（现属 Elastic）** | `web read` 的首选后端（`r.jina.ai`） | **token 制**；不带 key 有免费限速档 |
| **你自己选的第三方 Key** | 可选渠道：小宇宙转文字要 Groq/OpenAI Key；某些渠道要代理 | 按第三方定价 |

**Agent Reach 自己不收钱**——它只装工具、选线路、跑体检。

**关键分界（决定"我会不会被扣钱"）**：
- **不配 key ⇒ 你现在一分钱没付**，吃的是**共享的免费限速档**（Exa 撞了就是 429，Jina 撞了就是限速）。
- **一配 key / 给 key 充了 token ⇒ 开始按次扣**。

**Jina 归 Elastic 这件事值得单独记**（2026-10-29 22:2x 核 `jina.ai/reader` 与 `jina.ai/api-dashboard/rate-limits`）：
- 页脚已是 **"Elastic © 2026"**；FAQ 原文：*"Elastic has sold a commercial license as its own SKU **since August 10, 2026**"*。
- 计费口径：**token 余额挂在 API key 上，不是账号上**（*"Balance and rate limits belong to an API key, not to the account"*），
  支付走 Stripe，支持**自动充值**（auto top-up，阈值/上限可配）。
- ⇒ **Jina Reader 不是"永久免费白嫖"**：无 key 有免费限速档，但要长期用就得充 token。
  ⇒ 这不改变"read 不花钱（无 key 时）"的结论，但**不能说 Jina 永远免费**。

**⇒ 对本项目最省的那条路仍然是 SearxNG**：本机 docker 自建，
**零美元、无限速、无额度、无第三方依赖**——它现在只是没起。

### 10.6 Agent Reach 自己的限流纪律（与 Exa 的 hint 是两条不同指引）
`docs/troubleshooting.md` 错误码表：
> `RATE_LIMITED` ｜ *the platform is throttling* ｜ **等 `retry_after_seconds`；
> 不要换账号或换后端继续请求**（*"do not switch accounts or backends to continue requesting"*）

对比 Exa 自己那句 hint（*"Use the agent's built-in web search or wait before retrying."*）
⇒ **Exa 让你切回自带搜索；Agent Reach 让你等。** 两条不冲突，指向同一个结论：**它俩都认为搜索不是 Agent Reach 的主力。**

### 10.6 对本项目的三条直接含义

1. **匿名档是共享档 ⇒ 并发敏感**。5700 在本机跑，多张卡并发调搜索吃同一个匿名档；
   撞了是 429，而 `web` 渠道 search 无备选 ⇒ 直接失败。
   ⇒ **这正是 SearxNG（本机自建、零美元、零额度、无 QPS 限制）该当主力的理由。**
2. **要稳定就配自己的 key**：Exa Free tier 给 $10/月（≈2,500 次 Lite 搜索）+ 10 QPS，**不需绑卡**。
   成本极低、额度明确，比匿名档强得多。但 Agent Reach 当前**不带 key**（走匿名），
   要改成带 key 需改它的 web 渠道实现——**而 Jina 那半边你根本不需要它**。
3. **最省路径**：SearxNG 主力 ＋ Exa 仅作"Jina 也挂了"的三级兜底。
