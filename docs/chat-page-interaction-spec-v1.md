# 对话页高保真原型 · 交互规格 v1

**定版对象**：`推演agent/docs/prototype-chat-page-v1.html`（1524 行，单文件、零外链、`file://` 直开）
**规格日期**：2026-10-12
**这张卡**：card_454 高保真原型 · 对话页

## 定版声明

本原型只保真三样东西：**结构、事件类型、交互**。不做视觉设计——不设配色主题、不设圆角阴影渐变、不设深浅切换。页面样式是中性功能底色（白底、1px 线、系统字体、等宽字体标事件码）。

判定「像不像产品」不是本原型的判据；判定「对话页会出现的事件类型是否全部在面、每种类型的交互是否可点可验」才是。

### 三条验收口径

| # | 口径 | 在本原型里的落点 |
|---|---|---|
| ① | 覆盖层不锁主视野 | 卡详情抽屉、评审浮窗、通知面板都是非模态：主区照常可读可点，它们只占右侧一带。仅「起草」「现状」两个**输入型弹窗**可短暂接管点击，且都带可见取消（关闭键 + ✕ + Esc） |
| ② | 每个可进入状态有 ≥1 个鼠标可达出口 | 抽屉/浮窗/面板各有 ✕；弹窗有「关闭」+「✕」；排队条每行有 ✕；类型索引点头部即收；过程折叠点顶栏按钮即回。Esc 只是加速键，不是唯一出口 |
| ③ | 不设不带来动作的中间态 | 每个可点元素都改变状态或产出反馈：索引项→滚动定位并高亮该行；过程开关→折叠过程行；工具行→展开 IN/OUT；思考块→展开全文；发送→追加消息并进入运行态；排队三键→编辑/插话/删除；危险操作→两段确认；空态建议→填进输入框 |

---

## 一、全局布局

三层，对应旧原型 `templates/card.html` 的 `.work-page`：

```
.app (flex row)
├── .sidenav  260px   全树导航（项目行 / 卡片行 / 评审批次行 / 职能行）
└── .main    (flex col)
    ├── .topbar  40px  面包屑 + 过程细节 + 类型索引 + 卡详情 + 消息 + 评审
    ├── .stage (flex row)
    │   ├── .chat          类型索引 + 对话流 + 回底键
    │   ├── .insp  340px   卡详情（覆盖式，默认收起）
    │   └── .fwin  340px   评审浮窗（覆盖式，默认收起）
    └── .composer         排队条 + 输入卡
```

`#prototype-chat-page-v1.html:36` `.app{display:flex;height:100vh}`；`…:99` 是过程行 `.step-row` 的基础盒模型。

**左栏宽度**：本原型用 260px（旧原型 `ui.css` 实测 `body.page-chat .app-nav width:272px`，`nav.css` 非 rail 态 420px）。取 260 是中性值，宽度本身不属于事件类型判据——见附录 A 第 1 条。

---

## 二、事件类型全集（核心章）

事件类型的**唯一来源**是旧系统源码，不是推测：

- `static/esm/assembler.js` 的 `REGISTRY`（事件码 → kind 归类）＋ `CONTROL_TYPE`
- `static/stream.js` 的 `sysRow()`（sys 类总闸，switch 逐 case）、`renderChain()`、`renderTool()`、`renderSeats()`、`renderInjection()`、`renderCardMsg()`
- `static/esm/flow/card-msg-node.js` 的 `CARD_MSG_DISPLAY`（卡间信 9 种中文名）

原型内**索引面板**（`#idxBody`，`prototype-chat-page-v1.html:361` 起）按 kind 分组列出全部 87 项，点任一项滚动定位到对应样例。页面自检徽标 `#selfCheck` 显示：`id差集:0 · JS引用71 · 样例键83 · 索引87`。

> 87 与 83 的差：`msg_sync / msg_notify / msg_directive / msg_report` 这四个类型在「卡间信」与「系统条」两组各列一次——同一事件码在两种位置有不同渲染。

### 2.1 分组总表

| 组 | kind | 类型数 | 原型位置（行） |
|---|---|---|---|
| A 消息类 | user / assistant | 3 键 | 410 起（用户气泡含 pending 态、助手含思考块） |
| B 卡间信 | cardmsg | 9 | 786 起 |
| C 工具 | tool | 3 键 | 410 起（tool_call + 成功/失败两条 tool_result） |
| D 计划链 | chain | 6 | 410 起 |
| E 三席 | seats | 2 键 | 410 起（seats_enter + 席卡三态） |
| F 系统条 · 运行骨架 | sys | 13 | 410 起 |
| F 系统条 · wb_exec 委派 | sys | 10 | 578 起 |
| F 系统条 · 评审 | sys | 13 | 630 起 |
| F 系统条 · 卡与角色 | sys | 9 | 630 起后段 |
| F 系统条 · 消息与目标 | sys | 7 | 630 起后段 |
| G 注入 | injection | 7 | 380 起 |
| H 不产生可见行 | — | 5 | 894 起（视图态样例区内） |

### 2.2 消息类（kind=user / assistant）

| 类型码 | 中文 | 渲染形态 | 交互 |
|---|---|---|---|
| `user_message` | 用户消息 | 右侧气泡 `.flow-user-row > .flow-user-stack > .flow-user-bubble`；气泡下方操作条（时间／复制／重发），默认 `opacity:0`，悬停显形 | 悬停出操作条；复制/重发可点 |
| `user_message`（pending） | 乐观上屏未认领 | 同上，加 `.is-pending`：边缘虚线、整体 55% 透明；**不显示「重发」**（重发只给已确认消息） | 不可重发 |
| `assistant_text` | 助手正文 | `.msg > .md`，保留换行与行内代码；正文上方可挂思考块 | — |
| （含）思考块 | reasoning 并入 assistant | `.think-row`：`◈ Think ＋ 摘要单行 ＋ 计时`；点开显示全文（斜体灰） | 点击展开/收起全文 |

### 2.3 卡间信（kind=cardmsg，9 种）

统一结构（对应 `card-msg-node.js`）：`.msg.cmsg.<ask\|answer\|notify\|bad>` ＋ 左侧 `.bar`（类型色条）＋ `.body > .head/.txt/.replyto`。
`.head` 一行＝`类型码 · 中文名` ＋（`msg_ask` 追加「须回应」）＋ `来源 → 去向`。正文是**剥掉协议头**后的剩余全文。

| 类型码 | 中文 | 标记 |
|---|---|---|
| `msg_ask` | 询问 | 追加「须回应」标签，色条加重 |
| `msg_answer` | 应答 | — |
| `msg_notify` | 通知 | — |
| `msg_sync` | 同步 | 样式类沿用 notify（不新造视觉） |
| `msg_report` | 汇报 | 同上 |
| `msg_directive` | 指令 | 同上 |
| `coder_done` | 完工回报 | 同上 |
| `worker_done` | 完工回报 | 同上 |
| 格式异常（bad） | — | 头行不合法时不吞内容：头部显示「消息格式异常」，正文照示原文 |

有 `reply_to` 时追加一行「回应 &lt;id&gt;」。

### 2.4 工具（kind=tool，两态合一节点）

`tool_call` 与 `tool_result` 在装配器合成**一条**节点（同 key 合体），渲染成终态：

| 态 | 元素 | 表现 |
|---|---|---|
| 调用中（无 result） | `.step-row.tool-row.running` | 前导方块＋工具名＋参数摘要＋「调用中」 |
| 成功 | `.step-row.tool-row.ok` | `✓ ＋ 耗时`；结果有实质内容时挂一行摘要 `.tool-sum` |
| 失败 | `.step-row.tool-row.fail` | `✗ ＋ 耗时`；行说明转错误色；展开区是错误块 |

**交互**：点整行展开 `.tool-body`——首次展开才构建（两段式），内容是 `IN {…}` / `OUT {…}` 两段，最大高度内滚。再点收起。

结果摘要只在**有实质内容**时占第二行：全是「完成」的工具若每个都挂一行「完成」，工具区会变成一片空壳——状态点在第一行已经说了。

### 2.5 计划链（kind=chain）

| 类型码 | 渲染 |
|---|---|
| `judgment` | `判断：直答` / `判断：拆解上板` ＋ `verdict=` |
| `plan_created` | `计划上板：<名称>` ＋ `total=N`，行尾「N 步」；**紧随其后挂步骤清单** `.psteps`（每步：类型徽标＋步号＋提示＋step_id/criteria） |
| `step_start` | `.step-row.lv2`：`步 N/M ＋ 类型徽标 ＋ 提示 ＋ step_id/criteria` |
| `step_advanced` | `.step-row.lv2`：`步骤完成` 或 `全部步骤完成，计划收口`；行尾 `✓→ 下一步` / `✓收口` |
| 旧格式降级 | `{content, behavior}` 缺结构化字段的老数据 → 直接降级成 `历史记录（旧格式）` ＋ content 原文，**不解析自由文本去补字段**；降级行默认展开 |

点行展开原始载荷 `pre.step-detail`。

### 2.6 三席（kind=seats）

`seats_enter` 一行（`三席进场 · tool_results=N · steps=M`），其后 `.seats` 容器内每席一张 `.seat-card`：

- `.sc-head`：模型名 ＋ 状态（`思考中` / `✓ 已返回` / `✗ 已返回`）
- `.sc-body`：思考文本；已返回时追加 `.ot` 输出段
- `.sc-foot`：`ok=true|false` ＋ `耗时 · 字数`

三态各有一张样例卡（思考中 / 成功 / 失败）。

### 2.7 系统条（kind=sys）

行结构统一为 `.step-row[data-ev=类型码] > .ev（事件码徽标）＋ .txt（说明＋真实字段）＋ .sub（状态槽）`，有载荷时可展开 `.step-detail`。

**运行骨架（13）**：`run_start`、`round_start`、`context`、`deliver`、`synthesize`、`proposal`、`done`、`error`、`rescue`、`round_end`、`reasoning`（该轮无正式回复也照示）、`triage_verdict`（已退役不会出现）、`未识别事件`（C5 兜底）。

**wb_exec 执行体委派（10）**：`wb_exec_queued`、`wb_exec_start`、`wb_exec_model`、`wb_exec_done`、`wb_exec_fallback`、`wb_exec_resume`、`wb_exec_wait`、`wb_exec_unavailable`、`wb_exec_interrupted`、`wb_exec_error`。

**评审（13）**：`review_batch_opened`、`review_round_start`、`review_speaking`、`review_spoke`（通过/未通过两种形态）、`review_revise`、`review_draft_drift`、`review_dispatch`、`review_target_responded`、`review_target_turn_end`、`review_round_done`、`review_all_passed`、`review_batch_closed`。

**卡与角色（9）**：`card_created`、`card_block_edited`、`card_halted`、`card_unhalted`、`card_archived`、`role_judged`、`role_alarm`（warn / critical 两形态）、`card_message`。

**消息与目标（7）**：`msg_sync`、`msg_notify`、`msg_directive`、`msg_report`、`harness_message`（条件集消息）、`model_resume`、`goal_round`。

### 2.8 注入（kind=injection，7 种）

`.step-row.lv3`，仅在过程展开时可见：

`context_preamble`（固定总领）、`judgment_protocol`（判断协议）、`context_seat_identity`（三席身份）、`context_injection`（当前步骤）、`status_update_task`（现状更新）、`requirements_draft_task`（需求确定·拟草稿）、`requirements_final_task`（需求确定·定正稿）。

### 2.9 不产生可见行的类型（5 种）

这些事件码注册表认它，但渲染层不出行——原型的责任是**说明它为什么不出行**，不是画一条假行：

| 类型码 | 为什么不出行 |
|---|---|
| `ping` | SSE 保活帧；不加这条会掉进兜底刷屏「未识别事件」 |
| `turn_end` | 运行态内部帧，live 路径已滤 |
| `content_chunk` | 逐字正文增量，并入 assistant 节点 |
| `reasoning_chunk` | 逐字思考增量，落盘已由合并 reasoning 承载 |
| `revoke` | 控制位记录，只驱动软删（撤下 seq ≥ from_seq 的节点），不渲染成节点 |

### 2.10 兜底与降级

- **未识别类型**：注册表里没有的事件码 → 照示一行 `未识别事件`（带原始 data），**不丢不炸**。
- **格式异常卡间信**：见 2.3，宁可难看不可丢话。
- **旧格式环节块**：见 2.5，降级而非猜测。

---

## 三、对话流视图态

这些不属于事件流，是整屏/瞬时态，原型里给静态形态（`#pg-view`，`…:894`）：

| 态 | 元素 | 触发条件 |
|---|---|---|
| 首屏骨架 | `.chat-skeleton`（气泡块＋三段线条） | 首次拉 timeline 期间 |
| 空态 | `.chat-empty`：`◌ ＋ 标题 ＋ 说明 ＋ 三条建议` | 该卡一条记录都没有 |
| 运行条 | `.flow-turn-status`：`Deep diving...` | 回合进行中 |
| 运行条·停止中 | `.flow-turn-status.is-stopping`：`正在停止…` | 点了停止、后端尚未确认 |
| 运行条·时钟 | `.flow-turn-status-clock` | 运行满 15 秒后才显（`N秒` / `N分NN秒`） |
| 回底键 | `.flow-view-to-bottom` | 读者滚动脱离底部 60px 以上才出现 |
| 回底键·新消息 | 同元素加 `.has-new` | 脱离底部期间又有新消息 |
| 加载更早 | `.flow-view-older-btn` 三态 | 可点 / 加载中（disabled，文案「载入历史…」）/ 没有更多（整块隐藏，不是灰按钮） |
| 日期分隔 | `.date-divider > span` | 跨天时插入，文案「今天 / 昨天 / N月N日 / N年N月N日」 |

**空态建议的交互**：点击只把文本填进输入框，**不代发**。

---

## 四、输入区

`#composerSeat` 固定在主区底部，内容列与对话列同宽（960px）。

### 4.1 发送 / 停止（同一键两态）

`#sendBtn` 一个按钮两种身份：

| 态 | 文案 | 点击行为 |
|---|---|---|
| 空闲 | `发送 ↑` | 取输入框内容 → 追加用户气泡 → 进入运行态 |
| 运行中 | `停止 ■` | 清定时器 → 回空闲态 → toast「已停止」 |

**为什么是同一键**：运行中发送会排队，不会打断——所以这个位置在运行中的语义变成「停止」，而不是再给一个会排队的「发送」。用户要打断时手不用挪。

实测读数：运行中点它 → 文案回 `发送 ↑`；运行条隐藏。

### 4.2 排队条（`queue-dock`）

运行中再发送 / 按 Enter → 进队列，不打断当前回合。

| 状态 | 形态 |
|---|---|
| 0 条 | 整块隐藏 |
| 1 条 | 不出头部，直接一行预览 ＋ 三键（编辑 / 插话 / 删除） |
| 多条·收起 | 头部「N 条排队消息 ▴」，列表隐藏 |
| 多条·展开 | 头部「N 条排队消息 ▾」＋ 逐行列表 |
| 编辑态 | 行内预览变 input：Enter 保存、Esc 取消、空文本不保存 |

**插话键的可用性**：仅运行中可插话发送；非运行态该键 disabled，title 写「仅运行中可插话发送」。

**停止后的队列**：停止不清空队列——用户输入不丢。队列在下一次完整回合结束后才开始派送；要丢弃就按行的 ✕。

### 4.3 输入区四键

| 键 | 行为 |
|---|---|
| `#modelBadge` | 模型徽标，点开模型清单（示意 toast） |
| `#autoToggle` | 手动/自动模式两态切换，`aria-pressed` 同步 |
| `#statusDraftBtn` | 打开「更新现状」弹窗（原/新 diff） |
| `#draftBtn` | 打开「需求文档起草」弹窗 |

---

## 五、覆盖层

### 5.1 卡详情抽屉 `.insp`（`…:956`）

默认收起（`display:none`）。顶栏「卡详情」或右缘竖条打开。内容段：使命 / 现状（带「编辑现状」键）/ 需求文档 / 关系增删 / 修改历史 / 执行体 / 危险操作（归档、删除——两段确认：一次点击变「再次点击确认」，3 秒无操作自动复位）。

**不锁主区**：抽屉只是右侧一带，主区照常可读可点。

### 5.2 评审浮窗 `.fwin`（`…:981`）

默认收起。顶栏「评审」打开。收缩态内容：批次头（卡名＋通过数）、事件列表（✓ 通过 / … 待复核）、实时行。点 `⤢` 展开追加 iframe 区。

层级低于卡详情：两者同开时卡详情在上。

### 5.3 起草弹窗 `.modal#draftModal`（`…:1031`）

**输入型模态**（可短暂接管点击），三态：

1. 打开瞬间：`起草中…`（等待模型）
2. 起草完成：草稿区可编辑 ＋ 反馈输入框
3. 失败：错误行（示意）

底部三键：`关闭`（丢弃）/ `反馈`（带反馈重新起草，空反馈会拦）/ `确认`（提交评审）。头部 ✕ 同「关闭」。Esc 同「关闭」。

### 5.4 现状弹窗 `.modal#statusModal`（`…:1053`）

原现状只读（删除部分带删除线）＋ 可直接改的文本区。底部：`关闭`（不落盘）/ `同意`（按文本区内容更新现状）。头部 ✕、Esc 均可退出。

### 5.5 通知面板 `#notifyPanel`

顶栏「消息」打开，打开即把角标清零。列表项：来源 → 去向 ＋ 正文 ＋ 时间。

### 5.6 toast `#toasts`

底部居中短提示，四级：默认 / ok / warn / err。点击即关，2.6 秒自动消失。

---

## 六、过程折叠

顶栏「过程细节」按钮切换 `body.proc-collapsed`。

**折叠态只留两样**：工具行（`.tool-row`）与轮次分隔行（`[data-ev="round_start"]`）。注入、判断、计划、步骤、三席、评审、告警等过程注脚全部隐藏。

口径来源：`ui.css:2328` `body.page-chat.proc-collapsed .step-row:not(.tool-row):not([data-ev="round_start"]){display:none}`。

**为什么默认展开**：留痕是本产品的底色，默认藏起来等于改了产品性格。

> 从索引面板点一个被折叠的类型时，原型会自动展开过程细节再定位——不给用户「点了没反应」的死路。

---

## 七、键盘

只留必要项，不设隐藏快捷键堆：

| 键 | 作用 |
|---|---|
| `Enter`（输入框） | 发送；运行中则排队 |
| `Shift+Enter` | 换行 |
| `Enter` / `Esc`（排队条编辑中） | 保存 / 取消 |
| `Esc` | 加速键：按层级依次关 弹窗 → 通知面板 → 卡详情 → 评审浮窗 |

**Esc 不是唯一出口**：上述每个状态都有可见的鼠标出口（✕ / 关闭 / 头部折叠）。Esc 只是让熟练用户少走一步。

---

## 八、数据口径

与画布页 `canvas-page-interaction-spec-v1.md` §12 同源，保证两页联通时数据对得上：

- **15 卡三层**：Master 1（card_001 底层重构）→ Manager 4（card_074 产品与交互需求 / card_257 页面架构及 UI/UE / card_091 验收与证据基线 / 外部接入与联调）→ Unit 3（card_290 画布页交互规格 / card_291 组件与状态库 / card_132 绘图底座与几何）＋ worker（card_305 设计令牌与变量 / card_096 验收证据包 / card_331 交互轨迹留档）＋ QA 1（card_345 画布页 v3 验收）＋ PM 1（card_346 画布页需求文档）＋ UI 2（card_322 高保真原型·画布页 / card_454 高保真原型·对话页）
- **状态覆盖**：运行中 / 已完成 / 已暂停 / 受阻 / 归档
- **模型**：GMODEL 类默认，清单 `claude-opus-4-5 / claude-sonnet-4-5 / gpt-5.2 / gemini-3-pro / deepseek-v4`
- **评审批次**：card_345 下 2/3 通过
- 对话内容用中性示意，不编造业务数据；但形态全覆盖。

---

## 九、与旧原型结构对应

| 旧位置 | 本原型 | 说明 |
|---|---|---|
| `templates/card.html` `.work-page` | `.app` | 左右两栏骨架 |
| `#appNav` | `.sidenav` | 全树导航 |
| `.topbar` | `.topbar` | 顶栏 |
| `#chatArea` | `#col` | 对话流容器 |
| `#composerSeat` | `.composer` | 输入区 |
| `#inspector` + `#inspTab` | `.insp` | 卡详情 |
| `#rvWin` 两态 | `.fwin` | 评审浮窗 |
| `#draftModal` / `#statusModal` | 同名同义 | 两个输入型弹窗 |
| `#rulePanel` | **未移植** | 项目规则面板是只读展示，不属于事件类型；见附录 A 第 4 条 |
| `#themeToggleBtn` | **未移植** | 深/浅主题属视觉设计，本版不做；见附录 A 第 3 条 |
| `.step-row` / `.msg.cmsg` / `.seats` / `.flow-queue-*` / `.flow-turn-status` / `.chat-empty` / `.chat-skeleton` | 同名同义 | 类名照旧，便于逐条对照源码 |

**类名保留的意义**：原型和实现用同一套类名，评审时能直接把原型上的问题定位到源码里的对应位置。

---

## 十、如实边界

1. **`node --check` 未跑**：本卡是 UI 卡，系统未授予 `bash`/`python`。语法正确性靠两件事间接支撑——页面能完整加载并渲染 1524 行、`selfCheck` 徽标显示 `id差集:0`（说明脚本从头执行到尾、变量声明无误）。**如实申报：待 worker 卡代跑语法检查。**
2. **console 不回传**：浏览器工具只回可见文本与语义树，不回传 console 输出。所以把自检结果**上屏**成 `#selfCheck` 徽标，不依赖 console 读回。
3. **inspect 单次只给前 100 个元素**，分页参数未生效。因此元素总数超过 100 时，底部元素（输入区、弹窗内按钮）取不到 ref。本规格里标注的读数，是在折叠索引与过程行之后取得的。
4. **瞬时帧未采到**：运行条「15 秒后显时钟」未在线观察（窗口内未跑满 15 秒）；插话排队的「计数 1 → 2」变化抓到的是稳定态，不是瞬间跳变帧。
5. **真机手感未核**：滚动跟随的 24px 阈值、锚点保位这些滚动细节，本原型未实现（骨架里没有真实长历史），不声称已核。
6. **git 破坏性动作：无**。本卡未执行任何 `reset / amend / rebase / checkout ./ / clean / stash`。

---

## 附录 A · 改动对照索引

| # | 项 | 旧原型 | 本原型 | 为什么 |
|---|---|---|---|---|
| 1 | 左栏宽度 | `ui.css` 272px／`nav.css` 非 rail 420px／任务书 360px | 260px | 宽度不影响事件类型判据；三处来源互相矛盾，取中性值。**待裁** |
| 2 | 顶栏高度 | `ui.css` 52px／任务书 44px | 40px | 同上，取中性值。**待裁** |
| 3 | 深浅主题 | `#themeToggleBtn` 全库唯一 | **不做** | 用户明确：不要任何 UI 视觉层面的设计 |
| 4 | 项目规则面板 | `#rulePanel` 可开合 | **不做** | 只读展示，不属于对话事件类型 |
| 5 | 全部配色/圆角/阴影/渐变 | `ui.css` 完整设计系统 | **全部剥掉** | 同上 |
| 6 | 过渡动画（抽屉滑出） | transform + transition | 改为 display 切换 | 一是不要视觉设计，二是滑出后元素仍在「可见」语义里，状态无法被直接验证 |
| 7 | 左栏树展开折叠 | `.nav-chev` 旋转 + 子行显隐 | **只做搜索过滤** | 本次判据是事件类型全集，左栏不是重点；需要时可补 |
| 8 | 事件类型索引面板 | 无（原系统没有） | **新增** | 原型辅助件：87 项一览 + 点击定位，让「类型是否做全」可核 |
| 9 | 自动模式 / 模型徽标 | 真控件 | 示意（toast 反馈） | 事件类型原型的判据是「在面且可点出反馈」 |
| 10 | 发送/停止同一键 | 原型事实 | 照做 | — |
| 11 | 插话全部排队 | 原型事实 | 照做 | — |
| 12 | 过程折叠只留工具行+轮次 | `ui.css:2328` | 照做 | — |

## 附录 B · 交互入口表

| 入口 | 位置 | 打开什么 | 出口 |
|---|---|---|---|
| 顶栏「过程细节」 | `#procToggle` | 折叠/展开过程行 | 再点一次 |
| 顶栏「类型索引」 | `#idxToggle` | 收起/展开索引面板 | 再点一次 |
| 顶栏「卡详情」 | `#inspToggle` | 右侧卡详情抽屉 | 抽屉 ✕ / Esc |
| 顶栏「消息」 | `#notifyBtn` | 通知面板（角标清零） | 面板 ✕ / Esc |
| 顶栏「评审」 | `#rvToggle` | 评审浮窗 | 浮窗 ✕ / Esc |
| 索引面板项 | `#idxBody button` | 滚动定位到该类型样例并高亮 1.4 秒 | — |
| 过程行 / 工具行 / 思考块 | 行本体 | 展开载荷 / IN-OUT / 思考全文 | 再点一次 |
| 输入框 Enter | `#inputBox` | 发送或排队 | — |
| 主键 | `#sendBtn` | 发送 / 停止 | — |
| 排队行 | `.flow-queue-actions` | 编辑 / 插话 / 删除 | 编辑用 Esc 取消 |
| 空态建议 | `.ce-hint` | 填进输入框（不代发） | — |
| 回底键 | `#toBottom` | 滚动到底 | — |
| 加载更早 | `#olderBtn` | 前插一批历史（两次后禁用） | — |

## 附录 C · 实现行号

全部为本文件 `prototype-chat-page-v1.html`（1524 行）的实际行号：

| 位置 | 行 |
|---|---|
| `.app` 布局 | 36 |
| `.step-row` 过程行盒模型 | 99 |
| 视图态样式区起 | 202 |
| 排队条样式起 | 240 |
| 事件类型索引面板（DOM） | 361 |
| 注入类样例（7 种）起 | 380 |
| 运行骨架 / 消息 / 工具 / 计划链 / 三席起 | 410 |
| wb_exec 委派（10 种）起 | 578 |
| 评审（13 种）起 | 630 |
| 卡间信（9 种 + 格式异常）起 | 786 |
| 视图态样例区起 | 894 |
| 排队条四态说明 | 941 |
| 卡详情抽屉 | 956 |
| 评审浮窗 | 981 |
| 输入区 | 1000 |
| 起草弹窗 | 1031 |
| 现状弹窗 | 1053 |
| `buildIndex()` | 1143 |
| `doSend()` | 1296 |
| `renderQueue()` | 1323 |
| `selfCheck()` | 1489 |

---

**定版口径**：以本文件为准，不以 `canvas-page-interaction-spec-v1.md` 的布局章节为准（两页布局本就不同）。事件类型清单以源码为准；本文件若与源码冲突，以 `static/stream.js` 与 `static/esm/assembler.js` 为准，并应出勘误。
