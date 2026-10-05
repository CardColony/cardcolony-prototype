# THINK-ECO-T11 · 实读调研纪要 ——「已核实结论」机制实现（evidence-index）

> 链：proposal（上下文装配线移植·真移植方案）
> 日期：2026-10-05
> 目的：用户 2026-10-05 00:08 明令「这套东西怎么实现的，去读代码，然后按这套移植过去」——读实现代码，把做法拆透，作为折叠形态移植的范式来源。

---

## 一、读了什么

| 文件 | 行数 | 读法 |
|---|---|---|
| `D:\AI_station\ZhuGe\dsh-memory\src\gen3\tools\evidence-index.ts` | 558 | read 全量（callId `call_1wf8vcs4fy8g75u6e1c1crd8`） |
| `D:\AI_station\ZhuGe\dsh-memory\src\gen3\context\obs-paths.ts` | 239 | read 全量（callId `call_bt3ns3dj9r22qrhbkw0gtpmr`） |
| `D:\AI_station\ZhuGe\dsh-memory\src\index.ts` | — | grep 定位引用段（L65/L535/L575-578） |
| `D:\AI_station\ZhuGe\dsh-memory\src\gen3\memory\episodic-memory\present.ts` | — | grep 定位相关注释（L446/L522/L554-555） |

---

## 二、机制全貌（架构层，做法拆解）

### 2.1 这是什么

「证据索引常驻（结论 + 逐字原文 + 时间戳 + 出处/状态）」——**模型层登记长期结论、代码核实取回、每轮注入、逐轮对质源文件**的一套常驻机制。文件头自述（evidence-index.ts:1-15 逐字）：

> 「DT-MEM-EVIDENCE-01 —— 证据索引常驻（结论 + 逐字原文 + 时间戳 + 出处/状态）。
> 两层入口（互补，不可只做其一）：
>  - 机器层（produceEvidenceIndex）：从事件流自动生成「最近 N 次工具调用」紧凑索引——零模型参与；
>  - 模型层（record_finding 工具）：模型登记长期结论，代码按锚点逐字取原文并回执（模型只给位置）。」

### 2.2 两层入口（这是「【结论】＋【依据】」的源头）

| 层 | 入口 | 谁写 | 产出 |
|---|---|---|---|
| **机器层** | `produceEvidenceIndex` | **零模型参与**，代码从事件流自动生成 | 「最近 30 次工具调用」紧凑索引（时间·工具·参数摘要→状态(callId)），更早的以聚合行交代 |
| **模型层** | `record_finding` 工具 | **模型写一句话结论**，代码取回原文 | 「最近 K 条已核实结论」（结论＋逐字原文＋出处＋状态＋校验） |

**模型层 record_finding 的职责分工（evidence-index.ts:136-207 实读）**：
- 模型只提供四样：`conclusion`（一句话语义结论）、`sourceFile`（源文件）、`anchor`（要登记的原文，逐字）、`status`（四态）
- 代码做五件事：①解析源文件路径 ②`content.indexOf(anchor)` 核实原文存在 ③`content.slice(idx, idx+anchor.length)` **逐字取回** ④组装条目落盘 ⑤回执 `originalVerbatim`（回显取到的原文供模型核对）
- **原文绝不经模型之手（B-C5）**——锚点未命中时回给模型上下文便于修正（不静默，L174-183）

### 2.3 存储形态（evidence-index.ts:39-103 实读）

**证据条目结构**（`EvidenceEntry`，L40-57）八字段：

```
conclusion    模型写的语义结论（一句话压缩「这意味着什么」）
original      代码按锚点逐字取到的原文（绝不经模型之手）
anchor        模型给的锚点（= original 本身，逐字）
file          源文件绝对路径
head          读时 git HEAD（出处）
fileMtime     读时文件 mtime（epoch ms，出处）
status        established / doubtful / obsolete / deadend
recordedAt    条目形成时间（固定值 = 登记时刻，非「当前时间」，不伤缓存 B-C2）
```

**落盘**：`<obsRoot()>/evidence-index/<sid>.json`（每会话一份，obs-paths.ts:93-102 实读；目录自愈创建；**不落 present 表、不进凝练**，B-C3/N4）。
**有界**：`FINDINGS_CAP = 50`（L29）——超出时最旧的移入 `<sid>.json.archive`（L86-103），active 文件有界、数据不丢。

### 2.4 每轮注入（evidence-index.ts:489-557 实读）

`produceEvidenceIndex` 返回 `[{ role: 'system', content }]`——**一个 system 区**，内容两段：

**① 机器层 · 工具调用索引**（L509-534）：
```
【机器层 · 工具调用索引】（全量覆盖 · callId 查证锚；…）
  MM-DD HH:mm · 工具名 · 参数摘要(80字截断) → ok/error/no-result (callId)
  （更早 N 次：工具名×次数、…；error X 次 · 无结果 Y 次；逐条可经 tool_record_verify 查证）
```

**② 模型层 · 已核实结论**（L542-553）——每条五行：
```
  [MM-DD HH:mm] 结论：{conclusion}
    原文（逐字）：{original}
    出处：{file}（HEAD {head} · mtime {MM-DD HH:mm}）
    状态：{status} · {MM-DD HH:mm}
    校验：✓ 源未变 / ⚠ 源已变（逐字原文可能漂移，需复核）/ ⚠ 源缺失
```

**无内容时不注入**（`lines.length === 0 → return []`，L556）——保持上下文干净＋缓存稳定。

### 2.5 保鲜（陈旧判定 verifyStale，evidence-index.ts:227-260 实读）

逐轮现算，确定性函数（不含「当前时间」，不伤缓存 B-C2）：

| 条件 | 判定 |
|---|---|
| 源文件缺失 | ⚠ 源缺失 |
| 当前 mtime === 登记时 fileMtime | ✓ 源未变（内容字节未变 ⇒ 锚点仍在，不读全文） |
| mtime 变化 | 读全文 `includes(anchor)` → 仍在 ⇒ ✓ / 不在 ⇒ ⚠ 源已变（逐字原文可能漂移，需复核） |

进程内缓存 `{路径 → {mtime, 判定}}`——mtime 未变直接复用，避免每轮重读大文件（R3-V4）。

### 2.6 常驻通道与不变式（evidence-index.ts:8-13, index.ts:575-578 实读）

- **通道**：turn-injection 口子（`registerTurnInjection`，name='evidence-index'）——**只进请求、不进槽不进 session（F8 不变式）**
- **role 强制 'system'** ＋ 常数标记（CONTEXT-TAG-01 P1）——模型可辨由标记承载，不引入变化量 ⇒ 不伤缓存
- **时间戳全为固定值**（事件 time / 登记时刻），不含「当前时间」

### 2.7 机器层自建表（★ EVIDFIX-01，evidence-index.ts:282-487 实读）

session/event 钩子实时入账（O(1)/条）＋ 开局补一次 readFrom 建表（此后永不再全量扫）＋ 盘件持久化（原子写 tmp+rename）。**取代原「每步全量重扫」——313 万事件会话实测原实现每步 30-50s CPU。**

---

## 三、与折叠形态的移植对应（用户 00:03(3)＋00:08 的落点）

基线文档 §5.3 两件套定义已写（照「已核实结论」做法）。本次实读把「做法」对应到代码：

| 折叠形态要素 | 对应 evidence-index 的机制 | 移植方式 |
|---|---|---|
| **【结论】**（这次调用得到了什么，一句话） | `conclusion`——模型写的语义结论 | 折叠时对「该轮工具做了什么、返回了什么」做一次模型压出（与 record_finding 的 conclusion 同性质：一句话语义压缩） |
| **【依据】**（证据在哪、怎么找回原文） | 代码机械取——`resolveFile`＋`indexOf(anchor)`＋`slice` 逐字取回 | 代码机械登记（哪一步、哪个工具、参数、怎么重取原文）——不动脑子、不会编 |
| **配对/核实**（不静默） | 锚点未命中回给模型上下文便于修正（L174-183） | 压出失败 → 该轮不折、全文保留、留痕记错误条目（基线 §5.5，不留白） |
| **注入形态**（五行：结论/原文/出处/状态/校验） | `produceEvidenceIndex` 的模型层段（L542-553） | 折叠后的旧轮次在装配里呈现为【结论】＋【依据】两件套 |
| **校验**（逐轮对质源） | `verifyStale`（mtime→includes(anchor)） | 【依据】带「怎么重取原文」——模型需要细节时按它重取原文（基线 §5.3 原文去向） |

**边界（基线 §5.3 已写，实读确认一致）**：evidence-index 是**长期可对质的证据条目**（持久化、跨请求、每轮注入、逐轮校验）；折叠是**请求内的体积压缩**（轮槽内、不落盘、不推送）。**两者不互通**——移植的是「做法/范式」（结论谁写、原文怎么取、怎么对质），不是「存储」（折叠不建 evidence-index 表）。

---

## 四、关键设计原则（移植时必须保留的）

1. **原文绝不经模型之手**——代码取回、逐字核实（B-C5）
2. **时间戳用固定值**（登记时刻/事件 time），不含「当前时间」——不伤缓存（B-C2）
3. **陈旧判定是确定性函数**（exists/stat/includes），逐轮现算可复用（R3-V4）
4. **有界**（FINDINGS_CAP=50，溢出归档）——不得无上限增长（B-C1/N7）
5. **无内容不注入**——保持上下文干净＋缓存稳定
6. **fail-soft 但不静默**——锚点未命中回给模型修正上下文（L174-183）；入账异常绝不炸事件链
7. **机器层与模型层互补、不可只做其一**——机器层零模型参与提供权威调用记录，模型层提供语义结论

---

## 五、对本方案（装配线＋轮槽折叠）的直接意义

1. **折叠形态的范式来源已实读闭环**——用户 00:08「读代码，然后按这套移植过去」的代码已读、做法已拆透。
2. **机器层「工具调用索引」恰好就是折叠要替代的对象的权威形态**——它证明「旧工具调用以紧凑行呈现（时间·工具·参数摘要→状态·callId）」是这套机制自己的做法，折叠的【依据】与之同构。
3. **「校验」概念提示了折叠的另一层价值**——若折叠结果也带「重取路径」，模型需要细节时可按路径取回原文，与「原文去向」条款（基线 §5.3）一致。

---

## 六、未核实项（如实标）

- `turn-injection` 口子的宿主接线（`registerTurnInjection` 的实现）未实读——本方案不引入该口子（推演侧自己拼 messages），不影响移植判断。
- `present.ts` 中 CALLTEXT-01 相关段只 grep 到注释未读全文——涉及「折叠标记退役/参数摘要归 evidence-index」的历史沿革，与本方案无直接依赖。
- 机器层 `machineIndexApplyEvent` 的事件结构（tool/call、tool/result）是 dsh 宿主事件格式——推演侧无此事件流，机器层整体不移植（折叠只借模型层范式）。
